# EPUB 3 implementation notes

## Design choices

- **Content pipeline:** the scraper returns structured `Issue` and `Article` objects. XHTML cleaning happens once in `epub_builder.py`; PDF generation continues to use the existing HTML path.
- **EPUB packaging:** the project writes the EPUB 3 ZIP container directly. This keeps the MIT application free of an additional AGPL runtime dependency while retaining the required package, spine, navigation document, NCX compatibility file, CSS and media resources.
- **Offline resources:** article and cover images are downloaded into the package, deduplicated by SHA-256, and cached by source URL. HTML responses are cached separately so an interrupted run can resume without repeating successful requests.
- **Quality gates:** content completeness and package structure are checked independently. A `.quality.json` sidecar records both results. EPUBCheck can be invoked when its JAR is supplied.

## Reference projects

- FanFicFare: chapter organisation, image inclusion, update/cache patterns. <https://github.com/JimmXinu/FanFicFare>
- calibre: XHTML conversion, CSS and table-of-contents handling. <https://github.com/kovidgoyal/calibre>
- EbookLib: reviewed as an API reference for EPUB metadata, spine and navigation; not added as a dependency because its AGPL-3.0 license would add distribution obligations. <https://github.com/aerkalov/ebooklib>
- EPUBCheck: standards validation and release gate. <https://github.com/w3c/epubcheck>
- Standard Ebooks tools: semantic XHTML and publication quality conventions. <https://github.com/standardebooks/tools>

## Scope and limitations

The downloader only uses publicly accessible source pages and keeps the configured request delay. It does not bypass access controls. Historical year coverage remains limited to years and article pages exposed by the selected source plugin. A successful HTTP response does not by itself make a complete issue: failed articles and missing resources remain visible in the quality report.
