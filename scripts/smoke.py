"""Run inside the deployed app container; exercises real HTTP, Redis and FFmpeg."""
import functools
import json
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import requests

from config.config import settings
from util import cache_util

base = "http://127.0.0.1:4999"
session = requests.Session()
session.trust_env = False
assert session.get(base + "/api/v1/health", timeout=10).json()["redis"] == "ok"
assert "/api/v1/health" in session.get(base + "/openapi.json", timeout=10).json()["paths"]
assert session.get(base + "/docs", timeout=10).status_code == 200
bad = session.post(base + "/api/v1/download", json={"input_path": "no link"}, timeout=10)
assert bad.json()["code"] == 400
missing = session.get(base + "/api/v1/video/" + str(uuid.uuid4()), timeout=10)
assert missing.status_code == 404 and missing.json()["code"] == 404
print("PASS health, OpenAPI, docs, invalid input and missing video", flush=True)

title = "nas-smoke-" + uuid.uuid4().hex
output_dir = Path(settings.video_download_path) / "51" / title
token = None
with tempfile.TemporaryDirectory(prefix="anydownload-smoke-") as directory:
    fixture = Path(directory)
    subprocess.run([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
        "-i", "color=c=blue:s=160x120:r=10", "-t", "1", "-c:v", "libx264",
        "-f", "hls", "-hls_time", "1", str(fixture / "index.m3u8"),
    ], check=True, timeout=30)
    handler = functools.partial(SimpleHTTPRequestHandler, directory=directory)
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    origin = f"http://127.0.0.1:{server.server_port}"
    config = json.dumps({"video": {"url": origin + "/index.m3u8"}})
    (fixture / "article.html").write_text(
        f'<html><meta property="og:title" content="{title}">'
        f"<div class='dplayer' data-config='{config}'></div></html>", encoding="utf-8")
    try:
        response = session.post(base + "/api/v1/download", json={
            "input_path": origin + "/article.html"}, timeout=15)
        response.raise_for_status()
        assert response.json()["code"] == 200, response.text
        output = output_dir / (title + ".mp4")
        for _ in range(60):
            if output.exists() and output.stat().st_size > 0:
                check = subprocess.run([
                    "ffmpeg", "-hide_banner", "-loglevel", "error", "-i", str(output),
                    "-f", "null", "-"], capture_output=True, timeout=10)
                if check.returncode == 0:
                    break
            time.sleep(0.5)
        else:
            raise AssertionError("FFmpeg download did not produce a valid video")
        token = cache_util.create_token(str(output), expire_seconds=60)
        full = session.get(base + "/api/v1/video/" + token, timeout=10)
        assert full.status_code == 200 and full.content == output.read_bytes()
        partial = session.get(base + "/api/v1/video/" + token,
                              headers={"Range": "bytes=0-31"}, timeout=10)
        assert partial.status_code == 206 and partial.content == full.content[:32]
        print("PASS HTTP parsing -> HLS download -> valid MP4 -> Redis token -> HTTP 200/206", flush=True)
    finally:
        server.shutdown()
        server.server_close()
        if token:
            cache_util.delete_token(token)
        if output_dir.exists():
            shutil.rmtree(output_dir)
