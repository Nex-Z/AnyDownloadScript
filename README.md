# AnyDownloadScript

支持直接传入包含链接的文本，分析并解析图片、视频等资源。

## 使用方法

1. 启动fastapi服务，调用接口即可
2. （推荐用途）结合ios快捷指令，复制平台分析链接。执行快捷指令，读取剪贴板，调用接口解析，获取图片、视频链接，下载资源并添加到指定相册。

## 支持平台

[✅] 小红书

- 图片
- 视频（有水印）

小红书图片只返回 `fileId` 对应的原尺寸地址（`https://ci.xiaohongshu.com/<fileId>`），无需登录。
返回前会并发验证图片地址可访问且响应包含图片文件头，不添加缩放或格式转换参数。
任一图片缺少原图标识、原图不可访问或验证超时，整个请求明确失败，不返回部分结果，
也不回退到 H5、Open Graph 等可能带水印的预览图。

[✅] 抖音

- 图片

[✅] 微博

- 图片
- 视频

### X（Twitter）图片和视频

支持 `x.com` / `twitter.com` 的公开推文链接，继续使用 `POST /api/v1/download`：

```json
{"input_path":"https://x.com/用户名/status/推文数字ID"}
```

返回 `platform: "𝕏"`，`data` 为媒体下载链接列表，支持多张照片、多段视频和混合内容。
视频通过服务端流式下载，文件名为 `清理后的文案_推文ID_视频序号.mp4`，过滤链接、控制字符和文件名非法字符，
文案限制为 150 UTF-8 字节；无文案时使用 `视频`。链接默认一小时有效，过期后重新解析即可。
照片使用 `pbs.twimg.com/media` 的 `name=orig` 原尺寸地址，视频选择最高可用画质 MP4。
无需配置 Cookie；无媒体、已删除、私密或需要登录的推文可能返回错误。
服务器需要能访问 X 相关接口，客户端也需要能访问视频 CDN；直链请及时下载。
NAS 无法直连时，可在 `.env` 设置 `X_PROXY=http://host.docker.internal:7890`（替换为实际代理地址）；仅 X 解析使用此代理。
解析使用 [yt-dlp 的 Twitter 解析器](https://github.com/yt-dlp/yt-dlp/blob/master/yt_dlp/extractor/twitter.py)。
优先依次尝试 FxTwitter、VXTwitter 公共接口（会向它们发送推文 ID），保留混合媒体顺序，
仅接受对应推文的 `pbs.twimg.com/media` 图片和 `video.twimg.com` MP4，不返回头像或封面。
媒体接口均失败时再尝试 yt-dlp 视频解析。
媒体接口优先直连，传输失败时使用 `X_PROXY` 重试，避免代理不稳定导致连续解析超时。

### TODO

[ ] 抖音

- 视频

## 快捷指令

分享链接，复制到手机上，打开
https://www.icloud.com/shortcuts/971541b983a345399e19af1445aac406

声明：脚本仅供学习交流使用，请勿用于商业用途。

### Douyin public media extraction

Douyin share links and `/note/<id>` / `/video/<id>` links use public structured
post metadata. Image galleries preserve their order, signed CDN URLs and formats
(including WebP and CDN transformations). Every image must have an accessible,
valid image response; an incomplete gallery is an error.

Videos use the highest-resolution available playback rendition, with bitrate as
a tie breaker, and return a URL list compatible with the existing API/Shortcut.
Watermarked `download_addr` and `playwm` addresses are excluded. Playback URLs are
not rewritten and file headers are checked before returning them. This does not
establish that a rendition is the creator's original upload or guarantee that no
watermark was embedded by the creator.

No login cookies, signature generation or access-restriction bypass is used.
If Douyin withholds public metadata, the API returns an explicit 422 error rather
than an empty success; upstream/network/media failures return 502. Public access
varies by post and environment. The supplied regression link
`https://v.douyin.com/P8m2VakJop8/` resolved to `7693103726027205561`, but public
share responses on 2026-10-07 were intermittent. A successful cookie-free request
returned five WebP images; full downloads and FFprobe confirmed all five were
1344 x 2400. Later requests returned a verification page, correctly reported as an
error. Public video test posts did not provide accessible playback metadata, so
actual video quality and watermark absence have not yet been established.

Run dedicated offline and HTTP-probe regressions from the project root:

```powershell
Get-Content -Raw scripts/check_douyin.py | uv run python -
```

## NAS 部署（运行产物）

部署地址：`http://192.168.6.178:4999`，接口文档：`/docs`，健康检查：`/api/v1/health`。
本项目是 API 服务，没有独立网页首页。快捷指令使用 `POST /api/v1/download`，JSON 请求体为
`{"input_path":"包含分享链接的文本"}`。

本地使用 Python 3.12 编译并打包，依赖版本及哈希来自 `uv.lock`：

```powershell
uv run --no-project --python 3.12 scripts/package.py
```

`artifacts/anydownloadscript-<tag>.tar.gz` 包含 `.pyc`、依赖清单和容器配置；不包含应用源码、
本地 `.env`、日志或下载文件。字节码要求容器也使用 Python 3.12。

NAS 目录为 `/vol1/1000/Server/AnyDownloadScript`。将压缩包及 `.sha256` 上传到 `releases/`，
校验并解压到 `releases/<tag>/`，然后运行：

```sh
cd /vol1/1000/Server/AnyDownloadScript
# 将 <tag> 替换为打包输出的 tag。
docker build -t anydownloadscript:<tag> releases/<tag>
# 首次部署复制配置；更新时保留现有 .env 和数据目录。
cp releases/<tag>/compose.yaml compose.yaml
test -e .env || cp releases/<tag>/.env.example .env
chmod 600 .env
printf 'IMAGE_TAG=<tag>\n' > release.env
docker compose --env-file release.env up -d --wait --wait-timeout 90
docker compose --env-file release.env ps
```

`data/` 保存下载文件，`logs/` 保存应用日志，应用容器配置了自动重启。
复用 NAS 已有的共享 Redis，通过 `host.docker.internal:6379` 访问宿主机发布端口，使用 DB 1。
连接地址、数据库编号和密码由 `.env` 中的 `REDIS_HOST`、`REDIS_PORT`、`REDIS_DB`、`REDIS_PWD` 配置。
Compose 不启动 Redis 容器。更新前备份 `compose.yaml`、`release.env`
和 `.env`，保留旧镜像及 release 目录。回滚时恢复配置并重新执行上面的 `up` 命令。

部署后可从本地运行完整验收：

```powershell
Get-Content -Raw scripts/smoke.py | ssh nas 'docker exec -i anydownloadscript-app-1 python -'
```

验收通过本地生成的 HLS 样本检查解析接口、FFmpeg 下载、Redis 映射、视频完整下载及 Range 请求，
并自动清理样本；这不代表各第三方平台的线上分享链接始终有效。
