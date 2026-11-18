#!/usr/bin/env python3
"""转写/下载的 API 后端（只用 Python 3 标准库，不装本地模型）。

团队口径（2026-10-20 月明定）：转写只用 API，不在本地装模型。
- 中文：Redfox「视频播放链接提取文案」 submit/videoUrl（0.06 元/条，折扣价 0.03）+ result（免费，返回毫秒时间戳分句）。
- 外语：先用平台自带字幕；没有时 Groq 免费层 whisper（单文件 ≤25MB，超限自动切段合并）；再不行 Deepgram 赠送额度（带 mip_opt_out=true）。
- 下载：Redfox 抖音下载接口 videoDownload/douyin（0.06 元/次）优先，匿名抓取链路作备选。

Key 只从环境变量读取：REDFOX_API_KEY / GROQ_API_KEY / DEEPGRAM_API_KEY（由团队凭据库注入）。
本模块不打印、不保存任何 Key，也不把带签名的播放地址写进日志。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
import uuid

REDFOX = 'https://redfox.hk'
PRICES = {  # 元/次，Redfox 文档标价（2026-10-21 核对）；实扣以 Redfox 调用记录为准
    'redfox_download': 0.06,
    'redfox_transcribe': 0.06,
}
GROQ_SAFE_MAX_BYTES = 24 * 1024 * 1024  # 留 1MB multipart 边界余量，严守 Groq 25MB 上限
RETRYABLE_HTTP_CODES = {408, 429, 500, 502, 503, 504}


class BackendError(RuntimeError):
    pass


class ImageNoteError(BackendError):
    """作品为图文/图集（无口播视频音轨），不可转写且不应继续回退。"""

    def __init__(self, message: str, *, video_id: str = '', title: str = '', image_count: int = 0):
        super().__init__(message)
        self.video_id = video_id
        self.title = title
        self.image_count = image_count


def env_key(name: str) -> str:
    key = os.environ.get(name, '').strip()
    if not key:
        raise BackendError(f'缺环境变量 {name}（从团队凭据库注入，不要写进文件）')
    return key


def _post_json(url: str, body: dict, headers: dict, timeout: int = 60) -> dict:
    req = urllib.request.Request(url, data=json.dumps(body).encode('utf-8'), method='POST',
                                 headers={'Content-Type': 'application/json', **headers})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        raise BackendError(f'HTTP {e.code}（响应正文已省略）') from None
    except urllib.error.URLError as e:
        raise BackendError(f'连接失败：{e.reason}') from None


def _redfox(path: str, body: dict) -> dict:
    res = _post_json(REDFOX + path, body, {'REDFOX_API_KEY': env_key('REDFOX_API_KEY')})
    if res.get('code') != 2000:
        raise BackendError(f"Redfox code={res.get('code')}：{str(res.get('msg'))[:80]}")
    return res.get('data') or {}


def redfox_balance_detail() -> dict | None:
    """免费接口：查 Redfox 剩余积分详情（1 元 = 10 积分）。查询失败返回 None。"""
    try:
        req = urllib.request.Request(REDFOX + '/story/web/points/api-key/balance',
                                     headers={'REDFOX_API_KEY': env_key('REDFOX_API_KEY')})
        with urllib.request.urlopen(req, timeout=30) as r:
            d = json.load(r).get('data') or {}
        avail = d.get('totalAvailablePoints')
        if avail is None:
            return None
        total = d.get('totalPoints') or d.get('rechargeTotalPoints')
        return {
            'available_points': float(avail),
            'total_points': float(total) if total not in (None, '', 0, '0') else None,
        }
    except Exception:
        return None


def redfox_balance() -> float | None:
    """免费接口：查 Redfox 剩余积分（1 元 = 10 积分）。失败返回 None。"""
    detail = redfox_balance_detail()
    return detail['available_points'] if detail else None


def check_redfox_balance(needed_yuan: float) -> dict | None:
    """调用付费 Redfox 接口前核查余额：不足本次预计费用、≤10%（已知总额时）或触及安全底线时停用。"""
    if needed_yuan <= 0:
        return None
    detail = redfox_balance_detail()
    if detail is None:
        return None
    avail_pts = detail['available_points']
    needed_pts = round(needed_yuan * 10.0, 2)
    env_total = os.environ.get('REDFOX_TOTAL_POINTS', '').strip()
    total_pts = detail.get('total_points') or (float(env_total) if env_total else None)
    env_min = os.environ.get('REDFOX_MIN_POINTS', '').strip()
    min_pts = float(env_min) if env_min else 10.0  # 默认底线 10 积分（约 1.00 元）

    if avail_pts < needed_pts:
        raise BackendError(
            f'Redfox 余额不足：剩余 {avail_pts:.1f} 积分（约 {avail_pts / 10:.2f} 元），'
            f'本条预计需 {needed_pts:.1f} 积分，已停用并请月明充值'
        )
    if total_pts and total_pts > 0 and (avail_pts / total_pts) <= 0.10:
        suggest_yuan = max(10, round(total_pts / 10))
        raise BackendError(
            f'Redfox 余额剩余 {avail_pts:.1f}/{total_pts:.1f} 积分（≤10%），'
            f'按团队规则停用，建议充值 ≥{suggest_yuan} 元'
        )
    if not total_pts and avail_pts <= min_pts:
        raise BackendError(
            f'Redfox 余额剩余 {avail_pts:.1f} 积分（约 {avail_pts / 10:.2f} 元，≤安全线 {min_pts:.1f} 积分），'
            f'已停用并请月明充值'
        )
    return {**detail, 'needed_points': needed_pts}


def redfox_download_douyin(share_url: str) -> dict:
    """Redfox 抖音下载：返回 {video_url, title, desc, author, duration_sec}。0.06 元/次。"""
    d = _redfox('/story/api/parseWork/videoDownload/douyin', {'url': share_url})
    resources = d.get('resources') or []
    video = next((r for r in resources if r.get('type') == 'video'), {})
    images = [r for r in resources if r.get('type') in ('image', 'images', 'photo')]
    url = d.get('videoUrl') or video.get('downloadUrl')
    title = (d.get('title') or d.get('desc') or '').strip()
    if not url:
        if images or d.get('workType') in ('image', 'note', 68):
            raise ImageNoteError(
                f'该作品是图文/图集（共 {len(images)} 张图），不包含可转写的视频音轨',
                title=title,
                image_count=len(images),
            )
        raise BackendError('Redfox 下载接口没有返回视频地址')
    raw_author = d.get('author') or d.get('authorInfo')
    if isinstance(raw_author, dict):
        author = (raw_author.get('nickname') or raw_author.get('name') or '').strip() or None
    elif isinstance(raw_author, str):
        author = raw_author.strip() or None
    else:
        author = None
    return {
        'video_url': url,
        'title': title,
        'desc': d.get('desc') or '',
        'author': author,
        'duration_sec': video.get('durationSeconds'),
        'cover': d.get('cover') or video.get('coverUrl'),
    }


def redfox_transcribe_url(play_url: str, timeout_sec: int = 600, poll_sec: float = 5.0) -> dict:
    """Redfox 视频播放链接提取文案：提交 0.06 元，查询免费。返回 {text, segments:[{start_ms,end_ms,text}], task_id}。"""
    task = _redfox('/story/api/parseWork/audioTextExtract/submit/videoUrl', {'url': play_url}).get('taskId')
    if not task:
        raise BackendError('Redfox 提文案没有返回 taskId')
    t0 = time.time()
    while True:
        d = _redfox('/story/api/parseWork/audioTextExtract/result', {'taskId': task})
        st = d.get('status')
        if st == 'succeeded':
            segs = [{'start_ms': s.get('start'), 'end_ms': s.get('end'), 'text': (s.get('textSeg') or '').strip()}
                    for s in d.get('stampSents') or []]
            return {'text': d.get('text') or '\n'.join(s['text'] for s in segs), 'segments': segs, 'task_id': task}
        if st == 'failed':
            raise BackendError(f"Redfox 提文案失败：{str(d.get('failReason'))[:120]}")
        if time.time() - t0 > timeout_sec:
            raise BackendError(f'Redfox 提文案超时（taskId={task}，可稍后用免费查询接口续查）')
        time.sleep(poll_sec)


def _multipart(fields: dict, file_field: str, file_path: str, mime: str) -> tuple[bytes, str]:
    b = uuid.uuid4().hex
    out = []
    for k, v in fields.items():
        out += [f'--{b}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode()]
    name = os.path.basename(file_path)
    with open(file_path, 'rb') as f:
        payload = f.read()
    out += [
        f'--{b}\r\nContent-Disposition: form-data; name="{file_field}"; filename="{name}"\r\n'
        f'Content-Type: {mime}\r\n\r\n'.encode(),
        payload,
        f'\r\n--{b}--\r\n'.encode(),
    ]
    return b''.join(out), f'multipart/form-data; boundary={b}'


def _probe_duration_ms(audio_path: str) -> int | None:
    """用 ffprobe 读取音频精确毫秒时长；不可用时返回 None。"""
    ffprobe = shutil.which('ffprobe')
    if not ffprobe:
        return None
    cmd = [
        ffprobe, '-v', 'error', '-show_entries', 'format=duration',
        '-of', 'default=noprint_wrappers=1:nokey=1', audio_path,
    ]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        return None
    try:
        return int(round(float(r.stdout.strip()) * 1000))
    except ValueError:
        return None


def split_audio_for_asr(mp3_path: str, out_dir: str, segment_sec: int = 600) -> list[tuple[str, int]]:
    """把超长音频按固定秒数切分为 16kHz 单声道 48k mp3 分片，返回 [(chunk_path, duration_ms), ...]。"""
    pattern = os.path.join(out_dir, 'chunk_%03d.mp3')
    cmd = [
        'ffmpeg', '-y', '-hide_banner', '-loglevel', 'error',
        '-i', mp3_path,
        '-vn', '-ar', '16000', '-ac', '1', '-b:a', '48k',
        '-f', 'segment', '-segment_time', str(segment_sec),
        pattern,
    ]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise BackendError(f'超长音频切段失败：{r.stderr[-300:]}')
    files = sorted(
        os.path.join(out_dir, name)
        for name in os.listdir(out_dir)
        if name.startswith('chunk_') and name.endswith('.mp3')
    )
    if not files:
        raise BackendError('超长音频切段未生成有效分片')
    chunks: list[tuple[str, int]] = []
    for idx, fpath in enumerate(files):
        dur_ms = _probe_duration_ms(fpath)
        if dur_ms is None:
            dur_ms = segment_sec * 1000 if idx < len(files) - 1 else 0
        chunks.append((fpath, dur_ms))
    return chunks


def _groq_transcribe_single(mp3_path: str, lang: str | None, max_retries: int = 2) -> dict:
    """单分片 Groq Whisper 请求，对 429/5xx 与瞬时断连做有限退避重试。"""
    if os.path.getsize(mp3_path) > 25 * 1024 * 1024:
        raise BackendError('音频分片仍超过 Groq 25MB 上限')
    fields = {'model': 'whisper-large-v3-turbo', 'response_format': 'verbose_json'}
    if lang:
        fields['language'] = lang
    body, ctype = _multipart(fields, 'file', mp3_path, 'audio/mpeg')
    headers = {'Authorization': 'Bearer ' + env_key('GROQ_API_KEY'), 'Content-Type': ctype}
    last_err: Exception | None = None
    for attempt in range(max_retries + 1):
        req = urllib.request.Request(
            'https://api.groq.com/openai/v1/audio/transcriptions',
            data=body,
            method='POST',
            headers=headers,
        )
        try:
            with urllib.request.urlopen(req, timeout=300) as r:
                d = json.load(r)
            segs = [
                {'start_ms': int(round(s['start'] * 1000)), 'end_ms': int(round(s['end'] * 1000)), 'text': s['text'].strip()}
                for s in d.get('segments') or []
            ]
            return {'text': d.get('text', '').strip(), 'segments': segs, 'language': d.get('language')}
        except urllib.error.HTTPError as e:
            code = e.code
            e.close()
            last_err = BackendError(f'Groq HTTP {code}（响应正文已省略）')
            if code in RETRYABLE_HTTP_CODES and attempt < max_retries:
                time.sleep(1.5 * (attempt + 1))
                continue
            raise last_err from None
        except urllib.error.URLError as e:
            last_err = BackendError(f'Groq 连接失败：{e.reason}')
            if attempt < max_retries:
                time.sleep(1.5 * (attempt + 1))
                continue
            raise last_err from None
    raise last_err or BackendError('Groq 转写失败')


def groq_transcribe(
    mp3_path: str,
    lang: str | None,
    *,
    max_bytes: int = GROQ_SAFE_MAX_BYTES,
    segment_sec: int = 600,
) -> dict:
    """Groq 免费层 whisper-large-v3-turbo。单文件 ≤25MB；超出时自动切段转写并偏移合并时间戳。"""
    if not mp3_path or not os.path.exists(mp3_path):
        raise BackendError('Groq 转写缺少本地音频文件')
    if os.path.getsize(mp3_path) <= max_bytes:
        res = _groq_transcribe_single(mp3_path, lang)
        res['chunk_count'] = 1
        return res

    tmpdir = tempfile.mkdtemp(prefix='groq_seg_')
    try:
        chunks = split_audio_for_asr(mp3_path, tmpdir, segment_sec=segment_sec)
        merged_texts: list[str] = []
        merged_segs: list[dict] = []
        detected_lang: str | None = lang
        offset_ms = 0
        for chunk_path, dur_ms in chunks:
            part = _groq_transcribe_single(chunk_path, lang)
            if not detected_lang and part.get('language'):
                detected_lang = part['language']
            if part['text']:
                merged_texts.append(part['text'])
            last_end_in_chunk = 0
            for seg in part['segments']:
                s_ms = int(seg['start_ms']) + offset_ms
                e_ms = int(seg['end_ms']) + offset_ms
                merged_segs.append({'start_ms': s_ms, 'end_ms': max(s_ms, e_ms), 'text': seg['text']})
                last_end_in_chunk = max(last_end_in_chunk, int(seg['end_ms']))
            offset_ms += dur_ms if dur_ms > 0 else last_end_in_chunk
        return {
            'text': '\n'.join(merged_texts).strip(),
            'segments': merged_segs,
            'language': detected_lang,
            'chunk_count': len(chunks),
        }
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def deepgram_transcribe(mp3_path: str, lang: str | None, max_retries: int = 1) -> dict:
    """Deepgram Nova-3，赠送额度；mip_opt_out=true 不参与模型改进。"""
    if not mp3_path or not os.path.exists(mp3_path):
        raise BackendError('Deepgram 转写缺少本地音频文件')
    q = 'model=nova-3&smart_format=true&utterances=true&mip_opt_out=true' + (
        f'&language={lang}' if lang else '&detect_language=true'
    )
    with open(mp3_path, 'rb') as f:
        audio_bytes = f.read()
    headers = {'Authorization': 'Token ' + env_key('DEEPGRAM_API_KEY'), 'Content-Type': 'audio/mpeg'}
    for attempt in range(max_retries + 1):
        req = urllib.request.Request('https://api.deepgram.com/v1/listen?' + q, data=audio_bytes, method='POST', headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=300) as r:
                d = json.load(r)
            res = d.get('results') or {}
            alt = ((res.get('channels') or [{}])[0].get('alternatives') or [{}])[0]
            segs = [
                {'start_ms': int(round(u['start'] * 1000)), 'end_ms': int(round(u['end'] * 1000)), 'text': u['transcript'].strip()}
                for u in res.get('utterances') or []
            ]
            return {'text': alt.get('transcript', '').strip(), 'segments': segs}
        except urllib.error.HTTPError as e:
            if e.code in RETRYABLE_HTTP_CODES and attempt < max_retries:
                time.sleep(1.5 * (attempt + 1))
                continue
            raise BackendError(f'Deepgram HTTP {e.code}（响应正文已省略）') from None
        except urllib.error.URLError as e:
            if attempt < max_retries:
                time.sleep(1.5 * (attempt + 1))
                continue
            raise BackendError(f'Deepgram 连接失败：{e.reason}') from None
    raise BackendError('Deepgram 转写失败')


def to_srt(segments: list[dict]) -> str:
    def ts(ms):
        ms = int(ms or 0)
        h, ms = divmod(ms, 3600000)
        m, ms = divmod(ms, 60000)
        s, ms = divmod(ms, 1000)
        return f'{h:02d}:{m:02d}:{s:02d},{ms:03d}'

    return '\n'.join(
        f"{i}\n{ts(s['start_ms'])} --> {ts(s['end_ms'])}\n{s['text']}\n"
        for i, s in enumerate(segments, 1)
        if s.get('text')
    )
