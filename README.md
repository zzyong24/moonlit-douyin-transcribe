<p align="center">
  <img src="assets/readme/hero.png" width="100%" alt="月明·抖音转写 Skill 品牌图">
</p>

<h1 align="center">月明·抖音转写</h1>

<p align="center">下载抖音视频，并把语音转成文字。</p>

<p align="center">
  <a href="https://github.com/zzyong24/moonlit-douyin-transcribe/blob/main/SKILL.md">Skill 使用说明</a> ·
  <a href="https://github.com/zzyong24/moonlit-douyin-transcribe/tree/main/scripts">脚本</a> ·
  <a href="https://github.com/zzyong24/moonlit-douyin-transcribe/tree/main/evals">评估用例</a>
</p>

---

这个 Skill 接收抖音分享链接或作品 ID，抓取视频并用 API 转写（中文 Redfox，外语 Groq→Deepgram；不装本地模型）。支持只输出文字、保留音频，或保留音频和视频。

## 安装

将仓库克隆到你的 Agent 支持的 Skill 目录。例如 Codex：

```bash
git clone https://github.com/zzyong24/moonlit-douyin-transcribe.git ~/.codex/skills/moonlit-douyin-transcribe
```

准备 Python 环境并安装依赖：

```bash
cd ~/.codex/skills/moonlit-douyin-transcribe
python3 -m venv .venv
source .venv/bin/activate
# 只需 Python 3 标准库 + ffmpeg；opencc 可选：python -m pip install opencc-python-reimplemented
# 环境变量：REDFOX_API_KEY（中文/下载）、GROQ_API_KEY、DEEPGRAM_API_KEY（外语）
```

系统还需安装 `ffmpeg`。若需要最后的浏览器兜底，还需 Node.js 22+ 与 Chrome 或 Chromium。抖音匿名移动端 feed 回退不依赖浏览器。

## 使用

从仓库根目录执行：

```bash
python scripts/douyin_transcribe.py "https://v.douyin.com/xxxxx/"
```

默认输出原始逐字稿 `.txt`、毫秒时间轴 `.srt` 和 `metadata.json`（支持直接粘贴整段抖音分享口令；图文作品自动识别并以退出码 `3` 拦截；传入 `--glossary` 时额外生成独立的 `.corrected.md` 术语纠错稿）。示例：

```bash
# 只解析元数据，不下载或转写
python scripts/douyin_transcribe.py "https://v.douyin.com/xxxxx/" --dry-run

# 同时保留 mp3
python scripts/douyin_transcribe.py "https://v.douyin.com/xxxxx/" --mode audio

# 同时保留 mp4 和 mp3
python scripts/douyin_transcribe.py "https://v.douyin.com/xxxxx/" --mode video
```

执行完成后输出文件放在 `./douyin-output/{日期}/{video_id}/`。完整契约、参数和边界见 [SKILL.md](SKILL.md)。

## 抓取路径

1. 解析抖音短链并读取 `iesdouyin.com` 分享页。
2. 页面结构不可解析时，尝试匿名移动端 feed 主备节点，并精确匹配作品 ID。
3. 上述路径都失败时，可用隔离的临时无头浏览器请求作品 detail。临时浏览器不读取用户浏览器配置或 Cookie。
4. 中文把播放地址交给 Redfox「视频播放链接提取文案」；外语用 ffmpeg 抽音后交 Groq / Deepgram；装了 OpenCC 时统一为简体中文。

本项目不依赖 yt-dlp，不保存 Cookie、访问令牌或登录态。匿名接口可能受抖音服务端策略变化影响；私密、删除或需登录的作品无法匿名获取。嘈杂音频和重叠说话人可能影响识别准确度，转写结果应按需要人工校对。

## 开源许可

MIT License，详见 [LICENSE](LICENSE)。
