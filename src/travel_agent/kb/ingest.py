"""Convert guidebook PDFs into grep-friendly markdown.

Each book becomes `<guidebooks_dir>/<slug>.md`: the PDF's bookmark TOC is inserted as markdown
headings at the page where each section starts, and every page is prefixed with a `[p. N]` marker
so search hits can be cited and expanded by page range.

Usage: uv run travel-agent-ingest [--force]
"""

import argparse
import re
from pathlib import Path

import pymupdf

from travel_agent.config import get_settings

# Pirate-library suffixes and other noise in downloaded filenames
_NOISE = re.compile(r"\((?:[^)]*z-lib[^)]*|[^)]*eBook[^)]*)\)|full pdf ebook", re.IGNORECASE)


def slugify(pdf_name: str) -> str:
    name = _NOISE.sub("", Path(pdf_name).stem)
    # Drop remaining parentheticals (authors) but keep editions like "(2016)"
    name = re.sub(r"\((?![^)]*\d{4})[^)]*\)", "", name)
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def _clean(text: str) -> str:
    # Soft hyphens mark line-break hyphenation in these PDFs: "forma\xad tions" -> "formations"
    text = re.sub(r"\xad\s*", "", text)
    return re.sub(r"\s+", " ", text).strip()


def convert(pdf_path: Path, out_path: Path) -> int:
    doc = pymupdf.open(pdf_path)
    headings: dict[int, list[str]] = {}
    for level, title, page in doc.get_toc():
        if page >= 1 and re.search(r"\w", title):
            headings.setdefault(page, []).append(f"{'#' * min(level, 6)} {_clean(title)}")

    lines = [f"# {slugify(pdf_path.name)}", ""]
    for page_num, page in enumerate(doc, start=1):
        lines.extend(headings.get(page_num, []))
        lines.append(f"[p. {page_num}]")
        # One text block (≈ paragraph) per line, in content-stream order so multi-column
        # layouts don't get interleaved; this makes each grep hit a whole paragraph.
        for *_, text, _, block_type in page.get_text("blocks", flags=pymupdf.TEXT_DEHYPHENATE):
            text = _clean(text.replace("¨", " "))  # ¨ is the guidebooks' bullet glyph
            # Icon-font glyphs come out as "#" etc.; strip them so only TOC lines start with "#"
            text = text.lstrip("# ")
            if block_type == 0 and re.search(r"\w\w", text):
                lines.append(text)
        lines.append("")
    out_path.write_text("\n".join(lines), encoding="utf-8")
    return doc.page_count


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--force", action="store_true", help="re-convert existing books")
    args = parser.parse_args()

    settings = get_settings()
    settings.guidebooks_dir.mkdir(parents=True, exist_ok=True)
    for pdf in sorted(settings.reference_dir.glob("*.pdf")):
        out = settings.guidebooks_dir / f"{slugify(pdf.name)}.md"
        if out.exists() and not args.force:
            print(f"skip  {out.name}")
            continue
        pages = convert(pdf, out)
        print(f"wrote {out.name} ({pages} pages, {out.stat().st_size // 1024} KB)")


if __name__ == "__main__":
    main()
