from __future__ import annotations

import json
import posixpath
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET


def validate_epub(path: str | Path) -> dict:
    """Run local structural checks before optional EPUBCheck."""
    result = {"path": str(path), "valid": False, "errors": [], "warnings": []}
    try:
        with zipfile.ZipFile(path) as z:
            names = set(z.namelist())
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
                manifest[item.attrib.get("id", "")] = target
                if target not in names:
                    result["errors"].append(f"manifest target missing: {target}")
            if not opf.find(".//{*}spine") is not None:
                result["errors"].append("package has no spine")
            nav = next((p for p in manifest.values() if p.endswith("nav.xhtml")), None)
            if nav is None:
                result["warnings"].append("navigation document is not named nav.xhtml")
            elif nav not in names:
                result["errors"].append(f"navigation document missing: {nav}")
            result["valid"] = not result["errors"]
    except (OSError, KeyError, ET.ParseError, zipfile.BadZipFile) as exc:
        result["errors"].append(str(exc))
    return result


def write_quality_report(epub_path: str | Path, report: dict) -> Path:
    target = Path(epub_path).with_suffix(".quality.json")
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return target


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Validate an EPUB package locally")
    parser.add_argument("epub")
    args = parser.parse_args()
    result = validate_epub(args.epub)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    raise SystemExit(0 if result["valid"] else 1)
