<p align="center">
  <img width="120" height="120" alt="StreamKeeper" src="assets/streamkeeper-logo.png" />
</p>

<h3 align="center">StreamKeeper</h3>

<p align="center">
  一个直播录制工具
</p>

## 功能

- **多平台支持**：抖音、快手、哔哩哔哩
- **多画质支持**：录制画质支持从原画到流畅五档画质
- **自动值守**：支持监控开播状态，开播就录制
- **网盘归档**：录制的视频支持自动上传到夸克网盘、百度网盘、115网盘、光鸭网盘和联通网盘

## 快速启动

### 方式一：Docker 启动（推荐）

**1. 准备目录**

```bash
mkdir -p StreamKeeper && cd StreamKeeper
```
**2. 拉取仓库内置`docker-compose.yml`**
```bash
curl -fLO https://raw.githubusercontent.com/nianzhibai/StreamKeeper/main/docker-compose.yml
```
**3. 启动**
```bash
docker compose up -d
```

> 部署完成后访问：`http://服务器IP:8000/`，首次访问会要求设置管理员用户名和密码

**常用指令**

```bash
docker compose pull && docker compose up -d   # 更新并重启
docker compose logs -f                        # 查看日志
```

### 方式二：Python 启动

> Python版本需要 >= 3.10，并确保系统已安装 FFmpeg

**1. 拉取Release页面最新代码并解压**
```bash
curl -fL https://github.com/nianzhibai/StreamKeeper/releases/latest/download/streamkeeper-source.tar.gz \
  | tar -xz
```
**2. 进入项目目录并安装**
```bash
cd StreamKeeper
python3 -m pip install .
```

**3. 启动 StreamKeeper**

```bash
python3 -m stream_keeper
```

> 部署完成后访问：`http://服务器IP:8000/`

## 数据存放位置

| 启动方式 | 数据根目录 | 任务和程序配置 | 录像文件 |
| --- | --- | --- | --- |
| Docker | 容器内的 `/data` | `/data/tasks.db` | `/data/recordings` |
| Python | `~/.streamkeeper/data` | `~/.streamkeeper/data/tasks.db` | `~/.streamkeeper/data/recordings` |


## .env.example 部分字段说明

- `STREAM_KEEPER_BIND_ADDRESS`、`STREAM_KEEPER_WEB_PORT`：StreamKeeper监听的地址和端口
- `STREAM_KEEPER_DATA_DIR`：用于更改Python启动时，数据存放的目录，默认 `~/.streamkeeper/data`，对docker无效
- `STREAM_KEEPER_MAX_CONCURRENT_RECORDINGS`：最大同时录制数量
- `STREAM_KEEPER_*_COOKIE`：抖音、哔哩哔哩、快手的可选登录Cookie（默认不需要配置）
- `STREAM_KEEPER_PROXY`：可选代理地址。如果配置了，录制时会使用这个代理
- `STREAM_KEEPER_QUARK_*`、`STREAM_KEEPER_WOPAN_*`、`STREAM_KEEPER_BAIDU_*`、`STREAM_KEEPER_115_*`、`STREAM_KEEPER_GUANGYA_*`：各网盘的登录凭据和目录配置
- `STREAM_KEEPER_UPLOAD_*`：网盘上传模式、执行时间、文件等待时间和超时时间
- `TZ`：程序时区，默认 `Asia/Shanghai`


## 致谢

- [StreamCap](https://github.com/ihmily/StreamCap) — 参考其直播流接口
- [OpenList](https://github.com/OpenListTeam/OpenList) — 参考其网盘接口
