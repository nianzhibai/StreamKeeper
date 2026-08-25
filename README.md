# StreamKeeper

StreamKeeper一个直播录制工具，支持抖音、快手、哔哩哔哩三个平台。

## 功能

- **多平台支持**：抖音、快手、哔哩哔哩
- **多画质支持**：录制画质支持从原画到流畅五档画质
- **自动值守**：支持监控开播状态，开播就录制
- **网盘归档**：录制的视频支持自动上传到支持夸克网盘、百度网盘、115网盘、光鸭网盘和联通网盘

## 快速启动

### 方式一：Docker 启动（推荐）

**1. 部署并启动**
```bash
git clone https://github.com/nianzhibai/StreamKeeper.git
cd StreamKeeper
cp .env.example .env
docker compose up -d --build
```

**2. 常用指令**

```bash
docker compose logs -f recorder                       # 查看日志
docker compose pull && docker compose up -d --build   # 更新
```

部署完成后访问：`http://服务器IP:8000/`

### 方式二：Python 启动

```bash
git clone https://github.com/nianzhibai/StreamKeeper.git
cd StreamKeeper
pip install .
export STREAM_KEEPER_WEB_PASSWORD=replace-with-a-long-random-password
stream-keeper
```

部署完成后访问：`http://服务器IP:8000/`

## 数据存放位置

## .env.example简单说明

## 致谢

- [StreamCap](https://github.com/ihmily/StreamCap) — 参考其直播流接口
- [OpenList](https://github.com/OpenListTeam/OpenList) — 参考其网盘接口
