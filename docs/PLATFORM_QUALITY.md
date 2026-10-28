# 各平台直播流画质档位说明

本文档基于 2026-08-22 对抖音、哔哩哔哩、快手三个平台 Web 直播间的实测抓包数据（匿名访问）。

## 通用说明

任务保存平台原生画质标识。页面根据直播平台展示对应的常见档位；实际可用档位取决于直播间和账号权限。默认的 `best` 表示自动选择当前可获得的最高档，适合在主播未开播时创建值守任务。

抖音按 `sdk_key` 选择，哔哩哔哩传递 `qn`，快手按 `qualityType` 选择。指定档位不可用或平台返回其他档位时报告错误。`best` 在解析成功后返回实际选择的档位；旧接口没有返回画质标识时可能无法确认实际档位。

旧版任务保存的统一画质值会在数据库初始化时转换为对应平台标识；旧版最高档转换为 `best`。以下实测参数是历史样本，并非每个房间的固定参数。

---

## 一、抖音 (live.douyin.com)

数据来源：`webcast/room/web/enter/` 响应中的 `stream_url.live_core_sdk_data.pull_data`，以及官方播放器 `new-player-merged` JS。

### 标准五档（options.qualities）

| sdk_key | UI 名称 | 实测分辨率 | 声明码率 | fps | 备注 |
|---|---|---|---|---|---|
| `ld` | 标清 | 480x853 ~ 540x960 | 1.0 Mbps | 22-25 | |
| `sd` | 高清 | 540x952 ~ 720x1280 | 1.5-2.0 Mbps | 30 | |
| `hd` | 超清 | 720x1280 | 2.0-4.0 Mbps | 30 | 部分房间默认档 |
| `uhd` | 蓝光 | 1080x1920 | 6.0-7.5 Mbps | 30-45 | 部分房间没有此档 |
| `origin` | 原画 | 1080x1920（个别 1088x1920） | 2.25-7.7 Mbps | 22-60 | 恒为 `options.default_quality` |

### 附加档位（存在于 stream_data，不进画质面板）

| 档位 | 实测 | 说明 |
|---|---|---|
| `md` | 240x423/250kbps 或 360x640/800kbps，15fps | 极速/流畅档 |
| `ao` | 纯音频（vbitrate=0） | 同源流加 `only_audio=1` |
| `fhd` | 官方排序 `uhd(4) < fhd(5) < origin(6)` | 老接口命名 FULL_HD1=蓝光 |

### 特点

- 全部档位匿名可得；档位集合随主播推流配置变化（有的房间无 uhd 档）
- 每档多协议同时下发：`flv / hls / lls(LL-HLS) / http_ts / cmaf / dash`，且分 `main/backup` 主备线路
- 每档多编码：sdk_params 的 `VCodec` 可为 h264 或 h265（options 中 `v_codec` 显示 264 / bytevc1）
- `templateRealTimeInfo.bitrateKbps` 提供实时码率（如 origin 声明 6M、实时可达 8.9M）
- 部分线路 `enableEncryption=true`（加密流，项目会跳过）
- 官方默认档 = `options.default_quality.sdk_key`（实测均为 `origin`）

### 项目选项

`origin`、`uhd`、`fhd`、`hd`、`sd`、`ld`、`md`，以及自动选择 `best`。

---

## 二、哔哩哔哩 (live.bilibili.com)

数据来源：`xlive/web-room/v2/index/getRoomPlayInfo` 响应中的 `g_qn_desc` 与 `stream`，3 个直播间实测。

### 档位面板（g_qn_desc，服务器下发）

名称更新自 2026-10-28 对直播间 650 的匿名实测，具体描述以平台当时返回的数据为准。

| qn | UI 名称 | 说明 |
|---|---|---|
| 80 | 流畅 | 最低档 |
| 150 | 高清 | |
| 250 | 超清 | 测试房间中 qn=0 时服务器的默认返回 |
| 400 | 蓝光 | 匿名可得 |
| 10000 | 原画 | 匿名可得，匿名可获得的天花板 |
| 15000 | 2K（含 2K/HDR） | 需房间支持，通常需登录 |
| 20000 | 4K / 4K/HDR | 需房间支持，通常需登录 |
| 30000 | 杜比 | 通常需大会员 |

