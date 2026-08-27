# StreamKeeper

StreamKeeper 是一个面向服务器部署的多平台直播录制工具。通过 Web 界面管理直播间，在主播开播后自动录制到本地，并可将录像归档到多个网盘。

## 功能特性

- **多平台解析**：支持抖音、快手、哔哩哔哩直播间链接、分享短链及包含链接的分享文案
- **自动值守**：周期检查直播状态，开播自动录制；服务重启后自动恢复已启用任务
- **灵活录制**：支持原画、超清、高清、标清、流畅五档画质，以及 TS、MP4、MKV、FLV 格式
- **分段与限量**：可按时长分段，也可限制单次录制的段数
- **并发控制**：可设置同时录制上限，超出上限的任务按顺序等待
- **录像管理**：按主播和日期保存，可在浏览器中浏览、播放、下载和删除
- **网盘归档**：支持夸克网盘、联通云盘、百度网盘、115 网盘和光鸭网盘
- **运行日志**：记录任务、录制、归档、登录和系统事件，支持筛选与导出
- **访问保护**：管理员登录、会话管理、CSRF 防护及登录失败来源锁定

画质档位的具体平台映射见[各平台直播流画质档位说明](docs/PLATFORM_QUALITY.md)。实际可用画质取决于直播间、账号权限和平台限制。

## 快速启动

### Docker 部署（推荐）

需要安装 Docker 和 Docker Compose。

```bash
git clone https://github.com/nianzhibai/StreamKeeper.git
cd StreamKeeper
cp .env.example .env
docker compose up -d --build
```

启动后访问 `http://服务器IP:8000/`。默认 `.env` 使用初始化占位密码，首次打开登录页会要求设置管理员用户名和密码。

常用命令：

```bash
docker compose ps                 # 查看服务状态
docker compose logs -f recorder   # 查看容器日志
docker compose restart            # 重启服务
docker compose stop               # 停止服务
```

更新项目：

```bash
git pull --ff-only
docker compose up -d --build
```

### Python 部署

需要 Python 3.10 及以上（低于 4.0）和 FFmpeg，且 `ffmpeg` 命令已加入 `PATH`。

```bash
git clone https://github.com/nianzhibai/StreamKeeper.git
cd StreamKeeper
python -m venv .venv
source .venv/bin/activate
pip install .
export STREAM_KEEPER_WEB_USERNAME=admin
export STREAM_KEEPER_WEB_PASSWORD=replace-with-a-long-random-password
stream-keeper
```

也可使用 `python -m stream_keeper` 启动。Python 方式不会自动读取 `.env` 文件，需在启动进程的环境中设置变量；数据默认写入当前工作目录的 `data/`。

## 首次使用

1. 打开 Web 页面并完成管理员账号初始化。
2. 进入「录制任务」，粘贴直播间链接或平台分享文案。
3. 检查直播间信息，选择画质、格式、直播源和分段方式。
4. 创建任务并启用持续值守；主播开播后会自动开始录制。
5. 在「本地录像」中播放或下载文件；需要异地归档时，在「网盘归档」中配置目标和上传计划。

任务启用状态会保存在数据库中，正常重启服务不需要重新创建任务。设置了录制段数时，该任务代表一次有限录制，持续值守会自动关闭。

## 数据存放位置

Docker 部署使用 Compose 命名卷 `recorder-data`，挂载到容器内的 `/data`。Compose 通常会给实际卷名添加项目名前缀，可用以下命令确认：

```bash
docker compose exec recorder sh -c 'find /data -maxdepth 2 -type d -print'
docker volume ls
```

Python 部署默认使用项目工作目录下的 `data/`。主要内容如下：

```text
data/
├── tasks.db          # 任务、账号、设置、网盘凭据和运行事件
├── recordings/       # 录像，按“主播/日期”组织
└── preview-cache/    # 非 MP4 录像的浏览器播放转封装缓存
```

