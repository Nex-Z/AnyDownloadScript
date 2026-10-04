import asyncio
import json
from typing import List

import requests
from fastapi import HTTPException
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
        titles = html.xpath("//meta[@property='og:title']/@content") if html is not None else []
        if not titles:
            raise HTTPException(422, "页面不受支持或未包含可下载视频")
        title = titles[0]
        videos = self.parse_video(html) or []
        videos = list(set(videos))
        if not videos:
            raise HTTPException(422, "页面未包含可下载视频")

        # 只下载到服务器 不返回
        for i, video in enumerate(videos):
            download_util.submit_download_task(video, '51', title, f'{title}{i if len(videos) > 1 else ''}.mp4')
        return []

    def get_detail_html(self, detail_url: str):
        try:
            response = requests.get(detail_url, headers = {
                'User-Agent': "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36"}, timeout=20)
            response.raise_for_status()
        except requests.RequestException as exc:
            raise HTTPException(502, "访问目标页面失败，请稍后重试") from exc
        logger.info(f'详情页：当前地址 -> {detail_url}')
        if response and response.text:
            logger.debug(f'详情页：响应长度 -> {len(response.content)} bytes')
            return etree.HTML(response.text)
        return None

    def parse_video(self, html) -> List[str] | None:
        video_urls = []
        video_elements = html.xpath("//div[@class='dplayer']/@data-config")
        if video_elements:
            for video_element in video_elements:
                # 将data-config属性的值解析为JSON对象
                try:
                    config_json = json.loads(video_element)
                except ValueError:
                    continue
                video_url = config_json.get('video', {}).get('url', None)
                if video_url:
                    logger.success(f"详情页：播放地址:{video_url}")
                    video_urls.append(video_url)
                else:
                    logger.error("详情页：没有找到播放地址")

        return video_urls


async def main():
    wy_parser = W51Parser()
    resp = await wy_parser.parse("https://additional.oieqtip.cc/archives/230218/")
    print(resp)
    print(len(resp))


if __name__ == '__main__':
    asyncio.run(main())  # ✅ 正确执行
