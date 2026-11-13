from __future__ import annotations

import hashlib
import html
import mimetypes
import os
import re
import time
import uuid
import zipfile
from io import BytesIO
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

from models import Issue

RESOURCE_CACHE_VERSION = "resource-v2"
_SITE_DISCLAIMER_TITLES = {"网站声明", "网站声明页"}
MOBILE_MAX_IMAGE_WIDTH = 1600
MOBILE_JPEG_QUALITY = 88

def _is_site_disclaimer_title(title: str) -> bool:
    return title.strip().strip("【】[]()（）") in _SITE_DISCLAIMER_TITLES

_CSS = '''body{font-family:serif;line-height:1.85;margin:5%;color:inherit}
h1{text-align:center;font-size:1.55em;margin:1.5em 0 .8em}h2,h3{margin:1.6em 0 .6em}
p{margin:0 0 1em;text-indent:2em;text-align:justify}blockquote{margin:1em 0;padding:.2em 1em;border-left:.2em solid #b71c1c}
figure{margin:1em 0;text-align:center}img{max-width:100%;height:auto}figcaption{font-size:.9em;color:#666}
a{color:inherit}.cover{text-align:center;margin-top:25vh}.cover h1{font-size:2em}.toc li{margin:.5em 0}.issue-title{page-break-before:always;text-align:center}'''
_ALLOWED = {"p","div","span","strong","b","em","i","u","a","h1","h2","h3","h4","h5","h6","ul","ol","li","blockquote","figure","figcaption","table","thead","tbody","tr","th","td","sup","sub","br","img"}

@dataclass
class QualityReport:
    expected_articles: int
    written_articles: int = 0
    missing_articles: list[str] = field(default_factory=list)
    failed_resources: list[str] = field(default_factory=list)
    duplicate_titles: list[str] = field(default_factory=list)
    @property
    def complete(self) -> bool:
        return not self.missing_articles and not self.failed_resources and self.written_articles == self.expected_articles
    def as_dict(self) -> dict:
        return {"complete":self.complete,"expected_articles":self.expected_articles,"written_articles":self.written_articles,"missing_articles":self.missing_articles,"failed_resources":self.failed_resources,"duplicate_titles":self.duplicate_titles}

class ResourceFetcher:
    def __init__(self, cache_dir: Path|None=None, retries: int=3, timeout: float=20.0):
        self.cache_dir = Path(cache_dir) if cache_dir else None
        if self.cache_dir: self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.retries, self.timeout = retries, timeout
        self.session = requests.Session(); self.session.headers.update({"User-Agent":"QiushiDownloader/2.0"})
    def fetch(self, url: str) -> tuple[bytes,str]:
        key = hashlib.sha256(f"{RESOURCE_CACHE_VERSION}:{url}".encode()).hexdigest(); cached = self.cache_dir / key if self.cache_dir else None; meta = self.cache_dir / f"{key}.meta" if self.cache_dir else None
        if cached and cached.exists() and meta and meta.exists(): return cached.read_bytes(), meta.read_text(encoding="utf-8")
        last = None
        for attempt in range(self.retries):
            try:
                r = self.session.get(url, timeout=self.timeout); r.raise_for_status(); ctype = _normalise_media_type(r.headers.get("Content-Type","").split(";",1)[0].lower())
                if cached and meta: cached.write_bytes(r.content); meta.write_text(ctype, encoding="utf-8")
                return r.content, ctype
            except (requests.RequestException, OSError) as exc:
                last = exc
                if attempt + 1 < self.retries: time.sleep(0.5 * (2 ** attempt))
        raise RuntimeError(f"资源下载失败: {url}: {last}")

def _normalise_media_type(media_type: str) -> str:
    return {"image/jpg": "image/jpeg", "image/pjpeg": "image/jpeg"}.get(media_type, media_type or "application/octet-stream")

def _mobile_image_bytes(data: bytes, media_type: str) -> bytes:
    """Prepare images for phones/tablets without cropping content."""
    if media_type not in {"image/png", "image/jpeg"}:
        return data
    try:
        from PIL import Image
        image = Image.open(BytesIO(data))
        if getattr(image, "is_animated", False):
            return data
        if image.width > MOBILE_MAX_IMAGE_WIDTH:
            height = round(image.height * MOBILE_MAX_IMAGE_WIDTH / image.width)
            image = image.resize((MOBILE_MAX_IMAGE_WIDTH, height), Image.Resampling.LANCZOS)
        optimized = BytesIO()
        if media_type == "image/jpeg":
            image.convert("RGB").save(optimized, format="JPEG", quality=MOBILE_JPEG_QUALITY, optimize=True, progressive=True, subsampling=2)
        else:
            image.save(optimized, format="PNG", optimize=True)
        candidate = optimized.getvalue()
        return candidate if len(candidate) < len(data) else data
    except Exception:
        return data

