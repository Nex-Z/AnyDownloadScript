"""Reliability regression tests using local HTTP and deterministic upstream fixtures."""
import asyncio
import json
import unittest
from unittest.mock import AsyncMock, patch

import aiohttp
from aiohttp import web
from pydantic import SecretStr
from yarl import URL

from config.config import settings
from parser.xhs_parse import XhsParser
from util.parse_guard import ParseGuard
from util.upstream import ParseError, read_url, safe_url


class ReliabilityChecks(unittest.IsolatedAsyncioTestCase):
    async def test_http_retry_bounds_and_no_retry_on_denials(self):
        counts = {}
        async def handle(request):
            name = request.match_info['name']
            counts[name] = counts.get(name, 0) + 1
            status = int(name) if name.isdigit() else (503 if counts[name] == 1 else 200)
            return web.Response(status=status, body=b'x' * 40)
        app = web.Application()
        app.router.add_get('/{name}', handle)
        runner = web.AppRunner(app)
        await runner.setup()
        await web.TCPSite(runner, '127.0.0.1', 0).start()
        origin = f'http://127.0.0.1:{runner.addresses[0][1]}'
        try:
            async with aiohttp.ClientSession() as session:
                for name, expected, calls in [('recover', 200, 2), ('503', 503, 2), ('403', 403, 1), ('429', 429, 1)]:
                    result = await read_url(session, origin + '/' + name)
                    self.assertEqual(result[0], expected)
                    self.assertEqual(counts[name], calls)
                with self.assertRaises(ParseError) as error:
                    await read_url(session, origin + '/200', limit=20)
                self.assertEqual(error.exception.error_code, 'UPSTREAM_TOO_LARGE')
                self.assertEqual(len((await read_url(session, origin + '/200', prefix=True, limit=8))[2]), 8)
        finally:
            await runner.cleanup()

    async def test_challenge_redirect_stops_and_cookie_scope(self):
        async def fetch(session, url, **kwargs):
            jar = session.cookie_jar
            self.assertFalse(jar.filter_cookies(URL('https://xhslink.cn/o/test')))
            self.assertFalse(jar.filter_cookies(URL('https://evil.test/')))
            self.assertFalse(jar.filter_cookies(URL('http://www.xiaohongshu.com/')))
            self.assertEqual(jar.filter_cookies(URL('https://www.xiaohongshu.com/'))['web_session'].value, 'test-secret')
            return 302, {'Location': 'https://www.xiaohongshu.com/website-login/captcha?secret=x'}, b'', url
        with patch.object(settings, 'xhs_cookie', SecretStr('web_session=test-secret')), patch(
                'parser.xhs_parse.read_url', side_effect=fetch) as call:
            with self.assertRaises(ParseError) as error:
                await XhsParser().fetch_page('https://xhslink.cn/o/test')
        self.assertEqual(call.call_count, 1)
        self.assertEqual(error.exception.error_code, 'VERIFICATION_REQUIRED')

    async def test_external_redirect_is_rejected_before_fetch(self):
        with patch('parser.xhs_parse.read_url', AsyncMock(return_value=(
                302, {'Location': 'https://evil.test/'}, b'', 'https://xhslink.cn/o/test'))) as call:
            with self.assertRaises(ParseError) as error:
                await XhsParser().fetch_page('https://xhslink.cn/o/test')
        self.assertEqual(call.call_count, 1)
        self.assertEqual(error.exception.error_code, 'UNSUPPORTED_REDIRECT')

    async def test_xhs_status_classification(self):
        for status, code in [(429, 'UPSTREAM_RATE_LIMIT'), (403, 'UPSTREAM_ACCESS_DENIED'), (404, 'POST_UNAVAILABLE')]:
            with patch('parser.xhs_parse.read_url', AsyncMock(return_value=(status, {}, b'', ''))):
                with self.assertRaises(ParseError) as error:
                    await XhsParser().fetch_page('https://xhslink.cn/o/test')
            self.assertEqual(error.exception.error_code, code)

    async def test_desktop_fallback_only_after_normal_empty_page(self):
        note = {'noteId': 'abc', 'imageList': [{'fileId': 'original'}]}
        content = '<script>' + json.dumps({'noteDetailMap': {'abc': {'note': note}}}) + '</script>'
        parser = XhsParser()
        with patch.object(parser, 'fetch_page', AsyncMock(side_effect=[
                ('<html></html>', 'https://www.xiaohongshu.com/explore/abc'),
                (content, 'https://www.xiaohongshu.com/explore/abc')])) as fetch, patch.object(
                parser, 'validate_original_images', AsyncMock()):
            self.assertEqual(await parser.parse('https://xhslink.cn/o/test'), ['https://ci.xiaohongshu.com/original'])
            self.assertTrue(fetch.call_args.kwargs['desktop'])

    async def test_requested_note_identity_ignores_recommendations(self):
        content = '<script>' + json.dumps({'recommendation': {'note': {
            'noteId': 'wrong', 'imageList': [{'fileId': 'wrong'}]}}, 'noteData': {
            'data': {'noteData': {'noteId': 'correct', 'imageList': [{'fileId': 'correct'}]}}}}) + '</script>'
        self.assertEqual(XhsParser.extract_resources(content, 'correct'), ['https://ci.xiaohongshu.com/correct'])
        self.assertEqual(XhsParser.extract_resources(content, 'missing'), [])

    async def test_coalesce_and_cooldown(self):
        guard = ParseGuard()
        parser = XhsParser()
        started, release = asyncio.Event(), asyncio.Event()
        async def parse(url):
            started.set()
            await release.wait()
            raise ParseError(422, 'challenge', 'VERIFICATION_REQUIRED', retry_after=60)
        with patch.object(parser, 'parse', side_effect=parse) as call:
            first = asyncio.create_task(guard.run(parser, 'same'))
            await started.wait()
            second = asyncio.create_task(guard.run(parser, 'same'))
            await asyncio.sleep(0)
            release.set()
            results = await asyncio.gather(first, second, return_exceptions=True)
            self.assertTrue(all(isinstance(r, ParseError) for r in results))
            with self.assertRaises(ParseError) as error:
                await guard.run(parser, 'same')
            self.assertGreater(error.exception.retry_after, 0)
            self.assertEqual(call.call_count, 1)
            self.assertFalse(guard.pending)

    async def test_disconnected_waiter_does_not_cancel_shared_parse(self):
        guard = ParseGuard()
        parser = XhsParser()
        started, release = asyncio.Event(), asyncio.Event()
        async def parse(url):
            started.set()
            await release.wait()
            return ['image']
        with patch.object(parser, 'parse', side_effect=parse) as call:
            first = asyncio.create_task(guard.run(parser, 'same'))
            await started.wait()
            second = asyncio.create_task(guard.run(parser, 'same'))
            await asyncio.sleep(0)
            first.cancel()
            await asyncio.gather(first, return_exceptions=True)
            release.set()
            self.assertEqual(await second, ['image'])
            self.assertEqual(call.call_count, 1)

    async def test_safe_logging(self):
        self.assertEqual(safe_url('https://user:password@www.xiaohongshu.com/explore/id?xsec_token=secret#secret'),
                         'https://www.xiaohongshu.com/explore/id')


if __name__ == '__main__':
    unittest.main()
