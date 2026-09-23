---
name: douyin-transcribe
description: Use when the user provides a Douyin share link or video ID and asks to download a video or extract its speech as text or subtitles. Supports Douyin only; not for Bilibili, YouTube, or Xiaohongshu.
---

# Douyin Transcribe Skill

抖音视频抓取 + 转写一体化。**两条核心约束**：

1. **只做两件事**：抓视频、转文字。其他都不做。
2. **依次尝试 iesdouyin 页面、匿名移动端 feed、临时无头浏览器 detail**；不依赖 yt-dlp，不读取或保存用户 Cookie。

## 何时使用本 skill

- 用户粘贴 `https://v.douyin.com/...` 短链或 `https://www.douyin.com/video/{id}` 完整链接
- 用户口头描述："把这条抖音转写一下"/"提取文字"/"下载视频"/"抓无水印"/"转录"
- 用户给一个纯数字 video_id

## 何时**不要**使用本 skill

- 用户想抓 B 站、YouTube、小红书、公众号或 PDF → 使用相应平台工具或 Skill
- 用户只想看链接、不需要下载转写 → 不调用本 skill

## 三种模式（用 `--mode` 指定）

| mode | 保留产物 | 典型场景 |
|---|---|---|
| `text`（默认） | `.txt` + `metadata.json` | 只想读文字稿 |
| `audio` | `.txt` + `.mp3` + `metadata.json` | 还要把音频喂给其他工具 |
| `video` | `.txt` + `.mp3` + `.mp4` + `metadata.json` | 还需要原始画面 |

**默认行为**：mode=text, model-size=small, lang=zh, out-dir=./douyin-output/{YYYY-MM-DD}/{video_id}/

## 调用方式

调用本 skill 时，使用安装了 `faster-whisper` 与 `opencc` 的 Python 环境。通过仓库根目录运行示例：

```bash
python scripts/douyin_transcribe.py \
  "<抖音链接>" \
  --mode <text|audio|video> \
  --out-dir <输出根目录>
```

缺少 Python 依赖时，安装 `faster-whisper`、`opencc-python-reimplemented` 和系统 `ffmpeg`。浏览器兜底需要 PATH 中有 Node.js 22+ 与 Chrome/Chromium；默认自动查找，也可用 `--chrome-path` 指定。移动端 feed 元数据回退不依赖浏览器。

## 用户交互流程

1. **检查输入** → 接受抖音短链、完整链或纯数字 ID；不确定平台时再澄清
2. **选择模式** → 默认 `text`；用户要保留音频或视频时切换模式
3. **选择输出目录** → 默认 `./douyin-output/`，用户指定时使用其路径
4. **执行** → 等脚本退出 → 检查 stderr 的日志
5. **回报**：
   - 打印全文 + 元信息（作者、时长、video_id）
   - 列出实际产物路径
   - 标注耗时

## 关键参数

| 参数 | 默认 | 说明 |
|---|---|---|
| `url` (位置参数) | 必填 | 抖音分享链接 / 完整链 / 纯数字 video_id |
| `--mode` | `text` | `text` / `audio` / `video` |
| `--model-size` | `small` | `tiny` / `base` / `small` / `medium` / `large-v2` / `large-v3` |
| `--out-dir` | `./douyin-output` | 输出根目录；最终路径 = `{out-dir}/{YYYY-MM-DD}/{video_id}/` |
| `--lang` | `zh` | 转写语言；传空字符串 `""` 让模型自动检测 |
| `--dry-run` | off | 只解析元数据，不下载不转写（调试用） |
| `--keep-temp` | off | 保留中间临时文件（一般不需要） |
| `--browser-fallback` | `auto` | iesdouyin 和匿名移动端 feed 均失败时启用临时浏览器 detail；传 `off` 可禁用 |
| `--chrome-path` | 自动查找 | Chrome/Chromium 可执行文件路径；也支持环境变量 `CHROME_PATH` |

## 链路细节（实现已封装在脚本里）

```
抖音短链
  └─→ 跟随重定向提取 video_id
       └─→ iesdouyin 分享页解析 _ROUTER_DATA
            ├─ 成功 → video.play_addr / download_addr
            └─ 页面结构失效 → 匿名移动端 feed 主备节点
                 ├─ api5-normal-c-hl.amemv.com/aweme/v1/feed/
                 └─ aweme.snssdk.com/aweme/v1/feed/
                      └─ 精确匹配 aweme_id 后读取 play_addr / download_addr
                           └─ 两个节点均失败 → 临时无头浏览器 detail 兜底
                                └─→ 获取播放地址并交给 ffmpeg
                                     └─→ faster-whisper → opencc t2s
                                          └─→ 写 .txt + metadata.json
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
  "direct_url": "[抓取返回的临时播放地址]",
  "asr": {
    "model": "small",
    "language": "zh",
    "language_probability": 1.0,
    "duration_sec": 502.3,
    "segments": 277,
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
- 转写: faster-whisper small (lang=zh, prob=1)
- 归一化: opencc t2s

---

<正文>
```

## 已知限制 & 边界

- **直播、图集、私密视频、已删除视频** → 解析必然失败，报错清晰
- **超长视频（>1 小时）** → small 模型需要 ~10 分钟；建议先切分或用 `tiny` 跑一遍概览
- **多说话人 / 嘈杂环境** → small 模型对重叠人声识别率有限；不输出时间戳（如果以后需要，加 `--diarize` 或 `--timestamps`）
- **iesdouyin 页面结构失效** → 先试匿名移动端 feed 主备节点，再用临时浏览器 detail 兜底；该浏览器使用临时目录，退出后删除，不读取用户浏览器资料
- **没有 Node.js 或 Chrome/Chromium** → 两条匿名元数据链路仍可用；前两条链路均失败时才报告浏览器依赖缺失
- **私密、已删除或需要登录授权的作品** → 临时匿名浏览器不会借用用户登录态，仍会准确失败

## 调试技巧

```bash
# 只看元数据，不下载
python scripts/douyin_transcribe.py --dry-run "https://v.douyin.com/xxxxx/"

# 跑小模型快速验证
python scripts/douyin_transcribe.py --model-size tiny "https://v.douyin.com/xxxxx/"

# 让模型自动检测语言
python scripts/douyin_transcribe.py --lang "" "https://v.douyin.com/xxxxx/"

# 禁用浏览器兜底，专门诊断 iesdouyin 匿名链
python scripts/douyin_transcribe.py --browser-fallback off --dry-run "https://v.douyin.com/xxxxx/"
```
