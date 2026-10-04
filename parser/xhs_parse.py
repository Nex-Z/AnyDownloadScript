import asyncio
import json
import re
from urllib.parse import urlparse

import aiohttp
from fastapi import HTTPException
from lxml import etree

from core.enums import Platform
from parser.base_parse import BaseParser


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
        content = await self.fetch_page(url)
        resources = self.extract_resources(content)
        if not resources:
            raise HTTPException(422, "未能读取小红书笔记资源，请确认分享链接可访问；页面可能要求登录或验证")
        if resources[0].startswith(self.ORIGINAL_IMAGE_PREFIX):
            await self.validate_original_images(resources)
        return resources

    async def validate_original_images(self, urls: list[str]) -> None:
        # Probe the actual file rather than trusting a constructed URL or HEAD.
        # Reject redirects so the CDN cannot silently send us to a preview page.
        async def check(session, index, url):
            try:
                async with session.get(url, headers={"Range": "bytes=0-31"},
                                       allow_redirects=False) as response:
                    if response.status not in (200, 206):
                        raise HTTPException(502, f"第 {index} 张原图获取失败（HTTP {response.status}），未返回预览图")
                    try:
                        prefix = await response.content.readexactly(32)
                    except asyncio.IncompleteReadError as exc:
                        prefix = exc.partial
                    if not self.is_image_header(prefix):
                        raise HTTPException(502, f"第 {index} 张原图响应不是有效图片，未返回预览图")
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

    async def fetch_page(self, url: str) -> str:
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as session:
                async with session.get(url, headers=self.video_headers) as response:
                    response.raise_for_status()
                    return await response.text()
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            raise HTTPException(502, "访问小红书失败，请稍后重试") from exc

    @staticmethod
    def extract_resources(content: str) -> list[str]:
        html = etree.HTML(content)
        if html is None:
            return []
        # Decode only the note object: surrounding JS state may contain undefined.
        # Never execute upstream JavaScript or collect images from recommended notes.
        decoder = json.JSONDecoder()
        for script in html.xpath("//script/text()"):
            for match in re.finditer(r'"noteData"\s*:\s*(?=\{)', script):
                try:
                    note, _ = decoder.raw_decode(script, match.end())
                except ValueError:
                    continue
                if not note.get("noteId"):
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
                            raise HTTPException(422, f"第 {index} 张图片缺少有效原图标识，未返回预览图")
                        urls.append(XhsParser.ORIGINAL_IMAGE_PREFIX + file_id)
                if urls:
                    return list(dict.fromkeys(urls))
        # Older detail pages expose media through Open Graph metadata.
        # A generic login/home page must not return its site icon as a note image.
        titles = html.xpath("//meta[@property='og:title' or @name='og:title']/@content")
        if not titles:
            return []
        videos = html.xpath("//meta[@property='og:video' or @name='og:video']/@content")
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
