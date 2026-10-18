"""Bounded upstream reads, transient retries and safe diagnostics."""
import asyncio
from contextvars import ContextVar
from urllib.parse import urlsplit

import aiohttp
from fastapi import HTTPException
from loguru import logger

request_id = ContextVar("request_id", default="-")


class ParseError(HTTPException):
    def __init__(self, status, message, code, stage="page", retryable=False, retry_after=None):
        super().__init__(status, message, headers=(
            {"Retry-After": str(retry_after)} if retry_after is not None else None))
        self.error_code = code
        self.stage = stage
        self.retryable = retryable
        self.retry_after = retry_after


def safe_url(url):
    """Never log query tokens, credentials, fragments or multiline input."""
    try:
        p = urlsplit(str(url))
        return f"{p.scheme}://{p.hostname or ''}{p.path}".replace("\n", "").replace("\r", "")[:250]
    except ValueError:
        return "<invalid-url>"


async def read_url(session, url, *, headers=None, limit=4 * 1024 * 1024,
                   prefix=False, stage="page", **kwargs):
    # Only transient transport errors and 5xx are retried; never 403/429/challenges.
    for attempt in range(2):
        try:
            async with session.get(url, headers=headers, **kwargs) as response:
                status = response.status
                response_headers = response.headers.copy()
                final_url = str(response.url)
                if status in (500, 502, 503, 504) and attempt == 0:
                    logger.warning("request_id={} stage={} upstream={} status={} retry=1",
                                   request_id.get(), stage, safe_url(url), status)
                else:
                    try:
                        body = await response.content.readexactly(limit if prefix else limit + 1)
                    except asyncio.IncompleteReadError as exc:
                        body = exc.partial
                    if not prefix and len(body) > limit:
                        raise ParseError(502, "上游页面过大，已停止解析", "UPSTREAM_TOO_LARGE", stage)
                    logger.info("request_id={} stage={} upstream={} status={} bytes={}",
                                request_id.get(), stage, safe_url(final_url), status, len(body))
                    return status, response_headers, body, final_url
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            logger.warning("request_id={} stage={} upstream={} transport={} attempt={}",
                           request_id.get(), stage, safe_url(url), type(exc).__name__, attempt + 1)
            if attempt:
                raise ParseError(502, "连接上游超时或中断，请稍后重试", "UPSTREAM_NETWORK",
                                 stage, True) from exc
        await asyncio.sleep(0.4)
