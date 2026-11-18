#!/usr/bin/env python3
"""
douyin_transcribe.py — 抖音视频抓取 + 转写（只用 API，不装本地模型）

链路（无需用户 Cookie）：
  抖音分享口令 / 短链 / 作品页链接 / video_id
    -> 链接清洗与类型识别（拦截 /note/ 或图集作品，避免把图集 BGM 当口播转写）
    -> 下载/播放地址：Redfox 抖音下载接口（0.06 元/次，调用前查余额，优先）
       失败时备选匿名链路：iesdouyin 页面 → 匿名移动端 feed → 临时无头 Chrome detail
    -> 转写（scripts/asr_backends.py）：
       中文：Redfox「视频播放链接提取文案」（0.06 元/条，带毫秒时间戳）
       外语：Groq 免费层 whisper（>25MB 自动切段合并重试）→ Deepgram 赠送额度
    -> 落盘原始逐字稿 .txt + .srt (+ 可选术语纠错层 .corrected.md + 可选 .mp3 / .mp4) + metadata.json

依赖：Python 3 标准库 + 系统 ffmpeg；opencc 可选（装了才做繁简归一化）。
Key 只从环境变量读：REDFOX_API_KEY / GROQ_API_KEY / DEEPGRAM_API_KEY。
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parent))
import asr_backends as ab  # noqa: E402

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
VALID_ASR = ("auto", "redfox", "groq", "deepgram")
BROWSER_HELPER = Path(__file__).with_name("douyin_browser_detail.mjs")
EXIT_IMAGE_NOTE = 3
ImageNoteError = ab.ImageNoteError

URL_IN_TEXT_RE = re.compile(r"https?://[A-Za-z0-9._~:/?#\[\]@!$&'()*+,;=%-]+")
TRAILING_URL_PUNCT = ".,!?;:)'\""


# ---------------- 日志 ----------------

def log(tag: str, msg: str) -> None:
    print(f"[{tag}] {msg}", file=sys.stderr, flush=True)


# ---------------- 解析 ----------------

def extract_input_candidate(raw: str) -> str:
    """从整段抖音分享口令文本或链接字符串中提取干净的 URL 或纯数字 ID。
    注：口令中的数字前缀（如 '0.82 08/16'）仅是客户端口令标识，绝不作为发布日期。
    """
    s = raw.strip()
    if re.fullmatch(r"\d+", s):
        return s
    m = URL_IN_TEXT_RE.search(s)
    if m:
        cleaned = m.group(0).rstrip(TRAILING_URL_PUNCT)
        if cleaned:
            return cleaned
    return s


def _match_url_target(url: str) -> tuple[str | None, str]:
    """从已知结构的抖音 URL 中识别 (video_id, url_kind)，url_kind 取 video / note / unknown。"""
    m_video = re.search(r"/(?:share/)?video/(\d+)", url)
    if m_video:
        return m_video.group(1), "video"
    m_note = re.search(r"/(?:share/)?(?:note|slides)/(\d+)", url)
    if m_note:
        return m_note.group(1), "note"
    qs = parse_qs(urlparse(url).query)
    mid = (qs.get("modal_id") or qs.get("aweme_id") or [""])[0]
    if mid and re.fullmatch(r"\d+", mid):
        kind = "note" if "/note" in url else "unknown"
        return mid, kind
    return None, "unknown"


def resolve_douyin_target(url_or_id: str, max_retries: int = 2) -> dict:
    """解析抖音口令文本 / 短链 / 作品页链接 / 纯数字 ID。
    返回 {'video_id': str, 'clean_url': str, 'url_kind': 'video'|'note'|'unknown'}。
    """
    candidate = extract_input_candidate(url_or_id)
    if re.fullmatch(r"\d+", candidate):
        return {
            "video_id": candidate,
            "clean_url": f"https://www.douyin.com/video/{candidate}",
            "url_kind": "unknown",
        }
    if not candidate.startswith(("http://", "https://")):
        raise RuntimeError(f"无法从输入中识别有效的抖音链接或作品 ID: {url_or_id!r}")

    vid, kind = _match_url_target(candidate)
    if vid:
        return {"video_id": vid, "clean_url": candidate, "url_kind": kind}

    log("parse", f"短链跟随重定向: {candidate}")
    last_err: Exception | None = None
    final = candidate
    for attempt in range(max_retries + 1):
        try:
            req = Request(candidate, headers=PAGE_HEADERS)
            with urlopen(req, timeout=20) as resp:
                final = resp.url
            break
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last_err = exc
            if attempt < max_retries:
                time.sleep(1.0 * (attempt + 1))
                continue
            raise RuntimeError(f"抖音短链重定向连接失败: {exc}") from exc

    vid, kind = _match_url_target(final)
    if vid:
        return {"video_id": vid, "clean_url": candidate, "resolved_url": final, "url_kind": kind}
    raise RuntimeError(f"无法从重定向 URL 提取作品 ID: {final}")


def resolve_douyin_id(url_or_id: str) -> str:
    """从分享口令/短链/作品页链接/纯数字 ID 中提取抖音作品 ID。"""
    return resolve_douyin_target(url_or_id)["video_id"]


class MetadataParseError(RuntimeError):
    """页面已返回，但匿名元数据结构无法解析。"""


def _raise_if_image_item(item: dict, video_id: str) -> None:
    images = item.get("images")
    aweme_type = item.get("aweme_type")
    if (isinstance(images, list) and len(images) > 0) or aweme_type == 68:
        title = (item.get("desc") or item.get("title") or "").strip()
        count = len(images) if isinstance(images, list) else 0
        raise ImageNoteError(
            f"该作品是图文/图集（video_id={video_id}，共 {count} 张图），不包含可转写的口播视频音轨",
            video_id=video_id,
            title=title,
            image_count=count,
        )


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
    matched_key = None
    for key in ("video_(id)/page", "note_(id)/page"):
        node = loader.get(key)
        if node:
            items = (node.get("videoInfoRes") or {}).get("item_list") or []
            if items:
                item = items[0]
                matched_key = key
                break
    if not item:
        raise MetadataParseError("无法从 _ROUTER_DATA 中解析视频信息")

    # 必须在读取 video.play_addr 前拦截图集，防止把图集背景音乐流误当口播送去付费转写
    _raise_if_image_item(item, video_id)
    if matched_key == "note_(id)/page" and not (item.get("video") or {}).get("play_addr"):
        raise ImageNoteError(
            f"该作品位于图文页路由（video_id={video_id}），不包含可转写的口播视频音轨",
            video_id=video_id,
            title=(item.get("desc") or "").strip(),
        )

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
    """Legacy helper is retained for compatibility, but must not launch a browser."""
    raise RuntimeError("浏览器补证仅使用 ego-browser；先读其 Skill 并运行 ego-browser skill，当前 CLI 不启动旧无头浏览器")


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
            _raise_if_image_item(item, video_id)

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
        except ImageNoteError:
            raise
        except Exception as exc:
            errors.append(f"{urlparse(endpoint).netloc}: {exc}")
    raise MetadataParseError("匿名 mobile feed 均失败: " + "; ".join(errors))


def fetch_metadata(
    video_id: str,
    browser_fallback: bool = False,
    chrome_path: str | None = None,
) -> dict:
    """优先匿名 iesdouyin，再试匿名移动端 feed；浏览器补证交由已授权 Ego 会话。
    一旦任一节点确认为图文作品（ImageNoteError），立即短路退出，不继续启动后续兜底。
    """
    try:
        return fetch_metadata_iesdouyin(video_id)
    except ImageNoteError:
        raise
    except (MetadataParseError, urllib.error.URLError, TimeoutError, OSError) as primary:
        try:
            meta = fetch_metadata_mobile_feed(video_id)
            log("fallback", "iesdouyin 解析失败，已通过匿名移动端 feed 获取元数据")
            return meta
        except ImageNoteError:
            raise
        except Exception as mobile_feed:
            if not browser_fallback:
                raise RuntimeError(
                    f"匿名元数据链路失败: iesdouyin={primary}; mobile-feed={mobile_feed}"
                ) from mobile_feed
            try:
                return fetch_metadata_browser(video_id, chrome_path)
            except ImageNoteError:
                raise
            except Exception as fallback:
                raise RuntimeError(
                    f"三条元数据链路均失败: iesdouyin={primary}; "
                    f"mobile-feed={mobile_feed}; browser-detail={fallback}"
                ) from fallback


# ---------------- 音视频与本地缓存 ----------------

def ffmpeg_stream_to_audio(direct_url: str, mp3_path: str) -> int:
    """用 ffmpeg 直接从直链抽音频（不下载 mp4），原子落盘。返回 mp3 字节数。"""
    Path(mp3_path).parent.mkdir(parents=True, exist_ok=True)
    part_path = mp3_path + ".part.mp3"
    headers_str = "\r\n".join(f"{k}: {v}" for k, v in DL_HEADERS.items()) + "\r\n"
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-headers", headers_str,
        "-i", direct_url,
        "-vn", "-ar", "16000", "-ac", "1", "-b:a", "64k",
        part_path,
    ]
    log("dl", f"ffmpeg 流式抽音: {mp3_path}")
    t0 = time.time()
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        if os.path.exists(part_path):
            os.remove(part_path)
        raise RuntimeError(f"ffmpeg 抽音失败:\n{r.stderr[-500:]}")
    os.replace(part_path, mp3_path)
    size = os.path.getsize(mp3_path)
    log("dl", f"抽音完成 {size/1024:.1f} KB ({time.time()-t0:.1f}s)")
    return size


def download_mp4(direct_url: str, mp4_path: str) -> int:
    """urllib 下载 mp4 到本地（支持分段 HTTP 206 完整性续传），返回字节数。"""
    Path(mp4_path).parent.mkdir(parents=True, exist_ok=True)
    part_path = mp4_path + ".part"
    if os.path.exists(part_path):
        os.remove(part_path)
    t0 = time.time()
    downloaded = 0
    expected_total = None
    # Some media endpoints return a bounded HTTP 206 even without a Range header.
    # Content-Length then describes only that segment, not the complete MP4.
    try:
        for _attempt in range(32):
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
                with open(part_path, "ab" if downloaded else "wb") as f:
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
        os.replace(part_path, mp4_path)
    except Exception:
        if os.path.exists(part_path):
            try:
                os.remove(part_path)
            except OSError:
                pass
        raise
    size = os.path.getsize(mp4_path)
    log("dl", f"下载完成 {size/1024/1024:.2f} MB ({time.time()-t0:.1f}s) -> {mp4_path}")
    return size


def extract_mp3_from_mp4(mp4_path: str, mp3_path: str) -> int:
    """从本地 mp4 抽音，原子落盘。"""
    Path(mp3_path).parent.mkdir(parents=True, exist_ok=True)
    part_path = mp3_path + ".part.mp3"
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-i", mp4_path, "-vn", "-ar", "16000", "-ac", "1", "-b:a", "64k", part_path,
    ]
    t0 = time.time()
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        if os.path.exists(part_path):
            os.remove(part_path)
        raise RuntimeError(f"ffmpeg 抽音失败:\n{r.stderr[-500:]}")
    os.replace(part_path, mp3_path)
    size = os.path.getsize(mp3_path)
    log("dl", f"抽音完成 {size/1024:.1f} KB ({time.time()-t0:.1f}s)")
    return size


def _valid_cached_file(path: Path) -> bool:
    return path.is_file() and path.stat().st_size > 0


def prepare_local_media(
    out_dir: Path,
    video_id: str,
    direct_url: str,
    mode: str,
    need_local_mp3: bool,
    *,
    no_cache: bool = False,
) -> tuple[str | None, str | None, dict]:
    """拉取所需本地音视频并优先复用本地缓存（避免转写瞬断重跑时重复下载大文件）。
    缓存置于 out_dir/.cache/，转写中途失败可自动保留供下次续跑。
    返回 (mp3_path, mp4_path, cache_info)。
    """
    cache_dir = out_dir / ".cache"
    final_mp4 = out_dir / f"{video_id}.mp4"
    final_mp3 = out_dir / f"{video_id}.mp3"
    cache_mp4 = cache_dir / f"{video_id}.mp4"
    cache_mp3 = cache_dir / f"{video_id}.mp3"

    reused_mp4 = False
    reused_mp3 = False
    mp4_path: str | None = None
    mp3_path: str | None = None

    if mode == "video":
        if not no_cache and _valid_cached_file(final_mp4):
            mp4_path = str(final_mp4)
            reused_mp4 = True
            log("cache", f"复用已落盘视频: {mp4_path}")
        elif not no_cache and _valid_cached_file(cache_mp4):
            mp4_path = str(cache_mp4)
            reused_mp4 = True
            log("cache", f"复用本地缓存视频: {mp4_path}")
        else:
            cache_dir.mkdir(parents=True, exist_ok=True)
            mp4_path = str(cache_mp4)
            download_mp4(direct_url, mp4_path)

    if mode in ("audio", "video") or need_local_mp3:
        if not no_cache and _valid_cached_file(final_mp3):
            mp3_path = str(final_mp3)
            reused_mp3 = True
            log("cache", f"复用已落盘音频: {mp3_path}")
        elif not no_cache and _valid_cached_file(cache_mp3):
            mp3_path = str(cache_mp3)
            reused_mp3 = True
            log("cache", f"复用本地缓存音频: {mp3_path}")
        else:
            cache_dir.mkdir(parents=True, exist_ok=True)
            mp3_path = str(cache_mp3)
            if mp4_path:
                extract_mp3_from_mp4(mp4_path, mp3_path)
            else:
                ffmpeg_stream_to_audio(direct_url, mp3_path)

    return mp3_path, mp4_path, {"reused_mp3": reused_mp3, "reused_mp4": reused_mp4, "cache_dir": str(cache_dir)}


# ---------------- 转写 + 归一化 + 术语纠错分离 ----------------

def _t2s(text: str) -> tuple[str, bool]:
    """装了 opencc 就做繁简归一化；没装就原样返回（不强制安装）。不改任何词汇。"""
    try:
        from opencc import OpenCC  # 可选依赖
    except ImportError:
        return text, False
    return OpenCC("t2s").convert(text), True


def transcribe_api(asr: str, lang: str | None, play_url: str | None, mp3_path: str | None) -> tuple[str, dict, list]:
    """按团队口径选 API 后端。返回 (text, asr_info, segments)。"""
    order = [asr] if asr != "auto" else (["redfox"] if (lang or "zh").startswith("zh") else ["groq", "deepgram"])
    errors = []
    for name in order:
        try:
            t0 = time.time()
            if name == "redfox":
                if not play_url:
                    raise ab.BackendError("Redfox 提文案需要可直接访问的播放地址")
                r = ab.redfox_transcribe_url(play_url)
            elif name == "groq":
                r = ab.groq_transcribe(mp3_path, lang)
            else:
                r = ab.deepgram_transcribe(mp3_path, lang)
            text, normalized = _t2s(r["text"])
            segs = []
            for seg in r["segments"]:
                seg_text, _ = _t2s(seg["text"]) if normalized else (seg["text"], False)
                segs.append({**seg, "text": seg_text})
            log("asr", f"{name} 转写完成 {time.time()-t0:.1f}s, {len(segs)} 句, {len(text)} 字")
            info = {
                "backend": name,
                "language": lang or r.get("language") or "auto",
                "segments": len(segs),
                "raw_chars": len(r["text"]),
                "normalized_chars": len(text),
                "opencc_t2s": normalized,
                "cost_yuan": ab.PRICES["redfox_transcribe"] if name == "redfox" else 0,
            }
            if r.get("task_id"):
                info["task_id"] = r["task_id"]
            if r.get("chunk_count"):
                info["chunk_count"] = r["chunk_count"]
            return text, info, segs
        except ab.BackendError as e:
            errors.append(f"{name}: {e}")
            log("asr", f"{name} 失败：{e}")
    raise RuntimeError("所有转写后端都失败：" + "; ".join(errors))


def load_glossary_rules(glossary_path: str) -> list[dict]:
    """读取术语纠错字典（JSON 对象/列表，或制表符分隔的原词\\t校对词\\t依据）。"""
    content = Path(glossary_path).expanduser().read_text(encoding="utf-8").strip()
    if not content:
        return []
    if glossary_path.endswith(".json") or content.startswith(("{", "[")):
        raw = json.loads(content)
        if isinstance(raw, dict):
            return [
                {"wrong": str(k), "right": str(v), "reason": "术语表映射"}
                for k, v in raw.items()
                if k and k != v
            ]
        if isinstance(raw, list):
            return [
                {
                    "wrong": str(item["wrong"]),
                    "right": str(item["right"]),
                    "reason": str(item.get("reason") or "术语表映射"),
                }
                for item in raw
                if isinstance(item, dict) and item.get("wrong") and item.get("right") and item["wrong"] != item["right"]
            ]
        raise ValueError("术语表 JSON 须为对象或规则数组")
    rules = []
    for line in content.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) >= 2 and parts[0] != parts[1]:
            rules.append({
                "wrong": parts[0],
                "right": parts[1],
                "reason": parts[2] if len(parts) >= 3 and parts[2] else "术语表映射",
            })
    return rules


def build_corrected_markdown(meta: dict, raw_text: str, rules: list[dict]) -> tuple[str, list[dict]]:
    """基于原始逐字稿生成独立的术语纠错阅读稿与变更对照表，绝不修改原始 .txt / .srt。"""
    corrected = raw_text
    applied: list[dict] = []
    # 长词优先匹配，避免短词破坏复合词
    for rule in sorted(rules, key=lambda r: len(r["wrong"]), reverse=True):
        wrong, right, reason = rule["wrong"], rule["right"], rule["reason"]
        count = corrected.count(wrong)
        if count > 0:
            corrected = corrected.replace(wrong, right)
            applied.append({"wrong": wrong, "right": right, "count": count, "reason": reason})

    table_lines = [
        "| 序号 | ASR 原词 | 校对词 | 替换次数 | 依据 |",
        "|---|---|---|---|---|",
    ]
    if applied:
        for idx, item in enumerate(applied, 1):
            table_lines.append(
                f"| {idx} | `{item['wrong']}` | `{item['right']}` | {item['count']} | {item['reason']} |"
            )
    else:
        table_lines.append("| - | （无命中项） | - | 0 | 原文未出现词表待纠项 |")

    doc = (
        f"# {meta['title']}（术语纠错阅读稿）\n\n"
        f"- 稿件层: `term_corrected`（派生校对层；原始逐字稿见同目录 `{meta['video_id']}.txt` 与 `{meta['video_id']}.srt`）\n"
        f"- 作者: {meta.get('author') or '未知'}\n"
        f"- video_id: {meta['video_id']}\n"
        f"- 命中纠错项: {len(applied)} 类（共 {sum(x['count'] for x in applied)} 处）\n\n"
        f"## 术语纠错对照表\n\n"
        + "\n".join(table_lines)
        + "\n\n---\n\n## 校对后正文\n\n"
        + corrected
        + "\n"
    )
    return doc, applied


def build_handoff_block(meta: dict, asr_info: dict, saved: dict, fetched_at: str) -> dict:
    """生成向下游 moonlit-benchmark-research 与本人成片回流稿的标准交接摘要。"""
    vid = str(meta["video_id"])
    return {
        "benchmark_research": {
            "platform": "douyin",
            "work_id": vid,
            "account": meta.get("author") or "unknown",
            "public_url": f"https://www.douyin.com/video/{vid}",
            "captured_at": fetched_at[:10],
            "evidence_path": saved.get("txt"),
            "content": "text_available",
            "analysis": "unreviewed",
            "visual": "not_viewed",
            "job": {
                "status": "succeeded",
                "backend": asr_info.get("backend"),
                "task_id": asr_info.get("task_id"),
            },
        },
        "creator_return_flow": {
            "recommended_target": "works/<作品目录>/notes/成片实录文案.md",
            "raw_transcript_path": saved.get("txt"),
            "corrected_transcript_path": saved.get("corrected_md"),
            "authority_boundary": (
                "仅供发布后成片文案回流对照原稿与批注日学；"
                "不得冒充或覆盖由已登记同版成片字幕生成的 content/video/成片逐字稿-{version}.md (finalTranscript)"
            ),
        },
    }


# ---------------- 落盘 ----------------

def save_outputs(
    out_dir: Path,
    meta: dict,
    text: str,
    asr_info: dict,
    mp3_path: str | None,
    mp4_path: str | None,
    segments: list | None = None,
    glossary_rules: list[dict] | None = None,
) -> dict:
    """按 mode 决定保留哪些文件，落盘原始 .txt / .srt、可选纠错稿 .corrected.md 及 metadata.json。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    saved: dict[str, str] = {}
    fetched_at = datetime.now().isoformat(timespec="seconds")

    # 1) 原始逐字稿（禁止改词，仅允许已显式记录的繁简字符转换）
    txt_path = out_dir / f"{meta['video_id']}.txt"
    header = (
        f"# {meta['title']}\n"
        f"\n"
        f"- 作者: {meta.get('author') or '未知'}\n"
        f"- video_id: {meta['video_id']}\n"
        f"- 公开链接: https://www.douyin.com/video/{meta['video_id']}\n"
        f"- 时长: {meta.get('duration_sec')}s\n"
        f"- 抓取时间: {fetched_at}\n"
        f"- 链路: {meta.get('fetch_chain', 'iesdouyin.com')} / {meta['play_addr_src']} → play\n"
        f"- 转写: {asr_info['backend']} API (lang={asr_info['language']}, {asr_info['segments']} 句)\n"
        f"- 稿件层: raw_asr（原始识别逐字稿，未改词）\n"
        f"- 归一化: {'opencc t2s' if asr_info.get('opencc_t2s') else '未做（未装 opencc）'}\n"
        f"\n"
        f"---\n"
        f"\n"
    )
    txt_path.write_text(header + text + "\n", encoding="utf-8")
    saved["txt"] = str(txt_path)

    if segments:
        srt_path = out_dir / f"{meta['video_id']}.srt"
        srt_path.write_text(ab.to_srt(segments), encoding="utf-8")
        saved["srt"] = str(srt_path)

    # 2) 可选术语纠错层（与原始逐字稿物理分离）
    applied_corrections: list[dict] = []
    if glossary_rules is not None:
        corrected_doc, applied_corrections = build_corrected_markdown(meta, text, glossary_rules)
        corr_path = out_dir / f"{meta['video_id']}.corrected.md"
        corr_path.write_text(corrected_doc, encoding="utf-8")
        saved["corrected_md"] = str(corr_path)

    # 3) 音频（保留）
    if mp3_path and os.path.exists(mp3_path):
        target_mp3 = out_dir / f"{meta['video_id']}.mp3"
        if os.path.abspath(mp3_path) != os.path.abspath(target_mp3):
            os.replace(mp3_path, target_mp3)
        saved["mp3"] = str(target_mp3)

    # 4) 视频（保留）
    if mp4_path and os.path.exists(mp4_path):
        target_mp4 = out_dir / f"{meta['video_id']}.mp4"
        if os.path.abspath(mp4_path) != os.path.abspath(target_mp4):
            os.replace(mp4_path, target_mp4)
        saved["mp4"] = str(target_mp4)

    # 5) metadata.json（含分层登记与下游交接块）
    transcript_layers = {
        "raw_txt": saved.get("txt"),
        "raw_srt": saved.get("srt"),
        "corrected_md": saved.get("corrected_md"),
        "applied_corrections_count": len(applied_corrections),
    }
    handoff = build_handoff_block(meta, asr_info, saved, fetched_at)
    meta_full = {
        **{k: v for k, v in meta.items() if k != "direct_url"},  # 不落盘带签名的临时播放地址
        "public_url": f"https://www.douyin.com/video/{meta['video_id']}",
        "asr": asr_info,
        "transcript_layers": transcript_layers,
        "handoff": handoff,
        "saved_files": {k: v for k, v in saved.items()},
        "fetched_at": fetched_at,
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
        description="抖音视频抓取 + 转写（支持口令清洗、图文拦截、本地缓存与术语纠错稿分离）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例:\n"
            '  python douyin_transcribe.py "0.82 08/16 复制打开抖音 https://v.douyin.com/xxxxx/"\n'
            '  python douyin_transcribe.py "https://www.douyin.com/video/7647796267023650084" --mode video\n'
            '  python douyin_transcribe.py 7647796267023650084 --mode audio --download-via anonymous\n'
        ),
    )
    parser.add_argument("url", help="抖音分享口令文本 / 短链 / 作品页链接 / 纯数字 video_id")
    parser.add_argument(
        "--mode", choices=VALID_MODES, default="text",
        help="输出模式: text=文字+srt, audio=文字+srt+mp3, video=文字+srt+mp4+mp3 (默认 text)",
    )
    parser.add_argument(
        "--asr", choices=VALID_ASR, default="auto",
        help="转写后端：auto=中文走 Redfox，外语走 Groq→Deepgram（默认 auto）",
    )
    parser.add_argument(
        "--download-via", choices=("redfox", "anonymous"), default="redfox",
        help="取播放地址：redfox=Redfox 下载接口优先（0.06 元，失败自动退到匿名链路）；anonymous=只用匿名链路（免费）",
    )
    parser.add_argument(
        "--quote-only", action="store_true",
        help="只打印本条预计费用，不联网",
    )
    parser.add_argument(
        "--out-dir", default="./douyin-output",
        help="输出根目录（默认 ./douyin-output）",
    )
    parser.add_argument(
        "--lang", default="zh",
        help="转写语言代码，默认 zh；外语填 en/ja 等，传空字符串让后端自动检测",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="只解析元数据，不下载/不转写",
    )
    parser.add_argument(
        "--keep-temp", action="store_true",
        help="保留 .cache 中的中间音视频文件",
    )
    parser.add_argument(
        "--no-cache", action="store_true",
        help="忽略输出目录下已缓存的本地音视频，强制重新拉取",
    )
    parser.add_argument(
        "--glossary", default=None,
        help="可选术语纠错表路径（JSON 或 TSV）；生成独立 .corrected.md，绝不改写原始 .txt/.srt",
    )
    parser.add_argument(
        "--browser-fallback", choices=("auto", "off"), default="off",
        help="iesdouyin 和匿名移动端 feed 均失败时是否启用临时无头浏览器 detail 兜底（默认 auto）",
    )
    parser.add_argument(
        "--chrome-path", default=None,
        help="Chrome/Chromium 可执行文件路径；默认自动查找或读取 CHROME_PATH",
    )
    args = parser.parse_args()

    if args.lang == "":
        args.lang = None

    glossary_rules = load_glossary_rules(args.glossary) if args.glossary else None

    t_total = time.time()
    zh = (args.lang or "zh").startswith("zh")
    use_redfox_asr = args.asr == "redfox" or (args.asr == "auto" and zh)
    cost = (ab.PRICES["redfox_download"] if args.download_via == "redfox" else 0) + (
        ab.PRICES["redfox_transcribe"] if use_redfox_asr else 0
    )
    log("cost", f"本条预计 Redfox 费用 {cost:.2f} 元（下载 {args.download_via}，转写 {'redfox' if use_redfox_asr else 'groq/deepgram 免费额度'}）")
    if args.quote_only:
        print(json.dumps({"quote_yuan": cost}, ensure_ascii=False))
        return

    try:
        # 1) 解析输入口令 / 链接 + 核查余额
        target = resolve_douyin_target(args.url)
        video_id = target["video_id"]

        if not args.dry_run and cost > 0 and target["url_kind"] != "note":
            bal = ab.check_redfox_balance(cost)
            if bal:
                log("cost", f"Redfox 剩余积分 {bal['available_points']:.1f}（充足）")

        meta = None
        # 若输入链接本身已是 /note/ 图文路由，跳过付费下载接口，先走免费匿名核实图集并拦截
        if args.download_via == "redfox" and not args.dry_run and target["url_kind"] != "note":
            try:
                share = target["clean_url"]
                rf = ab.redfox_download_douyin(share)
                meta = {
                    "video_id": video_id,
                    "title": rf["title"] or f"douyin_{video_id}",
                    "author": rf.get("author"),
                    "duration_sec": rf["duration_sec"],
                    "play_addr_src": "redfox",
                    "fetch_chain": "redfox videoDownload/douyin",
                    "direct_url": rf["video_url"],
                }
            except ImageNoteError:
                raise
            except ab.BackendError as e:
                log("fallback", f"Redfox 下载失败（{e}），改用匿名链路")

        if meta is None:
            meta = fetch_metadata(
                video_id,
                browser_fallback=args.browser_fallback == "auto",
                chrome_path=args.chrome_path,
            )
    except ImageNoteError as exc:
        err_payload = {
            "error": "image_note_not_supported",
            "video_id": exc.video_id or None,
            "title": exc.title or None,
            "image_count": exc.image_count,
            "message": str(exc),
        }
        log("reject", str(exc))
        print(json.dumps(err_payload, ensure_ascii=False), file=sys.stdout)
        sys.exit(EXIT_IMAGE_NOTE)

    log("meta", f"title   = {meta['title'][:60]}")
    log("meta", f"author  = {meta.get('author') or '(未知)'}")
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

    # 3) 拉取音/视频（支持本地缓存复用，转写中途失败不丢已抽音频）
    keep_mp3 = args.mode in ("audio", "video")
    keep_mp4 = args.mode == "video"
    need_local_mp3 = keep_mp3 or (not use_redfox_asr)

    mp3_path, mp4_path, cache_info = prepare_local_media(
        out_dir,
        video_id,
        meta["direct_url"],
        args.mode,
        need_local_mp3,
        no_cache=args.no_cache,
    )

    # 4) 转写（中文 Redfox 若在已缓存本地音频场景失败，不覆盖原音频，方便续跑）
    text, asr_info, segments = transcribe_api(args.asr, args.lang, meta.get("direct_url"), mp3_path)

    # 5) 落盘
    mp3_path_for_save = mp3_path if keep_mp3 else None
    mp4_path_for_save = mp4_path if keep_mp4 else None

    saved = save_outputs(
        out_dir,
        meta,
        text,
        asr_info,
        mp3_path_for_save,
        mp4_path_for_save,
        segments,
        glossary_rules=glossary_rules,
    )

    # 成功后按模式清理临时缓存目录
    if not args.keep_temp:
        shutil.rmtree(cache_info["cache_dir"], ignore_errors=True)

    # 6) 打印全文
    print("\n" + "=" * 60)
    print(f"【{meta['title']}】")
    print(f"作者: {meta.get('author') or '未知'}  |  时长: {meta.get('duration_sec')}s")
    print("=" * 60)
    print(text)
    print("=" * 60)
    print(f"\n[done] 总耗时 {time.time()-t_total:.1f}s  |  输出: {out_dir}")
    for k, v in saved.items():
        size = ""
        try:
            sz = os.path.getsize(v)
            size = f"  ({sz/1024/1024:.2f} MB)" if sz > 1024 * 1024 else f"  ({sz/1024:.1f} KB)"
        except OSError:
            pass
        print(f"  - {k}: {v}{size}")


if __name__ == "__main__":
    main()
