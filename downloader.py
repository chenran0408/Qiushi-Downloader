"""
downloader.py
-------------
Threading glue: runs the (network-bound) scraping on a background
QThread so the UI never blocks, and calls back into the GUI-thread
`PdfRenderer` for the actual PDF write via a blocking queued connection.

Public API used by the UI:
    worker = DownloadWorker(source_id, output_dir, renderer)
    worker.years_ready.connect(...)
    worker.issues_ready.connect(...)
    worker.issue_progress.connect(...)
    worker.issue_done.connect(...)
    worker.issue_failed.connect(...)
    worker.log.connect(...)
    worker.start()
    worker.request_years()
    worker.request_issues(year, year_url)
    worker.request_download(issue)
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from queue import Queue, Empty

from PySide6.QtCore import QThread, Signal, QObject

from models import Issue, DownloadState
from scraper import registry, ScrapeError
from pdf_builder import build_issue_html, PdfBridge
from epub_builder import build_issue_epub, build_year_epub
from quality import validate_epub, write_quality_report
from config import config

log = logging.getLogger("qiushi.downloader")

_INVALID_FS_CHARS = re.compile(r'[\\/:*?"<>|]')


def safe_filename(issue: Issue) -> str:
    """'以相应日期和时间命名' -> filename is built from the issue's own
    publish date/time as reported by the site, with the issue label kept
    for human readability."""
    if issue.publish_dt:
        stamp = issue.publish_dt.strftime("%Y-%m-%d_%H-%M-%S")
    else:
        stamp = "date-unknown"
    label = f"求是_{issue.year}年第{issue.issue_no}期"
    name = f"{stamp}_{label}.pdf"
    return _INVALID_FS_CHARS.sub("_", name)


class _Job:
    __slots__ = ("kind", "payload")

    def __init__(self, kind: str, payload=None):
        self.kind = kind
        self.payload = payload


class DownloadWorker(QThread):
    years_ready = Signal(list)                 # [(year, url), ...]
    issues_ready = Signal(int, list)            # year, [Issue, ...]
    issue_progress = Signal(object, float, str) # issue, 0..1, phase text
    issue_done = Signal(object)                 # issue (state=DONE)
    issue_failed = Signal(object, str)          # issue, error text
    log_message = Signal(str)
    year_progress = Signal(int, int, int, str)  # year, completed, total, phase
    year_done = Signal(str)
    year_failed = Signal(str)

    def __init__(self, pdf_bridge: PdfBridge, parent: QObject | None = None):
        super().__init__(parent)
        self._pdf_bridge = pdf_bridge
        self._queue: Queue[_Job] = Queue()
        self._running = True
        self._cancel_year = False
        self._source_id = config.active_source_id

    # -- public, thread-safe entry points ---------------------------------
    def request_years(self):
        self._queue.put(_Job("years"))

    def request_issues(self, year: int, year_url: str):
        self._queue.put(_Job("issues", (year, year_url)))

    def request_download(self, issue: Issue):
        self._queue.put(_Job("download", issue))

    def request_year_epub(self, issues: list[Issue]):
        """Queue a year-book build from already fetched issues."""
        self._cancel_year = False
        self._queue.put(_Job("year_epub", issues))

    def cancel_year_epub(self):
        self._cancel_year = True

    def stop(self):
        self._running = False
        self._queue.put(_Job("__stop__"))

    # -- QThread main loop --------------------------------------------------
    def run(self):
        source = registry.get(
            self._source_id,
            delay_seconds=config.request_delay_seconds,
            cache_dir=Path(config.output_dir) / ".cache" / "html",
        )
        while self._running:
            try:
                job = self._queue.get(timeout=0.25)
            except Empty:
                continue

            try:
                if job.kind == "__stop__":
                    break
                elif job.kind == "years":
                    self._handle_years(source)
                elif job.kind == "issues":
                    year, url = job.payload
                    self._handle_issues(source, year, url)
                elif job.kind == "download":
                    self._handle_download(source, job.payload)
                elif job.kind == "year_epub":
                    self._handle_year_epub(source, job.payload)
            except Exception as exc:  # never let the worker die silently
                log.exception("worker job failed")
                self.log_message.emit(f"发生错误：{exc}")

    # -- job handlers ---------------------------------------------------------
    def _handle_years(self, source):
        self.log_message.emit("正在获取往期年份列表…")
        years = source.list_year_pages()
        self.years_ready.emit(years)

    def _handle_issues(self, source, year: int, year_url: str):
        self.log_message.emit(f"正在获取 {year} 年各期列表…")
        issues = source.list_issues(year, year_url)
        self.issues_ready.emit(year, issues)

    def _handle_download(self, source, issue: Issue):
        out_dir = Path(config.output_dir)
        pdf_target = out_dir / safe_filename(issue)
        epub_target = pdf_target.with_suffix(".epub")
        targets = [p for p, enabled in ((pdf_target, config.output_format in ("pdf", "both")), (epub_target, config.output_format in ("epub", "both"))) if enabled]
        if not targets:
            targets = [pdf_target]

        if not config.overwrite_existing and all(target.exists() for target in targets):
            issue.state = DownloadState.SKIPPED
            issue.output_path = str(targets[0])
            self.log_message.emit(f"已存在，跳过：{targets[0].name}")
            self.issue_done.emit(issue)
            return

        issue.state = DownloadState.FETCHING
        self.issue_progress.emit(issue, 0.0, "正在抓取文章内容…")

        def on_progress(p: float):
            self.issue_progress.emit(issue, p * 0.8, "正在抓取文章内容…")

        try:
            source.fetch_issue_detail(issue, progress_cb=on_progress)
        except Exception as exc:
            issue.state = DownloadState.FAILED
            issue.error = f"抓取失败：{exc}"
            self.issue_failed.emit(issue, issue.error)
            return

        if not issue.articles:
            issue.state = DownloadState.FAILED
            issue.error = "未解析到任何文章"
            self.issue_failed.emit(issue, issue.error)
            return

        issue.state = DownloadState.RENDERING
        report = None
        if config.output_format in ("epub", "both"):
            self.issue_progress.emit(issue, 0.85, "正在生成 EPUB…")
            try:
                report = build_issue_epub(issue, epub_target, cache_dir=out_dir / ".cache" / "resources")
                package_report = validate_epub(epub_target)
                write_quality_report(epub_target, {"content": report.as_dict(), "package": package_report})
            except Exception as exc:
                issue.state = DownloadState.FAILED
                issue.error = f"EPUB 生成失败：{exc}"
                self.issue_failed.emit(issue, issue.error)
                return
            if not report.complete or not package_report["valid"]:
                issue.state = DownloadState.FAILED
                issue.error = f"EPUB 质量检查未通过：{report.as_dict()} / {package_report}"
                self.issue_failed.emit(issue, issue.error)
                return
        if config.output_format in ("pdf", "both"):
            self.issue_progress.emit(issue, 0.9, "正在排版生成 PDF…")
            ok, err = self._pdf_bridge.render_blocking(build_issue_html(issue), str(pdf_target))
            if not ok:
                issue.state = DownloadState.FAILED
                issue.error = err or "PDF 渲染失败"
                self.issue_failed.emit(issue, issue.error)
                return

        issue.state = DownloadState.DONE
        issue.output_path = str(targets[0])
        issue.progress = 1.0
        self.issue_progress.emit(issue, 1.0, "完成")
        self.log_message.emit("已生成：" + ", ".join(p.name for p in targets))
        self.issue_done.emit(issue)

    def _handle_year_epub(self, source, issues: list[Issue]):
        if not issues:
            self.log_message.emit("没有可合订的期刊")
            return
        year = issues[0].year
        self.log_message.emit(f"正在抓取 {year} 年内容并生成合订本…")
        ordered = sorted(issues, key=lambda i: i.sort_key)
        total = len(ordered)
        for completed, issue in enumerate(ordered, start=1):
            if self._cancel_year:
                self._cancel_year = False
                self.log_message.emit(f"已取消 {year} 年年度 EPUB 任务")
                self.year_failed.emit("年度任务已取消")
                return
            if issue.articles:
                self.year_progress.emit(year, completed, total, f"已缓存 {issue.title}")
                continue
            try:
                source.fetch_issue_detail(issue)
            except Exception as exc:
                issue.expected_article_count = max(issue.expected_article_count, 1)
                issue.failed_articles.append(f"期号抓取失败：{issue.title} ({exc})")
                self.log_message.emit(f"跳过失败期号：{issue.title}；年度合订本将标记为不完整")
            self.year_progress.emit(year, completed, total, f"已处理 {issue.title}")
        target = Path(config.output_dir) / f"求是_{year}年合订本.epub"
        if target.exists() and not config.overwrite_existing:
            self.log_message.emit(f"年度合订本已存在，跳过：{target.name}")
            self.year_done.emit(str(target))
            return
        try:
            report = build_year_epub(issues, target, cache_dir=Path(config.output_dir) / ".cache" / "resources")
            package_report = validate_epub(target)
            write_quality_report(target, {"content": report.as_dict(), "package": package_report})
        except Exception as exc:
            self.log_message.emit(f"年度 EPUB 生成失败：{exc}")
            self.year_failed.emit(str(exc))
            return
        if report.complete and package_report["valid"]:
            self.log_message.emit(f"已生成年度合订本：{target.name}")
            self.year_done.emit(str(target))
        else:
            self.log_message.emit(f"年度合订本质量检查未通过，请查看报告：{target.with_suffix('.quality.json').name}")
            self.year_failed.emit("年度合订本质量检查未通过")