可以通过 `STREAM_KEEPER_DATA_DIR`、`STREAM_KEEPER_RECORDINGS_DIR` 和 `STREAM_KEEPER_DATABASE_PATH` 调整 Python 部署的数据路径。

> `docker compose down` 不会删除命名卷，但 `docker compose down -v` 会删除数据卷。升级或迁移前请备份数据库与录像目录。

## 环境变量

完整模板和注释见 [`.env.example`](.env.example)。只有 `STREAM_KEEPER_*` 命名空间中的应用变量会生效，`TZ` 除外。

### Web 与运行参数

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `STREAM_KEEPER_WEB_USERNAME` | `admin` | 首次启动使用的管理员用户名 |
| `STREAM_KEEPER_WEB_PASSWORD` | 无 | 必填，至少 10 个字符；模板占位值会启用首次访问初始化 |
| `STREAM_KEEPER_BIND_ADDRESS` | `0.0.0.0` | 宿主机监听地址；仅本机访问可设为 `127.0.0.1` |
| `STREAM_KEEPER_WEB_PORT` | `8000` | Web 端口 |
| `STREAM_KEEPER_SESSION_TTL_HOURS` | `168` | 登录会话有效时间，范围 1～720 小时 |
| `STREAM_KEEPER_LOGIN_MAX_ATTEMPTS` | `3` | 单个来源在统计窗口内允许的失败次数 |
| `STREAM_KEEPER_LOGIN_WINDOW_SECONDS` | `3600` | 登录失败统计窗口，范围 10～86400 秒 |
| `STREAM_KEEPER_MAX_CONCURRENT_RECORDINGS` | `3` | 同时录制上限，范围 1～100；之后可在 Web 中修改 |
| `STREAM_KEEPER_FETCH_TIMEOUT_SECONDS` | `45` | 获取直播状态和流地址的超时时间，最小 5 秒 |
| `TZ` | `Asia/Shanghai` | 定时归档和页面显示使用的时区 |

应用当前只支持单 Web worker。Docker 配置已固定 `STREAM_KEEPER_WEB_WORKERS=1`，Python 部署也不要改为多 worker。

### 直播平台参数

| 变量 | 说明 |
| --- | --- |
| `STREAM_KEEPER_DOUYIN_COOKIE` | 可选，抖音登录 Cookie |
| `STREAM_KEEPER_DOUYIN_COOKIE_FILE` | 可选，从指定文件读取抖音 Cookie；优先于上面的变量 |
| `STREAM_KEEPER_BILIBILI_COOKIE` | 可选，哔哩哔哩登录 Cookie，可用于账号有权限观看的画质 |
| `STREAM_KEEPER_KUAISHOU_COOKIE` | 可选，快手登录 Cookie，匿名访问受限时可配置 |
| `STREAM_KEEPER_PROXY` | 可选，三个平台共用的 HTTP/HTTPS 代理 |

对应的 `.env` 配置示例：

```dotenv
STREAM_KEEPER_DOUYIN_COOKIE=
STREAM_KEEPER_DOUYIN_COOKIE_FILE=
STREAM_KEEPER_BILIBILI_COOKIE=
STREAM_KEEPER_KUAISHOU_COOKIE=
STREAM_KEEPER_PROXY=
```

Cookie 不能包含换行符。Docker 使用 `STREAM_KEEPER_DOUYIN_COOKIE_FILE` 时，还需把该文件只读挂载到容器内，并填写容器内路径。

### 网盘归档参数

网盘凭据可以通过 `.env` 初始化，也可以登录后在「网盘归档」页面配置。夸克、联通、115 和光鸭支持扫码授权；百度通过页面提供的授权流程或开放平台凭据连接。

