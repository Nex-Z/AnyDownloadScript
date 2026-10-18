"""Verify XHS fallback ordering, bounded attempts and complete original images."""
import asyncio
import json
import unittest
from unittest.mock import AsyncMock, patch

from parser.xhs_parse import XhsParser
from util.parse_guard import ParseGuard
from util.upstream import ParseError

URL = 'https://xhslink.cn/o/test'
RESOLVED = 'https://www.xiaohongshu.com/discovery/item/abc?xsec_token=keep-me'


def page(count=2):
    return '<script>' + json.dumps({'noteData': {
        'noteId': 'abc', 'imageList': [{'fileId': f'notes_pre_post/image{i}'} for i in range(count)]
    }}) + '</script>'


class XhsFallbackChecks(unittest.IsolatedAsyncioTestCase):
    async def test_primary_success_does_not_request_fallback(self):
        p = XhsParser()
        with patch.object(p, 'fetch_page', AsyncMock(return_value=(page(), RESOLVED))) as fetch, patch.object(
                p, 'validate_original_images', AsyncMock()) as validate:
            self.assertEqual(len(await p.parse(URL)), 2)
        self.assertEqual(fetch.call_count, 1)
        self.assertFalse(fetch.call_args.kwargs['mobile_compat'])
        validate.assert_awaited_once()

    async def test_challenge_falls_back_once_and_validates_all_originals(self):
        p = XhsParser()
        with patch.object(p, 'fetch_page', AsyncMock(side_effect=[
                ParseError(422, 'challenge', 'VERIFICATION_REQUIRED'), (page(18), RESOLVED)])) as fetch, patch.object(
                p, 'validate_original_images', AsyncMock()) as validate:
            result = await p.parse(URL)
        self.assertEqual(len(result), 18)
        self.assertEqual(fetch.call_count, 2)
        self.assertEqual(fetch.call_args.args[0], URL)
        self.assertTrue(fetch.call_args.kwargs['mobile_compat'])
        validate.assert_awaited_once_with(result)
        self.assertTrue(all(u.startswith('https://ci.xiaohongshu.com/notes_pre_post/') for u in result))

    async def test_network_error_uses_compatibility_request(self):
        p = XhsParser()
        with patch.object(p, 'fetch_page', AsyncMock(side_effect=[
                ParseError(502, 'network', 'UPSTREAM_NETWORK'), (page(), RESOLVED)])) as fetch, patch.object(
                p, 'validate_original_images', AsyncMock()):
            self.assertEqual(len(await p.parse(URL)), 2)
        self.assertEqual(fetch.call_count, 2)

    async def test_all_empty_pages_have_three_attempt_limit(self):
        p = XhsParser()
        with patch.object(p, 'fetch_page', AsyncMock(return_value=('<html></html>', RESOLVED))) as fetch:
            with self.assertRaises(ParseError) as error:
                await p.parse(URL)
        self.assertEqual(fetch.call_count, 3)
        self.assertEqual(error.exception.error_code, 'METADATA_UNAVAILABLE')

    async def test_rate_limit_and_missing_post_do_not_trigger_fallback(self):
        for code, status in [('UPSTREAM_RATE_LIMIT', 429), ('POST_UNAVAILABLE', 422), ('COOKIE_CONFIG_INVALID', 500)]:
            p = XhsParser()
            with patch.object(p, 'fetch_page', AsyncMock(side_effect=ParseError(status, code, code))) as fetch:
                with self.assertRaises(ParseError):
                    await p.parse(URL)
            self.assertEqual(fetch.call_count, 1)

    async def test_original_failure_never_returns_partial_or_preview(self):
        p = XhsParser()
        with patch.object(p, 'fetch_page', AsyncMock(return_value=(page(), RESOLVED))) as fetch, patch.object(
                p, 'validate_original_images', AsyncMock(side_effect=ParseError(502, 'image unavailable', 'IMAGE_HTTP_ERROR', 'image'))):
            with self.assertRaises(ParseError) as error:
                await p.parse(URL)
        self.assertEqual(fetch.call_count, 1)
        self.assertEqual(error.exception.error_code, 'IMAGE_HTTP_ERROR')

    async def test_fallback_still_rejects_wrong_note_and_partial_metadata(self):
        for content in [page().replace('"abc"', '"wrong"'), page().replace('"fileId"', '"url"')]:
            p = XhsParser()
            with patch.object(p, 'fetch_page', AsyncMock(side_effect=[
                    ParseError(422, 'challenge', 'VERIFICATION_REQUIRED'), (content, RESOLVED)])), patch.object(
                    p, 'validate_original_images', AsyncMock()) as validate:
                with self.assertRaises(ParseError):
                    await p.parse(URL)
            validate.assert_not_awaited()

    async def test_cooldown_only_after_both_profiles_fail(self):
        p = XhsParser()
        guard = ParseGuard()
        with patch.object(p, 'fetch_page', AsyncMock(side_effect=ParseError(
                422, 'challenge', 'VERIFICATION_REQUIRED', retry_after=60))) as fetch:
            for _ in range(2):
                with self.assertRaises(ParseError):
                    await guard.run(p, URL)
        self.assertEqual(fetch.call_count, 2)

    async def test_successful_fallback_does_not_poison_next_request(self):
        p = XhsParser()
        guard = ParseGuard()
        with patch.object(p, 'fetch_page', AsyncMock(side_effect=[
                ParseError(422, 'challenge', 'VERIFICATION_REQUIRED'), (page(), RESOLVED),
                (page(), RESOLVED)])) as fetch, patch.object(p, 'validate_original_images', AsyncMock()):
            self.assertEqual(len(await guard.run(p, URL)), 2)
            self.assertEqual(len(await guard.run(p, URL)), 2)
        self.assertFalse(guard.cooldowns)
        self.assertEqual(fetch.call_count, 3)


if __name__ == '__main__':
    unittest.main()
