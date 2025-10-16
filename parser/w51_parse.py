import asyncio
import json
from typing import List

import requests
from loguru import logger
from lxml import etree

from core.enums import Platform
from parser.base_parse import BaseParser
from util import download_util


class W51Parser(BaseParser):

    async def get_platform(self) -> Platform:
        return Platform.WY_51

    async def is_me(self, url: str) -> bool:
        return True

    async def parse(self, url: str) -> list[str]:
        html = self.get_detail_html(url)
        title = html.xpath("//meta[@property='og:title']/@content")[0]
        videos = self.parse_video(html) or []
        videos = list(set(videos))

        # 只下载到服务器 不返回
        for i, video in enumerate(videos):
            download_util.submit_download_task(video, '51', title, f'{title}{i if len(videos) > 1 else ''}.mp4')
        return []

    def get_detail_html(self, detail_url: str):
        response = requests.get(detail_url, headers = {
            'User-Agent': "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36"})
        logger.info(f'详情页：当前地址 -> {detail_url}')
        if response and response.text:
            logger.info(f'详情页：页面内容 -> {response.text}')
            return etree.HTML(response.text)
        return None

    def parse_video(self, html) -> List[str] | None:
        video_urls = []
        video_elements = html.xpath("//div[@class='dplayer']/@data-config")
        if video_elements:
            for video_element in video_elements:
                # 将data-config属性的值解析为JSON对象
                config_json = json.loads(video_element)
                video_url = config_json.get('video', {}).get('url', None)
                if video_url:
                    logger.success(f"详情页：播放地址:{video_url}")
                    video_urls.append(video_url)
                else:
                    logger.error("详情页：没有找到播放地址")
                video_urls.append(video_url)

        return video_urls


async def main():
    wy_parser = W51Parser()
    resp = await wy_parser.parse("https://additional.oieqtip.cc/archives/230218/")
    print(resp)
    print(len(resp))


if __name__ == '__main__':
    asyncio.run(main())  # ✅ 正确执行
