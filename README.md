# StreamKeeper

面向服务器的多平台直播录制工具，支持抖音、哔哩哔哩和快手。在浏览器里添加直播间、自动值守开播、本地保存录像，并可自动归档到网盘。

## 功能

- **任务管理**：新建、编辑、启动、停止和删除录制任务，粘贴分享文案即可识别直播间
- **多画质分段录制**：原画到流畅五档画质，TS / MP4 / MKV / FLV，可按时长分段
- **自动值守**：开播后自动开始录制，服务器重启后继续已启用的任务
- **本地录像**：按主播和日期浏览、搜索、下载与在线播放
- **网盘归档**：支持夸克、联通、百度、115 和光鸭网盘，定时或录制完成后自动上传
- **运行日志**：关键事件一眼可查，异常与警告重点标注
- **账号保护**：独立登录页，登录失败过多会锁定来源 IP

## 快速启动

### 方式一：Docker 启动（推荐）

需要 Docker 和 Docker Compose。

```bash
git clone https://github.com/nianzhibai/StreamKeeper.git
cd StreamKeeper
cp .env.example .env
docker compose up -d --build
```

使用默认 `.env` 启动后，首次打开登录页会引导设置管理员账号和密码。平台 Cookie 等可选配置见 `.env.example` 注释；旧版本升级请以 `.env.example` 为准迁移，仅 `STREAM_KEEPER_*` 命名空间的变量生效。

```bash
docker compose logs -f recorder                       # 查看日志
docker compose pull && docker compose up -d --build   # 更新
```

### 方式二：Python 启动

需要 Python 3.10 及以上，且 FFmpeg 已安装并在 `PATH` 中可用。

```bash
git clone https://github.com/nianzhibai/StreamKeeper.git
cd StreamKeeper
pip install .
export STREAM_KEEPER_WEB_PASSWORD=replace-with-a-long-random-password
stream-keeper
```

Python 方式不读取 `.env` 文件，配置通过同名环境变量设置；数据与录像默认保存在工作目录的 `data/` 下。

启动后浏览器访问 `http://服务器IP:8000/`；默认监听所有网络接口，仅本机访问可设置 `STREAM_KEEPER_BIND_ADDRESS=127.0.0.1`。

## 使用提示

- `STREAM_KEEPER_PROXY` 会同时用于三个直播平台的状态检查与流地址解析
- 对公网开放时建议自行配置 HTTPS 反向代理
- 请只录制你有权保存的内容，并遵守平台规则与当地法律

## 许可

Apache-2.0。本项目从 StreamCap 抽离和重构，请保留 `LICENSE` 与 `NOTICE`。
