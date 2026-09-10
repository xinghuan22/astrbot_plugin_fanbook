import abc
import asyncio
import base64
import io
import secrets
from pathlib import Path

import aiohttp
from PIL import Image as PILImage
from PIL import ImageDraw, ImageFont

import astrbot.core.message.components as Comp
from astrbot.api import logger
from astrbot.core.platform.astr_message_event import AstrMessageEvent


class ImageWorkflow(abc.ABC):
    def __init__(self, proxy: str | None = None):
        connector = aiohttp.TCPConnector()
        self.session = aiohttp.ClientSession(connector=connector)
        self.proxy = proxy

    async def _download_image(self, url: str) -> bytes | None:
        try:
            async with self.session.get(url, proxy=self.proxy) as resp:
                resp.raise_for_status()
                return await resp.read()
        except Exception as e:
            logger.error(f"图片下载失败: {e}")
            return None

    def _extract_first_frame_sync(self, raw: bytes) -> bytes:
        """
        使用PIL库处理图片数据。如果是GIF，则提取第一帧并转为PNG。
        """
        img_io = io.BytesIO(raw)
        img = PILImage.open(img_io)
        if img.format != "GIF":
            return raw
        logger.info("检测到GIF, 将抽取 GIF 的第一帧来生图")
        first_frame = img.convert("RGBA")
        out_io = io.BytesIO()
        first_frame.save(out_io, format="PNG")
        return out_io.getvalue()

    async def _load_bytes(self, src: str) -> bytes | None:
        raw: bytes | None = None
        loop = asyncio.get_running_loop()

        if await asyncio.to_thread(Path(src).is_file):
            raw = await loop.run_in_executor(None, Path(src).read_bytes)
        elif src.startswith("http"):
            raw = await self._download_image(src)
        elif src.startswith("base64://"):
            raw = await loop.run_in_executor(None, base64.b64decode, src[9:])

        if not raw:
            return None
        return await loop.run_in_executor(None, self._extract_first_frame_sync, raw)

    async def get_first_image(self, event: AstrMessageEvent) -> bytes | None:
        for s in event.message_obj.message:
            if isinstance(s, Comp.Reply) and s.chain:
                for seg in s.chain:
                    if isinstance(seg, Comp.Image):
                        if seg.url and (img := await self._load_bytes(seg.url)):
                            return img
                        if seg.file and (img := await self._load_bytes(seg.file)):
                            return img
        for seg in event.message_obj.message:
            if isinstance(seg, Comp.Image):
                if seg.url and (img := await self._load_bytes(seg.url)):
                    return img
                if seg.file and (img := await self._load_bytes(seg.file)):
                    return img
        return None

    async def terminate(self):
        if self.session and not self.session.closed:
            await self.session.close()

    def _add_watermark(self, img: PILImage.Image, text=""):
        """
        在图片右下角添加与背景色相近的微小水印
        """
        if text == "":
            text = secrets.token_hex(10)
        draw = ImageDraw.Draw(img)
        width, height = img.size

        # 1. 设置字体大小 (自适应图片高度，很小)
        # 大约占图片高度的 1.5% 到 2%，最小 10px
        font_size = max(10, int(height * 0.01))

        try:
            # 尝试加载常用字体，如果没有则使用默认字体
            # Windows/Linux 路径可能不同，这里尝试加载 Arial
            font = ImageFont.truetype("arial.ttf", font_size)
        except OSError:
            font = ImageFont.load_default(size=font_size)

        # 2. 计算文字宽高
        bbox = draw.textbbox((0, 0), text, font=font)
        text_w = bbox[2] - bbox[0]
        text_h = bbox[3] - bbox[1]

        # 3. 确定位置 (右下角，留出少量边距)
        margin = 5
        x = width - text_w - margin
        y = height - text_h - margin

        # 边界检查，防止图片太小文字出界
        if x < 0:
            x = 0
        if y < 0:
            y = 0

        # 4. 采样背景颜色以计算"相近色"
        # 获取文字区域中心点的颜色
        sample_x = min(width - 1, int(x + text_w / 2))
        sample_y = min(height - 1, int(y + text_h / 2))

        bg_color = img.getpixel((sample_x, sample_y))

        # 提取 RGB
        if isinstance(bg_color, int):  # 灰度图
            r = g = b = bg_color
            a = 255
        elif isinstance(bg_color, tuple) and len(bg_color) == 4:  # RGBA
            r, g, b, a = bg_color
        elif isinstance(bg_color, tuple):  # RGB
            r, g, b = bg_color
            a = 255

        # 计算亮度 (Luminance)
        luminance = 0.299 * r + 0.587 * g + 0.114 * b

        # 5. 生成水印颜色
        # 策略：如果背景亮，文字就稍微暗一点；如果背景暗，文字就稍微亮一点
        # delta 控制色差大小，值越小越"隐形"
        delta = 5

        if luminance > 128:
            # 背景亮 -> 文字微暗
            new_r = max(0, r - delta)
            new_g = max(0, g - delta)
            new_b = max(0, b - delta)
        else:
            # 背景暗 -> 文字微亮
            new_r = min(255, r + delta)
            new_g = min(255, g + delta)
            new_b = min(255, b + delta)

        text_color = (new_r, new_g, new_b, int(a * 0.9))  # 稍微加点透明度融合更好

        # 6. 绘制文字
        draw.text((x, y), text, font=font, fill=text_color)

        return img