def _clean_fragment(fragment: str, base_url: str, fetcher: ResourceFetcher, report: QualityReport, resources: dict[str,tuple[str,bytes,str]]) -> str:
    soup = BeautifulSoup(fragment or "", "lxml"); root = soup.body or soup
    for node in list(root.find_all(True)):
        if node.name in {"script", "style", "iframe", "object", "embed", "form", "noscript"}: node.decompose(); continue
        if node.name not in _ALLOWED: node.unwrap(); continue
        for attr in list(node.attrs):
            if attr not in {"href","src","alt","title","colspan","rowspan"}: del node.attrs[attr]
        if node.name == "a" and node.get("href","").lower().startswith(("javascript:","data:")): node.unwrap()
        if node.name == "img":
            src = node.get("src", "") or node.get("data-src", "")
            if not src: node.decompose(); continue
            absolute = src if urlparse(src).scheme else requests.compat.urljoin(base_url, src)
            try:
                data, ctype = fetcher.fetch(absolute); ctype = _normalise_media_type(ctype); data = _mobile_image_bytes(data, ctype); ext = mimetypes.guess_extension(ctype) or Path(urlparse(absolute).path).suffix or ".bin"; name = f"images/{hashlib.sha256(data).hexdigest()[:16]}{ext.lower()}"
                resources.setdefault(name, (ctype, data, absolute)); node.attrs = {"src":f"../{name}","alt":node.get("alt","")}
            except Exception: report.failed_resources.append(absolute); node.decompose()
    for block in root.find_all(["p", "li", "blockquote"]):
        first_text = block.find(string=True)
        if first_text:
            block_text = str(first_text)
            first_text.replace_with(re.sub(r"^[\s\u00a0\u2003\u3000]+", "", block_text))
    cleaned = "".join(str(x) for x in root.contents).strip()
    for marker in ("网站编辑", "网站声明"):
        marker_index = cleaned.find(marker)
        if marker_index >= 0:
            cleaned = cleaned[:marker_index].rstrip()
    return cleaned

def _xhtml(title: str, body: str, stylesheet: str = "../styles/style.css") -> bytes:
    return (f'<?xml version="1.0" encoding="utf-8"?><!DOCTYPE html><html xmlns="http://www.w3.org/1999/xhtml" lang="zh-CN"><head><meta charset="utf-8"/><title>{html.escape(title)}</title><link rel="stylesheet" type="text/css" href="{stylesheet}"/></head><body>{body}</body></html>').encode()

def _slug(index: int, title: str) -> str:
    safe = re.sub(r"[^\w\-]+", "_", title, flags=re.UNICODE).strip("_")[:60] or "article"; return f"text/{index:04d}-{safe}.xhtml"

def build_issue_epub(issue: Issue, output_path: str|Path, cache_dir: str|Path|None=None, fetcher: ResourceFetcher|None=None) -> QualityReport:
    return build_epub([issue], output_path, f"《求是》{issue.title}", cache_dir, fetcher)

def build_year_epub(issues: list[Issue], output_path: str|Path, cache_dir: str|Path|None=None, fetcher: ResourceFetcher|None=None) -> QualityReport:
    ordered = sorted(issues, key=lambda i:i.sort_key); title = f"《求是》{ordered[0].year}年合订本" if ordered else "《求是》年度合订本"; return build_epub(ordered, output_path, title, cache_dir, fetcher)

