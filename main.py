# main.py
import os
import time
import uuid
from contextlib import asynccontextmanager
from typing import Optional, Any

from fastapi import FastAPI, HTTPException, APIRouter
from fastapi.middleware.cors import CORSMiddleware
from loguru import logger
from pydantic import BaseModel
from starlette.responses import FileResponse, JSONResponse
from starlette.responses import StreamingResponse
import requests
from urllib.parse import quote
from fastapi import Request
from util.x_media import get_media

from config.config import settings
from parser.dy_parse import DyParser
from parser.parser_factory import ParserFactory
from parser.w51_parse import W51Parser
from parser.wb_parse import WbParser
from parser.xhs_parse import XhsParser
from parser.x_parse import XParser
from util import ip_util, cache_util
from util.upstream import request_id, safe_url
from util.parse_guard import parse_guard

logger.add(
    "logs/log_{time}.log",  # 文件名可以带时间占位
    rotation = "10 MB",  # 文件超过 10 MB 自动切割
    retention = "10 days",  # 只保留 10 天
    compression = "zip",  # 旧文件压缩
    encoding = "utf-8",
    format = "{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {message}"
)

# 注册解析器
ParserFactory.register(XhsParser())
ParserFactory.register(DyParser())
ParserFactory.register(WbParser())
ParserFactory.register(XParser())
ParserFactory.register(W51Parser())


class ResponseModel(BaseModel):
    code: int = 200
    msg: str = "success"
    platform: str | None = None
    data: Any | None = None

    model_config = {
        "populate_by_name": True,
        "arbitrary_types_allowed": True,
    }

    @classmethod
    def error(cls, msg: str, code: int = 400):
        return cls(code = code, msg = msg, data = None)


# Lifespan 生命周期
@asynccontextmanager
async def lifespan(app: FastAPI):
    # 启动事件
    local_ip = ip_util.get_local_ip()
    public_ip = ip_util.get_public_ip()
    logger.info(f"本机局域网 IP: {settings.network_url}")
    logger.info(f"公网 IP: {public_ip}:{settings.server_port}")
    yield
    # 关闭事件（可选）
    logger.info("服务即将关闭...")


# =========================
# FastAPI 实例
# =========================
app = FastAPI(lifespan = lifespan)
api_router = APIRouter(prefix = "/api/v1")


@app.middleware("http")
async def request_context(request, call_next):
    identity = uuid.uuid4().hex[:12]
    token = request_id.set(identity)
    request.state.request_id = identity
    started = time.monotonic()
    try:
        response = await call_next(request)
        response.headers["X-Request-ID"] = identity
        if request.url.path == "/api/v1/download":
            logger.info("request_id={} event=download status={} elapsed_ms={}",
                        identity, response.status_code, round((time.monotonic() - started) * 1000))
        return response
    finally:
        request_id.reset(token)

# =========================
# 中间件
# =========================
app.add_middleware(
    CORSMiddleware,
    allow_origins = ["*"],
    allow_credentials = True,
    allow_methods = ["*"],
    allow_headers = ["*"],
)


# 模拟解析函数
async def parse_input_string(input_str: str) -> Optional[str]:
    """
    根据你的项目需求解析输入字符串
    - 如果是 URL，直接返回
    - 如果是文本，需要提取资源 URL
    """
    import re
    if input_str.startswith("http"):
        return input_str

    # 使用正则表达式提取URL
    url_pattern = r'http[s]?://(?:[a-zA-Z]|[0-9]|[$-_@.&+]|[!*\\(\\),]|(?:%[0-9a-fA-F][0-9a-fA-F]))+'
    urls = re.findall(url_pattern, input_str)

    if urls:
        return urls[0]  # 返回找到的第一个URL

    return None


# =========================
# 路由
# =========================
class DownloadRequest(BaseModel):
    input_path: str


