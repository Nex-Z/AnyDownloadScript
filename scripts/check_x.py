"""Run with uv run python -m scripts.check_x."""
import asyncio
import unittest
from unittest.mock import patch

from fastapi import HTTPException
from yt_dlp.utils import DownloadError

from parser.x_parse import XParser
from parser.parser_factory import ParserFactory
from parser.w51_parse import W51Parser


class XChecks(unittest.TestCase):
    def setUp(self):
        naming = patch('parser.x_parse.named_video', side_effect=lambda url, *args: url)
        self.naming = naming.start()
        self.addCleanup(naming.stop)

    def test_filename_cleaning(self):
        from util.x_media import video_filename
        name = video_filename('你好：世界 / "测试"?\n https://t.co/abc', '123', 2)
        self.assertTrue(name.endswith('_123_2.mp4'))
        self.assertNotRegex(name, r'[<>:"/\\|?*\n]')
        self.assertNotIn('https', name)
        self.assertLess(len(video_filename('好'*500, '123', 1).encode('utf-8')), 200)
        self.assertEqual(video_filename('', '123', 1), '视频_123_1.mp4')

    def test_routing_and_hosts(self):
        parser = XParser()
        with patch.object(ParserFactory, '_parsers', [parser, W51Parser()]):
            for host in ('x.com', 'www.x.com', 'mobile.twitter.com'):
                self.assertIs(asyncio.run(ParserFactory.get_parser(f'https://{host}/user/status/123')), parser)
        for url in ('https://x.com.evil.test/u/status/123', 'https://other.test/x.com', 'file://x.com/u/status/123'):
            self.assertFalse(asyncio.run(parser.is_me(url)))

    def test_quality_multiple_videos_and_deduplication(self):
        formats = [
            {'ext': 'mp4', 'protocol': 'https', 'height': 360, 'url': 'low'},
            {'ext': 'mp4', 'protocol': 'https', 'height': 1080, 'url': 'high'},
            {'ext': 'mp4', 'protocol': 'm3u8_native', 'height': 2160, 'url': 'hls'},
        ]
        with patch('parser.x_parse.YoutubeDL') as ydl, patch.object(XParser, '_fallback', return_value=[]):
            ydl.return_value.__enter__.return_value.extract_info.return_value = {
                '_type': 'playlist', 'entries': [{'formats': formats}, {'formats': formats},
                    {'formats': [{'ext': 'mp4', 'protocol': 'https', 'url': 'second'}]}]}
            self.assertEqual(asyncio.run(XParser().parse('https://twitter.com/u/status/123/video/1?s=20')), ['high', 'second'])
            ydl.return_value.__enter__.return_value.extract_info.assert_called_once_with(
                'https://x.com/i/web/status/123', download=False)

    def test_invalid_and_empty(self):
        with self.assertRaises(HTTPException) as error:
            asyncio.run(XParser().parse('https://x.com/user'))
        self.assertEqual(error.exception.status_code, 422)
        with patch('parser.x_parse.YoutubeDL') as ydl, patch.object(XParser, '_fallback', return_value=[]):
            extractor = ydl.return_value.__enter__.return_value.extract_info
            extractor.return_value = {'formats': []}
            with self.assertRaises(HTTPException) as error:
                asyncio.run(XParser().parse('https://x.com/u/status/123'))
            self.assertEqual(error.exception.status_code, 502)
            extractor.side_effect = DownloadError('unavailable')
            with self.assertRaises(HTTPException) as error:
                asyncio.run(XParser().parse('https://x.com/u/status/123'))
            self.assertEqual(error.exception.status_code, 502)

    def test_fallback_after_primary_error(self):
        with patch.object(XParser, '_extract', side_effect=DownloadError('no video')), patch.object(
                XParser, '_fallback', return_value=['https://video.twimg.com/a.mp4']) as fallback:
            self.assertEqual(asyncio.run(XParser().parse('https://x.com/u/status/123')),
                             ['https://video.twimg.com/a.mp4'])
            fallback.assert_called_once_with('123')

    def test_backup_quality_and_identity(self):
        data = {'code': 200, 'tweet': {'id': '123', 'media': {'videos': [
            {'type': 'video', 'formats': [
                {'container': 'mp4', 'bitrate': 100, 'url': 'https://video.twimg.com/low.mp4'},
                {'container': 'mp4', 'bitrate': 200, 'url': 'https://video.twimg.com/high.mp4'}]}]}}}
        self.assertEqual(XParser._fallback_videos('fxtwitter', data, '123'), ['https://video.twimg.com/high.mp4'])
        self.assertEqual(XParser._fallback_videos('fxtwitter', data, '999'), [])
        self.assertEqual(XParser._fallback_videos('vxtwitter', {'tweetID': '123', 'media_extended': [
            {'type': 'image', 'url': 'https://pbs.twimg.com/a.jpg'},
            {'type': 'video', 'url': 'https://evil.test/a.mp4'}]}, '123'), [])

    def test_second_provider_when_first_unavailable(self):
        with patch('parser.x_parse.requests.Session') as session:
            get = session.return_value.__enter__.return_value.get
            get.side_effect = [__import__('requests').Timeout(), __import__('requests').Timeout(), unittest.mock.Mock(
                json=lambda: {'tweetID': '123', 'media_extended': [
                    {'type': 'video', 'url': 'https://video.twimg.com/a.mp4'}]})]
            self.assertEqual(XParser._fallback('123'), ['https://video.twimg.com/a.mp4'])
            self.assertEqual(get.call_count, 3)

    def test_direct_then_proxy_retry(self):
        with patch('parser.x_parse.requests.Session') as session, patch('parser.x_parse.settings.x_proxy', 'http://proxy.test:7890'):
            client = session.return_value.__enter__.return_value
            client.get.side_effect = [__import__('requests').Timeout(), unittest.mock.Mock(
                json=lambda: {'tweetID': '123', 'media_extended': []}), unittest.mock.Mock(
                json=lambda: {'tweetID': '123', 'media_extended': [
                    {'type': 'video', 'url': 'https://video.twimg.com/a.mp4'}]})]
            self.assertEqual(XParser._fallback('123'), ['https://video.twimg.com/a.mp4'])
            self.assertFalse(client.trust_env)
            self.assertEqual(client.get.call_args_list[0].kwargs['proxies'], {})
            self.assertEqual(client.get.call_args_list[1].kwargs['proxies'], {'https': 'http://proxy.test:7890'})

    def test_original_photos_and_mixed_order(self):
        media = [
            {'type': 'photo', 'url': 'https://pbs.twimg.com/media/one.jpg?name=small'},
            {'type': 'video', 'url': 'https://video.twimg.com/two.mp4'},
            {'type': 'photo', 'url': 'https://pbs.twimg.com/media/three?format=png&name=large'},
            {'type': 'photo', 'url': 'https://pbs.twimg.com/media/one.jpg:large'}]
        expected = ['https://pbs.twimg.com/media/one?format=jpg&name=orig',
                    'https://video.twimg.com/two.mp4',
                    'https://pbs.twimg.com/media/three?format=png&name=orig']
        data = {'code': 200, 'tweet': {'id': '123', 'media': {'all': media}}}
        self.assertEqual(XParser._fallback_videos('fxtwitter', data, '123'), expected)
        media[0]['type'] = media[2]['type'] = media[3]['type'] = 'image'
        self.assertEqual(XParser._fallback_videos('vxtwitter', {'tweetID': '123', 'media_extended': media}, '123'), expected)
        for url in ('https://pbs.twimg.com/profile_images/avatar.jpg',
                    'https://pbs.twimg.com/ext_tw_video_thumb/cover.jpg',
                    'https://pbs.twimg.com.evil.test/media/a.jpg'):
            self.assertIsNone(XParser._original_photo(url))

    def test_photo_only_skips_video_extractor(self):
        photo = 'https://pbs.twimg.com/media/one?format=jpg&name=orig'
        with patch.object(XParser, '_fallback', return_value=[photo]), patch.object(XParser, '_extract') as extract:
            self.assertEqual(asyncio.run(XParser().parse('https://x.com/u/status/123/photo/1')), [photo])
            extract.assert_not_called()


if __name__ == '__main__':
    unittest.main()
