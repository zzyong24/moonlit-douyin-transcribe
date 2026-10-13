#!/usr/bin/env python3
"""
douyin_transcribe.py — 抖音视频抓取 + 转写

链路（无需用户 Cookie、无需 yt-dlp）：
  抖音分享链接
    -> 短链跟随重定向提取 video_id
    -> iesdouyin.com/share/video/{id} 解析 window._ROUTER_DATA
    -> 页面结构失效时，依次尝试匿名移动端 feed 与临时无头 Chrome detail
    -> play_addr.url_list[0] 替换 playwm->play （去水印直链）
    -> ffmpeg 流式抽音频（text/audio 模式）或 urllib 下载 mp4（video 模式）
    -> faster-whisper (small 默认) 转写
    -> opencc t2s 繁简归一化
    -> 落盘 .txt (+ 可选 .mp3 / .mp4) + metadata.json

基础依赖：faster-whisper, opencc, ffmpeg(系统)
浏览器兜底依赖：Node.js 22+ 与 Chrome/Chromium（只在前两种匿名元数据链路失效时启动）
Python 运行环境需安装 faster-whisper 与 opencc；ffmpeg 需在系统 PATH 中可用。
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse, parse_qs
from urllib.request import Request, urlopen

# ---------------- 常量 ----------------

MOBILE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_2 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) "
    "EdgiOS/121.0.2277.107 Version/17.0 Mobile/15E148 Safari/604.1"
)
PAGE_HEADERS = {
    "User-Agent": MOBILE_UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9",
}
DL_HEADERS = {
    **PAGE_HEADERS,
    "Referer": "https://www.douyin.com/",
}

VALID_MODES = ("text", "audio", "video")
VALID_MODELS = ("tiny", "base", "small", "medium", "large-v2", "large-v3")
BROWSER_HELPER = Path(__file__).with_name("douyin_browser_detail.mjs")


# ---------------- 日志 ----------------

def log(tag: str, msg: str) -> None:
    print(f"[{tag}] {msg}", file=sys.stderr, flush=True)


# ---------------- 解析 ----------------

def resolve_douyin_id(url_or_id: str) -> str:
    """从分享链接/纯数字 ID 中提取抖音 video_id。短链自动跟随重定向。"""
    s = url_or_id.strip()
    if re.fullmatch(r"\d+", s):
        return s
    m = re.search(r"/video/(\d+)", s)
    if m:
        return m.group(1)
    qs = parse_qs(urlparse(s).query)
    if (qs.get("modal_id") or [""])[0]:
        return (qs.get("modal_id") or [""])[0]

    log("parse", f"短链跟随重定向: {s}")
    req = Request(s, headers=PAGE_HEADERS)
    with urlopen(req, timeout=20) as resp:
        final = resp.url
    m = re.search(r"/video/(\d+)", final)
    if m:
        return m.group(1)
    qs = parse_qs(urlparse(final).query)
    mid = (qs.get("modal_id") or [""])[0]
    if mid:
        return mid
    raise RuntimeError(f"无法从重定向 URL 提取视频 ID: {final}")


class MetadataParseError(RuntimeError):
    """页面已返回，但匿名元数据结构无法解析。"""


def fetch_metadata_iesdouyin(video_id: str) -> dict:
    """访问 iesdouyin.com 解析 _ROUTER_DATA，返回元数据 + 无水印直链。"""
    page_url = f"https://www.iesdouyin.com/share/video/{video_id}/"
    log("parse", f"GET {page_url}")
    req = Request(page_url, headers=PAGE_HEADERS)
    with urlopen(req, timeout=20) as resp:
        html = resp.read().decode("utf-8", errors="replace")

    m = re.search(r"window\._ROUTER_DATA\s*=\s*(.*?)</script>", html, re.DOTALL)
    if not m:
        raise MetadataParseError("未在页面中找到 window._ROUTER_DATA")

    try:
        data = json.loads(m.group(1).strip())
    except json.JSONDecodeError as exc:
        raise MetadataParseError("_ROUTER_DATA JSON 解析失败") from exc
    loader = data.get("loaderData") or data

    item = None
    for key in ("video_(id)/page", "note_(id)/page"):
        node = loader.get(key)
        if node:
            items = (node.get("videoInfoRes") or {}).get("item_list") or []
            if items:
                item = items[0]
                break
    if not item:
        raise MetadataParseError("无法从 _ROUTER_DATA 中解析视频信息")

    url_list = ((item.get("video") or {}).get("play_addr") or {}).get("url_list") or []
    src = "play_addr"
    if not url_list:
        url_list = ((item.get("video") or {}).get("download_addr") or {}).get("url_list") or []
        src = "download_addr"
    if not url_list:
        raise MetadataParseError("未找到视频播放地址")

    direct = url_list[0].replace("playwm", "play")
    title = (item.get("desc") or f"douyin_{video_id}").strip()
    author = ((item.get("author") or {}).get("nickname") or "").strip()
    duration_ms = (item.get("video") or {}).get("duration") or 0
    cover = (((item.get("video") or {}).get("cover") or {}).get("url_list") or [None])[0]

    return {
        "video_id": video_id,
        "title": title,
        "author": author,
        "duration_ms": duration_ms,
        "duration_sec": round(duration_ms / 1000, 1) if duration_ms else None,
        "play_addr_src": src,
        "direct_url": direct,
        "cover_url": cover,
        "aweme_id": item.get("aweme_id"),
        "fetch_chain": "iesdouyin.com/router-data",
    }


def fetch_metadata_browser(video_id: str, chrome_path: str | None = None) -> dict:
    """用临时无头浏览器读取抖音 detail；不读取或保存用户 Cookie。"""
    node = shutil.which("node")
    if not node:
        raise RuntimeError("浏览器兜底需要 Node.js 22+，但 PATH 中未找到 node")
    if not BROWSER_HELPER.exists():
        raise RuntimeError(f"浏览器兜底脚本不存在: {BROWSER_HELPER}")
    cmd = [node, str(BROWSER_HELPER), video_id]
    if chrome_path:
        cmd.append(chrome_path)
    log("fallback", "iesdouyin 结构失效，启动临时无头浏览器读取 detail")
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=90)
    if result.returncode != 0:
        summary = (result.stderr or result.stdout or "未知错误").strip().splitlines()[-1]
        raise RuntimeError(f"浏览器 detail 兜底失败: {summary}")
    try:
        meta = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("浏览器 detail 兜底返回了无效 JSON") from exc
    if not isinstance(meta, dict) or not meta.get("direct_url"):
        raise RuntimeError("浏览器 detail 兜底未返回播放地址")
    duration_ms = meta.get("duration_ms") or 0
    meta["duration_sec"] = round(duration_ms / 1000, 1) if duration_ms else None
    return meta


def fetch_metadata_mobile_feed(video_id: str) -> dict:
    """从匿名抖音移动端 feed 接口读取普通视频元数据。"""
    feed_ua = (
        "com.ss.android.ugc.aweme/320303 (Linux; U; Android 13; zh_CN; "
        "Pixel 7; Build/TP1A.220624.014)"
    )
    endpoints = (
        "https://api5-normal-c-hl.amemv.com/aweme/v1/feed/",
        "https://aweme.snssdk.com/aweme/v1/feed/",
    )
    errors = []
    for endpoint in endpoints:
        url = f"{endpoint}?aweme_id={video_id}&aid=1128"
        try:
            req = Request(url, headers={"User-Agent": feed_ua, "Accept": "application/json"})
            with urlopen(req, timeout=15) as resp:
                root = json.loads(resp.read().decode("utf-8", errors="replace"))
            items = root.get("aweme_list") or []
            item = next(
                (entry for entry in items if str(entry.get("aweme_id")) == video_id),
                None,
            )
            if not item:
                raise MetadataParseError("mobile feed 未返回作品信息")
            if item.get("images"):
                raise MetadataParseError("该作品是图集，不包含可转写的视频音轨")

            video = item.get("video") or {}
            play_urls = (video.get("play_addr") or {}).get("url_list") or []
            src = "mobile_feed/play_addr"
            if not play_urls:
                play_urls = (video.get("download_addr") or {}).get("url_list") or []
                src = "mobile_feed/download_addr"
            if not play_urls:
                raise MetadataParseError("mobile feed 未返回视频播放地址")

            cover = video.get("cover") or video.get("origin_cover") or {}
            author = item.get("author") or {}
            duration_ms = video.get("duration") or 0
            return {
                "video_id": video_id,
                "title": (item.get("desc") or item.get("title") or f"douyin_{video_id}").strip(),
                "author": (author.get("nickname") or author.get("name") or "").strip(),
                "duration_ms": duration_ms,
                "duration_sec": round(duration_ms / 1000, 1) if duration_ms else None,
                "play_addr_src": src,
                "direct_url": play_urls[0].replace("playwm", "play"),
                "cover_url": (cover.get("url_list") or [None])[0],
                "aweme_id": item.get("aweme_id"),
                "fetch_chain": "douyin.com/mobile-feed",
            }
        except Exception as exc:
            errors.append(f"{urlparse(endpoint).netloc}: {exc}")
    raise MetadataParseError("匿名 mobile feed 均失败: " + "; ".join(errors))


def fetch_metadata(
    video_id: str,
    browser_fallback: bool = True,
    chrome_path: str | None = None,
) -> dict:
    """优先匿名 iesdouyin，再试匿名移动端 feed，最后回落临时浏览器 detail。"""
    try:
        return fetch_metadata_iesdouyin(video_id)
    except MetadataParseError as primary:
        try:
            meta = fetch_metadata_mobile_feed(video_id)
            log("fallback", "iesdouyin 解析失败，已通过匿名移动端 feed 获取元数据")
            return meta
        except Exception as mobile_feed:
            if not browser_fallback:
                raise RuntimeError(
                    f"匿名元数据链路失败: iesdouyin={primary}; mobile-feed={mobile_feed}"
                ) from mobile_feed
        try:
            return fetch_metadata_browser(video_id, chrome_path)
        except Exception as fallback:
            raise RuntimeError(
                f"三条元数据链路均失败: iesdouyin={primary}; "
                f"mobile-feed={mobile_feed}; browser-detail={fallback}"
            ) from fallback


# ---------------- 音视频 ----------------

def ffmpeg_stream_to_audio(direct_url: str, mp3_path: str) -> int:
    """
    用 ffmpeg 直接从直链抽音频（不下载 mp4）。
    返回 mp3 字节数。
    """
    headers_str = "\r\n".join(f"{k}: {v}" for k, v in DL_HEADERS.items()) + "\r\n"
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-headers", headers_str,
        "-i", direct_url,
        "-vn", "-ar", "16000", "-ac", "1", "-b:a", "64k",
        mp3_path,
    ]
    log("dl", f"ffmpeg 流式抽音: {mp3_path}")
    t0 = time.time()
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"ffmpeg 抽音失败:\n{r.stderr[-500:]}")
    size = os.path.getsize(mp3_path)
    log("dl", f"抽音完成 {size/1024:.1f} KB ({time.time()-t0:.1f}s)")
    return size


def download_mp4(direct_url: str, mp4_path: str) -> int:
    """urllib 下载 mp4 到本地，返回字节数。"""
    t0 = time.time()
    downloaded = 0
    expected_total = None
    # Some media endpoints return a bounded HTTP 206 even without a Range header.
    # Content-Length then describes only that segment, not the complete MP4.
    for attempt in range(32):
        headers = dict(DL_HEADERS)
        if downloaded:
            headers["Range"] = f"bytes={downloaded}-"
        req = Request(direct_url, headers=headers)
        with urlopen(req, timeout=120) as resp:
            content_range = resp.headers.get("Content-Range")
            length = resp.headers.get("Content-Length")
            expected_chunk = int(length) if length else None
            if content_range:
                match = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)", content_range.strip())
                if not match:
                    raise RuntimeError("媒体返回无法核验的 Content-Range")
                start, end, total = map(int, match.groups())
                if start != downloaded or end < start or end >= total:
                    raise RuntimeError("媒体分段起止位置不匹配，拒绝拼接")
                if expected_total is not None and total != expected_total:
                    raise RuntimeError("媒体总长度变化，拒绝拼接")
                expected_total = total
                expected_chunk = end - start + 1
            elif downloaded or resp.status == 206:
                raise RuntimeError("媒体续传缺少有效 Content-Range，拒绝拼接")
            elif expected_chunk is not None:
                expected_total = expected_chunk
            received = 0
            with open(mp4_path, "ab" if downloaded else "wb") as f:
                while True:
                    buf = resp.read(256 * 1024)
                    if not buf:
                        break
                    f.write(buf)
                    received += len(buf)
            if expected_chunk is not None and received != expected_chunk:
                raise RuntimeError("媒体响应字节数不足，下载未完成")
            if not received:
                raise RuntimeError("媒体响应为空，下载未完成")
            downloaded += received
            if expected_total is None or downloaded == expected_total:
                break
            if downloaded > expected_total:
                raise RuntimeError("媒体字节数超过预期，拒绝使用")
    else:
        raise RuntimeError("媒体分段次数超过限制，下载未完成")
    size = os.path.getsize(mp4_path)
    log("dl", f"下载完成 {size/1024/1024:.2f} MB ({time.time()-t0:.1f}s) -> {mp4_path}")
    return size


def extract_mp3_from_mp4(mp4_path: str, mp3_path: str) -> int:
    """从本地 mp4 抽音。"""
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-i", mp4_path, "-vn", "-ar", "16000", "-ac", "1", "-b:a", "64k", mp3_path,
    ]
    t0 = time.time()
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"ffmpeg 抽音失败:\n{r.stderr[-500:]}")
    size = os.path.getsize(mp3_path)
    log("dl", f"抽音完成 {size/1024:.1f} KB ({time.time()-t0:.1f}s)")
    return size


# ---------------- 转写 + 归一化 ----------------

def _load_opencc():
    """懒加载 opencc。"""
    from opencc import OpenCC
    return OpenCC("t2s")


def transcribe(mp3_path: str, model_size: str, lang: str | None) -> tuple[str, dict]:
    """faster-whisper 转写 + opencc 繁简归一化。返回 (text, info_dict)。"""
    from faster_whisper import WhisperModel
    log("asr", f"加载 faster-whisper {model_size} (cpu, int8)")
    t0 = time.time()
    model = WhisperModel(model_size, device="cpu", compute_type="int8")
    log("asr", f"模型加载 {time.time()-t0:.1f}s")

    log("asr", f"转写: {mp3_path} (lang={lang or 'auto'})")
    t0 = time.time()
    segments, info = model.transcribe(mp3_path, language=lang, vad_filter=True)
    parts = []
    for seg in segments:
        parts.append(seg.text.strip())
    raw = "\n".join(p for p in parts if p)
    log("asr", f"转写完成 {time.time()-t0:.1f}s, lang={info.language} prob={info.language_probability:.2f}, "
            f"{len(parts)} 段, {len(raw)} 字")

    log("asr", "opencc t2s 繁简归一化")
    cc = _load_opencc()
    text = cc.convert(raw)

    asr_info = {
        "model": model_size,
        "language": info.language,
        "language_probability": round(info.language_probability, 4),
        "duration_sec": round(info.duration, 1),
        "segments": len(parts),
        "raw_chars": len(raw),
        "normalized_chars": len(text),
    }
    return text, asr_info


# ---------------- 落盘 ----------------

def save_outputs(
    out_dir: Path,
    meta: dict,
    text: str,
    asr_info: dict,
    mp3_path: str | None,
    mp4_path: str | None,
) -> dict:
    """按 mode 决定保留哪些文件，落盘 metadata.json / .txt。返回落盘清单。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    saved = {}

    # 1) 文本
    txt_path = out_dir / f"{meta['video_id']}.txt"
    header = (
        f"# {meta['title']}\n"
        f"\n"
        f"- 作者: {meta['author'] or '未知'}\n"
        f"- video_id: {meta['video_id']}\n"
        f"- 时长: {meta.get('duration_sec')}s\n"
        f"- 抓取时间: {datetime.now().isoformat(timespec='seconds')}\n"
        f"- 链路: {meta.get('fetch_chain', 'iesdouyin.com')} / {meta['play_addr_src']} → play\n"
        f"- 转写: faster-whisper {asr_info['model']} (lang={asr_info['language']}, "
        f"prob={asr_info['language_probability']})\n"
        f"- 归一化: opencc t2s\n"
        f"\n"
        f"---\n"
        f"\n"
    )
    txt_path.write_text(header + text + "\n", encoding="utf-8")
    saved["txt"] = str(txt_path)

    # 2) 音频（保留）
    if mp3_path and os.path.exists(mp3_path):
        target_mp3 = out_dir / f"{meta['video_id']}.mp3"
        os.replace(mp3_path, target_mp3)
        saved["mp3"] = str(target_mp3)

    # 3) 视频（保留）
    if mp4_path and os.path.exists(mp4_path):
        target_mp4 = out_dir / f"{meta['video_id']}.mp4"
        os.replace(mp4_path, target_mp4)
        saved["mp4"] = str(target_mp4)

    # 4) metadata.json
    meta_full = {
        **meta,
        "asr": asr_info,
        "saved_files": {k: v for k, v in saved.items()},
        "fetched_at": datetime.now().isoformat(timespec="seconds"),
    }
    meta_path = out_dir / "metadata.json"
    meta_path.write_text(
        json.dumps(meta_full, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    saved["metadata"] = str(meta_path)

    return saved


# ---------------- 入口 ----------------

def main():
    parser = argparse.ArgumentParser(
        description="抖音视频抓取 + 转写（两条匿名元数据链路 + 临时浏览器兜底）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例:\n"
            '  python douyin_transcribe.py "https://v.douyin.com/xxxxx/"\n'
            '  python douyin_transcribe.py "https://www.douyin.com/video/7647796267023650084" --mode video\n'
            '  python douyin_transcribe.py 7647796267023650084 --mode audio --model-size medium\n'
        ),
    )
    parser.add_argument("url", help="抖音分享链接（短链或完整）或纯数字 video_id")
    parser.add_argument(
        "--mode", choices=VALID_MODES, default="text",
        help="输出模式: text=只文字, audio=文字+mp3, video=文字+mp4+mp3 (默认 text)",
    )
    parser.add_argument(
        "--model-size", choices=VALID_MODELS, default="small",
        help="faster-whisper 模型 (默认 small)",
    )
    parser.add_argument(
        "--out-dir", default="./douyin-output",
        help="输出根目录（默认 ./douyin-output）",
    )
    parser.add_argument(
        "--lang", default="zh",
        help="转写语言代码，默认 zh；传空字符串让模型自动检测",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="只解析元数据，不下载/不转写",
    )
    parser.add_argument(
        "--keep-temp", action="store_true",
        help="保留中间临时文件（text 模式的临时 mp3）",
    )
    parser.add_argument(
        "--browser-fallback", choices=("auto", "off"), default="auto",
        help="iesdouyin 和匿名移动端 feed 均失败时是否启用临时无头浏览器 detail 兜底（默认 auto）",
    )
    parser.add_argument(
        "--chrome-path", default=None,
        help="Chrome/Chromium 可执行文件路径；默认自动查找或读取 CHROME_PATH",
    )
    args = parser.parse_args()

    if args.lang == "":
        args.lang = None

    t_total = time.time()

    # 1) 解析 video_id + 元数据
    video_id = resolve_douyin_id(args.url)
    meta = fetch_metadata(
        video_id,
        browser_fallback=args.browser_fallback == "auto",
        chrome_path=args.chrome_path,
    )
    log("meta", f"title   = {meta['title'][:60]}")
    log("meta", f"author  = {meta['author'] or '(未知)'}")
    log("meta", f"duration= {meta.get('duration_sec')}s")
    log("meta", f"src     = {meta['play_addr_src']}")
    log("meta", f"chain   = {meta.get('fetch_chain', 'iesdouyin.com')}")

    if args.dry_run:
        print(json.dumps({k: v for k, v in meta.items() if k != "direct_url"},
                         ensure_ascii=False, indent=2))
        return

    # 2) 输出目录
    today = datetime.now().strftime("%Y-%m-%d")
    out_dir = Path(args.out_dir).expanduser().resolve() / today / video_id

    # 3) 拉取音/视频
    tmpdir = tempfile.mkdtemp(prefix=f"dy_{video_id}_")
    mp3_path: str | None = None
    mp4_path: str | None = None
    keep_mp3 = args.mode in ("audio", "video")
    keep_mp4 = args.mode == "video"

    if args.mode == "text":
        # 流式抽音 → 临时文件 → 转写后删除（除非 --keep-temp）
        mp3_path = os.path.join(tmpdir, f"{video_id}.mp3")
        ffmpeg_stream_to_audio(meta["direct_url"], mp3_path)
    else:
        if args.mode == "video":
            mp4_path = os.path.join(tmpdir, f"{video_id}.mp4")
            download_mp4(meta["direct_url"], mp4_path)
            mp3_path = os.path.join(tmpdir, f"{video_id}.mp3")
            extract_mp3_from_mp4(mp4_path, mp3_path)
        else:  # audio
            mp3_path = os.path.join(tmpdir, f"{video_id}.mp3")
            ffmpeg_stream_to_audio(meta["direct_url"], mp3_path)

    # 4) 转写
    text, asr_info = transcribe(mp3_path, args.model_size, args.lang)

    # 5) 落盘
    if not keep_mp3:
        # 临时文件用完就清掉（text 模式）
        try:
            os.remove(mp3_path)
        except OSError:
            pass
        mp3_path_for_save = None
    else:
        mp3_path_for_save = mp3_path
    mp4_path_for_save = mp4_path if keep_mp4 else None

    saved = save_outputs(out_dir, meta, text, asr_info, mp3_path_for_save, mp4_path_for_save)

    # 6) 打印全文
    print("\n" + "=" * 60)
    print(f"【{meta['title']}】")
    print(f"作者: {meta['author'] or '未知'}  |  时长: {meta.get('duration_sec')}s")
    print("=" * 60)
    print(text)
    print("=" * 60)
    print(f"\n[done] 总耗时 {time.time()-t_total:.1f}s  |  输出: {out_dir}")
    for k, v in saved.items():
        size = ""
        try:
            sz = os.path.getsize(v)
            size = f"  ({sz/1024/1024:.2f} MB)" if sz > 1024*1024 else f"  ({sz/1024:.1f} KB)"
        except OSError:
            pass
        print(f"  - {k}: {v}{size}")

    # 清理临时目录
    if not args.keep_temp:
        try:
            for p in [mp3_path, mp4_path]:
                if p and os.path.exists(p):
                    os.remove(p)
            os.rmdir(tmpdir)
        except OSError:
            pass


if __name__ == "__main__":
    main()
