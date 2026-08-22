# 各平台直播流画质档位说明

本文档基于 2026-08-22 对抖音、哔哩哔哩、快手三个平台 Web 直播间的实测抓包数据（匿名访问）。

## 通用说明

- 三个平台均为五档 UI：`OD`(原画) / `UHD`(超清) / `HD`(高清) / `SD`(标清) / `LD`(流畅)
- 项目默认画质为 `OD`，即各平台匿名可获得的最高档
- 实测全部为匿名访问，未登录也能拿到各档位流地址

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

### 项目映射

```python
OD  → origin（max-bitrate 选取）
UHD → hd / uhd / fhd
HD  → sd / hd
SD  → ld / sd
LD  → md / ld
```

---

## 二、哔哩哔哩 (live.bilibili.com)

数据来源：`xlive/web-room/v2/index/getRoomPlayInfo` 响应中的 `g_qn_desc` 与 `stream`，3 个直播间实测。

### 档位面板（g_qn_desc，服务器下发）

| qn | UI 名称 | 说明 |
|---|---|---|
| 80 | 极速 | 最低档 |
| 150 | 流畅 | |
| 250 | 高清 | qn=0 时服务器的默认返回 |
| 400 | 蓝光 | 匿名可得 |
| 10000 | 原画 | 匿名可得，匿名可获得的天花板 |
| 15000 | 2K（含 2K/HDR） | 需房间支持，通常需登录 |
| 20000 | 4K / 4K/HDR | 需房间支持，通常需登录 |
| 30000 | 杜比 | 通常需大会员 |

### 实测可获取情况（3 个直播间一致）

- 匿名 accept_qn 均为 `[10000, 400, 250]`，即匿名可拿**原画/蓝光/高清**三档
- 编码分层：avc / hevc 覆盖全档位；**av1 只出现在蓝光(400)与以上档位**
- 协议/封装：`http_stream`(FLV) + `http_hls`(TS 与 fMP4)
- 每路 URL 的 extra 带签名参数：`origin_bitrate`（源码率）、`expected_qn` 等
- 部分房间/账号对高画质收紧：accept_qn 不含 10000，只能到 400 或 250
- 2K/4K/杜比为增值档位，项目不请求（需要登录/大会员，匿名与普通用户拿不到）

### 项目映射

```python
_QUALITY_QN = {
    "OD": 10000,   # 原画
    "UHD": 400,    # 蓝光
    "HD": 250,     # 高清
    "SD": 150,     # 流畅
    "LD": 80,      # 极速
}
```

> 请求的档位不可用时，服务器会自动降级返回邻近档位，项目按「qn 接近度 + 编码偏好（avc > hevc > av1）」选择，不会报错。

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

### 项目映射

```python
_QUALITY_BITRATE = {"OD": 10**18, "UHD": 2000, "HD": 1000, "SD": 800, "LD": 600}
```

选择逻辑：取 `bitrate ≤ 上限` 的最高档；无匹配时取最低档。

| UI 档 | 实际选到 | 对应平台档 |
|---|---|---|
| OD | 8000 / 16000 | 蓝光质臻 / 2K（房间最高） |
| UHD | 2000 | 超清 |
| HD | 1000 | 高清 |
| SD / LD | 1000（兜底） | 最低档（平台已无标清） |

---

## 汇总

| 平台 | OD 对应 | 匿名最高可达 | 档位齐全度 |
|---|---|---|---|
| 抖音 | `origin` 原画 | 原画（fps/码率最高） | 5 档 + md/ao 附加 |
| 哔哩哔哩 | qn=10000 原画 | 原画（2K/4K/杜比需会员） | 5 档（80-10000） |
| 快手 | 最高 bitrate 档 | 蓝光质臻 / 2K | 4-5 档 |

- 三平台默认 `OD` 录制的均为该房间**匿名可获得的最高画质**
- 抖音映射与平台完全一致；B 站映射已于 2026-08-22 修正为 `UHD→400`（此前 UHD 实际录到的是高清 250）；快手阈值与现行档位对齐