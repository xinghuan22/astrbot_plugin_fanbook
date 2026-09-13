import asyncio
import base64
import mimetypes
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlencode, urljoin, urlparse, urlsplit

import aiohttp
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
    GATEWAY_SOURCE_HOSTS = {
        "danbooru.donmai.us",
        "gelbooru.com",
        "yande.re",
        "konachan.com",
    }

    def __init__(
        self,
        proxy: str | None = None,
        gateway_base_url: str = "https://image.lospro.kissnab.top",
    ) -> None:
        self.proxy = proxy.strip() if proxy else None
        self.gateway_base_url = gateway_base_url.strip().rstrip("/")
        gateway = urlsplit(self.gateway_base_url)
        if gateway.scheme not in {"http", "https"} or not gateway.netloc:
            raise ValueError("image_gateway_url 必须是有效的 HTTP(S) 地址")
        self.client = aiohttp.ClientSession(
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/139.0.0.0 Safari/537.36"
                ),
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            },
            timeout=aiohttp.ClientTimeout(total=60, connect=15),
        )
        self._init_lock = asyncio.Lock()
        self._initialized = False
        self.services = self.DEFAULT_SERVICES

    async def init_session(self, force: bool = False) -> None:
        async with self._init_lock:
            if self._initialized and not force:
                return
            if force:
                self.client.cookie_jar.clear()
            response = await self._request_with_network_retry(
                "GET", self.BASE_URL, headers={"Referer": self.BASE_URL}
            )
            response.raise_for_status()
            soup = BeautifulSoup(await response.text(), "html.parser")
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
            has_cookie = any(
                cookie.key == "iqdb_bcc" for cookie in self.client.cookie_jar
            )
            logger.info(
                f"IQDB 会话初始化完成，Cookie={'有效' if has_cookie else '未下发'}，"
                f"服务编号={','.join(self.services)}，"
                f"代理={'已启用' if self.proxy else '未启用'}"
            )

    async def ensure_session(self) -> None:
        has_cookie = any(
            cookie.key == "iqdb_bcc" for cookie in self.client.cookie_jar
        )
        if not self._initialized or not has_cookie:
            await self.init_session(force=True)

    async def close(self) -> None:
        if not self.client.closed:
            await self.client.close()

    async def process(self, event: AstrMessageEvent):
        """处理一次完整的 IQDB 搜图请求并生成 AstrBot 消息结果。"""
        image = await self.get_image(event)
        if image is None:
            yield event.plain_result("请在消息中附带一张图片")
            return

        image_bytes, mime_type = image
        try:
            results = await self.search(image_bytes, mime_type)
        except Exception as exc:
            logger.exception(f"IQDB 搜图失败: {exc}")
            yield event.plain_result(f"IQDB 搜图失败: {exc}")
            return

        if not results:
            yield event.plain_result("未找到相似度超过 30% 的结果")
            return

        thumbnails = await asyncio.gather(
            *(self.download_result_thumbnail(result) for result in results)
        )
        nodes = [
            self._build_result_node(event, result, thumbnail)
            for result, thumbnail in zip(results, thumbnails)
        ]
        yield event.chain_result([Comp.Nodes(nodes=nodes)])

    def _build_result_node(
        self,
        event: AstrMessageEvent,
        result: IQDBResult,
        thumbnail: bytes | None,
    ) -> Comp.Node:
        content: list[Comp.BaseMessageComponent] = []
        if thumbnail:
            content.append(Comp.Image.fromBytes(byte=thumbnail))
        content.append(Comp.Plain(f"匹配类型：{result.match_type}\n"))
        content.append(Comp.Plain(f"来源：{' / '.join(result.sources)}\n"))
        content.append(Comp.Plain(f"相似度：{result.similarity:g}%\n"))
        for source, url in result.source_urls.items():
            content.append(Comp.Plain(f"{source}：{self.replace_source_host(url)}\n"))
        if result.dimensions:
            content.append(Comp.Plain(f"尺寸：{result.dimensions}\n"))
        if result.rating:
            content.append(Comp.Plain(f"Rating：{result.rating}\n"))
        if result.tags:
            content.append(Comp.Plain(f"Tags：{result.tags}"))
        return Comp.Node(
            content=content,
            uin=event.get_self_id() or "0",
            name="IQDB 搜图",
        )

    def replace_source_host(self, url: str) -> str:
        """将支持的作品链接转换为 image-gateway 查看地址。"""
        parsed = urlsplit(url)
        host = (parsed.hostname or "").lower().removeprefix("www.")
        if host not in self.GATEWAY_SOURCE_HOSTS:
            return url
        return f"{self.gateway_base_url}/view?{urlencode({'url': url})}"

    async def _request_with_network_retry(self, method: str, url: str, **kwargs):
        data_factory = kwargs.pop("data_factory", None)
        last_error: Exception | None = None
        for attempt in range(2):
            try:
                request_kwargs = kwargs.copy()
                if data_factory:
                    request_kwargs["data"] = data_factory()
                async with self.client.request(
                    method,
                    url,
                    proxy=self.proxy,
                    allow_redirects=True,
                    **request_kwargs,
                ) as response:
                    await response.read()
                    return response
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
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
    def _is_search_result_page(response: aiohttp.ClientResponse, html: str) -> bool:
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
        headers = {"Referer": self.BASE_URL, "Origin": "https://iqdb.org"}

        def build_form() -> aiohttp.FormData:
            form = aiohttp.FormData()
            for service in self.services:
                form.add_field("service[]", service)
            form.add_field(
                "file",
                image,
                filename=f"{filename}{extension}",
                content_type=mime_type,
            )
            return form

        for attempt in range(2):
            response = await self._request_with_network_retry(
                "POST",
                self.BASE_URL,
                headers=headers,
                data_factory=build_form,
            )
            html = await response.text()
            if self._is_search_result_page(response, html):
                response.raise_for_status()
                return self.parse_results(html)
            if attempt == 0:
                logger.warning("IQDB Cookie 或结果页异常，正在静默刷新会话并重试")
                await self.init_session(force=True)
                continue
            response.raise_for_status()
            soup = BeautifulSoup(html, "html.parser")
            title = soup.title.get_text(" ", strip=True) if soup.title else "无标题"
            logger.error(
                "IQDB 返回异常页面: "
                f"status={response.status}, url={response.url}, "
                f"title={title!r}, bytes={len(await response.read())}, "
                "cookies="
                f"{','.join(cookie.key for cookie in self.client.cookie_jar) or 'none'}"
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

    async def download_thumbnail(
        self, url: str, referer: str | None = None, label: str = "IQDB"
    ) -> bytes | None:
        try:
            response = await self._request_with_network_retry(
                "GET", url, headers={"Referer": referer or self.BASE_URL}
            )
            response.raise_for_status()
            content_type = response.headers.get("content-type", "")
            if not content_type.startswith("image/"):
                logger.warning(
                    f"{label} 缩略图返回非图片内容: "
                    f"status={response.status}, content_type={content_type or 'unknown'}"
                )
                return None
            return await response.read()
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            logger.warning(f"{label} 缩略图下载失败: {exc}")
            return None

    async def download_result_thumbnail(self, result: IQDBResult) -> bytes | None:
        """支持的图站优先使用 image-gateway，其他结果使用 IQDB 缩略图。"""
        for source_url in result.source_urls.values():
            parsed = urlsplit(source_url)
            host = (parsed.hostname or "").lower().removeprefix("www.")
            if host not in self.GATEWAY_SOURCE_HOSTS:
                continue
            try:
                resolve_url = (
                    f"{self.gateway_base_url}/api/resolve?"
                    f"{urlencode({'url': source_url})}"
                )
                response = await self._request_with_network_retry("GET", resolve_url)
                response.raise_for_status()
                post = await response.json(content_type=None)
                site = str(post.get("site", ""))
                post_id = str(post.get("id", ""))
                if not site or not post_id:
                    continue
                variant = "preview" if post.get("preview_url") else "sample"
                if variant == "sample" and not post.get("sample_url"):
                    continue
                media_url = (
                    f"{self.gateway_base_url}/media/{site}/{post_id}/{variant}"
                )
                thumbnail = await self.download_thumbnail(
                    media_url,
                    referer=f"{self.gateway_base_url}/",
                    label="image-gateway",
                )
                if thumbnail:
                    return thumbnail
            except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
                logger.warning(f"image-gateway 缩略图下载失败，将回退 IQDB: {exc}")

        return await self.download_thumbnail(result.thumbnail_url)

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
                    raw = await response.read()
                    mime = response.headers.get("content-type", "").split(";", 1)[0]
                    return (
                        raw,
                        mime if mime.startswith("image/") else self.detect_mime(raw),
                    )
                path = Path(source)

                if await asyncio.to_thread(path.is_file):
                    raw = await asyncio.to_thread(path.read_bytes)
                    return raw, self.detect_mime(raw)
            except (
                OSError,
                ValueError,
                aiohttp.ClientError,
                asyncio.TimeoutError,
            ) as exc:
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
