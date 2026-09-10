import asyncio
import base64
import mimetypes
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup, Tag

import astrbot.api.message_components as Comp
from astrbot.api import logger
from astrbot.core.platform.astr_message_event import AstrMessageEvent


@dataclass(slots=True)
class IQDBResult:
    match_type: str
    sources: list[str]
    similarity: float
    thumbnail_url: str
    source_urls: dict[str, str] = field(default_factory=dict)
    dimensions: str | None = None
    rating: str | None = None
    tags: str | None = None


class IQDBClient:
    BASE_URL = "https://iqdb.org/"
    MAX_IMAGE_BYTES = 20 * 1024 * 1024
    # iqdb.org 当前编号不是连续值：Zerochan=11、Anime-Pictures=13。
    DEFAULT_SERVICES = ("1", "2", "3", "4", "5", "6", "11", "13")
    SOURCE_DOMAINS = {
        "danbooru.donmai.us": "Danbooru",
        "konachan.com": "Konachan",
        "anime-pictures.net": "Anime-Pictures",
        "gelbooru.com": "Gelbooru",
        "sankakucomplex.com": "Sankaku Channel",
        "chan.sankakucomplex.com": "Sankaku Channel",
        "e-shuushuu.net": "e-shuushuu",
        "zerochan.net": "Zerochan",
        "yande.re": "Yande.re",
    }

    def __init__(self, proxy: str | None = None) -> None:
        self.proxy = proxy.strip() if proxy else None
        self.client = httpx.AsyncClient(
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/139.0.0.0 Safari/537.36"
                ),
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            },
            timeout=httpx.Timeout(60, connect=15),
            follow_redirects=True,
            proxy=self.proxy,
        )
        self._init_lock = asyncio.Lock()
        self._initialized = False
        self.services = self.DEFAULT_SERVICES

    async def init_session(self, force: bool = False) -> None:
        async with self._init_lock:
            if self._initialized and not force:
                return
            if force:
                self.client.cookies.clear()
            response = await self._request_with_network_retry(
                "GET", self.BASE_URL, headers={"Referer": self.BASE_URL}
            )
            response.raise_for_status()
            soup = BeautifulSoup(response.text, "html.parser")
            discovered = tuple(
                dict.fromkeys(
                    str(item.get("value"))
                    for item in soup.select('input[name="service[]"][value]')
                    if str(item.get("value", "")).isdigit()
                )
            )
            if discovered:
                self.services = discovered
            self._initialized = True
            has_cookie = "iqdb_bcc" in self.client.cookies
            logger.info(
                f"IQDB 会话初始化完成，Cookie={'有效' if has_cookie else '未下发'}，"
                f"服务编号={','.join(self.services)}，"
                f"代理={'已启用' if self.proxy else '未启用'}"
            )

    async def ensure_session(self) -> None:
        if not self._initialized or "iqdb_bcc" not in self.client.cookies:
            await self.init_session(force=True)

    async def close(self) -> None:
        await self.client.aclose()

    async def _request_with_network_retry(self, method: str, url: str, **kwargs):
        last_error: Exception | None = None
        for attempt in range(2):
            try:
                return await self.client.request(method, url, **kwargs)
            except httpx.HTTPError as exc:
                last_error = exc
                if attempt == 0:
                    logger.warning(f"IQDB 网络请求失败，将重试一次: {exc}")
        assert last_error is not None
        raise last_error

    @staticmethod
    def normalize_url(url: str) -> str:
        if url.startswith("//"):
            return f"https:{url}"
        return urljoin(IQDBClient.BASE_URL, url)

    @staticmethod
    def _is_search_result_page(response: httpx.Response) -> bool:
        html = response.text
        lowered = html.lower()
        if any(
            marker in lowered
            for marker in ("botcheck=failed", "bot-check", "bot check", "captcha")
        ) or "botcheck=failed" in str(response.url).lower():
            return False
        soup = BeautifulSoup(html, "html.parser")
        title = soup.title.get_text(" ", strip=True).lower() if soup.title else ""
        has_pages = soup.select_one("body #pages") is not None
        has_search_summary = any(
            paragraph.get_text(" ", strip=True).lower().startswith("searched ")
            for paragraph in soup.select("body p")
        )
        return has_pages and (
            has_search_summary
            or "search result" in title
            or any(
                marker in lowered
                for marker in (
                    "best match",
                    "additional match",
                    "possible match",
                    "no relevant matches",
                )
            )
        )

    async def search(
        self, image: bytes, mime_type: str, filename: str = "image"
    ) -> list[IQDBResult]:
        if not image:
            raise ValueError("图片内容为空")
        if len(image) > self.MAX_IMAGE_BYTES:
            raise ValueError("图片超过 20MB，无法搜索")
        await self.ensure_session()
        extension = mimetypes.guess_extension(mime_type) or ".img"
        data = {"service[]": list(self.services)}
        headers = {"Referer": self.BASE_URL, "Origin": "https://iqdb.org"}

        for attempt in range(2):
            response = await self._request_with_network_retry(
                "POST",
                self.BASE_URL,
                headers=headers,
                data=data,
                files={"file": (f"{filename}{extension}", image, mime_type)},
            )
            if self._is_search_result_page(response):
                response.raise_for_status()
                return self.parse_results(response.text)
            if attempt == 0:
                logger.warning("IQDB Cookie 或结果页异常，正在静默刷新会话并重试")
                await self.init_session(force=True)
                continue
            response.raise_for_status()
            soup = BeautifulSoup(response.text, "html.parser")
            title = soup.title.get_text(" ", strip=True) if soup.title else "无标题"
            logger.error(
                "IQDB 返回异常页面: "
                f"status={response.status_code}, url={response.url}, "
                f"title={title!r}, bytes={len(response.content)}, "
                f"cookies={','.join(self.client.cookies.keys()) or 'none'}"
            )
            raise RuntimeError("IQDB 返回内容不是有效的搜索结果页")
        return []

    @classmethod
    def _source_name(cls, url: str) -> str | None:
        host = (urlparse(url).hostname or "").lower()
        host = host.removeprefix("www.")
        for domain, name in cls.SOURCE_DOMAINS.items():
            if host == domain or host.endswith(f".{domain}"):
                return name
        return None

    @classmethod
    def parse_results(cls, html: str) -> list[IQDBResult]:
        soup = BeautifulSoup(html, "html.parser")
        results: list[IQDBResult] = []
        match_pattern = re.compile(
            r"\b(Best match|Additional match|Possible match)\b", re.I
        )
        similarity_pattern = re.compile(r"([0-9]+(?:\.[0-9]+)?)%\s*similarity", re.I)

        blocks = soup.select("#pages > *, #more1 div.pages > *")
        for block in blocks:
            text = block.get_text(" ", strip=True)
            match = match_pattern.search(text)
            similarity_match = similarity_pattern.search(text)
            if not match or not similarity_match or "Your image" in text:
                continue
            image = block.find("img")
            if not isinstance(image, Tag) or not image.get("src"):
                continue
            thumbnail_url = cls.normalize_url(str(image["src"]))
            source_urls: dict[str, str] = {}
            for anchor in block.find_all("a", href=True):
                url = cls.normalize_url(str(anchor["href"]))
                source = cls._source_name(url)
                if source and source not in source_urls:
                    source_urls[source] = url
            if not source_urls:
                continue
            dimensions_match = re.search(r"\b(\d{2,6}\s*[x×]\s*\d{2,6})\b", text)
            rating_match = re.search(r"\bRating:\s*([^|,;]+)", text, re.I)
            tags_match = re.search(r"\bTags?:\s*(.+?)(?:\s+Rating:|$)", text, re.I)
            results.append(
                IQDBResult(
                    match_type=match.group(1).capitalize(),
                    sources=list(source_urls),
                    similarity=float(similarity_match.group(1)),
                    thumbnail_url=thumbnail_url,
                    source_urls=source_urls,
                    dimensions=(dimensions_match.group(1) if dimensions_match else None),
                    rating=(rating_match.group(1).strip() if rating_match else None),
                    tags=(tags_match.group(1).strip() if tags_match else None),
                )
            )
        unique: dict[tuple[str, str], IQDBResult] = {}
        for result in results:
            key = (result.thumbnail_url, next(iter(result.source_urls.values())))
            unique[key] = result
        return sorted(
            (result for result in unique.values() if result.similarity > 30),
            key=lambda result: result.similarity,
            reverse=True,
        )

    async def download_thumbnail(self, url: str) -> bytes | None:
        try:
            response = await self._request_with_network_retry(
                "GET", url, headers={"Referer": self.BASE_URL}
            )
            response.raise_for_status()
            if not response.headers.get("content-type", "").startswith("image/"):
                return None
            return response.content
        except httpx.HTTPError as exc:
            logger.warning(f"IQDB 缩略图下载失败: {exc}")
            return None

    async def get_image(self, event: AstrMessageEvent) -> tuple[bytes, str] | None:
        images: list[Comp.Image] = []
        for component in event.get_messages():
            if isinstance(component, Comp.Reply) and component.chain:
                images.extend(
                    item for item in component.chain if isinstance(item, Comp.Image)
                )
            elif isinstance(component, Comp.Image):
                images.append(component)
        for image in images:
            source = image.url or image.file
            if not source:
                continue
            try:
                if source.startswith("base64://"):
                    raw = base64.b64decode(source.removeprefix("base64://"))
                    return raw, self.detect_mime(raw)
                if source.startswith("file:///"):
                    source = source[8:]
                if source.startswith("http"):
                    response = await self._request_with_network_retry("GET", source)
                    response.raise_for_status()
                    raw = response.content
                    mime = response.headers.get("content-type", "").split(";", 1)[0]
                    return (
                        raw,
                        mime if mime.startswith("image/") else self.detect_mime(raw),
                    )
                path = Path(source)

                if await asyncio.to_thread(path.is_file):
                    raw = await asyncio.to_thread(path.read_bytes)
                    return raw, self.detect_mime(raw)
            except (OSError, ValueError, httpx.HTTPError) as exc:
                logger.warning(f"读取待搜索图片失败: {exc}")
        return None

    @staticmethod
    def detect_mime(image: bytes) -> str:
        signatures = (
            (b"\xff\xd8\xff", "image/jpeg"),
            (b"\x89PNG\r\n\x1a\n", "image/png"),
            (b"GIF87a", "image/gif"),
            (b"GIF89a", "image/gif"),
            (b"BM", "image/bmp"),
        )
        for signature, mime in signatures:
            if image.startswith(signature):
                return mime
        if image.startswith(b"RIFF") and image[8:12] == b"WEBP":
            return "image/webp"
        raise ValueError("不支持的图片格式")
