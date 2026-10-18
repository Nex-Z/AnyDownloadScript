import asyncio
import json
import re
from urllib.parse import urlparse, urljoin
from http.cookies import SimpleCookie

import aiohttp
from fastapi import HTTPException
from lxml import etree
from yarl import URL
from loguru import logger

from core.enums import Platform
from parser.base_parse import BaseParser
from util.upstream import ParseError, read_url, safe_url, request_id


class XhsParser(BaseParser):
    ORIGINAL_IMAGE_PREFIX = "https://ci.xiaohongshu.com/"

    def __init__(self):
        """
        初始化
        """
        super().__init__()
        self.imgs_headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36 Edg/141.0.0.0"
        }
        self.video_headers = {
            "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 18_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.5 Mobile/15E148 Safari/604.1 Edg/140.0.0.0"
        }
        # Ordinary mobile Safari share-page request, verified on the NAS.
        # No account cookie, browser runtime or third-party parsing service required.
        self.compat_headers = {
            "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1",
            "Referer": "https://www.xiaohongshu.com/",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        }

    async def get_platform(self) -> Platform:
        return Platform.XIAO_HONG_SHU

    async def is_me(self, url: str) -> bool:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        return parsed.scheme in ("http", "https") and (
            host in {"xhslink.com", "xhslink.cn", "xiaohongshu.com"}
            or host.endswith(".xiaohongshu.com")
        )

    async def parse(self, url: str) -> list[str]:
        try:
            return await self._parse_profile(url)
        except ParseError as exc:
            if exc.stage != "page" or exc.error_code not in {
                "VERIFICATION_REQUIRED", "UPSTREAM_ACCESS_DENIED", "METADATA_UNAVAILABLE",
                "UPSTREAM_NETWORK", "UPSTREAM_HTTP_ERROR",
            }:
                raise
            logger.warning("request_id={} event=xhs_fallback strategy=mobile_safari reason={}",
                           request_id.get(), exc.error_code)
        # One alternate normal share request. Never execute or submit a challenge.
        # Only the final failure reaches the outer cooldown guard.
        resources = await self._parse_profile(url, mobile_compat=True)
        logger.info("request_id={} event=xhs_fallback_success strategy=mobile_safari count={}",
                    request_id.get(), len(resources))
        return resources

    async def _parse_profile(self, url, mobile_compat=False):
        content, resolved = await self.fetch_page(url, mobile_compat=mobile_compat)
        identity = self.note_id(resolved) or self.note_id(url)
        resources = self.extract_resources(content, identity)
        if not resources and not mobile_compat:
            # Desktop structure is tried only after a normal page, never a challenge.
            content, resolved = await self.fetch_page(resolved, desktop=True)
            resources = self.extract_resources(content, identity)
        if not resources:
            raise ParseError(422, "页面未提供该笔记的完整资源，请重新复制分享链接；也可配置有效的 XHS_COOKIE",
                             "METADATA_UNAVAILABLE")
        if resources[0].startswith(self.ORIGINAL_IMAGE_PREFIX):
            await self.validate_original_images(resources)
        return resources

    @staticmethod
    def note_id(url):
        match = re.search(r"/(?:explore|discovery/item)/([A-Za-z0-9]+)(?:/|$)", urlparse(url).path)
        return match.group(1) if match else None

    async def validate_original_images(self, urls: list[str]) -> None:
        # Probe the actual file rather than trusting a constructed URL or HEAD.
        # Reject redirects so the CDN cannot silently send us to a preview page.
        async def check(session, index, url):
            try:
                status, _, prefix, _ = await read_url(session, url,
                    headers={"Range": "bytes=0-31"}, allow_redirects=False,
                    prefix=True, limit=32, stage="image")
                if status not in (200, 206):
                    raise ParseError(502, f"第 {index} 张原图获取失败（HTTP {status}），未返回预览图",
                                     "IMAGE_HTTP_ERROR", "image", status >= 500)
                if not self.is_image_header(prefix):
                    raise ParseError(502, f"第 {index} 张原图响应不是有效图片，未返回预览图",
                                     "IMAGE_INVALID", "image")
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                raise HTTPException(502, f"第 {index} 张原图访问失败，未返回预览图") from exc

        try:
            async with aiohttp.ClientSession(
                connector=aiohttp.TCPConnector(limit=4),
                timeout=aiohttp.ClientTimeout(total=15),
            ) as session:
                results = await asyncio.wait_for(asyncio.gather(*(
                    check(session, index, url) for index, url in enumerate(urls, 1)
                ), return_exceptions=True), timeout=25)
                for result in results:
                    if isinstance(result, BaseException):
                        raise result
        except asyncio.TimeoutError as exc:
            raise HTTPException(502, "原图验证超时，未返回预览图") from exc

    @staticmethod
    def is_image_header(data: bytes) -> bool:
        return (
            data.startswith((b"\xff\xd8\xff", b"\x89PNG\r\n\x1a\n", b"GIF87a", b"GIF89a",
                             b"II*\x00", b"MM\x00*", b"BM"))
            or (data.startswith(b"RIFF") and data[8:12] == b"WEBP")
            or (data[4:8] == b"ftyp" and data[8:12] in
                (b"avif", b"avis", b"heic", b"heix", b"hevc", b"hevx", b"mif1", b"msf1"))
        )

    async def fetch_page(self, url: str, desktop=False, mobile_compat=False) -> tuple[str, str]:
        # At most three page attempts (primary, normal-page desktop, compatibility)
        # leave room for the 25-second image probe inside the 60-second API budget.
        try:
            async with asyncio.timeout(10):
                return await self._fetch_page(url, desktop, mobile_compat)
        except TimeoutError as exc:
            raise ParseError(502, "小红书页面请求超时，请稍后重试", "UPSTREAM_NETWORK", retryable=True) from exc

    async def _fetch_page(self, url, desktop=False, mobile_compat=False):
        from config.config import settings
        jar = aiohttp.CookieJar()
        if settings.xhs_cookie:
            cookies = SimpleCookie()
            try:
                cookies.load(settings.xhs_cookie.get_secret_value())
                if not cookies:
                    raise ValueError("Empty cookie configuration")
                scoped = SimpleCookie()
                for name, value in cookies.items():
                    scoped[name] = value.value
                    scoped[name]["domain"] = ".xiaohongshu.com"
                    scoped[name]["path"] = "/"
                    scoped[name]["secure"] = True
                jar.update_cookies(scoped, response_url=URL("https://www.xiaohongshu.com/"))
            except Exception as exc:
                raise ParseError(500, "XHS_COOKIE 配置格式无效", "COOKIE_CONFIG_INVALID", "config") from exc
        try:
            async with aiohttp.ClientSession(cookie_jar=jar, trust_env=False,
                    timeout=aiohttp.ClientTimeout(total=15)) as session:
                for _ in range(6):
                    if not await self.is_me(url):
                        raise ParseError(422, "小红书链接跳转到不支持的网站", "UNSUPPORTED_REDIRECT")
                    path = urlparse(url).path.lower()
                    if any(part in path for part in ("captcha", "verify", "website-login", "/login")):
                        logger.warning("request_id={} stage=page challenge={}", request_id.get(), safe_url(url))
                        raise ParseError(422, "小红书要求登录或验证码验证；请在浏览器完成验证后更新 NAS 的 XHS_COOKIE，再重试",
                                         "VERIFICATION_REQUIRED", retry_after=60)
                    # Never send configured secure cookies over an HTTP redirect.
                    if url.startswith("http://"):
                        url = "https://" + url[7:]
                    status, headers, body, _ = await read_url(session, url,
                        headers=(self.compat_headers if mobile_compat else
                                 self.imgs_headers if desktop else self.video_headers),
                        allow_redirects=False)
                    if status in (301, 302, 303, 307, 308):
                        location = headers.get("Location")
                        if not location:
                            raise ParseError(502, "小红书返回无效跳转", "UPSTREAM_REDIRECT_INVALID")
                        url = urljoin(url, location)
                        continue
                    if status == 429:
                        raise ParseError(429, "小红书请求受限，请稍后重试", "UPSTREAM_RATE_LIMIT", retryable=True, retry_after=60)
                    if status in (401, 403):
                        raise ParseError(422, "小红书拒绝访问，请检查分享链接和 XHS_COOKIE", "UPSTREAM_ACCESS_DENIED")
                    if status in (404, 410):
                        raise ParseError(422, "分享链接或笔记已失效，请重新复制链接", "POST_UNAVAILABLE")
                    if status != 200:
                        raise ParseError(502, f"小红书页面获取失败（HTTP {status}）", "UPSTREAM_HTTP_ERROR", retryable=status >= 500)
                    content = body.decode("utf-8", errors="replace")
                    if "<title>安全验证" in content or "id=\"captcha\"" in content:
                        raise ParseError(422, "小红书要求验证码验证，请在浏览器完成验证并更新 XHS_COOKIE",
                                         "VERIFICATION_REQUIRED", retry_after=60)
                    return content, url
                raise ParseError(502, "小红书分享链接跳转次数过多", "UPSTREAM_REDIRECT_LIMIT")
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            raise HTTPException(502, "访问小红书失败，请稍后重试") from exc

    @staticmethod
    def extract_resources(content: str, note_id=None) -> list[str]:
        html = etree.HTML(content)
        if html is None:
            return []
        # Decode only the note object: surrounding JS state may contain undefined.
        # Never execute upstream JavaScript or collect images from recommended notes.
        decoder = json.JSONDecoder()
        for script in html.xpath("//script/text()"):
            for match in re.finditer(r'"(?:noteData|note)"\s*:\s*(?=\{)', script):
                try:
                    note, _ = decoder.raw_decode(script, match.end())
                except ValueError:
                    continue
                if not note.get("noteId") or (note_id and note["noteId"] != note_id):
                    continue
                urls = []
                if note.get("type") == "video":
                    streams = (note.get("video") or {}).get("media", {}).get("stream", {})
                    for codec in ("h264", "h265", "av1"):
                        for stream in streams.get(codec, []):
                            if stream.get("masterUrl"):
                                urls.append(stream["masterUrl"])
                        if urls:
                            break
                    urls = urls[:1]
                else:
                    for index, item in enumerate(note.get("imageList", []), 1):
                        file_id = item.get("fileId")
                        if not isinstance(file_id, str) or not re.fullmatch(
                            r"[A-Za-z0-9_-]+(?:/[A-Za-z0-9_-]+)*", file_id
                        ):
                            raise ParseError(422, f"第 {index} 张图片缺少有效原图标识，未返回预览图",
                                             "ORIGINAL_ID_MISSING", "metadata")
                        urls.append(XhsParser.ORIGINAL_IMAGE_PREFIX + file_id)
                if urls:
                    return list(dict.fromkeys(urls))
        # Older detail pages expose media through Open Graph metadata.
        # A generic login/home page must not return its site icon as a note image.
        titles = html.xpath("//meta[@property='og:title' or @name='og:title']/@content")
        if not titles:
            return []
        videos = html.xpath("//meta[@property='og:video' or @name='og:video']/@content") if not note_id else []
        if videos:
            return videos[:1]
        # Open Graph images can be watermarked previews; they are never a fallback.
        return []

    async def parse_image(self, url: str) -> list[str]:
        return await self.parse(url)

    async def parse_video(self, url: str) -> list[str]:
        return await self.parse(url)


async def main():
    xhs_parser = XhsParser()
    resp = await xhs_parser.parse(
        "http://xhslink.com/o/19LlzeMQqpJ")
    print(resp)


if __name__ == '__main__':
    asyncio.run(main())  # ✅ 正确执行
