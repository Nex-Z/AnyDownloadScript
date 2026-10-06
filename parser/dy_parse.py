"""Parse the requested public Douyin post, preserving signed media URLs."""
import asyncio
import json
import re
from urllib.parse import unquote, urljoin, urlparse

import aiohttp
from fastapi import HTTPException
from lxml import etree
from core.enums import Platform
from parser.base_parse import BaseParser


class DyParser(BaseParser):
    def __init__(self):
        self.headers = {"User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 18_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.5 Mobile/15E148 Safari/604.1"}

    async def get_platform(self):
        return Platform.DOU_YIN

    @staticmethod
    def allowed_url(url):
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        return parsed.scheme in ("http", "https") and any(
            host == domain or host.endswith("." + domain)
            for domain in ("douyin.com", "iesdouyin.com"))

    async def is_me(self, url):
        return self.allowed_url(url)

    @staticmethod
    def post_id(url):
        match = re.search(r"/(?:share/)?(?:video|note)/(\d+)(?:/|$)", urlparse(url).path)
        return match.group(1) if match else None

    async def fetch_page(self, session, url):
        for _ in range(6):
            if not self.allowed_url(url):
                raise HTTPException(422, "Douyin link redirected to an unsupported site")
            async with session.get(url, allow_redirects=False) as response:
                if response.status in (301, 302, 303, 307, 308):
                    location = response.headers.get("Location")
                    if not location:
                        raise HTTPException(502, "Invalid Douyin share redirect")
                    url = urljoin(url, location)
                    continue
                if response.status != 200:
                    raise HTTPException(502, f"Douyin page unavailable (HTTP {response.status})")
                try:
                    body = await response.content.readexactly(4 * 1024 * 1024 + 1)
                except asyncio.IncompleteReadError as exc:
                    body = exc.partial
                if len(body) > 4 * 1024 * 1024:
                    raise HTTPException(502, "Douyin page response too large")
                return body.decode("utf-8", errors="replace"), url
        raise HTTPException(502, "Too many Douyin share redirects")

    @staticmethod
    def states(content):
        tree = etree.HTML(content)
        if tree is None:
            return
        for script in tree.xpath("//script"):
            text = script.text or ""
            if script.get("id") in ("RENDER_DATA", "__NEXT_DATA__"):
                try:
                    yield json.loads(unquote(text) if script.get("id") == "RENDER_DATA" else text)
                except (ValueError, TypeError):
                    pass
            for match in re.finditer(r"(?:window\s*\.\s*)?(?:_ROUTER_DATA|__INITIAL_STATE__)\s*=\s*", text):
                try:
                    yield json.JSONDecoder().raw_decode(text[match.end():])[0]
                except ValueError:
                    pass

    @classmethod
    def find_post(cls, state, post_id):
        # Never substitute recommendations, avatars, or covers for the requested post.
        if isinstance(state, dict):
            identity = state.get("aweme_id", state.get("awemeId"))
            if identity is not None and (post_id is None or str(identity) == str(post_id)):
                if "images" in state or "image_post_info" in state or "video" in state:
                    return state
            for value in state.values():
                found = cls.find_post(value, post_id)
                if found is not None:
                    return found
        elif isinstance(state, list):
            for value in state:
                found = cls.find_post(value, post_id)
                if found is not None:
                    return found
        return None

    @staticmethod
    def addresses(value):
        if not isinstance(value, dict):
            return []
        urls = value.get("url_list", value.get("urlList", []))
        if not isinstance(urls, list):
            return []
        # CDN signatures, transformation parameters and WebP extensions are preserved.
        return list(dict.fromkeys(u for u in urls if isinstance(u, str)
            and urlparse(u).scheme in ("http", "https") and urlparse(u).hostname))

    @staticmethod
    def quality(address, bitrate=None):
        def number(value):
            try:
                return max(0, int(value))
            except (ValueError, TypeError):
                return 0
        return number(address.get("width")) * number(address.get("height")), number(bitrate)

    @classmethod
    def candidates(cls, post):
        images = post.get("images")
        if images is None and isinstance(post.get("image_post_info"), dict):
            images = post["image_post_info"].get("images")
        if images or post.get("aweme_type") in (68, 150) or "image_post_info" in post:
            if not isinstance(images, list) or not images:
                raise HTTPException(422, "Douyin gallery has no available images")
            groups = []
            for image in images:
                if not isinstance(image, dict):
                    raise HTTPException(422, "Incomplete Douyin image metadata")
                urls = cls.addresses(image.get("display_image")) or cls.addresses(image)
                if not urls:
                    raise HTTPException(422, "Douyin image URL missing; incomplete gallery not returned")
                groups.append(urls)
            return "image", groups
        video = post.get("video")
        if not isinstance(video, dict):
            raise HTTPException(422, "Douyin post has no available media")
        variants = []
        for key in ("play_addr_h264", "play_addr", "playAddr"):
            addr = video.get(key)
            if isinstance(addr, dict):
                variants.append((cls.quality(addr), addr))
        rates = video.get("bit_rate", [])
        for rate in rates if isinstance(rates, list) else []:
            if isinstance(rate, dict) and isinstance(rate.get("play_addr"), dict):
                variants.append((cls.quality(rate["play_addr"], rate.get("bit_rate")), rate["play_addr"]))
        urls = []
        for _, address in sorted(variants, key=lambda v: v[0], reverse=True):
            if address.get("has_watermark") or address.get("hasWatermark"):
                continue
            for url in cls.addresses(address):
                parsed = urlparse(url)
                if "playwm" not in parsed.path.lower() and "watermark=1" not in parsed.query.lower():
                    if url not in urls:
                        urls.append(url)
        # download_addr is generally watermarked. Do not rewrite playwm or construct URLs.
        if not urls:
            raise HTTPException(422, "Douyin did not provide a public non-watermarked playback URL")
        return "video", [urls]

    @classmethod
    def extract_resources(cls, content, post_id=None):
        for state in cls.states(content):
            post = cls.find_post(state, post_id)
            if post is not None:
                _, groups = cls.candidates(post)
                return [group[0] for group in groups]
        return []

    async def validate_media(self, session, kind, groups):
        from parser.xhs_parse import XhsParser
        async def select(index, urls):
            for url in urls:
                try:
                    async with session.get(url, headers={"Range": "bytes=0-63"}) as response:
                        if response.status not in (200, 206):
                            continue
                        try:
                            prefix = await response.content.readexactly(64)
                        except asyncio.IncompleteReadError as exc:
                            prefix = exc.partial
                        valid = XhsParser.is_image_header(prefix) if kind == "image" else (
                            len(prefix) >= 12 and prefix[4:8] == b"ftyp")
                        if valid:
                            return url
                except (aiohttp.ClientError, asyncio.TimeoutError):
                    continue
            raise HTTPException(502, f"Douyin media {index} unavailable or invalid; please retry later")
        # Limit traffic and retain complete gallery ordering.
        return [await select(index, urls) for index, urls in enumerate(groups, 1)]

    async def parse(self, url) -> list[str]:
        try:
            async with aiohttp.ClientSession(headers=self.headers,
                timeout=aiohttp.ClientTimeout(total=20), connector=aiohttp.TCPConnector(limit=4)) as session:
                content, resolved = await self.fetch_page(session, url)
                identity = self.post_id(resolved) or self.post_id(url)
                if identity is None:
                    raise HTTPException(422, "Cannot identify Douyin post; use a post share link")
                canonical = f"https://www.iesdouyin.com/share/video/{identity}/"
                for attempt in range(2):
                    for state in self.states(content):
                        post = self.find_post(state, identity)
                        if post is not None:
                            kind, groups = self.candidates(post)
                            return await self.validate_media(session, kind, groups)
                    if attempt == 0 and resolved.rstrip("/") != canonical.rstrip("/"):
                        content, _ = await self.fetch_page(session, canonical)
                    else:
                        break
                raise HTTPException(422, "Douyin public page did not provide media. Login or verification may be required, or the post may be unavailable; access restrictions were not bypassed.")
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            raise HTTPException(502, "Douyin connection failed or timed out; please retry later") from exc
