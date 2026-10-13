import importlib.util
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('douyin', Path(__file__).parents[1] / 'scripts/douyin_transcribe.py')
dy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dy)

class Response(io.BytesIO):
    def __init__(self, data, headers, status=206):
        super().__init__(data)
        self.headers = headers
        self.status = status

class DownloadTests(unittest.TestCase):
    def run_download(self, responses):
        with tempfile.TemporaryDirectory(prefix='download with spaces ') as d:
            path = Path(d) / 'video.mp4'
            with patch.object(dy, 'urlopen', side_effect=responses) as mock:
                dy.download_mp4('https://example.test/video', str(path))
                return path.read_bytes(), mock.call_args_list

    def test_partial_206_is_resumed(self):
        data, calls = self.run_download([
            Response(b'abc', {'Content-Length':'3', 'Content-Range':'bytes 0-2/6'}),
            Response(b'def', {'Content-Length':'3', 'Content-Range':'bytes 3-5/6'})])
        self.assertEqual(data, b'abcdef')
        self.assertEqual(calls[1].args[0].get_header('Range'), 'bytes=3-')

    def test_full_200_finishes_once(self):
        data, calls = self.run_download([Response(b'abc', {'Content-Length':'3'}, 200)])
        self.assertEqual(data, b'abc')
        self.assertEqual(len(calls), 1)

    def test_wrong_range_is_rejected(self):
        with self.assertRaisesRegex(RuntimeError, '不匹配'):
            self.run_download([Response(b'abc', {'Content-Range':'bytes 0-2/6'}),
                               Response(b'def', {'Content-Range':'bytes 0-2/6'})])

    def test_truncated_response_is_rejected(self):
        with self.assertRaisesRegex(RuntimeError, '字节数不足'):
            self.run_download([Response(b'ab', {'Content-Length':'3'}, 200)])

if __name__ == '__main__':
    unittest.main()
