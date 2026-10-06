import asyncio
import re
import requests
from loguru import logger
from urllib.parse import urlsplit, parse_qs, urlencode, urlunsplit

from fastapi import HTTPException
from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError

from core.enums import Platform
from config.config import settings
from parser.base_parse import BaseParser
from util.x_media import named_video


class XParser(BaseParser):
    HOSTS = {f'{prefix}{domain}' for domain in ('x.com', 'twitter.com')
             for prefix in ('', 'www.', 'm.', 'mobile.')}
    STATUS_PATH = re.compile(r'^/(?:[^/]+/status|i/web/status|statuses)/(\d+)(?:/(?:video|photo)/\d+)?/?$')

    async def get_platform(self) -> Platform:
        return Platform.X

    async def is_me(self, url: str) -> bool:
        try:
            parsed = urlsplit(url)
            return parsed.scheme in ('http', 'https') and parsed.hostname in self.HOSTS
        except ValueError:
            return False

    async def parse(self, url: str) -> list[str]:
        parsed = urlsplit(url)
        match = self.STATUS_PATH.fullmatch(parsed.path)
        if not await self.is_me(url) or not match:
            raise HTTPException(422, '请提供 X / Twitter 推文链接（包含 /status/数字）')
        # Strip tracking parameters and media index to return all videos in the tweet.
        canonical = f'https://x.com/i/web/status/{match.group(1)}'
        # Media APIs include photos and preserve mixed-media order; yt-dlp only extracts videos.
        result = await asyncio.to_thread(self._fallback, match.group(1))
        if result:
            return result
        try:
            return await asyncio.to_thread(self._extract, canonical)
        except (DownloadError, HTTPException) as exc:
            logger.warning('X 媒体接口与视频解析均失败：{}', type(exc).__name__)
            raise HTTPException(502, 'X 解析接口均未获取到图片或视频，请稍后重试或检查推文是否可访问') from exc

    @staticmethod
    def _original_photo(value) -> str | None:
        if not isinstance(value, str):
            return None
        parsed = urlsplit(value)
        if parsed.scheme != 'https' or parsed.hostname != 'pbs.twimg.com' or not parsed.path.startswith('/media/'):
            return None
        path = re.sub(r':(?:small|medium|large|orig|thumb)$', '', parsed.path)
        query = parse_qs(parsed.query)
        extension = re.search(r'\.(jpg|jpeg|png|webp)$', path, re.I)
        fmt = extension.group(1).lower() if extension else query.get('format', [''])[0].lower()
        if fmt not in ('jpg', 'jpeg', 'png', 'webp'):
            return None
        if extension:
            path = path[:extension.start()]
        return urlunsplit(('https', 'pbs.twimg.com', path, urlencode({'format': fmt, 'name': 'orig'}), ''))

    @staticmethod
    def _video_url(value) -> bool:
        if not isinstance(value, str):
            return False
        try:
            parsed = urlsplit(value)
            return parsed.scheme == 'https' and parsed.hostname == 'video.twimg.com' and parsed.path.endswith('.mp4')
        except ValueError:
            return False

    @classmethod
    def _fallback(cls, status_id: str) -> list[str]:
        for provider in ('fxtwitter', 'vxtwitter'):
            routes = [None, settings.x_proxy] if settings.x_proxy else [None, None]
            for proxy in routes:
                route = '代理' if proxy else '直连'
                try:
                    # Third-party media APIs are reachable directly on the NAS.
                    # Do not inherit the system proxy; retry transport failures via X_PROXY.
                    with requests.Session() as session:
                        session.trust_env = False
                        response = session.get(f'https://api.{provider}.com/i/status/{status_id}',
                                               proxies={'https': proxy} if proxy else {}, timeout=(4, 12))
                        response.raise_for_status()
                        result = cls._fallback_videos(provider, response.json(), status_id)
                    if result:
                        logger.info('X 媒体解析成功：{} {}，资源数 {}', provider, route, len(result))
                        return result
                    break
                except (requests.RequestException, ValueError, TypeError, AttributeError) as exc:
                    logger.warning('X 备用接口 {} {} 失败：{}', provider, route, type(exc).__name__)
        return []

    @classmethod
    def _fallback_videos(cls, provider: str, data: dict, status_id: str) -> list[str]:
        if not isinstance(data, dict):
            return []
        if provider == 'fxtwitter':
            tweet = data.get('tweet') or {}
            if data.get('code') != 200 or str(tweet.get('id')) != status_id:
                return []
            details = tweet.get('media') or {}
            media = details.get('all') or (details.get('photos') or []) + (details.get('videos') or [])
        else:
            if str(data.get('tweetID')) != status_id:
                return []
            media = data.get('media_extended') or []
        result = []
        for item in media:
            if item.get('type') in ('photo', 'image'):
                url = cls._original_photo(item.get('url'))
                if not url:
                    return []
                if url not in result:
                    result.append(url)
                continue
            if item.get('type') not in ('video', 'gif'):
                continue
            formats = [fmt for fmt in item.get('formats', [])
                       if fmt.get('container') == 'mp4' and cls._video_url(fmt.get('url'))]
            url = max(formats, key=lambda fmt: fmt.get('bitrate') or 0)['url'] if formats else item.get('url')
            if not cls._video_url(url):
                return []
            if url not in result:
                result.append(url)
        text = tweet.get('text') if provider == 'fxtwitter' else data.get('text')
        video_index = 0
        for i, url in enumerate(result):
            if cls._video_url(url):
                video_index += 1
                result[i] = named_video(url, text or '', status_id, video_index)
        return result

    @staticmethod
    def _extract(url: str) -> list[str]:
        options = {
            'quiet': True, 'no_warnings': True, 'skip_download': True,
            'socket_timeout': 20, 'retries': 1, 'extractor_retries': 1,
            'extractor_args': {'twitter': {'api': ['syndication']}},
        }
        if settings.x_proxy:
            options['proxy'] = settings.x_proxy
        with YoutubeDL(options) as downloader:
            info = downloader.extract_info(url, download=False)
        entries = (info or {}).get('entries') if (info or {}).get('_type') in ('playlist', 'multi_video') else [info]
        result = []
        for entry in entries or []:
            formats = [item for item in (entry or {}).get('formats', [])
                       if item.get('ext') == 'mp4' and item.get('protocol') in ('http', 'https')
                       and item.get('vcodec') != 'none' and item.get('url')]
            if not formats:
                raise HTTPException(422, '推文未包含可直接下载的 MP4 视频')
            best = max(formats, key=lambda item: (item.get('height') or 0,
                                                  item.get('width') or 0,
                                                  item.get('tbr') or 0))
            if best['url'] not in result:
                result.append(best['url'])
        if not result:
            raise HTTPException(422, '推文未包含可下载视频')
        status_id = urlsplit(url).path.split('/')[-1]
        text = (info or {}).get('description') or ''
        return [named_video(video, text, status_id, i + 1) for i, video in enumerate(result)]
