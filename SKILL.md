---
name: moonlit-douyin-transcribe
description: "Use when the user provides a Douyin share link or video ID and asks to download a video or extract its speech as text or subtitles（用户说“把这条抖音转成文字”“提取文案”“扒口播稿”“下载抖音视频”“抓无水印”“出字幕”时使用）。转写只用 API：中文走 Redfox 视频播放链接提取文案（约 0.06 元/条），外语走自带字幕→Groq→Deepgram；下载 Redfox 优先，匿名链路备选，不用 Cookie。Supports Douyin only; not for Bilibili, YouTube, or Xiaohongshu, and not for batch crawling or reposting."
---

# 月明·抖音转写

抖音视频抓取 + 转写一体化。**三条核心约束**：

1. **只做两件事**：抓视频、转文字（附带 SRT 时间轴）。其他都不做。
2. **转写只用 API，不装本地模型**（团队口径 2026-10-20）：中文走 Redfox「视频播放链接提取文案」；外语依次用平台自带字幕 → Groq 免费层 → Deepgram 赠送额度。
3. **下载 Redfox 优先**：Redfox 抖音下载接口拿播放地址；失败时才用匿名备选链路（iesdouyin 页面 → 匿名移动端 feed → 临时无头浏览器 detail）。不读取或保存用户 Cookie，只下单条公开作品，只用于拆解学习。

## 何时使用本 skill

- 用户粘贴 `https://v.douyin.com/...` 短链或 `https://www.douyin.com/video/{id}` 完整链接
- 用户说："把这条抖音转成文字""提取文案""扒一下口播稿""下载这个抖音视频""抓无水印""转录""出字幕"
- 用户给一个纯数字 video_id

## 何时**不要**使用本 skill

- 用户想抓 B 站、YouTube、小红书、公众号或 PDF → 使用相应平台工具（素材工具的 yt-dlp / Redfox 对应平台接口）
- 用户只想看链接、不需要下载转写 → 不调用本 skill
- 批量爬主页、搬运或二次发布别人的视频 → 不做

## 费用（调用前先看）

| 步骤 | 后端 | 单价 |
|---|---|---|
| 取播放地址 | Redfox `videoDownload/douyin` | 0.06 元/次（折扣价 0.03） |
| 中文转写 | Redfox `audioTextExtract/submit/videoUrl`，结果查询免费 | 0.06 元/条（折扣价 0.03） |
| 外语转写 | Groq 免费层 → Deepgram 赠送额度 | 免费额度内 0 |
| 备选下载 | 匿名链路 | 0 |

一条中文抖音约 0.12 元，属于团队已批准的 Redfox 低价档，不用逐条审批。用 `--quote-only` 先看本条预计费用。调用前查 Redfox 余额（免费接口），剩余 ≤10% 时停用并报充值金额。平台专用的「视频提文案」0.6 元/条不在本脚本里，要用时每条先问月明。

## 三种模式（用 `--mode` 指定）

| mode | 保留产物 | 典型场景 |
|---|---|---|
| `text`（默认） | `.txt` + `metadata.json` | 只想读文字稿 |
| `audio` | `.txt` + `.mp3` + `metadata.json` | 还要把音频喂给其他工具 |
| `video` | `.txt` + `.mp3` + `.mp4` + `metadata.json` | 还需要原始画面 |

**默认行为**：mode=text, asr=auto（中文 Redfox）, download-via=redfox, lang=zh, out-dir=./douyin-output/{YYYY-MM-DD}/{video_id}/；产物多一个 `{video_id}.srt`（毫秒时间轴）。

## 调用方式

只需 Python 3 标准库和系统 `ffmpeg`；`opencc` 可选（装了才做繁简归一化）。Key 只从环境变量读取：`REDFOX_API_KEY`（中文与下载）、`GROQ_API_KEY`、`DEEPGRAM_API_KEY`（外语），由团队凭据库注入，不写进文件。

```bash
python scripts/douyin_transcribe.py \
  "<抖音链接>" \
  --mode <text|audio|video> \
  --out-dir <输出根目录>
```

浏览器兜底需要 PATH 中有 Node.js 22+ 与 Chrome/Chromium；默认自动查找，也可用 `--chrome-path` 指定。移动端 feed 元数据回退不依赖浏览器。

## 用户交互流程

1. **检查输入** → 接受抖音短链、完整链或纯数字 ID；不确定平台时再澄清
2. **选择模式** → 默认 `text`；用户要保留音频或视频时切换模式
3. **选择输出目录** → 默认 `./douyin-output/`，用户指定时使用其路径
4. **执行** → 等脚本退出 → 检查 stderr 的日志。媒体返回 HTTP 206 时必须按 Content-Range 总长度补齐并核验字节；不能把一段响应的 Content-Length 当成全片完成。失败文件不能用于转写或画面分析。
5. **回报**：
   - 打印全文 + 元信息（作者、时长、video_id）
   - 列出实际产物路径
   - 标注耗时

