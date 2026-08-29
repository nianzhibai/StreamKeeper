# StreamKeeper

StreamKeeper一个直播录制工具，支持抖音、快手、哔哩哔哩三个平台。

## 功能

- **多平台支持**：抖音、快手、哔哩哔哩
- **多画质支持**：录制画质支持从原画到流畅五档画质
- **自动值守**：支持监控开播状态，开播就录制
- **网盘归档**：录制的视频支持自动上传到支持夸克网盘、百度网盘、115网盘、光鸭网盘和联通网盘

## 快速启动

### 方式一：Docker 启动（推荐）

**1. 下载部署文件并启动**

```bash
mkdir -p StreamKeeper && cd StreamKeeper
curl -fLO https://github.com/nianzhibai/StreamKeeper/releases/latest/download/docker-compose.yml
docker compose pull
docker compose up -d
```

默认使用最新 Release 镜像。首次打开登录页会要求设置管理员用户名和密码；需要自定义其他配置时再下载 `.env.example` 并保存为 `.env`。

**2. 常用指令**

```bash
docker compose logs -f recorder                       # 查看日志
docker compose ps                                     # 查看容器和健康状态
docker compose pull && docker compose up -d            # 更新到最新 Release
```

部署完成后访问：`http://服务器IP:8000/`

### 方式二：Python 启动

```bash
curl -fL https://github.com/nianzhibai/StreamKeeper/releases/latest/download/streamkeeper-source.tar.gz \
  | tar -xz
cd StreamKeeper
pip install .
stream-keeper
```

部署完成后访问：`http://服务器IP:8000/`

## 数据存放位置

- Docker 启动时，数据保存在 Docker 数据卷 `recorder-data` 中，对应容器内的 `/data` 目录。
- Python 启动时，数据默认保存在项目运行目录下的 `data` 目录。
- `tasks.db` 保存录制任务和程序配置，`recordings` 目录保存录像文件。

> 执行 `docker compose down` 不会删除数据；执行 `docker compose down -v` 会同时删除数据卷，请谨慎操作。

## .env.example简单说明

- `STREAM_KEEPER_IMAGE`：Docker 镜像，默认使用最新 Release。
- `STREAM_KEEPER_BIND_ADDRESS`、`STREAM_KEEPER_WEB_PORT`：Web 服务监听地址和端口。
- `STREAM_KEEPER_MAX_CONCURRENT_RECORDINGS`：最大同时录制数量。
- `STREAM_KEEPER_*_COOKIE`：抖音、哔哩哔哩、快手的可选登录 Cookie。
- `STREAM_KEEPER_PROXY`：三个直播平台共用的代理地址。
- `STREAM_KEEPER_QUARK_*`、`STREAM_KEEPER_WOPAN_*`、`STREAM_KEEPER_BAIDU_*`、`STREAM_KEEPER_115_*`、`STREAM_KEEPER_GUANGYA_*`：各网盘的登录凭据和目录配置。
- `STREAM_KEEPER_UPLOAD_*`：网盘上传模式、执行时间、文件等待时间和超时时间。
- `TZ`：程序时区，默认 `Asia/Shanghai`。

详细配置项和默认值请查看项目中的 `.env.example` 文件。首次访问登录页时会引导设置管理员账号和密码。

## 致谢

- [StreamCap](https://github.com/ihmily/StreamCap) — 参考其直播流接口
- [OpenList](https://github.com/OpenListTeam/OpenList) — 参考其网盘接口