### 实测可获取情况（3 个直播间一致）

- 历史样本中匿名 accept_qn 均为 `[10000, 400, 250]`；该列表不保证这些档位当前均能实际获取
- 编码分层：avc / hevc 覆盖全档位；**av1 只出现在蓝光(400)与以上档位**
- 协议/封装：`http_stream`(FLV) + `http_hls`(TS 与 fMP4)
- 每路 URL 的 extra 带签名参数：`origin_bitrate`（源码率）、`expected_qn` 等
- 部分房间/账号对高画质收紧：即使 accept_qn 包含 10000，实际返回的流也可能低于原画
- 2K/4K/杜比为增值档位，通常需要登录或大会员

### 项目选项

`30000`、`20000`、`15000`、`10000`、`400`、`250`、`150`、`80`，以及自动选择 `best`。高画质是否可用由平台和账号权限决定。直播间 650 在 2026-10-28 的匿名测试中，接口虽列出原画，但实际返回的最高档为蓝光 400（AV1/HLS）。

当平台返回 AV1 流时，项目不允许将已知 AV1 流直接保存为 TS：实测 FFmpeg 会将其写为无法识别的视频数据。应选择 MKV 或 MP4 保存格式。

---

## 三、快手 (live.kuaishou.com)

数据来源：在播直播间页面 `__INITIAL_STATE__.liveroom.playList[].liveStream.playUrls`（匿名可得），以及 `live_api/home/list` 48 个房间快照；官方 JS `purecommon.js` 文案映射。

### 档位阶梯（adaptationSet.representation）

| qualityType | level | 码率 | UI 名称 | 说明 |
|---|---|---|---|---|
| `STANDARD` | 30 | 1000 kbps | 高清 | 最低视频档 |
| `HIGH` | 50 | 2000 kbps | 超清 | **`defaultSelect=true`，官方默认档** |
| `SUPER` | 70 | 4000 kbps | 蓝光 4M | |
| `BLUE_RAY` | 130 | 8000 kbps | 蓝光 质臻 / 蓝光Plus / 蓝光 8M | 名称随房间变化 |
| `WQHD_2K` | 250 | 16000 kbps | 2K | 仅部分游戏房间 |

官方 JS 另有 `uhd4k`(4K) 枚举，快照中未见实例。

### 档位集合按房间变化（实测 4 种形态）

- `[2000]` — 超清房间（仅 1 档）
- `[2000, 8000]` — 蓝光质臻房间（无 1000/4000）
- `[1000, 2000, 4000, 8000]` — 标准 4 档
- `[1000, 2000, 4000, 8000, 16000]` — 2K 房间（5 档）

### 特点

- 全部档位匿名可得（在播房间实测拿到完整 4 档 + 签名流地址）
- 双编码：`playUrls` 按 codec 分键（`h264` / `hevc`），各有一套完整档位；项目与官方播放器均优先 h264
- 协议：主推 **FLV**（`tx-origin.pull.yximgs.com/gifshow/{id}_GameAvc{档位}.flv`，带 txSecret/txTime 签名）；liveStream 另有 `hlsPlayUrl` 字段提供 HLS
- representation 字段：`bitrate / qualityType / level / name / shortName / defaultSelect / hidden / enableAdaptive / url`，播放器 JS 会拼接 `backupUrl` 备用线路
- 平台已无低于 1000kbps 的"标清"档位，SD/LD 会降级到最低档
- 个别主播/房间设置 `needLoginToWatchHD`（页面 config 开关），高画质需登录

### 项目选项

`WQHD_2K`、`BLUE_RAY`、`SUPER`、`HIGH`、`STANDARD`，以及自动选择 `best`。旧版快手标清和流畅此前会回退到最低档，迁移为 `STANDARD`。