# @api_router.post("/download", response_model = ResponseModel)
@api_router.post("/download")
async def download_endpoint(req: DownloadRequest):
    """
    接收字符串，解析资源，返回文件下载
    """
    url = await parse_input_string(req.input_path)
    logger.info("request_id={} event=parse url={}", request_id.get(), safe_url(url))
    if not url:
        return ResponseModel.error("无法解析输入内容")

    parser = await ParserFactory.get_parser(url)
    if not parser:
        return ResponseModel.error("不支持的平台", 500)

    platform = await parser.get_platform()  # ⚠️ 注意加 await
    if isinstance(parser, (XhsParser, DyParser)):
        urls = await parse_guard.run(parser, url)
    else:
        urls = await parser.parse(url)

    logger.info("request_id={} event=parsed platform={} count={}", request_id.get(), platform.value, len(urls))
    return ResponseModel(platform = platform.value, data = urls)


@api_router.get("/video/{token}")
def get_video(token: str):
    path = cache_util.get_path(token)
    if not path or not os.path.exists(path):
        raise HTTPException(404, "Video expired or missing")
    return FileResponse(path, media_type = "video/mp4")


@api_router.get('/x-media/{token}/{filename}')
def download_x_media(token: str, filename: str, request: Request):
    media = get_media(token)
    if not media or filename != media['filename']:
        raise HTTPException(404, '下载链接已过期，请重新解析推文')
    headers = {'User-Agent': 'AnyDownloadScript'}
    if request.headers.get('range'):
        headers['Range'] = request.headers['range']
    session = requests.Session()
    session.trust_env = False
    try:
        try:
            upstream = session.get(media['url'], headers=headers, stream=True, timeout=(4, 30))
            if upstream.status_code not in (200, 206) and settings.x_proxy:
                upstream.close()
                upstream = session.get(media['url'], headers=headers, stream=True, timeout=(8, 30),
                                       proxies={'https': settings.x_proxy})
        except requests.RequestException:
            if not settings.x_proxy:
                raise
            upstream = session.get(media['url'], headers=headers, stream=True, timeout=(8, 30),
                                   proxies={'https': settings.x_proxy})
        if upstream.status_code not in (200, 206):
            upstream.close()
            session.close()
            raise HTTPException(502, '视频 CDN 暂时不可用，请重新解析或稍后重试')
    except requests.RequestException as exc:
        session.close()
        raise HTTPException(502, '连接视频 CDN 失败，请稍后重试') from exc
    def chunks():
        try:
            yield from upstream.iter_content(256 * 1024)
        finally:
            upstream.close()
            session.close()
    output_headers = {'Content-Disposition': f"attachment; filename*=UTF-8''{quote(filename, safe='')}"}
    for key in ('Content-Length', 'Content-Range', 'Accept-Ranges'):
        if upstream.headers.get(key):
            output_headers[key] = upstream.headers[key]
    return StreamingResponse(chunks(), status_code=upstream.status_code,
                             media_type='video/mp4', headers=output_headers)


# =========================
# 健康检查
# =========================
@api_router.get("/health")
def health():
    try:
        cache_util.r.ping()
    except Exception:
        raise HTTPException(503, "Redis unavailable")
    return {"status": "ok", "redis": "ok"}


app.include_router(api_router)


# =========================
# 异常处理示例
# =========================
@app.exception_handler(HTTPException)
async def http_exception_handler(request, exc):
    body = ResponseModel.error(exc.detail, exc.status_code).model_dump()
    body["request_id"] = getattr(request.state, "request_id", "-")
    body["error_code"] = getattr(exc, "error_code", "PARSE_ERROR")
    body["stage"] = getattr(exc, "stage", "parse")
    body["retryable"] = getattr(exc, "retryable", False)
    body["retry_after"] = getattr(exc, "retry_after", None)
    logger.warning("request_id={} event=failed status={} code={} stage={} detail={}",
                   body["request_id"], exc.status_code, body["error_code"], body["stage"], exc.detail)
    return JSONResponse(
        status_code=exc.status_code,
        content=body,
        headers=exc.headers,
    )


# =========================
# 启动
# =========================
if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "main:app",
        host = "0.0.0.0",
        port = settings.server_port,
        workers = 20,
        reload = True  # 开发模式自动重载
    )

# http://xhslink.com/o/u0vljpIPsM
