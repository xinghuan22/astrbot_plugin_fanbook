import abc
import asyncio
import base64
import io
from pathlib import Path

import aiohttp
from PIL import Image as PILImage

import astrbot.core.message.components as Comp
from astrbot.api import logger
from astrbot.core.platform.astr_message_event import AstrMessageEvent


class ImageWorkflow(abc.ABC):
    def __init__(self, proxy=None):
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

        if Path(src).is_file():
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
