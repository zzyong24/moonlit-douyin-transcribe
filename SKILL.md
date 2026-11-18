---
name: moonlit-douyin-transcribe
description: "Use when the user provides a Douyin share command, short link, work page URL, or video ID and asks to download a video or extract its speech as text or subtitles（用户粘贴抖音分享口令/短链/作品链接，说“把这条抖音转成文字”“提取文案”“扒口播稿”“下载抖音视频”“抓无水印”“出字幕”“回流成片实录文案”时使用）。转写只用 API：中文走 Redfox 视频播放链接提取文案（约 0.06 元/条），外语走 Groq（超 25MB 自动切段合并）→Deepgram；下载 Redfox 优先，匿名链路备选，不用 Cookie。自动识别并拦截图文/图集作品（退出码 3）；原始逐字稿与术语纠错稿物理分离。Supports Douyin single public video works only; not for Bilibili, YouTube, or Xiaohongshu, and not for batch crawling or reposting."
---

# 月明·抖音转写

抖音单条公开视频抓取 + API 转写一体化。**四条核心约束**：

1. **职责边界清晰**：只抓单条公开抖音视频并产出原始逐字稿、毫秒级 SRT、元数据（按需保留音频/视频）及可选独立术语纠错层。不做多博主筛选、结构拆解、评论抓取或重写润色。
2. **转写只用 API，不装本地模型**（团队口径 2026-10-20）：中文走 Redfox「视频播放链接提取文案」；外语走 Groq 免费层（超 24MB 安全线自动按时间切段转写并偏移合并时间轴）→ Deepgram 赠送额度。
3. **下载 Redfox 优先，图文误入即刻熔断**：Redfox 抖音下载接口优先拿播放地址，失败退回匿名链路（iesdouyin 页面 → 匿名移动端 feed → 经授权 Ego 补证（CLI 不自动启动其他浏览器））。不读取或保存用户 Cookie。任一节点识别为图文/图集（`/note/`、`images` 非空、`aweme_type=68`）立即停止后续兜底与付费转写（退出码 `3`），绝不把图集背景音乐当口播送去转写。
4. **原始逐字稿不可篡改**：`{video_id}.txt` 与 `{video_id}.srt` 固定保留 ASR 原始字词（仅允许显式记录的 `opencc t2s` 繁简字符转换）；专名/同音术语校对必须另存为独立的 `{video_id}.corrected.md` 并附变更对照表，禁止静默覆写原话。

## 浏览器补证边界

匿名接口均失败时保留元数据缺口。浏览器自动化只用当前 `ego-browser`，先读 Skill 并运行 `ego-browser skill`；本 CLI 默认 `--browser-fallback off`，旧 `auto` 参数也不允许启动临时 Chrome。Ego 现场补证未完成时不声称下载/转写成功。

## 何时使用本 skill

- 用户粘贴整段抖音客户端分享口令文本（如 `"0.82 08/16 复制打开抖音，看看【…】 https://v.douyin.com/..."`）、短链、`/video/{id}`、`?modal_id={id}` 或纯数字 `video_id`
- 用户说："把这条抖音转成文字""提取文案""扒一下口播稿""下载这个抖音视频""抓无水印""转录""出字幕""抓一下这条已发抖音的成片实录文案"

## 何时**不要**使用本 skill

- 用户想抓 B 站、YouTube、小红书、公众号或 PDF → 使用对应平台工具
- 已知是抖音图文/图集（`https://www.douyin.com/note/{id}`）且只需读图内文字 → 本脚本会以退出码 `3` 拦截并返回标题与图片数，不做图片 OCR
- 用户只要看链接不需要下载或转写，或要求批量爬主页、搬运重发 → 不调用本 skill

## 费用与余额守卫（调用前自动核查）

| 步骤 | 后端 | 单价 |
|---|---|---|
| 取播放地址 | Redfox `videoDownload/douyin` | 0.06 元/次（折扣价 0.03） |
| 中文转写 | Redfox `audioTextExtract/submit/videoUrl`，结果查询免费 | 0.06 元/条（折扣价 0.03） |
| 外语转写 | Groq 免费层（超限自动切段）→ Deepgram 赠送额度 | 免费额度内 0 |
| 备选下载 | 匿名链路（iesdouyin / mobile-feed / 临时浏览器） | 0 |

一条中文抖音约 0.12 元，属团队已批准的 Redfox 低价档，无需逐条审批。用 `--quote-only` 可离线查看预计费用。脚本在发起付费请求前自动调免费余额接口核查：若剩余积分不足本条预计消耗、已知总额且剩余 `≤10%`（可通过 `REDFOX_TOTAL_POINTS` 指定总额基数），或未知总额时低于安全底线（`REDFOX_MIN_POINTS`，默认 `10` 积分 = `1.0` 元），立即阻断请求并报剩余积分与充值提醒。平台专用的「视频提文案」0.6 元/条不在本脚本中，若需启用须逐条先经月明批准。

## 三种模式（用 `--mode` 指定）

