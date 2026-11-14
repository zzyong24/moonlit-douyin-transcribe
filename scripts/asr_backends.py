#!/usr/bin/env python3
"""转写/下载的 API 后端（只用 Python 3 标准库，不装本地模型）。

团队口径（2026-10-20 月明定）：转写只用 API，不在本地装模型。
- 中文：Redfox「视频播放链接提取文案」 submit/videoUrl（0.06 元/条，折扣价 0.03）+ result（免费，返回毫秒时间戳分句）。
- 外语：先用平台自带字幕；没有时 Groq 免费层 whisper（单文件 ≤25MB）；再不行 Deepgram 赠送额度（带 mip_opt_out=true）。
- 下载：Redfox 抖音下载接口 videoDownload/douyin（0.06 元/次）优先，匿名抓取链路作备选。

Key 只从环境变量读取：REDFOX_API_KEY / GROQ_API_KEY / DEEPGRAM_API_KEY（由团队凭据库注入）。
本模块不打印、不保存任何 Key，也不把带签名的播放地址写进日志。
"""
from __future__ import annotations

import json
import os
import time
import uuid
import urllib.error
import urllib.request

REDFOX = 'https://redfox.hk'
PRICES = {  # 元/次，Redfox 文档标价（2026-10-21 核对）；实扣以 Redfox 调用记录为准
    'redfox_download': 0.06,
    'redfox_transcribe': 0.06,
}


class BackendError(RuntimeError):
    pass


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


def redfox_balance() -> float | None:
    """免费接口：查 Redfox 剩余积分（1 元 = 10 积分）。失败返回 None。"""
    try:
        req = urllib.request.Request(REDFOX + '/story/web/points/api-key/balance',
                                     headers={'REDFOX_API_KEY': env_key('REDFOX_API_KEY')})
        with urllib.request.urlopen(req, timeout=30) as r:
            d = json.load(r).get('data') or {}
        return float(d.get('totalAvailablePoints'))
    except Exception:
        return None


def redfox_download_douyin(share_url: str) -> dict:
    """Redfox 抖音下载：返回 {video_url, title, desc, duration_sec}。0.06 元/次。"""
    d = _redfox('/story/api/parseWork/videoDownload/douyin', {'url': share_url})
    video = next((r for r in d.get('resources') or [] if r.get('type') == 'video'), {})
    url = d.get('videoUrl') or video.get('downloadUrl')
    if not url:
        raise BackendError('Redfox 下载接口没有返回视频地址')
    return {'video_url': url, 'title': d.get('title') or d.get('desc') or '', 'desc': d.get('desc') or '',
            'duration_sec': video.get('durationSeconds'), 'cover': d.get('cover') or video.get('coverUrl')}


def redfox_transcribe_url(play_url: str, timeout_sec: int = 600, poll_sec: float = 5.0) -> dict:
    """Redfox 视频播放链接提取文案：提交 0.06 元，查询免费。返回 {text, segments:[{start_ms,end_ms,text}]}。"""
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
    out += [f'--{b}\r\nContent-Disposition: form-data; name="{file_field}"; filename="{name}"\r\n'
            f'Content-Type: {mime}\r\n\r\n'.encode(), open(file_path, 'rb').read(), f'\r\n--{b}--\r\n'.encode()]
    return b''.join(out), f'multipart/form-data; boundary={b}'


def groq_transcribe(mp3_path: str, lang: str | None) -> dict:
    """Groq 免费层 whisper-large-v3-turbo。单文件 ≤25MB（先转 16k 单声道 mp3）。"""
    if os.path.getsize(mp3_path) > 25 * 1024 * 1024:
        raise BackendError('音频超过 Groq 25MB 上限，先切段或降码率')
    fields = {'model': 'whisper-large-v3-turbo', 'response_format': 'verbose_json'}
    if lang:
        fields['language'] = lang
    body, ctype = _multipart(fields, 'file', mp3_path, 'audio/mpeg')
    req = urllib.request.Request('https://api.groq.com/openai/v1/audio/transcriptions', data=body, method='POST',
                                 headers={'Authorization': 'Bearer ' + env_key('GROQ_API_KEY'), 'Content-Type': ctype})
    try:
        with urllib.request.urlopen(req, timeout=300) as r:
            d = json.load(r)
    except urllib.error.HTTPError as e:
        raise BackendError(f'Groq HTTP {e.code}（响应正文已省略）') from None
    segs = [{'start_ms': int(s['start'] * 1000), 'end_ms': int(s['end'] * 1000), 'text': s['text'].strip()}
            for s in d.get('segments') or []]
    return {'text': d.get('text', '').strip(), 'segments': segs, 'language': d.get('language')}


def deepgram_transcribe(mp3_path: str, lang: str | None) -> dict:
    """Deepgram Nova-3，赠送额度；mip_opt_out=true 不参与模型改进。"""
    q = 'model=nova-3&smart_format=true&utterances=true&mip_opt_out=true' + (f'&language={lang}' if lang else '&detect_language=true')
    req = urllib.request.Request('https://api.deepgram.com/v1/listen?' + q, data=open(mp3_path, 'rb').read(), method='POST',
                                 headers={'Authorization': 'Token ' + env_key('DEEPGRAM_API_KEY'), 'Content-Type': 'audio/mpeg'})
    try:
        with urllib.request.urlopen(req, timeout=300) as r:
            d = json.load(r)
    except urllib.error.HTTPError as e:
        raise BackendError(f'Deepgram HTTP {e.code}（响应正文已省略）') from None
    res = d.get('results') or {}
    alt = ((res.get('channels') or [{}])[0].get('alternatives') or [{}])[0]
    segs = [{'start_ms': int(u['start'] * 1000), 'end_ms': int(u['end'] * 1000), 'text': u['transcript'].strip()}
            for u in res.get('utterances') or []]
    return {'text': alt.get('transcript', '').strip(), 'segments': segs}


def to_srt(segments: list[dict]) -> str:
    def ts(ms):
        ms = int(ms or 0); h, ms = divmod(ms, 3600000); m, ms = divmod(ms, 60000); s, ms = divmod(ms, 1000)
        return f'{h:02d}:{m:02d}:{s:02d},{ms:03d}'
    return '\n'.join(f"{i}\n{ts(s['start_ms'])} --> {ts(s['end_ms'])}\n{s['text']}\n" for i, s in enumerate(segments, 1) if s.get('text'))
