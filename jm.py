import asyncio
import hashlib
import hmac
import json
import os
import secrets
import time

import aiohttp

import img2pdf
from jmcomic import (
    JmImageDetail,
    JmImageTool,
    create_option_by_file,
    download_album,
    multi_thread_launcher,
)
from telegraph.aio import RetryAfterError, Telegraph

from astrbot.api import logger


def _as_strings(value):
    if not value:
        return []
    if isinstance(value, str):
        return [part.strip() for part in value.split(",") if part.strip()]
    return [str(part).strip() for part in value if str(part).strip()]


async def publish_jm_reader(
    album_id: str, config_path: str, gateway_base_url: str, publish_secret: str
) -> str:
    if len(publish_secret) < 32:
        raise ValueError("请在插件配置中填写至少 32 个字符的漫画发布密钥")

    def build_manifest():
        option = create_option_by_file(config_path)
        client = option.new_jm_client()
        album = client.get_album_detail(album_id)
        chapters = []
        for order, chapter_ref in enumerate(album, start=1):
            photo = client.get_photo_detail(chapter_ref.photo_id, False)
            pages = []
            for index, image in enumerate(photo, start=1):
                source_url = getattr(image, "download_url", None) or image.img_url
                segments = int(JmImageTool.get_num_by_detail(image))
                pages.append(
                    {
                        "index": index,
                        "source_url": source_url,
                        "file_name": str(image.img_file_name),
                        "decode": {
                            "scheme": "jm_vertical_v1" if segments else "",
                            "segments": segments,
                        },
                    }
                )
            chapters.append(
                {
                    "id": str(photo.photo_id),
                    "title": str(getattr(photo, "name", "") or f"章节 {order}"),
                    "order": order,
                    "pages": pages,
                }
            )
        return {
            "provider": "jm",
            "album_id": str(album.id),
            "title": str(album.name),
            "authors": _as_strings(getattr(album, "authors", [])),
            "description": str(getattr(album, "description", "") or ""),
            "chapters": chapters,
            "updated_at": int(time.time()),
        }

    manifest = await asyncio.to_thread(build_manifest)
    body = json.dumps(manifest, ensure_ascii=False, separators=(",", ":")).encode()
    timestamp = str(int(time.time()))
    nonce = secrets.token_hex(16)
    body_hash = hashlib.sha256(body).hexdigest()
    signature = hmac.new(
        publish_secret.encode(),
        f"{timestamp}\n{nonce}\n{body_hash}".encode(),
        hashlib.sha256,
    ).hexdigest()
    endpoint = gateway_base_url.rstrip("/") + "/api/manga/publish"
    headers = {
        "Content-Type": "application/json",
        "X-Manga-Timestamp": timestamp,
        "X-Manga-Nonce": nonce,
        "X-Manga-Signature": signature,
    }
    timeout = aiohttp.ClientTimeout(total=180)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(endpoint, data=body, headers=headers) as response:
            response_text = await response.text()
            try:
                payload = json.loads(response_text)
            except (json.JSONDecodeError, TypeError):
                payload = None
            if response.status != 200:
                message = "发布失败"
                if isinstance(payload, dict):
                    error = payload.get("error")
                    if isinstance(error, dict):
                        message = str(error.get("message") or message)
                elif response_text.strip():
                    # 不记录完整响应，避免网关错误页或上游信息污染日志。
                    message = response_text.strip().replace("\n", " ")[:200]
                raise RuntimeError(f"image-gateway: {message} ({response.status})")
            if not isinstance(payload, dict) or not payload.get("url"):
                raise RuntimeError(
                    f"image-gateway 返回格式错误 ({response.status}, "
                    f"Content-Type: {response.headers.get('Content-Type', 'unknown')})"
                )
            return str(payload["url"])


def webp_to_pdf(folder_name, output_pdf):
    # 获取所有webp图片并按名称排序
    try:
        files = [f for f in os.listdir(folder_name) if f.lower().endswith(".webp")]
        files.sort()
        file_paths = [os.path.join(folder_name, f) for f in files]

        # 合并为PDF
        with open(output_pdf, "wb") as f:
            b = img2pdf.convert(file_paths)
            if b:
                f.write(b)

        # 返回文件数量
        return len(files)
    except Exception as e:
        raise Exception(f"转换PDF时出错: {str(e)}")