## 关键参数

| 参数 | 默认 | 说明 |
|---|---|---|
| `url` (位置参数) | 必填 | 抖音分享链接 / 完整链 / 纯数字 video_id |
| `--mode` | `text` | `text` / `audio` / `video` |
| `--asr` | `auto` | `auto`（中文 Redfox，外语 Groq→Deepgram）/ `redfox` / `groq` / `deepgram` |
| `--download-via` | `redfox` | `redfox`（失败自动退匿名链路）/ `anonymous`（只用免费匿名链路） |
| `--quote-only` | off | 只打印本条预计费用，不联网 |
| `--out-dir` | `./douyin-output` | 输出根目录；最终路径 = `{out-dir}/{YYYY-MM-DD}/{video_id}/` |
| `--lang` | `zh` | 转写语言；外语填 `en`/`ja` 等；传空字符串 `""` 让后端自动检测 |
| `--dry-run` | off | 只用匿名链路解析元数据，不下载不转写（免费） |
| `--keep-temp` | off | 保留中间临时文件 |
| `--browser-fallback` | `auto` | 匿名链路前两步失败时启用临时浏览器 detail；传 `off` 可禁用 |
| `--chrome-path` | 自动查找 | Chrome/Chromium 可执行文件路径；也支持环境变量 `CHROME_PATH` |

## 链路细节（实现在 scripts/douyin_transcribe.py 与 scripts/asr_backends.py）

```
抖音短链 / 完整链 / video_id
  └─→ Redfox videoDownload/douyin → videoUrl（0.06 元）
       └─ 失败 → 匿名备选：iesdouyin _ROUTER_DATA → 匿名移动端 feed → 临时无头浏览器 detail
            └─→ 中文：Redfox audioTextExtract/submit/videoUrl（直接传播放地址，不在本地抽音）
                 └─→ 轮询 result（免费）→ text + stampSents（毫秒）
                 外语：ffmpeg 抽 16k 单声道 mp3 → Groq whisper（≤25MB）→ 失败再 Deepgram（mip_opt_out=true）
                      └─→ 写 .txt + .srt + metadata.json（不落盘带签名的临时播放地址）
```

## 产物格式

### `metadata.json`
```json
{
  "video_id": "7647796267023650084",
  "title": "运营科普：...",
  "author": "示例账号",
  "duration_sec": 502.3,
  "play_addr_src": "play_addr",
  "fetch_chain": "iesdouyin.com/router-data",
  "asr": {
    "backend": "redfox",
    "language": "zh",
    "segments": 277,
    "cost_yuan": 0.06,
    "raw_chars": 2803,
    "normalized_chars": 2803
  },
  "saved_files": {...},
  "fetched_at": "2026-06-06T01:09:55"
}
```

### `{video_id}.txt`
```markdown
# <视频标题>

- 作者: <作者昵称>
- video_id: <id>
- 时长: <秒>s
- 抓取时间: <ISO8601>
- 链路: iesdouyin.com / play_addr → play
- 转写: redfox API (lang=zh, 277 句)
- 归一化: opencc t2s（未装 opencc 时写“未做”）

---

<正文>
```

## 已知限制 & 边界

- **直播、图集、私密视频、已删除视频** → 解析必然失败，报错清晰
- **超长视频（>1 小时）** → Redfox 处理时间变长，脚本最多等 10 分钟，超时会给出 taskId，可用免费查询接口续查；外语走 Groq 时单文件 ≤25MB，超出先切段
- **多说话人 / 嘈杂环境** → API 对重叠人声识别率有限；时间戳按后端返回的分句写进 .srt
- **iesdouyin 页面结构失效** → 先试匿名移动端 feed 主备节点，再用临时浏览器 detail 兜底；该浏览器使用临时目录，退出后删除，不读取用户浏览器资料
- **没有 Node.js 或 Chrome/Chromium** → 两条匿名元数据链路仍可用；前两条链路均失败时才报告浏览器依赖缺失
- **私密、已删除或需要登录授权的作品** → 临时匿名浏览器不会借用用户登录态，仍会准确失败

## 调试技巧

```bash
# 看本条预计费用（不联网）
python scripts/douyin_transcribe.py --quote-only "https://v.douyin.com/xxxxx/"

# 只看元数据，不下载（免费匿名链路）
python scripts/douyin_transcribe.py --dry-run "https://v.douyin.com/xxxxx/"

# 不花 Redfox 下载费，只用匿名链路取地址
python scripts/douyin_transcribe.py --download-via anonymous "https://v.douyin.com/xxxxx/"

# 外语视频：Groq → Deepgram
python scripts/douyin_transcribe.py --lang en "https://v.douyin.com/xxxxx/"
```
