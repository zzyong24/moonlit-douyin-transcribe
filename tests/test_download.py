import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

SKILL_ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(SKILL_ROOT / 'scripts'))

spec = importlib.util.spec_from_file_location('douyin', SKILL_ROOT / 'scripts/douyin_transcribe.py')
dy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dy)
ab = dy.ab


class Response(io.BytesIO):
    def __init__(self, data: bytes, headers: dict | None = None, status: int = 200, url: str = 'https://example.test/video'):
        super().__init__(data)
        self.headers = headers or {}
        self.status = status
        self.url = url

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


class DownloadTests(unittest.TestCase):
    def run_download(self, responses):
        with tempfile.TemporaryDirectory(prefix='download with spaces ') as d:
            path = Path(d) / 'video.mp4'
            with patch.object(dy, 'urlopen', side_effect=responses) as mock:
                dy.download_mp4('https://example.test/video', str(path))
                return path.read_bytes(), mock.call_args_list

    def test_partial_206_is_resumed(self):
        data, calls = self.run_download([
            Response(b'abc', {'Content-Length': '3', 'Content-Range': 'bytes 0-2/6'}, 206),
            Response(b'def', {'Content-Length': '3', 'Content-Range': 'bytes 3-5/6'}, 206),
        ])
        self.assertEqual(data, b'abcdef')
        self.assertEqual(calls[1].args[0].get_header('Range'), 'bytes=3-')

    def test_full_200_finishes_once(self):
        data, calls = self.run_download([Response(b'abc', {'Content-Length': '3'}, 200)])
        self.assertEqual(data, b'abc')
        self.assertEqual(len(calls), 1)

    def test_wrong_range_is_rejected(self):
        with self.assertRaisesRegex(RuntimeError, '不匹配'):
            self.run_download([
                Response(b'abc', {'Content-Range': 'bytes 0-2/6'}, 206),
                Response(b'def', {'Content-Range': 'bytes 0-2/6'}, 206),
            ])

    def test_truncated_response_is_rejected(self):
        with self.assertRaisesRegex(RuntimeError, '字节数不足'):
            self.run_download([Response(b'ab', {'Content-Length': '3'}, 200)])


class LinkAndShareTextTests(unittest.TestCase):
    def test_share_command_text_extracts_short_link_and_follows_redirect_with_retry(self):
        share_text = (
            '0.82 08/16 I@v.Sy 复制打开抖音，看看【拉斐尔的作品】分享5个定时任务 '
            'https://v.douyin.com/Bed46kFi73o/，长按复制此条消息！'
        )
        responses = [
            urllib.error.URLError('TLS EOF'),
            Response(b'', {}, 200, url='https://www.iesdouyin.com/share/video/7688329564770110762/?region=CN'),
        ]
        with patch.object(dy, 'urlopen', side_effect=responses), patch.object(dy.time, 'sleep'):
            target = dy.resolve_douyin_target(share_text)
        self.assertEqual(target['video_id'], '7688329564770110762')
        self.assertEqual(target['clean_url'], 'https://v.douyin.com/Bed46kFi73o/')
        self.assertEqual(target['url_kind'], 'video')

    def test_note_url_and_modal_id_parsed_without_network(self):
        note_target = dy.resolve_douyin_target('https://www.douyin.com/note/7683423215574387057')
        self.assertEqual(note_target['video_id'], '7683423215574387057')
        self.assertEqual(note_target['url_kind'], 'note')

        modal_target = dy.resolve_douyin_target('https://www.douyin.com/discover?modal_id=7647796267023650084')
        self.assertEqual(modal_target['video_id'], '7647796267023650084')


