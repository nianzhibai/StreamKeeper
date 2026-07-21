# DouYinStreamKeeper

面向服务器的抖音直播录制工具。在浏览器里添加直播间、自动值守开播、本地保存录像，并支持把录像归档到夸克网盘或联通云盘。

## 功能

- **任务管理**：新建、编辑、启动、停止和删除录制任务
- **粘贴即用**：支持直接粘贴抖音分享文案，自动识别直播间链接
- **多画质录制**：支持原画、蓝光、超清、高清、标清
- **分段保存**：支持 TS / MP4 / MKV / FLV，可按时长分段
- **本地录像**：按主播和日期浏览、搜索、下载，并支持在线播放
- **自动值守**：开播后自动开始录制，服务器重启后继续已启用的任务
- **网盘归档**：支持夸克、联通云盘扫码登录，定时或手动上传录像
- **账号保护**：独立登录页，登录失败过多会锁定来源 IP

## 安装

服务器需要 Docker 和 Docker Compose。

```bash
git clone https://github.com/nianzhibai/DouYinStreamKeeper.git
cd DouYinStreamKeeper
cp .env.example .env
```

编辑 `.env`，至少设置登录密码：

```dotenv
DOUYIN_WEB_USERNAME=admin
DOUYIN_WEB_PASSWORD=请替换成足够长的随机密码
```

启动：

```bash
docker compose up -d --build
```

浏览器访问 `http://服务器IP:8000/`（默认监听本机 `127.0.0.1:8000`；若需直接外网访问，可在 `.env` 中设置 `DOUYIN_BIND_ADDRESS=0.0.0.0`）。

常用命令：

```bash
docker compose logs -f recorder   # 查看日志
docker compose restart            # 重启
docker compose stop               # 停止
docker compose pull && docker compose up -d --build   # 更新代码后重建
```

## 使用提示

- 登录后可在「设置」中扫码绑定夸克或联通云盘，再在「网盘归档」中启用自动上传
- 录像默认保存在 Docker 数据卷中；网盘上传成功后，可按配置清理本地文件
- 对公网开放时建议自行配置 HTTPS 反向代理
- 请只录制你有权保存的内容，并遵守平台规则与当地法律

## 许可

Apache-2.0。本项目从 StreamCap 抽离和重构，请保留 `LICENSE` 与 `NOTICE`。
