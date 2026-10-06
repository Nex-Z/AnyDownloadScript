"""Douyin regression checks: structured identity, signed URLs and real HTTP media probes."""
import asyncio
import json
import unittest
from urllib.parse import quote
from unittest.mock import AsyncMock, patch

import aiohttp
from aiohttp import web
from fastapi import HTTPException
from parser.dy_parse import DyParser

ID = '7693103726027205561'

def page(post, container='_ROUTER_DATA'):
    state = {'loaderData': {'video_(id)/page': {'videoInfoRes': {'item_list': [post]}}}}
    encoded = json.dumps(state)
    if container == 'RENDER_DATA':
        return '<script id="RENDER_DATA">' + quote(encoded) + '</script>'
    return '<script>window.' + container + '=' + encoded + ';</script>'

class DouyinChecks(unittest.TestCase):
    def test_hosts(self):
        p = DyParser()
        for url in ['https://v.douyin.com/abc/', 'https://www.douyin.com/note/'+ID, 'https://www.iesdouyin.com/share/video/'+ID+'/']:
            self.assertTrue(asyncio.run(p.is_me(url)))
        for url in ['https://douyin.com.evil.test/video/1', 'https://evil.test/douyin.com', 'file://douyin.com/video/1']:
            self.assertFalse(asyncio.run(p.is_me(url)))

    def test_gallery_signed_webp_and_order(self):
        urls = ['https://p3-pc-sign.douyinpic.com/tos-cn-i/test~tplv-shrink.webp?water=0&x-sign=a%2Fb',
                'https://p9-sign.douyinpic.com/tos-cn-i/other~resize.jpg?x-sign=c']
        post = {'aweme_id':ID, 'images':[{'url_list':[u]} for u in urls],
                'video':{'cover':{'url_list':['https://cdn.test/cover.jpg']}}}
        for container in ['_ROUTER_DATA','__INITIAL_STATE__','RENDER_DATA']:
            self.assertEqual(DyParser.extract_resources(page(post,container),ID),urls)
        self.assertEqual(DyParser.extract_resources(page(post).replace('/',r'\u002F'),ID),urls)

    def test_only_requested_post(self):
        wrong = {'aweme_id':'1','images':[{'url_list':['https://cdn.test/recommendation.jpg']}]}
        self.assertEqual(DyParser.extract_resources(page(wrong),ID),[])
        self.assertEqual(DyParser.extract_resources('<img src="https://cdn.test/avatar.jpg">',ID),[])

    def test_partial_gallery_rejected(self):
        post={'aweme_id':ID,'images':[{'url_list':['https://cdn.test/a.jpg']},{}]}
        with self.assertRaises(HTTPException) as error:
            DyParser.extract_resources(page(post),ID)
        self.assertEqual(error.exception.status_code,422)

    def test_nested_display_image(self):
        post={'aweme_id':ID,'image_post_info':{'images':[{'display_image':{'url_list':['https://cdn.test/a.webp']}}]}}
        self.assertEqual(DyParser.extract_resources(page(post),ID),['https://cdn.test/a.webp'])

    def test_video_list_highest_resolution_no_download_addr(self):
        low={'url_list':['https://cdn.test/low.mp4'],'width':720,'height':1280}
        high={'url_list':['https://cdn.test/high.mp4'],'width':1080,'height':1920}
        post={'aweme_id':ID,'images':[], 'video':{'play_addr':low,
            'download_addr':{'url_list':['https://cdn.test/watermarked.mp4']},
            'bit_rate':[{'bit_rate':2000,'play_addr':high}]}}
        self.assertEqual(DyParser.extract_resources(page(post),ID),['https://cdn.test/high.mp4'])

    def test_watermark_only_fails_without_rewriting(self):
        for addr in [{'url_list':['https://www.douyin.com/aweme/v1/playwm/?video_id=a']},
                     {'has_watermark':True,'url_list':['https://cdn.test/a.mp4']}]:
            with self.assertRaises(HTTPException):
                DyParser.extract_resources(page({'aweme_id':ID,'video':{'play_addr':addr}}),ID)

    def test_malformed_state_not_executed(self):
        self.assertEqual(DyParser.extract_resources('<script>window._ROUTER_DATA={broken};</script>',ID),[])

    def test_live_http_probes_backup_and_complete_failure(self):
        async def run():
            seen=[]
            async def handle(request):
                seen.append((request.path,request.headers.get('Range')))
                if request.path=='/missing': return web.Response(status=403)
                if request.path=='/html': return web.Response(body=b'<html>login</html>',content_type='image/jpeg')
                if request.path=='/video': return web.Response(body=b'\0\0\0\x18ftypisom'+b'\0'*100)
                if request.path=='/webp': return web.Response(body=b'RIFF'+b'\0'*4+b'WEBP'+b'\0'*100)
                return web.Response(body=b'\xff\xd8\xff'+b'\0'*100)
            app=web.Application();app.router.add_get('/{name}',handle)
            runner=web.AppRunner(app);await runner.setup()
            await web.TCPSite(runner,'127.0.0.1',0).start()
            origin=f'http://127.0.0.1:{runner.addresses[0][1]}'
            try:
                async with aiohttp.ClientSession() as session:
                    p=DyParser()
                    self.assertEqual(await p.validate_media(session,'image',[[origin+'/missing',origin+'/webp'],[origin+'/jpg']]),[origin+'/webp',origin+'/jpg'])
                    self.assertEqual(await p.validate_media(session,'video',[[origin+'/video']]),[origin+'/video'])
                    for kind in ['image','video']:
                        with self.assertRaises(HTTPException) as error:
                            await p.validate_media(session,kind,[[origin+'/jpg'],[origin+'/html']])
                        self.assertEqual(error.exception.status_code,502)
                    self.assertTrue(all(h=='bytes=0-63' for _,h in seen))
            finally:
                await runner.cleanup()
        asyncio.run(run())

    def test_parse_empty_is_error_not_success(self):
        p=DyParser()
        with patch.object(p,'fetch_page',AsyncMock(return_value=('<html></html>','https://www.iesdouyin.com/share/video/'+ID+'/'))):
            with self.assertRaises(HTTPException) as error:
                asyncio.run(p.parse('https://v.douyin.com/test/'))
        self.assertEqual(error.exception.status_code,422)

    def test_fetch_upstream_failure_redirect_and_fragmented_response(self):
        async def run():
            body=page({'aweme_id':ID,'images':[{'url_list':['https://cdn.test/a.jpg']}]}).encode()
            async def handle(request):
                if request.path=='/bad':return web.Response(status=403)
                if request.path=='/redirect':return web.Response(status=302,headers={'Location':'https://evil.test/'})
                response=web.StreamResponse();await response.prepare(request)
                await response.write(body[:30]);await asyncio.sleep(.01);await response.write(body[30:]);await response.write_eof();return response
            app=web.Application();app.router.add_get('/{name}',handle)
            runner=web.AppRunner(app);await runner.setup();await web.TCPSite(runner,'127.0.0.1',0).start()
            origin=f'http://127.0.0.1:{runner.addresses[0][1]}'
            p=DyParser()
            try:
                with patch.object(p,'allowed_url',side_effect=lambda u:u.startswith(origin+'/')):
                    async with aiohttp.ClientSession() as session:
                        text,_=await p.fetch_page(session,origin+'/ok')
                        self.assertEqual(text.encode(),body)
                        for path,code in [('bad',502),('redirect',422)]:
                            with self.assertRaises(HTTPException) as error:await p.fetch_page(session,origin+'/'+path)
                            self.assertEqual(error.exception.status_code,code)
            finally:await runner.cleanup()
        asyncio.run(run())

if __name__=='__main__':unittest.main()
