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

- 图片（图集）
- 视频（公开播放版本；支持无平台水印播放地址）

[✅] 微博

- 图片
- 视频

### 抖音图集和视频

支持抖音分享短链、`/note/<id>`、`/video/<id>` 及对应 `iesdouyin.com` 分享页面，
继续使用 `POST /api/v1/download`；也可直接传入包含分享链接的文本。例如：

```json
{"input_path":"https://v.douyin.com/QCw6Aerg1jo/"}
```

成功响应的 `platform` 为 `抖音`，`data` 始终为媒体 URL 列表，兼容现有 iOS 快捷指令。
只读取与请求作品 ID 匹配的公开结构化元数据，不把头像、封面或推荐作品当作下载结果。

- 图集：按作品顺序返回全部图片，保留 CDN 签名、WebP 格式和地址中的变换参数；
  逐张检查可访问性及实际文件头。任何图片缺失或不可用时返回错误，不返回不完整图集。
  返回地址可能是平台提供的变换或压缩版本，不保证为上传原图。
- 视频：优先使用公开元数据中的播放版本，按分辨率、码率排序；不返回带水印的
  `download_addr` 或 `playwm` 地址。若分享页只提供 `playwm` 和公开视频 URI，使用普通
  `https://www.douyin.com/aweme/v1/play/?video_id=<uri>&ratio=1080p` 播放端点，
  验证实际 MP4 文件头；1080p 不可用时再验证默认播放版本。URI 必须来自请求作品的元数据，
  不改写签名 URL，也不转码或处理视频画面。

视频 URI 路径参考了[公开播放端点示例](https://github.com/belingud/douyin-downloader-skill/blob/main/scripts/download.py#L310-L320)，
并独立进行了 NAS 实测。该方案可取得公开 HD 播放版本，**不等同于证明取得创作者上传的原始文件**；
也不能保证所有作品没有创作者自行嵌入的水印，或都存在可访问的无平台水印版本。

实现保持轻量、未登录：没有新增依赖、浏览器服务、账号 Cookie 管理或签名生成。
普通公开页面可能自动签发匿名 `ttwid` Cookie；若刚签发时页面仍未提供媒体元数据，
最多额外请求一次该公开页面。Cookie 仅保留在本次解析的内存会话内，结束即丢弃，
不会持久化，也不会执行或重放验证挑战。匿名会话 Cookie 不代表账号登录。

抖音页面和媒体请求不读取环境代理变量，不使用 `X_PROXY`；该配置仅供 X/Twitter 路径使用。
这不排除路由器等外部网络对出口的影响。无需修改系统代理、VPN 或网络安全设置。

公开元数据被上游隐藏、仅出现验证页、或无法取得可用播放信息时，API 返回明确的 422 错误，
不会把空列表当作成功；页面、网络或媒体下载验证失败返回 502。快捷指令应检查响应状态及
`code`，失败时不要把 `data: null` 当作下载列表。平台公开访问会随作品、会话和环境变化；
本次成功验收不代表所有链接或每次请求都可靠。

本地回归测试从项目根目录运行：

```powershell
Get-Content -Raw scripts/check_douyin.py | uv run python -
Get-Content -Raw scripts/check_parsers.py | uv run python -
Get-Content -Raw scripts/check_x.py | uv run python -
```

共 38 项组合回归：17 项抖音、11 项其他解析器、10 项 X，覆盖作品匹配、图集完整性、
WebP/签名地址、视频 URI 和 HD 回退、匿名 Cookie 的有界重试、验证页停止及真实 HTTP 文件头验证。
这些回归包含模拟数据和本地 HTTP 服务，不能代替真实平台验收。

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

## 快捷指令

分享链接，复制到手机上，打开
https://www.icloud.com/shortcuts/971541b983a345399e19af1445aac406

声明：脚本仅供学习交流使用，请勿用于商业用途。

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

### 本次抖音验收记录

本次验收发生于 2026-10-07（NAS 时区 UTC+8，对应 UTC 2026-10-06）。
NAS 运行产物版本为 `20261006-201519`，应用源码提交为
`6967698fb3783ab048953f7d43b70c1517f1d923`。本次文档更新不另行部署。

- 图集 `P8m2VakJop8` 对应作品 `7693103726027205561`，完整下载并解码了 5 张
  1344 × 2400 WebP。随后 NAS 记录了三个用户图集请求成功，分别返回 5、5、8 张图片。
- 视频 `QCw6Aerg1jo` 对应作品 `7670013728340342970`。默认公开播放版本为
  720 × 1280；1080p 版本为 1080 × 1920、H.264、13.30 秒、4,932,374 字节。
  NAS 实际 API 连续两次返回 200 和单个视频 URL；返回 URL 完整下载后通过 FFmpeg 解码。
  抽查 0、4、8、12.8 秒画面未见抖音水印，未证明为上传原始字节或逐帧验证所有水印。
- 用户随后确认 iOS 快捷指令图集及上述视频下载可用。API 日志只记录解析响应，
  不能独立确认手机已完成保存；此处客户端结论来自用户反馈。
- 本地和部署后的字节码运行环境均通过上述 38 项回归；NAS 的
  `scripts/smoke.py` 通过健康检查、HLS/FFmpeg、Redis 映射及 HTTP 200/206 Range 验证。
  容器健康，部署解析器校验值匹配产物清单；`.env`、`data/`、`logs/` 保留。

此次更新的前一版本 `20261006-200258` 及配置备份保留在
`rollback/20261006-201519/`。若需回滚，在 NAS 应用目录恢复备份的 `compose.yaml`
和 `release.env`，再执行上述 Compose 启动命令；保留现有 `.env` 和数据目录。

图集验收期间，同一运行版本 `20261006-200258` 曾先返回 422，随后返回成功，
另有上游验证页的诊断记录。这支持公开访问存在波动，但不能仅凭记录确定是限流、
会话还是外部出口策略导致。上述视频原有 422 另有已确认的播放候选选择缺陷，
本次已修复；不能把所有失败都归因于上游波动。未修改代理或网络设置。