| 网盘 | 环境变量 |
| --- | --- |
| 夸克网盘 | `STREAM_KEEPER_QUARK_COOKIE`、`STREAM_KEEPER_QUARK_ROOT_ID` |
| 联通云盘 | `STREAM_KEEPER_WOPAN_ACCESS_TOKEN`、`STREAM_KEEPER_WOPAN_REFRESH_TOKEN`、`STREAM_KEEPER_WOPAN_ROOT_ID`、`STREAM_KEEPER_WOPAN_FAMILY_ID` |
| 百度网盘 | `STREAM_KEEPER_BAIDU_ACCESS_TOKEN`、`STREAM_KEEPER_BAIDU_REFRESH_TOKEN`、`STREAM_KEEPER_BAIDU_CLIENT_ID`、`STREAM_KEEPER_BAIDU_CLIENT_SECRET` |
| 115 网盘 | `STREAM_KEEPER_115_COOKIE`、`STREAM_KEEPER_115_ACCESS_TOKEN`、`STREAM_KEEPER_115_REFRESH_TOKEN`、`STREAM_KEEPER_115_ROOT_ID` |
| 光鸭网盘 | `STREAM_KEEPER_GUANGYA_ACCESS_TOKEN`、`STREAM_KEEPER_GUANGYA_REFRESH_TOKEN`、`STREAM_KEEPER_GUANGYA_CLIENT_ID`、`STREAM_KEEPER_GUANGYA_DEVICE_ID`、`STREAM_KEEPER_GUANGYA_ROOT_ID` |

上传行为：

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `STREAM_KEEPER_UPLOAD_MODE` | `scheduled` | `scheduled` 每日定时执行；`recording_completed` 每场录制结束后执行 |
| `STREAM_KEEPER_UPLOAD_HOUR` | `1` | 定时上传的本地整点小时，范围 0～23 |
| `STREAM_KEEPER_UPLOAD_MIN_AGE_MINUTES` | `5` | 定时扫描时文件需要保持不变的最短时间，范围 0～1440 分钟 |
| `STREAM_KEEPER_UPLOAD_TIMEOUT_SECONDS` | `300` | 单次网盘请求超时，最小 30 秒 |

录像固定归档到所选网盘根目录下的 `/DouYinStreamKeeper`。启用多个目标时，只有文件在所有目标上都确认成功后才会删除本地录像；任一目标失败都会保留本地文件以便重试。

## 安全与运维

- 默认监听 `0.0.0.0`。仅在本机使用时将 `STREAM_KEEPER_BIND_ADDRESS` 改为 `127.0.0.1`。
- 对公网开放时请使用足够强的管理员密码，并配置 HTTPS 反向代理；项目提供了 [Nginx 配置示例](deploy/nginx.conf.example)。
- 不要提交包含 Cookie、Token 或密码的 `.env` 文件。
- 录像开始前和录制过程中都会检查磁盘空间；剩余空间达到 1 GiB 保留水位时会停止录制。
- 容器日志通过 `docker compose logs` 查看，Web「运行日志」保存需要关注的业务事件。
- 健康检查地址为 `/health`。

## 项目结构

```text
src/stream_keeper/
├── platforms/       # 抖音、哔哩哔哩、快手解析与统一路由
├── cloud/           # 各网盘客户端与配置模型
├── web/             # FastAPI、任务调度、归档服务和 Web 页面
├── recorder.py      # FFmpeg 录制流程
├── ffmpeg.py        # 直播源选择与 FFmpeg 命令构建
├── settings.py      # 环境配置与启动校验
└── storage.py       # 磁盘空间保护
```

平台解析器只负责将直播间转换为统一直播信息，调度器负责任务生命周期和并发控制，录制器负责 FFmpeg 进程，归档服务负责多网盘上传与本地清理，组件职责彼此独立。

## 开发与测试

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
pytest
ruff check .
```

测试默认不会访问真实平台，平台实测说明记录在 `docs/` 中。

## 使用须知

直播平台接口和风控策略可能随时变化。请只录制你有权保存的内容，并遵守平台规则、著作权要求和当地法律。

## 许可与致谢

本项目采用 [Apache License 2.0](LICENSE)，分发或修改时请保留 `LICENSE` 与 `NOTICE`。

- [StreamCap](https://github.com/ihmily/StreamCap) — 直播流接口参考
- [OpenList](https://github.com/OpenListTeam/OpenList) — 网盘接口参考