class ImageNoteInterceptionTests(unittest.TestCase):
    def test_iesdouyin_image_post_with_bgm_is_blocked_and_does_not_fallback_to_browser(self):
        router_payload = {
            'loaderData': {
                'note_(id)/page': {
                    'videoInfoRes': {
                        'item_list': [{
                            'aweme_id': '7683423215574387057',
                            'aweme_type': 68,
                            'desc': '自媒体能做好的底色，是自我取悦。',
                            'images': [{'uri': 'img1'}, {'uri': 'img2'}, {'uri': 'img3'}],
                            'video': {'play_addr': {'url_list': ['https://bgm.example.test/music.mp3']}},
                        }]
                    }
                }
            }
        }
        html = f'<script>window._ROUTER_DATA = {json.dumps(router_payload)}</script>'.encode('utf-8')
        with patch.object(dy, 'urlopen', return_value=Response(html, {}, 200)), \
             patch.object(dy, 'fetch_metadata_mobile_feed') as mock_feed, \
             patch.object(dy, 'fetch_metadata_browser') as mock_browser:
            with self.assertRaises(dy.ImageNoteError) as ctx:
                dy.fetch_metadata('7683423215574387057', browser_fallback=True)
            self.assertEqual(ctx.exception.image_count, 3)
            mock_feed.assert_not_called()
            mock_browser.assert_not_called()

    def test_redfox_image_resources_raise_image_note_error(self):
        fake_data = {
            'title': '产品和交付是生命线',
            'resources': [{'type': 'image', 'downloadUrl': 'https://img/1.jpg'}, {'type': 'image', 'downloadUrl': 'https://img/2.jpg'}],
        }
        with patch.object(ab, '_redfox', return_value=fake_data):
            with self.assertRaises(ab.ImageNoteError) as ctx:
                ab.redfox_download_douyin('https://www.douyin.com/note/7680883972331978673')
            self.assertEqual(ctx.exception.image_count, 2)

    def test_cli_exits_with_code_3_on_image_note(self):
        router_payload = {
            'loaderData': {
                'note_(id)/page': {
                    'videoInfoRes': {
                        'item_list': [{
                            'aweme_id': '7683423215574387057',
                            'aweme_type': 68,
                            'desc': '图文测试标题',
                            'images': [{'uri': '1'}, {'uri': '2'}],
                        }]
                    }
                }
            }
        }
        code = (
            'import json, sys, unittest.mock as m\n'
            f'sys.path.insert(0, {str(SKILL_ROOT / "scripts")!r})\n'
            'import douyin_transcribe as dy\n'
            f'html = {("<script>window._ROUTER_DATA = " + json.dumps(router_payload) + "</script>")!r}.encode()\n'
            'class R:\n'
            '  def __enter__(self): return self\n'
            '  def __exit__(self, *a): pass\n'
            '  def read(self): return html\n'
            'with m.patch.object(dy, "urlopen", return_value=R()), m.patch.object(sys, "argv", ["douyin_transcribe.py", "https://www.douyin.com/note/7683423215574387057"]):\n'
            '  dy.main()\n'
        )
        proc = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True)
        self.assertEqual(proc.returncode, 3)
        payload = json.loads(proc.stdout.strip())
        self.assertEqual(payload['error'], 'image_note_not_supported')
        self.assertEqual(payload['image_count'], 2)


class CacheAndChunkRetryTests(unittest.TestCase):
    def test_local_audio_cache_is_reused_after_interrupted_run(self):
        with tempfile.TemporaryDirectory() as d:
            out_dir = Path(d) / '2026-11-18' / '123456'
            cache_mp3 = out_dir / '.cache' / '123456.mp3'
            cache_mp3.parent.mkdir(parents=True, exist_ok=True)
            cache_mp3.write_bytes(b'cached-mp3-bytes')

            with patch.object(dy, 'ffmpeg_stream_to_audio') as mock_ffmpeg:
                mp3_path, mp4_path, info = dy.prepare_local_media(
                    out_dir, '123456', 'https://example.test/play.mp4', mode='text', need_local_mp3=True
                )
            mock_ffmpeg.assert_not_called()
            self.assertEqual(mp3_path, str(cache_mp3))
            self.assertIsNone(mp4_path)
            self.assertTrue(info['reused_mp3'])

    def test_oversized_groq_audio_splits_and_offsets_timestamps_with_retry(self):
        with tempfile.TemporaryDirectory() as d:
            big_mp3 = Path(d) / 'big.mp3'
            big_mp3.write_bytes(b'x' * 2048)

            def fake_split(_src, tmpdir, segment_sec=600):
                c0 = os.path.join(tmpdir, 'chunk_000.mp3')
                c1 = os.path.join(tmpdir, 'chunk_001.mp3')
                Path(c0).write_bytes(b'part0')
                Path(c1).write_bytes(b'part1')
                return [(c0, 600_000), (c1, 120_000)]

            http_429 = urllib.error.HTTPError('https://api.groq.com', 429, 'Too Many Requests', {}, None)
            resp0 = Response(json.dumps({
                'text': 'Hello first part.',
                'language': 'en',
                'segments': [{'start': 0.5, 'end': 4.2, 'text': 'Hello first part.'}],
            }).encode('utf-8'))
            resp1 = Response(json.dumps({
                'text': 'Second chunk here.',
                'language': 'en',
                'segments': [{'start': 1.0, 'end': 3.5, 'text': 'Second chunk here.'}],
            }).encode('utf-8'))

            with patch.dict(os.environ, {'GROQ_API_KEY': 'test_dummy_key'}), \
                 patch.object(ab, 'split_audio_for_asr', side_effect=fake_split), \
                 patch.object(ab.urllib.request, 'urlopen', side_effect=[http_429, resp0, resp1]), \
                 patch.object(ab.time, 'sleep'):
                result = ab.groq_transcribe(str(big_mp3), 'en', max_bytes=1024, segment_sec=600)

            self.assertEqual(result['chunk_count'], 2)
            self.assertIn('Hello first part.', result['text'])
            self.assertIn('Second chunk here.', result['text'])
            self.assertEqual(result['segments'][0]['start_ms'], 500)
            self.assertEqual(result['segments'][1]['start_ms'], 601_000)
            self.assertEqual(result['segments'][1]['end_ms'], 603_500)

    def test_redfox_balance_guard_blocks_low_points(self):
        with patch.object(ab, 'redfox_balance_detail', return_value={'available_points': 8.0, 'total_points': 100.0}):
            with self.assertRaisesRegex(ab.BackendError, '≤10%'):
                ab.check_redfox_balance(0.12)


