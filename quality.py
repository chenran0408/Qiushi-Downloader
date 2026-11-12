from __future__ import annotations

import json
import posixpath
import subprocess
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET
from urllib.parse import urlparse


def validate_epub(path: str | Path) -> dict:
    """Run local structural checks before optional EPUBCheck."""
    result = {"path": str(path), "valid": False, "errors": [], "warnings": []}
    try:
        with zipfile.ZipFile(path) as z:
            names = set(z.namelist())
            entries = z.infolist()
            if not entries or entries[0].filename != "mimetype" or entries[0].compress_type != zipfile.ZIP_STORED:
                result["errors"].append("mimetype must be the first uncompressed ZIP entry")
            if "mimetype" not in names or z.read("mimetype") != b"application/epub+zip":
                result["errors"].append("mimetype missing or not first-class EPUB value")
            container = ET.fromstring(z.read("META-INF/container.xml"))
            rootfile = container.find(".//{*}rootfile")
            if rootfile is None:
                result["errors"].append("container.xml has no rootfile")
                return result
            opf_path = rootfile.attrib.get("full-path", "")
            if opf_path not in names:
                result["errors"].append(f"missing package document: {opf_path}")
                return result
            opf = ET.fromstring(z.read(opf_path)); base = posixpath.dirname(opf_path)
            manifest = {}
            for item in opf.findall(".//{*}manifest/{*}item"):
                href = item.attrib.get("href", "")
                target = posixpath.normpath(posixpath.join(base, href))
                item_id = item.attrib.get("id", "")
                if not item_id or item_id in manifest:
                    result["errors"].append(f"duplicate or empty manifest id: {item_id}")
                manifest[item_id] = target
                if target not in names:
                    result["errors"].append(f"manifest target missing: {target}")
            if not opf.find(".//{*}spine") is not None:
                result["errors"].append("package has no spine")
            nav = next((p for p in manifest.values() if p.endswith("nav.xhtml")), None)
            if nav is None:
                result["warnings"].append("navigation document is not named nav.xhtml")
            elif nav not in names:
                result["errors"].append(f"navigation document missing: {nav}")
            for document in (name for name in names if name.endswith((".xhtml", ".html"))):
                try:
                    root = ET.fromstring(z.read(document))
                except ET.ParseError as exc:
                    result["errors"].append(f"invalid XHTML {document}: {exc}")
                    continue
                for element in root.iter():
                    for attr in ("href", "src"):
                        value = element.attrib.get(attr, "")
                        if not value or value.startswith(("http:", "https:", "//", "data:", "#")):
                            continue
                        target = value.split("#", 1)[0]
                        resolved = posixpath.normpath(posixpath.join(posixpath.dirname(document), target))
                        if resolved not in names:
                            result["errors"].append(f"XHTML reference missing: {document} -> {resolved}")
            result["valid"] = not result["errors"]
    except (OSError, KeyError, ET.ParseError, zipfile.BadZipFile) as exc:
        result["errors"].append(str(exc))
    return result


def write_quality_report(epub_path: str | Path, report: dict) -> Path:
    target = Path(epub_path).with_suffix(".quality.json")
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return target


def run_epubcheck(epub_path: str | Path, epubcheck_jar: str | Path) -> dict:
    """Run the official EPUBCheck JAR when the user supplies one."""
    try:
        completed = subprocess.run(
            ["java", "-jar", str(epubcheck_jar), str(epub_path)],
            capture_output=True, text=True, timeout=120, check=False,
        )
        return {"available": True, "valid": completed.returncode == 0,
                "returncode": completed.returncode,
                "stdout": completed.stdout[-4000:], "stderr": completed.stderr[-4000:]}
    except (OSError, subprocess.SubprocessError) as exc:
        return {"available": False, "valid": False, "error": str(exc)}


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Validate an EPUB package locally")
    parser.add_argument("epub")
    parser.add_argument("--epubcheck", help="path to the EPUBCheck JAR")
    args = parser.parse_args()
    result = validate_epub(args.epub)
    if args.epubcheck:
        result["epubcheck"] = run_epubcheck(args.epub, args.epubcheck)
        result["valid"] = result["valid"] and result["epubcheck"]["valid"]
    print(json.dumps(result, ensure_ascii=False, indent=2))
    raise SystemExit(0 if result["valid"] else 1)