| mode | 保留产物 | 典型场景 |
|---|---|---|
| `text`（默认） | `{id}.txt` + `{id}.srt` + `metadata.json`（+ 可选 `{id}.corrected.md`） | 提取逐字稿与时间轴 |
| `audio` | 上述文本产物 + `{id}.mp3` | 需保留 16kHz 单声道音频供后续复听或工具处理 |
| `video` | 上述文本产物 + `{id}.mp3` + `{id}.mp4` | 需保留完整视频抽帧或核对画面文字 |

**默认行为**：`mode=text`, `asr=auto`（中文 Redfox）, `download-via=redfox`, `lang=zh`, `out-dir=./douyin-output/{YYYY-MM-DD}/{video_id}/`。需要抽音或下载时默认写入 `{out_dir}/.cache/`，若 ASR 网络瞬断失败会保留本地已拉取音视频，重跑时自动复用缓存免重下（传 `--no-cache` 可强制重拉）。

## 调用方式

只需 Python 3 标准库和系统 `ffmpeg`（超长音频切段时可选用 `ffprobe` 精算分片时长）；`opencc` 可选（装了才做繁简字符转换）。Key 只从环境变量读取：`REDFOX_API_KEY`、`GROQ_API_KEY`、`DEEPGRAM_API_KEY`，由凭据库注入，严禁写进文件或打印。

```bash
python scripts/douyin_transcribe.py \
  "<抖音口令文本或链接>" \
  --mode <text|audio|video> \
  --out-dir <输出根目录>
```

浏览器兜底需 PATH 中有 Node.js 22+ 与 Chrome/Chromium（可用 `--chrome-path` 或 `CHROME_PATH` 指定）；前两步匿名链路不依赖浏览器。

## 执行与交互流程

1. **解析与清洗输入**：直接把用户粘贴的整段口令或链接传给脚本，脚本自动剥离口令前后缀与末尾粘连标点。**注意：分享口令中的 `08/16` 等数字是客户端口令码，绝不能当作视频发布日期**。
2. **图文误入快速识别**：若脚本退出码为 `3`（`image_note_not_supported`），直接向用户回报该作品为图文/图集（含作品 ID、标题文案与图片张数），说明无口播音轨、未产生付费转写消耗，不强行重试。
3. **媒体完整性与缓存续跑**：下载 MP4 遇分段 `HTTP 206` 时，脚本强制按 `Content-Range` 总字节续传核验，拒绝把单段 `Content-Length` 当全片；失败或残缺文件绝不送入转写或视觉分析。若首次因网络波动或 Key 未注入导致转写中断，重跑同一命令会自动复用 `{out_dir}/.cache/` 下已抽好的音频。
4. **原始稿保护与术语纠错分离**：
   - 有确定术语表时传 `--glossary <json|tsv>`，脚本在不改动 `{id}.txt`/`{id}.srt` 的前提下生成 `{id}.corrected.md`（含校对正文 + `ASR 原词 → 校对词 → 替换次数 → 依据` 对照表）。
   - 若执行后由 Agent 结合画面/上下文人工核对专名（如 `Agent`、`Bitwarden`、`Skill` 同音误识），同样**严禁改写 `{id}.txt` 与 `{id}.srt`**，必须按相同结构另写 `{id}.corrected.md` 并注明校对依据（画面文字 / 母稿对照 / 上下文推断）。
5. **结果回报**：输出原始全文（或明确标注层级的纠错阅读稿 + 变更表）、元信息（作者、时长、`video_id`、公开链接）、实际落盘路径及耗时。

## 关键参数

| 参数 | 默认 | 说明 |
|---|---|---|
| `url` (位置参数) | 必填 | 抖音分享口令整段文本 / 短链 / `/video/` 或 `/note/` 链接 / 纯数字 `video_id` |
| `--mode` | `text` | `text` / `audio` / `video` |
| `--asr` | `auto` | `auto`（中文 Redfox，外语 Groq→Deepgram）/ `redfox` / `groq` / `deepgram` |
| `--download-via` | `redfox` | `redfox`（失败自动退匿名链路）/ `anonymous`（只用免费匿名链路） |
| `--glossary` | 无 | 可选术语纠错字典（JSON 或 TSV），独立产出 `{id}.corrected.md` |
| `--no-cache` | off | 忽略 `{out-dir}` 下已缓存的 `.mp3`/`.mp4`，强制重新拉取 |
| `--quote-only` | off | 只打印本条预计费用 JSON，不联网 |
| `--out-dir` | `./douyin-output` | 输出根目录；最终路径 = `{out-dir}/{YYYY-MM-DD}/{video_id}/` |
| `--lang` | `zh` | 转写语言；外语填 `en`/`ja` 等；传空字符串 `""` 让后端自动检测 |
| `--dry-run` | off | 只解析元数据（图文仍返回退出码 `3`），不下载不转写 |
| `--keep-temp` | off | 成功后仍保留 `.cache/` 中间文件 |
| `--browser-fallback` | `auto` | 匿名链路前两步均失败时启用经授权 Ego 补证（CLI 不自动启动其他浏览器）；传 `off` 禁用 |
| `--chrome-path` | 自动查找 | Chrome/Chromium 可执行文件路径；也支持环境变量 `CHROME_PATH` |

