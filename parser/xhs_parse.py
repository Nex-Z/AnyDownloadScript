import asyncio
import re

import aiohttp
from lxml import etree

from core.enums import Platform
from parser.base_parse import BaseParser


class XhsParser(BaseParser):

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
        return url.startswith("http://xhslink.com/") or url.startswith("https://xhslink.com/")

    async def parse(self, url: str) -> list[str]:
        videos = await self.parse_video(url) or []
        if videos:
            return videos
        imgs = await self.parse_image(url) or []
        return imgs

    async def parse_image(self, url: str) -> list[str]:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers = self.imgs_headers) as response:
                resp = await response.text()
                html = etree.HTML(resp)
                meta_url_list = html.xpath("//meta[@name='og:image']/@content")
                return list(set(meta_url_list))

    async def parse_video(self, url: str) -> list[str]:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers = self.video_headers) as response:
                resp = await response.text()

                resp = resp.replace(r"\u002F", "/")
                pattern = re.compile(r'https?:\/\/sns-video-[a-z]{2,}\.xhscdn\.com\/[^"\'\s]+', re.S)
                urls = re.findall(pattern, resp)
                if urls:
                    # 有水印 http://sns-video-qc.xhscdn.com/stream/79/110/259/01e8e3cdf1e8b9b30103700399b9dcd5ae_259.mp4?sign=0b0a9dbfb471923283af843b05d62cdf&t=68e91596
                    # 无水印 https://sns-video-hs.xhscdn.com/stream/79/110/114/01e8e3cdf1e8b9b34f03700199b9dd02af_114.mp4
                    return urls[0]
                return []


async def main():
    xhs_parser = XhsParser()
    resp = await xhs_parser.parse(
        "http://xhslink.com/o/19LlzeMQqpJ")
    print(resp)


if __name__ == '__main__':
    asyncio.run(main())  # ✅ 正确执行
