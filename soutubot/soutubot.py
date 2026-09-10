import io
import os
import re
import sys
from typing import Any

import httpx

import astrbot.api.message_components as Comp
from astrbot.api import logger
from astrbot.core.platform.astr_message_event import AstrMessageEvent

plugin_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.append(plugin_dir)

from image_workflow.workflow import ImageWorkflow  # noqa: E402
from PIL import Image  # noqa: E402


class SoutuBotClient(ImageWorkflow):
    def __init__(self):
        self.base_url = "https://soutubot.moe"
        self.search_api = f"{self.base_url}/api/search"
        self.client = httpx.AsyncClient(
            http2=True,
            timeout=httpx.Timeout(60.0),
            follow_redirects=True,
        )
        self.headers = {
            "Accept": "application/json",
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/139.0.0.0 Safari/537.36"
            ),
            "Origin": self.base_url,
            "Referer": f"{self.base_url}/",
            "DNT": "1",
            "Accept-Language": "zh-CN",
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin",
            "Sec-CH-UA-Platform": '"Windows"',
        }
        super().__init__()

    async def search_image(self, image_bytes: bytes, filename: str = "query.jpg"):
        """
        上传图片进行搜索
        :param image_bytes: 图片的二进制数据
        :param filename: 虚拟文件名
        :return: 搜图接口 JSON 响应
        """
        data = {"factor": "1.2", "metadata_mode": "display", "top_k": "25"}
        files = {"file": (filename, image_bytes, "image/jpeg")}
        try:
            response = await self.client.post(
                self.search_api, headers=self.headers, data=data, files=files
            )
            response.raise_for_status()
            payload = response.json()
            if payload.get("status") != "ok":
                raise ValueError(payload.get("error") or "接口返回非成功状态")
            return payload
        except (httpx.HTTPError, ValueError) as exc:
            logger.error(f"搜图请求失败: {exc}")
            raise RuntimeError(str(exc)) from exc

    @staticmethod
    def _result_fields(hit: dict[str, Any]) -> dict[str, Any] | None:
        segments = hit.get("path_segments") or []
        if not segments:
            return None
        segment = segments[0]
        metadata = segment.get("metadata") or {}
        title_data = metadata.get("title") or {}
        post = metadata.get("post") or {}
        source = metadata.get("source") or {}
        facts = metadata.get("facts") or {}
        asset_id = str((metadata.get("asset") or {}).get("asset_id") or "")
        if re.fullmatch(r"[0-9a-fA-F]{32}", asset_id):
            asset_id = asset_id.lower()
            thumbnail = (
                "https://img4.gelbooru.com/thumbnails//"
                f"{asset_id[:2]}/{asset_id[2:4]}/thumbnail_{asset_id}.jpg"
            )
        else:
            thumbnail = segment.get("thumbnail_url")
        title = title_data.get("primary") or post.get("post_id") or hit.get("raw_path")
        return {
            "title": title or "未知标题",
            "language": facts.get("language") or segment.get("language") or "未知",
            "page": segment.get("page_no"),
            "similarity": hit.get("score", 0),
            "thumbnail": thumbnail,
            "url": segment.get("source_url") or source.get("url") or post.get("post_url"),
        }

    async def terminate(self):
        # 关闭 httpx
        if self.client and not self.client.is_closed:
            await self.client.aclose()
        # 关闭父类 ImageWorkflow 的 aiohttp
        await super().terminate()

    async def _download_preview(self, url: str) -> bytes | None:
        if url.startswith("https://img4.gelbooru.com/thumbnails/"):
            try:
                async with self.session.get(
                    url,
                    headers={"Referer": "https://gelbooru.com/"},
                    proxy=self.proxy,
                ) as response:
                    response.raise_for_status()
                    return await response.read()
            except Exception as exc:
                logger.error(f"Gelbooru 缩略图下载失败: {exc}")
                return None
        return await self._download_image(url)

    async def process(self, message_event: AstrMessageEvent):
        image_bytes = await self.get_first_image(message_event)
        if not isinstance(image_bytes, bytes):
            yield message_event.plain_result("请在消息中附带一张图片")
            return
        try:
            payload = await self.search_image(image_bytes=image_bytes)
            node_list = []
            for hit in payload.get("results", []):
                result = self._result_fields(hit)
                if result and result["similarity"] > 30:
                    preview_bytes = None
                    if result["thumbnail"]:
                        preview_bytes = await self._download_preview(result["thumbnail"])
                    page_text = f"第{result['page']}页\n" if result["page"] else ""
                    node_items: list[Comp.BaseMessageComponent] = [
                        Comp.Plain(f"标题: {result['title']}\n"),
                        Comp.Plain(f"语言: {result['language']}\n"),
                        Comp.Plain(f"相似度: {result['similarity']:.2f}%\n"),
                    ]
                    if page_text:
                        node_items.insert(2, Comp.Plain(page_text))
                    if isinstance(preview_bytes, bytes):
                        try:
                            img = Image.open(io.BytesIO(preview_bytes)).convert("RGBA")
                            res = self._add_watermark(img)
                            output_buffer = io.BytesIO()
                            res.save(output_buffer, format="PNG")
                            node_items.append(
                                Comp.Image.fromBytes(byte=output_buffer.getvalue())
                            )
                        except Exception as exc:
                            logger.warning(f"预览图处理失败: {exc}")
                    if result["url"]:
                        node_items.append(Comp.Plain(f"链接: {result['url']}"))
                    node_list.append(Comp.Node(node_items))
            if not node_list:
                yield message_event.plain_result("未找到相似度大于30%的结果")
                return
            for start in range(0, len(node_list), 10):
                yield message_event.chain_result(
                    [Comp.Nodes(node_list[start : start + 10])]
                )
        except RuntimeError as exc:
            yield message_event.plain_result(f"搜图失败: {exc}")


_soutu_client_instance = None


def get_soutu_client():
    global _soutu_client_instance
    if _soutu_client_instance is None:
        _soutu_client_instance = SoutuBotClient()
    return _soutu_client_instance
