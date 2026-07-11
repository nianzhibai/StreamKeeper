# DouYinStreamKeeper

面向服务器部署的抖音直播录制 Web 服务。从 StreamCap 中抽离抖音录制链路，只保留浏览器管理、任务持久化、循环值守、抓流和 FFmpeg 录制，不包含桌面端、托盘、多平台 Handler 或 Flet。

## 功能

- 浏览器中新建、编辑、启动、停止和删除录制任务。
- 支持直接粘贴抖音分享文案，自动提取 `live.douyin.com`、`v.douyin.com` 或 `www.douyin.com/user/...` 链接。
- 支持 OD、UHD、HD、SD、LD 画质。
- 支持 TS、MP4、MKV、FLV 和按时长分段；段数默认 4，达到上限或本次直播提前结束都会自动停止任务。
- 自动优先 FLV；FLV 为 H.265/HEVC 时回退 HLS。
- SQLite 持久化任务，服务器重启后自动恢复已启用的值守任务。
- 限制同时录制数，避免 FFmpeg 占满服务器资源。
- 独立登录页、SQLite 会话、HttpOnly Cookie、CSRF 校验和登录失败 IP 永久黑名单。
- 健康检查与登录静态资源公开，管理页面和 API 需要有效会话。
- Docker Compose 一键部署，数据和录像保存在持久化卷中。

抖音接口和直播流解析由 `streamget` 提供；本项目负责单平台 Web 管理、调度和录制生命周期。

## 架构

```text
浏览器
  │  Session Cookie + CSRF + JSON API
  ▼
FastAPI（单 worker）
  ├─ 静态管理页面
  ├─ SQLite 任务仓库
  └─ asyncio 任务调度器
       ├─ streamget：检查开播、解析 FLV/HLS
       └─ FFmpeg：服务器本地落盘
```

录制任务运行在 Web 服务进程的后台调度器中，因此必须保持一个 Uvicorn worker。多个 worker 会重复恢复同一批任务，项目启动时会主动拒绝这种配置。需要横向扩容时，应先把调度器拆成独立 worker，并增加分布式任务锁。

## 推荐部署：Docker Compose

服务器需要 Docker 和 Docker Compose。进入项目目录：

```bash
cd DouYinStreamKeeper
cp .env.example .env
```

编辑 `.env`，至少修改：

```dotenv
DOUYIN_WEB_USERNAME=admin
DOUYIN_WEB_PASSWORD=请替换成足够长的随机密码
```

构建并启动：

```bash
docker compose up -d --build
docker compose logs -f recorder
```

默认只监听服务器的 `127.0.0.1:8000`。在服务器本机可访问：

```text
http://127.0.0.1:8000
```

浏览器会显示项目自己的登录页。登录成功后服务端签发不可被 JavaScript 读取的 HttpOnly 会话 Cookie，默认有效期为 7 天。会话剩余时间不足一半时，正常访问会自动续期到完整的 7 天；有效会话会保存在 SQLite 中并跨服务重启保留，退出登录则会立即撤销。配置的用户名或密码发生变化后，服务会在下次启动时撤销全部已有会话；数据库只保存经过 scrypt 处理的凭据指纹，不保存明文密码。

登录失败记录和黑名单保存在 SQLite 中。默认同一客户端 IP 在滚动 1 小时内累计失败 3 次后会被永久拉黑，第三次及后续登录返回 `403 Forbidden`，服务重启或输入正确密码都不会自动解封。达到阈值前成功登录会清除该 IP 的失败记录。已登录的管理员可以通过黑名单 API 查询和手动解封；使用反向代理时需要确保 Uvicorn 收到可信的真实客户端 IP，避免将代理或整个 NAT 出口误封。

如果唯一的管理 IP 被误封且已经没有有效会话，可以直接从持久化数据库解封；将示例 IP 替换为实际地址：

```bash
docker compose exec recorder python -c "import sqlite3; db=sqlite3.connect('/data/tasks.db'); db.execute('DELETE FROM web_login_blacklist WHERE client_key=?', ('203.0.113.10',)); db.commit()"
```

对公网提供服务时仍应使用 HTTPS，因为 HTTP 下登录密码和会话 Cookie 都可能被截获。可以使用 Nginx/Caddy 终止 TLS，示例见 [`deploy/nginx.conf.example`](deploy/nginx.conf.example)；也可以后续配置 Uvicorn 原生 TLS。

如果只在可信内网直接访问，可以在 `.env` 中设置：

```dotenv
DOUYIN_BIND_ADDRESS=0.0.0.0
```

同时应通过服务器防火墙限制允许访问的来源地址。

### 数据位置

Compose 默认使用 `recorder-data` 命名卷：

```text
/data/tasks.db          SQLite 任务数据库
/data/recordings/       录像文件
```

查看卷位置：

```bash
docker volume inspect douyinstreamkeeper_recorder-data
```

如果希望录像直接出现在当前目录，可以将 Compose 中的卷改成：

```yaml
volumes:
  - ./data:/data
```

容器使用 UID `10001`，绑定宿主机目录前需要赋予写权限：

```bash
mkdir -p data/recordings
sudo chown -R 10001:10001 data
```

备份时至少保留 `tasks.db` 和 `recordings/`。复制 SQLite 数据库前建议先停止容器，或使用 SQLite 在线备份命令。

## Cookie 与代理

公开直播间通常可以直接解析。遇到风控或需要账号状态时，可在 `.env` 中设置：

