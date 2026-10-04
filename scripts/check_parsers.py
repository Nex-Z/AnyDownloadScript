"""Regression checks; pipe into the Python 3.12 runtime image."""
import asyncio
import json
import unittest
from unittest.mock import AsyncMock, patch

from aiohttp import web
from fastapi import HTTPException
from lxml import etree
from parser.parser_factory import ParserFactory
from parser.w51_parse import W51Parser
from parser.xhs_parse import XhsParser


class ParserChecks(unittest.TestCase):
    def test_xhs_hosts_and_factory(self):
        parser = XhsParser()
        with patch.object(ParserFactory, "_parsers", [parser, W51Parser()]):
            for host in ("xhslink.cn", "xhslink.com", "www.xiaohongshu.com"):
                self.assertIs(asyncio.run(ParserFactory.get_parser(f"https://{host}/o/test")), parser)
            for url in ("https://xhslink.cn.evil.test/o/a", "https://other.test/xhslink.cn"):
                self.assertFalse(asyncio.run(parser.is_me(url)))

    def test_mobile_note_preserves_image_order(self):
        note = {"noteId": "abc", "type": "normal", "imageList": [
            {"fileId": "notes_pre_post/two", "url": "https://cdn.test/preview2.jpg"},
            {"fileId": "notes_pre_post/one", "urlDefault": "https://cdn.test/preview1.jpg"},
            {"fileId": "notes_pre_post/two"}]}
        content = '<script>window.__INITIAL_STATE__={"unused":undefined,"noteData":' + json.dumps(note) + '};</script>'
        content += '<img src="https://cdn.test/recommendation.jpg">'
        self.assertEqual(XhsParser.extract_resources(content), [
            "https://ci.xiaohongshu.com/notes_pre_post/two",
            "https://ci.xiaohongshu.com/notes_pre_post/one"])

    def test_missing_original_id_fails_whole_note(self):
        for invalid in (None, "", "../preview", "id!h5_1080jpg", "id?resize=1080"):
            note = {"noteId": "abc", "type": "normal", "imageList": [
                {"fileId": "valid"}, {"fileId": invalid, "url": "https://cdn.test/preview.jpg"}]}
            with self.assertRaises(HTTPException) as error:
                XhsParser.extract_resources('<script>' + json.dumps({"noteData": note}) + '</script>')
            self.assertEqual(error.exception.status_code, 422)

    def test_video_returns_list(self):
        note = {"noteId": "abc", "type": "video", "video": {"media": {"stream": {
            "h264": [{"masterUrl": "https://cdn.test/video.mp4"}]}}},
            "imageList": [{"url": "https://cdn.test/cover.jpg"}]}
        self.assertEqual(XhsParser.extract_resources('<script>' + json.dumps({"noteData": note}) + '</script>'),
                         ["https://cdn.test/video.mp4"])

    def test_login_icon_is_not_resource(self):
        content = '<title>Login</title><meta property="og:image" content="https://cdn.test/icon.png">'
        self.assertEqual(XhsParser.extract_resources(content), [])

    def test_legacy_open_graph_images_are_not_fallback(self):
        self.assertEqual(XhsParser.extract_resources(
            '<meta property="og:title" content="Note"><meta name="og:image" content="https://cdn.test/a.jpg">'),
            [])

    def test_origin_validation_rejects_missing_redirect_and_html(self):
        async def run():
            requests = []
            async def handle(request):
                requests.append((request.path, request.headers.get("Range")))
                if request.path == "/missing":
                    return web.Response(status=404)
                if request.path == "/redirect":
                    return web.Response(status=302, headers={"Location": "/valid"})
                if request.path == "/html":
                    return web.Response(body=b"<html>access denied</html>", content_type="image/jpeg")
                return web.Response(status=206, body=b"\xff\xd8\xff" + b"\0" * 29,
                                    content_type="application/octet-stream")
            app = web.Application()
            app.router.add_get("/{name}", handle)
            runner = web.AppRunner(app)
            await runner.setup()
            site = web.TCPSite(runner, "127.0.0.1", 0)
            await site.start()
            origin = f"http://127.0.0.1:{runner.addresses[0][1]}"
            try:
                parser = XhsParser()
                await parser.validate_original_images([origin + "/valid"])
                for path in ("missing", "redirect", "html"):
                    with self.assertRaises(HTTPException) as error:
                        await parser.validate_original_images([origin + "/valid", origin + "/" + path])
                    self.assertEqual(error.exception.status_code, 502)
                self.assertTrue(all(header == "bytes=0-31" for _, header in requests))
                self.assertEqual(sum(path == "/valid" for path, _ in requests), 4)
            finally:
                await runner.cleanup()
        asyncio.run(run())

    def test_parse_propagates_original_failure(self):
        parser = XhsParser()
        note = {"noteId": "abc", "imageList": [{"fileId": "a"}, {"fileId": "b"}]}
        content = '<script>' + json.dumps({"noteData": note}) + '</script>'
        with patch.object(parser, "fetch_page", AsyncMock(return_value=content)), patch.object(
            parser, "validate_original_images", AsyncMock(side_effect=HTTPException(502, "original unavailable"))
        ):
            with self.assertRaises(HTTPException) as error:
                asyncio.run(parser.parse("https://xhslink.cn/o/test"))
        self.assertEqual(error.exception.status_code, 502)

    def test_empty_note_has_actionable_error(self):
        parser = XhsParser()
        with patch.object(parser, "fetch_page", AsyncMock(return_value="<html></html>")):
            with self.assertRaises(HTTPException) as error:
                asyncio.run(parser.parse("https://xhslink.cn/o/test"))
        self.assertEqual(error.exception.status_code, 422)

    def test_generic_page_missing_title_does_not_crash(self):
        parser = W51Parser()
        with patch.object(parser, "get_detail_html", return_value=etree.HTML("<html>Login</html>")):
            with self.assertRaises(HTTPException) as error:
                asyncio.run(parser.parse("https://example.test"))
        self.assertEqual(error.exception.status_code, 422)

    def test_invalid_player_config_is_ignored(self):
        html = etree.HTML("<div class='dplayer' data-config='{broken'></div>")
        self.assertEqual(W51Parser().parse_video(html), [])


unittest.main()
