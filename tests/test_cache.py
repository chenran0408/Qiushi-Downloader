import tempfile
import unittest
from pathlib import Path
from scraper import QstheorySource
from epub_builder import ResourceFetcher

class Response:
    content = '<html><body>测试</body></html>'.encode('utf-8')
    text = '<html><body>测试</body></html>'
    apparent_encoding = 'utf-8'
    encoding = 'utf-8'
    def raise_for_status(self): pass

class Session:
    def __init__(self): self.calls = 0
    def get(self, url, timeout): self.calls += 1; return Response()

class CacheTests(unittest.TestCase):
    def test_resource_cache_reuses_bytes(self):
        with tempfile.TemporaryDirectory() as d:
            f = ResourceFetcher(Path(d)); calls = []
            class S:
                def get(self, url, timeout): calls.append(url); return type('R', (), {'content':b'x','headers':{'Content-Type':'image/png'},'raise_for_status':lambda self:None})()
            f.session = S(); f.fetch('https://example.test/a.png'); f.fetch('https://example.test/a.png')
            self.assertEqual(calls, ['https://example.test/a.png'])

    def test_html_cache_reuses_page(self):
        with tempfile.TemporaryDirectory() as d:
            source = QstheorySource(delay_seconds=0, cache_dir=d); session = Session(); source.session = session
            source._get('https://example.test/page'); source._get('https://example.test/page')
            self.assertEqual(session.calls, 1)

if __name__ == '__main__': unittest.main()