```dotenv
DOUYIN_COOKIE=ttwid=...; sessionid=...
DOUYIN_PROXY=http://127.0.0.1:7890
```

Cookie 不会通过 Web API 返回，也不会保存到 SQLite。更安全的方式是把 Cookie 保存为服务器只读文件，然后向容器挂载并设置：

```dotenv
DOUYIN_COOKIE_FILE=/run/secrets/douyin-cookie
```

并在 `docker-compose.yml` 的 `recorder.volumes` 中增加：

```yaml
- ./secrets/douyin-cookie.txt:/run/secrets/douyin-cookie:ro
```

不要把真实 Cookie 提交到 Git、镜像或日志中。

## 不使用 Docker

环境要求：

- Python 3.10+
- FFmpeg
- Node.js

安装并启动：

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e .

export DOUYIN_WEB_USERNAME=admin
export DOUYIN_WEB_PASSWORD='替换成随机密码'
export DOUYIN_DATA_DIR=/srv/DouYinStreamKeeper
export DOUYIN_WEB_HOST=127.0.0.1
export DOUYIN_WEB_PORT=8000

python -m douyin_recorder
```

也可以使用安装后的入口：

```bash
douyin-stream-keeper
```

开发环境临时关闭鉴权：

```bash
DOUYIN_ALLOW_INSECURE=true python -m douyin_recorder
```

这个开关只能用于本机调试。

## 配置项

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| `DOUYIN_WEB_USERNAME` | `admin` | Web 登录用户名 |
| `DOUYIN_WEB_PASSWORD` | 无 | Web 登录密码；生产环境必填 |
| `DOUYIN_SESSION_TTL_HOURS` | `168` | 登录会话的滑动有效小时数，访问到半周期后自动续期，范围 1–720 |
| `DOUYIN_LOGIN_MAX_ATTEMPTS` | `3` | 窗口内触发永久 IP 黑名单的失败次数 |
| `DOUYIN_LOGIN_WINDOW_SECONDS` | `3600` | 统计登录失败的滚动窗口秒数 |
| `DOUYIN_WEB_HOST` | `127.0.0.1` | 服务监听地址 |
| `DOUYIN_WEB_PORT` | `8000` | 服务监听端口 |
| `DOUYIN_DATA_DIR` | `data` | 数据根目录 |
| `DOUYIN_RECORDINGS_DIR` | `$DATA/recordings` | 录像目录 |
| `DOUYIN_DATABASE_PATH` | `$DATA/tasks.db` | SQLite 路径 |
| `DOUYIN_MAX_CONCURRENT_RECORDINGS` | `3` | 同时运行的 FFmpeg 数量 |
| `DOUYIN_FETCH_TIMEOUT_SECONDS` | `45` | 单次直播状态解析超时 |
| `DOUYIN_COOKIE` | 无 | 抖音 Cookie |
| `DOUYIN_COOKIE_FILE` | 无 | Cookie 文件，优先于环境变量 |
| `DOUYIN_PROXY` | 无 | 抓流和录制使用的 HTTP 代理 |
| `FFMPEG` | `ffmpeg` | FFmpeg 可执行文件路径 |
| `WEB_CONCURRENCY` | `1` | 必须保持为 1 |

## Web API

登录后可以访问 `/api/docs` 查看 OpenAPI 文档。主要接口：

| 方法 | 地址 | 用途 |
| --- | --- | --- |
| `GET` | `/health` | 无鉴权健康检查 |
| `POST` | `/api/auth/login` | 验证账号并创建会话 |
| `GET` | `/api/auth/session` | 获取当前会话与 CSRF 令牌 |
| `POST` | `/api/auth/logout` | 撤销当前会话并清除 Cookie |
| `GET` | `/api/auth/blocked-clients` | 查询永久登录黑名单 |
| `DELETE` | `/api/auth/blocked-clients/{ip}` | 手动解除指定 IP 的登录黑名单 |
| `GET/POST` | `/api/tasks` | 查询或创建任务 |
| `PATCH/DELETE` | `/api/tasks/{id}` | 更新或删除任务 |
| `POST` | `/api/tasks/{id}/start` | 启动值守 |
| `POST` | `/api/tasks/{id}/stop` | 停止值守和录制 |
| `POST` | `/api/inspect` | 检测直播间，不返回签名流地址 |
| `GET` | `/api/system` | 磁盘、FFmpeg、Node 和并发状态 |

## 代码结构

| 模块 | 职责 |
| --- | --- |
| `client.py` | 抖音 URL 分流和 `streamget` 适配 |
| `ffmpeg.py` | FLV/HLS 选择和 FFmpeg 参数 |
| `recorder.py` | 文件命名、录制进程和优雅停止 |
| `web/store.py` | SQLite 任务、会话、凭据指纹和登录黑名单持久化 |
| `web/auth.py` | Session Cookie 和 CSRF 校验 |
| `web/scheduler.py` | 检查、排队、录制和重启恢复 |
| `web/app.py` | FastAPI 接口、生命周期和静态页面 |
| `web/static/` | 无外部 CDN 的浏览器管理页面 |

## 测试

```bash
pip install -e '.[dev]'
pytest
ruff check .
ruff format --check .
```

测试使用临时数据库和伪造抓流/录制器，不会真的启动抖音录制。

## 许可与使用提示

本项目从 Apache-2.0 许可的 StreamCap 抽离和重构，`streamget` 使用 MIT 许可。迁移到新仓库时请保留 `LICENSE` 和 `NOTICE`。

请只录制你有权保存的直播内容，并遵守平台规则及当地法律。