## 产物结构与下游交接契约

### 1. `metadata.json`（含分层登记与下游 `handoff`）
```json
{
  "video_id": "7647796267023650084",
  "public_url": "https://www.douyin.com/video/7647796267023650084",
  "title": "运营科普：...",
  "author": "示例账号",
  "duration_sec": 502.3,
  "play_addr_src": "play_addr",
  "fetch_chain": "iesdouyin.com/router-data",
  "asr": {
    "backend": "redfox",
    "language": "zh",
    "segments": 277,
    "raw_chars": 2803,
    "normalized_chars": 2803,
    "opencc_t2s": false,
    "cost_yuan": 0.06,
    "task_id": "rf_task_xxx"
  },
  "transcript_layers": {
    "raw_txt": ".../7647796267023650084.txt",
    "raw_srt": ".../7647796267023650084.srt",
    "corrected_md": null,
    "applied_corrections_count": 0
  },
  "handoff": {
    "benchmark_research": {
      "platform": "douyin",
      "work_id": "7647796267023650084",
      "account": "示例账号",
      "public_url": "https://www.douyin.com/video/7647796267023650084",
      "captured_at": "2026-11-18",
      "evidence_path": ".../7647796267023650084.txt",
      "content": "text_available",
      "analysis": "unreviewed",
      "visual": "not_viewed",
      "job": { "status": "succeeded", "backend": "redfox", "task_id": "rf_task_xxx" }
    },
    "creator_return_flow": {
      "recommended_target": "works/<作品目录>/notes/成片实录文案.md",
      "raw_transcript_path": ".../7647796267023650084.txt",
      "corrected_transcript_path": null,
      "authority_boundary": "仅供发布后成片文案回流对照原稿与批注日学；不得冒充或覆盖由已登记同版成片字幕生成的 content/video/成片逐字稿-{version}.md (finalTranscript)"
    }
  },
  "saved_files": { "txt": "...", "srt": "...", "metadata": "..." },
  "fetched_at": "2026-11-18T00:15:00"
}
```

### 2. 下游交接硬规则
- **交接给 `moonlit-benchmark-research`（外部对标研究）**：直接读取 `metadata.json` 的 `handoff.benchmark_research` 写入研究台账或 `source.json`。转写成功仅代表 `content = "text_available"`、`analysis = "unreviewed"`；**即使使用 `--mode video` 下载了 `.mp4`，未实际抽帧审看前 `visual` 必须保持 `"not_viewed"`**，禁止把文字转写完成升级为视觉已审。引用专名或提示词前须结合画面或 `corrected.md` 核实，不把原始 ASR 同音错词直接当原作者术语。
- **交接给「本人已发成片文案回流」（内部复盘）**：按工作区《成片文案回流铁律》，将本工具提取的实录信息整理落盘至对应作品目录 `works/<作品目录>/notes/成片实录文案.md`（保留作品 ID、抓取时间、`raw_asr` 原文及与母稿差异批注），供国内运营对照母稿复盘。**严禁用本工具的 ASR 识别稿冒充或覆盖 `content/video/成片逐字稿-{version}.md`（`work.json.artifacts.finalTranscript` 仅能由已登记同版成片字幕生成）**。

## 已知限制 & 边界

- **图文/图集（退出码 `3`）、直播、私密或已删除视频** → 图集明确返回 `image_note_not_supported` 并熔断；直播、私密与已删除作品匿名链路不借用登录态，准确报错退出。
- **超长视频（>1 小时 / 音频 >24MB）** → 中文 Redfox 最长轮询 10 分钟，超时报错中保留 `taskId` 供免费查询接口续查；外语 Groq 超 24MB 自动通过 `ffmpeg` 按 10 分钟分片转写并累加毫秒偏移生成连续 `.srt`，单分片遇 `429/5xx` 自动退避重试，仍失败则回退 Deepgram。
- **多说话人 / 嘈杂环境 / 中英混杂术语** → API 对重叠人声与英文缩写易出同音字，保留原始 `.txt`/`.srt` 不改，通过 `--glossary` 或下游独立 `.corrected.md` 做可追溯校对。

## 调试与自检命令

```bash
# 离线看预计费用
python scripts/douyin_transcribe.py --quote-only "0.82 08/16 复制打开抖音 https://v.douyin.com/xxxxx/"

# 只解析元数据并验证是否为视频（免费匿名链路，图集返回退出码 3）
python scripts/douyin_transcribe.py --dry-run "https://v.douyin.com/xxxxx/"

# 带术语表转写，同时保留原始 .txt 与独立 .corrected.md
python scripts/douyin_transcribe.py "https://v.douyin.com/xxxxx/" --glossary glossary.json

# 运行离线单元测试（覆盖 HTTP 206 续传、口令解析、图文熔断、缓存复用、超限切段、纠错分离与交接字段）
python3 -m unittest discover -s tests -v
```
