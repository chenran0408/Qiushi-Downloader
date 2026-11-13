import tempfile
import unittest
import zipfile
from datetime import datetime
from io import BytesIO
from pathlib import Path

from PIL import Image

from epub_builder import build_issue_epub, build_year_epub, _mobile_image_bytes
from models import Article, Issue

class FakeFetcher:
    def fetch(self, url):
        return b'fake-image', 'image/png'

class EpubBuilderTests(unittest.TestCase):
    def issue(self, no, title):
        return Issue('test', 2025, no, f'2025年第{no}期', 'https://example.test/toc', datetime(2025, 1, no), cover_image_url='https://example.test/cover.png', articles=[Article(title, 'https://example.test/article', html_body='<p>\u2003\u2003中文<strong>正文</strong></p><script>alert(1)</script><style>bad{}</style><img src="/a.png" alt="图">')])

    def test_single_issue_is_offline_and_semantic(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / 'issue.epub'
            report = build_issue_epub(self.issue(1, '第一篇'), out, fetcher=FakeFetcher())
            self.assertTrue(report.complete)
            with zipfile.ZipFile(out) as z:
                names = set(z.namelist())
                self.assertIn('mimetype', names)
                self.assertIn('OEBPS/nav.xhtml', names)
                body = z.read(next(n for n in names if n.endswith('.xhtml') and '0001' in n)).decode()
                self.assertIn('<strong>正文</strong>', body)
                self.assertNotIn('alert(1)', body)
                self.assertNotIn('bad{}', body)
                self.assertNotIn('\u2003', body)
                self.assertIn('原文链接', body)
                self.assertTrue(any(n.startswith('OEBPS/images/') for n in names))
                self.assertIn('cover-image', z.read('OEBPS/content.opf').decode())
                self.assertIn('第一篇', z.read('OEBPS/nav.xhtml').decode())
            self.assertFalse((Path(d) / '.issue.epub.tmp').exists())

    def test_year_book_orders_issues_and_reports_missing(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / 'year.epub'
            first, second = self.issue(2, '第二篇'), self.issue(1, '第一篇')
            report = build_year_epub([first, second], out, fetcher=FakeFetcher())
            self.assertTrue(report.complete)
            data = zipfile.ZipFile(out).read('OEBPS/nav.xhtml').decode()
            self.assertLess(data.index('issue-001'), data.index('issue-002'))
            first.failed_articles.append('缺失文章')
            report = build_year_epub([first, second], Path(d) / 'incomplete.epub', fetcher=FakeFetcher())
            self.assertFalse(report.complete)
            self.assertIn('缺失文章', report.missing_articles)

    def test_site_disclaimer_is_removed(self):
        with tempfile.TemporaryDirectory() as d:
            issue = self.issue(1, '正文')
            issue.articles.append(Article('网站声明', 'https://example.test/disclaimer', html_body='<p>网站声明</p>'))
            out = Path(d) / 'clean.epub'
            report = build_issue_epub(issue, out, fetcher=FakeFetcher())
            self.assertTrue(report.complete)
            with zipfile.ZipFile(out) as z:
                text = z.read('OEBPS/nav.xhtml').decode()
                self.assertIn('正文', text)
                self.assertNotIn('网站声明', text)

    def test_mobile_jpeg_profile_keeps_dimensions_within_limit(self):
        image = Image.new('RGB', (2400, 1200), (120, 80, 40)); source = BytesIO(); image.save(source, 'JPEG', quality=100)
        optimized = _mobile_image_bytes(source.getvalue(), 'image/jpeg')
        result = Image.open(BytesIO(optimized))
        self.assertEqual(result.width, 1600)
        self.assertLess(len(optimized), len(source.getvalue()))

if __name__ == '__main__':
    unittest.main()