class TranscriptSeparationAndHandoffTests(unittest.TestCase):
    def test_raw_transcript_untouched_and_corrected_layer_plus_handoff_saved(self):
        with tempfile.TemporaryDirectory() as d:
            out_dir = Path(d) / 'out'
            meta = {
                'video_id': '7685606580478487915',
                'title': 'Agent 安全读密钥实战',
                'author': '示例创作者',
                'duration_sec': 62.4,
                'play_addr_src': 'mobile_feed/play_addr',
                'fetch_chain': 'douyin.com/mobile-feed',
                'direct_url': 'https://signed.example.test/secret_token_url',
            }
            raw_text = '先装好 BTW 命令行，再让 AZIN 自动读取环境变量里的密钥。'
            segments = [
                {'start_ms': 0, 'end_ms': 3200, 'text': '先装好 BTW 命令行，'},
                {'start_ms': 3200, 'end_ms': 7100, 'text': '再让 AZIN 自动读取环境变量里的密钥。'},
            ]
            asr_info = {
                'backend': 'redfox',
                'language': 'zh',
                'segments': 2,
                'raw_chars': len(raw_text),
                'normalized_chars': len(raw_text),
                'opencc_t2s': False,
                'cost_yuan': 0.06,
                'task_id': 'rf_998877',
            }
            glossary = [
                {'wrong': 'BTW', 'right': 'Bitwarden', 'reason': '画面终端命令 bws 与产品名核对'},
                {'wrong': 'AZIN', 'right': 'Agent', 'reason': '英文专名同音误识'},
            ]
            saved = dy.save_outputs(out_dir, meta, raw_text, asr_info, None, None, segments, glossary_rules=glossary)

            raw_txt_content = Path(saved['txt']).read_text(encoding='utf-8')
            raw_srt_content = Path(saved['srt']).read_text(encoding='utf-8')
            corr_content = Path(saved['corrected_md']).read_text(encoding='utf-8')
            meta_json = json.loads(Path(saved['metadata']).read_text(encoding='utf-8'))

            # 1) 原始逐字稿与 SRT 严禁被术语表静默改写
            self.assertIn('BTW', raw_txt_content)
            self.assertIn('AZIN', raw_txt_content)
            self.assertNotIn('Bitwarden', raw_txt_content)
            self.assertIn('AZIN', raw_srt_content)

            # 2) 独立纠错稿包含修正正文与逐项依据表
            self.assertIn('先装好 Bitwarden 命令行，再让 Agent 自动读取环境变量里的密钥。', corr_content)
            self.assertIn('| `BTW` | `Bitwarden` | 1 | 画面终端命令 bws 与产品名核对 |', corr_content)

            # 3) 带签名的临时播放地址绝不落盘，下游交接字段齐全且 visual 锁定为 not_viewed
            self.assertNotIn('direct_url', meta_json)
            self.assertNotIn('secret_token_url', Path(saved['metadata']).read_text(encoding='utf-8'))
            br = meta_json['handoff']['benchmark_research']
            self.assertEqual(br['platform'], 'douyin')
            self.assertEqual(br['work_id'], '7685606580478487915')
            self.assertEqual(br['content'], 'text_available')
            self.assertEqual(br['visual'], 'not_viewed')
            self.assertEqual(br['job']['task_id'], 'rf_998877')
            crf = meta_json['handoff']['creator_return_flow']
            self.assertIn('notes/成片实录文案.md', crf['recommended_target'])
            self.assertIn('finalTranscript', crf['authority_boundary'])


if __name__ == '__main__':
    unittest.main()

class BrowserPolicyTests(unittest.TestCase):
    def test_default_failure_never_launches_legacy_browser(self):
        with patch.object(dy, "fetch_metadata_iesdouyin", side_effect=dy.MetadataParseError("fixture")), \
             patch.object(dy, "fetch_metadata_mobile_feed", side_effect=RuntimeError("fixture")), \
             patch.object(dy, "fetch_metadata_browser") as browser:
            with self.assertRaises(RuntimeError):
                dy.fetch_metadata("123456")
            browser.assert_not_called()

    def test_explicit_legacy_browser_route_requires_ego(self):
        with patch.object(dy.subprocess, "run") as run:
            with self.assertRaisesRegex(RuntimeError, "ego-browser"):
                dy.fetch_metadata_browser("123456")
            run.assert_not_called()