def cb(album, downloader, save_path, progress_callback):
    try:
        # 获取下载目录中的文件夹名称
        download_folder = os.path.join(save_path, album.name)

        # 计算文件数量用于预估转换时间
        file_count = 0
        if os.path.exists(download_folder):
            file_count = len(
                [f for f in os.listdir(download_folder) if f.lower().endswith(".webp")]
            )

        # 如果提供了进度回调函数，发送预估时间给用户
        # 预估PDF制作时间（假设每张图片需要0.5秒处理时间）
        estimated_time = file_count * 0.5
        estimated_str = f"{estimated_time:.1f}秒"
        # 使用同步方式调用回调函数
        progress_callback(file_count, estimated_str)

        # 使用id_name的形式命名PDF文件，但需要处理特殊字符
        # 移除或替换文件名中的特殊字符
        pdfname = f"{album.id}_{album.name}.pdf"
        pdf_path = os.path.join(save_path, pdfname)

        # 确保保存目录存在
        os.makedirs(save_path, exist_ok=True)

        # 转换webp图片为PDF，并获取文件数量
        if not os.path.exists(pdf_path):
            webp_to_pdf(folder_name=download_folder, output_pdf=pdf_path)

        # 返回PDF文件路径和文件数量
        return pdf_path, file_count
    except Exception as e:
        raise Exception(f"处理PDF文件时出错: {str(e)}")


def JmDownload(
    album_id: str, save_path: str, config_path: str = "", progress_callback=None
):
    # 如果没有提供配置文件路径，则使用默认路径
    if config_path == "":
        # 获取当前脚本所在目录
        current_dir = os.path.dirname(os.path.abspath(__file__))
        config_path = os.path.join(current_dir, "op.yml")

    option = create_option_by_file(config_path)
    # 更新下载目录为指定的保存路径
    option.dir_rule.base_dir = save_path

    # 用于存储PDF路径和文件数量的变量
    pdf_path_result = None
    file_count_result = 0

    # 定义回调函数
    def download_callback(album, downloader):
        nonlocal pdf_path_result, file_count_result
        pdf_path_result, file_count_result = cb(
            album, downloader, save_path, progress_callback
        )

    # 执行下载
    download_album(jm_album_id=album_id, option=option, callback=download_callback)

    # 等待下载完成（简单等待一段时间，直到结果准备好）
    import time

    start_time = time.time()
    while pdf_path_result is None and time.time() - start_time < 120:  # 最多等待2分钟
        time.sleep(0.1)

    # 返回PDF文件路径和文件数量
    return pdf_path_result, file_count_result


async def jmToph(albume_id: str, config_path: str) -> list[str]:
    option = create_option_by_file(config_path)
    client = option.new_jm_client()
    album = client.get_album_detail(albume_id)

    def blocking_io():
        image_dict = {}

        def fetch(photo):
            # 章节实体类
            photo = client.get_photo_detail(photo.photo_id, False)
            print(f"章节id: {photo.photo_id}")
            image_list: list[str] = []

            # 图片实体类
            image: JmImageDetail
            for image in photo:
                image_list.append(image.img_url)

            image_dict[f"chapter_{photo.photo_id}:{photo.name}"] = image_list

        multi_thread_launcher(iter_objs=album, apply_each_obj_func=fetch)
        return image_dict

    # 使用 asyncio.to_thread 在另一个线程运行同步代码，防止卡死机器人
    # 注意：asyncio.to_thread 需要 Python 3.9+
    image_dict = await asyncio.to_thread(blocking_io)

    logger.info(f"image dict :{image_dict}")
    return await getgraph(image_dict, album.id, album.name)


async def getgraph(
    image_dict: dict[str, list[str]], albume_id: str, albume_name: str
) -> list[str]:
    res = []
    telegraph = Telegraph()
    print(await telegraph.create_account(short_name="1337"))

    html_content = f"<p>{albume_name}</p>"
    # 对 key先后进行排序，然后遍历
    for key in sorted(image_dict.keys()):
        id = key.split(":")[0].replace("chapter_", "")
        if id != albume_id:
            chapter_name = key.split(":")[1]
            html_content += f"<p>{chapter_name}</p>"
        for image_url in image_dict[key]:
            html_content += f"<img src='{image_url}'></img>"
        while True:
            try:
                logger.info("正在尝试发布到 Telegraph...")
                response = await telegraph.create_page(
                    id,
                    html_content=html_content,
                )
                # 如果成功了，就跳出循环
                break
            except RetryAfterError as e:
                # 如果触发限制
                logger.info(
                    f"Telegraph 限制频率（Flood control），需要等待 {e.retry_after} 秒..."
                )
                # 乖乖等待官方要求的时间，多加1秒缓冲
                await asyncio.sleep(e.retry_after + 1)
                logger.info("等待结束，正在重试...")
            except Exception as e:
                # 其他错误直接抛出
                raise e
        res.append(response["url"].replace(".ph", ".kissnab.top"))
        html_content = f"<p>{albume_name}</p>"

    return res
