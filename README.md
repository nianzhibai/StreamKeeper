# StreamKeeper

面向服务器的多平台直播录制工具，支持抖音、哔哩哔哩和快手。在浏览器里添加直播间、自动值守开播、本地保存录像，并可把录像归档到夸克网盘或联通云盘。

## 功能

- **任务管理**：新建、编辑、启动、停止和删除录制任务
- **多平台解析**：自动识别抖音、哔哩哔哩和快手直播间及平台分享短链
- **粘贴即用**：可以直接粘贴平台分享文案，自动提取并规范化直播间链接
- **多画质录制**：支持原画、超清、高清、标清、流畅五档
- **分段保存**：支持 TS / MP4 / MKV / FLV，可按时长分段
- **本地录像**：按主播和日期浏览、搜索、下载，并使用 ArtPlayer 在线播放
- **自动值守**：开播后自动开始录制，服务器重启后继续已启用的任务
- **网盘归档**：支持夸克、联通云盘扫码登录，定时整体归档，也可在录像列表里单独上传并查看实时进度
- **运行日志**：网页里查看开播、录制、归档、登录等关键事件，一眼判断服务是否正常
- **账号保护**：独立登录页，登录失败过多会锁定来源 IP

## 项目结构

核心代码使用与平台无关的 `stream_keeper` 包，各平台解析器只负责把直播间转换成统一的直播信息：

```text
src/
└── stream_keeper/
    ├── platforms/
    │   ├── douyin/
    │   │   ├── client.py      # 统一平台适配器
    │   │   ├── transport.py   # 跳转、Cookie 和 API 请求
    │   │   └── parser.py      # 房间数据与直播流纯解析
    │   ├── bilibili/          # 哔哩哔哩解析器
    │   ├── kuaishou/          # 快手解析器
    │   ├── base.py            # 平台公共约定
    │   └── router.py          # 链接识别与平台路由
    ├── cloud/                 # 网盘客户端
    ├── web/                   # Web API、调度器和静态页面
    ├── recorder.py            # FFmpeg 录制
    └── settings.py            # 运行配置
```

Python 代码从 `stream_keeper` 导入，服务可通过 `python -m stream_keeper` 或 `stream-keeper` 启动。

## 安装

服务器需要 Docker 和 Docker Compose。

```bash
git clone https://github.com/nianzhibai/StreamKeeper.git
cd StreamKeeper
cp .env.example .env
```

`.env` 中默认保留初始化占位值：

```dotenv
STREAM_KEEPER_WEB_USERNAME=admin
STREAM_KEEPER_WEB_PASSWORD=replace-with-a-long-random-password
```

使用这组默认值启动时，首次打开登录页会要求设置管理员用户名和密码，完成后自动登录；凭据以慢哈希保存在当前服务器。也可以在启动前直接把以上两项改为自定义凭据。对外开放服务前应先完成初始化，密码建议使用 20 位以上随机字符串。

平台 Cookie 均为可选项。哔哩哔哩登录 Cookie 可用于获取账号有权限观看的高画质，快手遇到风控或匿名访问限制时建议配置 Cookie：

```dotenv
STREAM_KEEPER_DOUYIN_COOKIE=
STREAM_KEEPER_BILIBILI_COOKIE=
STREAM_KEEPER_KUAISHOU_COOKIE=
```

> 从旧版本升级时，请以 `.env.example` 为准迁移配置。当前版本仅读取 `STREAM_KEEPER_*` 命名空间；旧的 `DOUYIN_*`、`BILIBILI_COOKIE`、`KUAISHOU_COOKIE`、`WEB_CONCURRENCY` 和 `FFMPEG` 变量不再生效。`TZ` 仍使用标准名称。

启动：

```bash
docker compose up -d --build
```

浏览器访问 `http://服务器IP:8000/`（默认监听 `0.0.0.0:8000`，即所有网络接口都可访问；只想本机访问可在 `.env` 中设置 `STREAM_KEEPER_BIND_ADDRESS=127.0.0.1`）。

常用命令：

```bash
docker compose logs -f recorder   # 查看日志
docker compose restart            # 重启
docker compose stop               # 停止
docker compose pull && docker compose up -d --build   # 更新代码后重建
```

## 使用提示

- 支持 `live.douyin.com` / `v.douyin.com`、`live.bilibili.com` / `b23.tv`、`live.kuaishou.com` / `v.kuaishou.com` 链接
- `STREAM_KEEPER_PROXY` 会同时用于三个直播平台的状态检查与流地址解析
- 登录后可在「设置」中扫码绑定夸克或联通云盘，再在「网盘归档」中启用自动上传
- 录像默认保存在 Docker 数据卷中；网盘上传成功后，可按配置清理本地文件
- 对公网开放时建议自行配置 HTTPS 反向代理
- 请只录制你有权保存的内容，并遵守平台规则与当地法律

## 许可

Apache-2.0。本项目从 StreamCap 抽离和重构，请保留 `LICENSE` 与 `NOTICE`。