def build_epub(issues: list[Issue], output_path: str|Path, title: str, cache_dir: str|Path|None=None, fetcher: ResourceFetcher|None=None) -> QualityReport:
    articles = [a for issue in issues for a in issue.articles if not _is_site_disclaimer_title(a.title)]; expected = sum((i.expected_article_count or len([a for a in i.articles if not _is_site_disclaimer_title(a.title)])) for i in issues); report = QualityReport(expected); seen = set()
    report.missing_articles.extend(title for issue in issues for title in issue.failed_articles)
    for a in articles:
        if a.title in seen: report.duplicate_titles.append(a.title)
        seen.add(a.title)
    fetcher = fetcher or ResourceFetcher(cache_dir); resources = {}; pages = []; page_titles = {}; idx = 0
    cover_href = ""
    cover_url = next((issue.cover_image_url for issue in issues if issue.cover_image_url), "")
    if cover_url:
        try:
            cover_data, cover_type = fetcher.fetch(cover_url); cover_type = _normalise_media_type(cover_type); cover_data = _mobile_image_bytes(cover_data, cover_type)
            cover_ext = mimetypes.guess_extension(cover_type) or Path(urlparse(cover_url).path).suffix or ".bin"
            cover_name = f"images/{hashlib.sha256(cover_data).hexdigest()[:16]}{cover_ext.lower()}"
            resources.setdefault(cover_name, (cover_type, cover_data, cover_url))
            cover_href = f"../{cover_name}"
        except Exception:
            report.failed_resources.append(cover_url)
    for issue in issues:
        if len(issues) > 1:
            issue_path = f"text/issue-{issue.issue_no:03d}.xhtml"
            pages.append((issue_path, _xhtml(issue.title, f'<h1 class="issue-title">{html.escape(issue.title)}</h1>')))
            page_titles[issue_path] = issue.title
        for article in issue.articles:
            if _is_site_disclaimer_title(article.title):
                continue
            idx += 1; path = _slug(idx, article.title)
            metadata = f"<p><em>{html.escape(issue.title)}"
            if issue.display_date: metadata += f" · {html.escape(issue.display_date)}"
            if article.author: metadata += f" · {html.escape(article.author)}"
            metadata += f'</em></p><p><a href="{html.escape(article.url, quote=True)}">原文链接</a></p>'
            body = f"<h1>{html.escape(article.title)}</h1>{metadata}"
            cleaned = _clean_fragment(article.html_body, article.url, fetcher, report, resources)
            if not cleaned: report.missing_articles.append(article.title); continue
            pages.append((path, _xhtml(article.title, body + cleaned))); page_titles[path] = article.title; report.written_articles += 1
    nav_items = "".join(f'<li><a href="{p}">{html.escape(page_titles[p])}</a></li>' for p,_ in pages); nav = _xhtml(title, f"<h1>{html.escape(title)}</h1><ol class=\"toc\">{nav_items}</ol>", "styles/style.css"); cover_img = f'<img src="{cover_href}" alt="封面"/>' if cover_href else ""; cover = _xhtml(title, f'<section class="cover">{cover_img}<h1>{html.escape(title)}</h1><p>个人离线阅读归档</p></section>')
    manifest = ['<item id="nav" properties="nav" href="nav.xhtml" media-type="application/xhtml+xml"/>','<item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>','<item id="style" href="styles/style.css" media-type="text/css"/>','<item id="cover" href="text/cover.xhtml" media-type="application/xhtml+xml"/>']; spine = ['<itemref idref="cover"/>']
    for n,(path,_) in enumerate(pages): manifest.append(f'<item id="p{n}" href="{path}" media-type="application/xhtml+xml"/>'); spine.append(f'<itemref idref="p{n}"/>')
    for n,(path,(ctype,_,_)) in enumerate(resources.items()):
        prop = ' properties="cover-image"' if path == cover_href.removeprefix("../") else ""
        manifest.append(f'<item id="img{n}" href="{path}" media-type="{ctype}"{prop}/>')
    uid = str(uuid.uuid4()); modified = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"); source_meta = "".join(f"<dc:source>{html.escape(issue.toc_url)}</dc:source>" for issue in issues if issue.toc_url); opf = f'<?xml version="1.0" encoding="utf-8"?><package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="uid"><metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:identifier id="uid">urn:uuid:{uid}</dc:identifier><dc:title>{html.escape(title)}</dc:title><dc:language>zh-CN</dc:language>{source_meta}<meta property="dcterms:modified">{modified}</meta></metadata><manifest>{"".join(manifest)}</manifest><spine toc="ncx">{"".join(spine)}</spine></package>'.encode()
    ncx_items = ''.join(f'<navPoint id="n{i}" playOrder="{i}"><navLabel><text>{html.escape(page_titles[p])}</text></navLabel><content src="{p}"/></navPoint>' for i,(p,_) in enumerate(pages,1)); ncx = f'<?xml version="1.0" encoding="UTF-8"?><ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1"><head><meta name="dtb:uid" content="urn:uuid:{uid}"/></head><docTitle><text>{html.escape(title)}</text></docTitle><navMap>{ncx_items}</navMap></ncx>'.encode()
    out = Path(output_path); out.parent.mkdir(parents=True, exist_ok=True); temp = out.with_name(f".{out.name}.tmp")
    try:
        with zipfile.ZipFile(temp,"w") as z:
            z.writestr("mimetype","application/epub+zip",compress_type=zipfile.ZIP_STORED)
            z.writestr("META-INF/container.xml",'<?xml version="1.0"?><container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles></container>')
            z.writestr("OEBPS/content.opf",opf); z.writestr("OEBPS/toc.ncx",ncx); z.writestr("OEBPS/nav.xhtml",nav); z.writestr("OEBPS/text/cover.xhtml",cover); z.writestr("OEBPS/styles/style.css",_CSS)
            for path,data in pages: z.writestr(f"OEBPS/{path}",data)
            for path,(_,data,_) in resources.items(): z.writestr(f"OEBPS/{path}",data)
        os.replace(temp, out)
    except Exception:
        temp.unlink(missing_ok=True)
        raise
    return report
