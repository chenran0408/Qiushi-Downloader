import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from models import Article, Issue
from epub_builder import build_issue_epub
from quality import validate_epub

class QualityTests(unittest.TestCase):
    def test_generated_package_passes_local_validation(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / 'x.epub'
            issue = Issue('x', 2025, 1, '2025年第1期', 'https://example.test', datetime.now(), articles=[Article('标题', 'https://example.test/a', html_body='<p>正文</p>')])
            build_issue_epub(issue, p)
            result = validate_epub(p)
            self.assertTrue(result['valid'], result)

    def test_invalid_package_is_reported(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / 'bad.epub'; p.write_bytes(b'bad')
            self.assertFalse(validate_epub(p)['valid'])

if __name__ == '__main__': unittest.main()
