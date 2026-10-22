#!/usr/bin/env python3
"""
Goodreads Audiobook Radar
Scans your Goodreads to-read list and finds downloadable audiobooks (or ebooks) at KCLS.

Usage:
    python radar.py goodreads_library_export.csv
    python radar.py goodreads_library_export.csv --output results.html
    python radar.py goodreads_library_export.csv --format ebook
    python radar.py goodreads_library_export.csv --debug
"""

import argparse
import csv
import json
import re
import sys
import time
from dataclasses import dataclass
from html import escape
from typing import Optional
from urllib.parse import urlencode

import requests

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

KCLS_CATALOG_BASE = "https://kcls.bibliocommons.com"
KCLS_SEARCH_URL = f"{KCLS_CATALOG_BASE}/v2/search"
REQUEST_DELAY = 1.5  # seconds between requests — be polite

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Format:
    code: str   # BiblioCommons f_FORMAT value
    noun: str   # e.g. "audiobook", used in report text
    title: str  # e.g. "Audiobook Radar"


# BiblioCommons format codes: AB = eAudiobook, EBOOK = eBook (both downloadable).
# BOOK_CD (audio CD) and BK (print) are deliberately not offered.
FORMATS = {
    "audiobook": Format(code="AB", noun="audiobook", title="Audiobook Radar"),
    "ebook": Format(code="EBOOK", noun="ebook", title="Ebook Radar"),
}
DEFAULT_FORMAT = "audiobook"


@dataclass
class Book:
    title: str
    author: str
    goodreads_id: str = ""
    date_added: str = ""  # YYYY/MM/DD from Goodreads export


@dataclass
class SearchResult:
    book: Book
    found: bool = False
    bib_id: str = ""
    kcls_title: str = ""
    kcls_author: str = ""
    available: bool = False
    copies_available: int = 0
    total_copies: int = 0
    catalog_url: str = ""
    error: str = ""


# ---------------------------------------------------------------------------
# Goodreads CSV parsing
# ---------------------------------------------------------------------------

def load_to_read_books(filepath: str) -> list[Book]:
    """Load books from a Goodreads CSV export filtered to the 'to-read' shelf."""
    books = []
    try:
        with open(filepath, encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            for row in reader:
                if row.get("Exclusive Shelf", "").strip() != "to-read":
                    continue
                title = row.get("Title", "").strip()
                author = row.get("Author", "").strip()
                if not title:
                    continue
                books.append(Book(
                    title=title,
                    author=author,
                    goodreads_id=row.get("Book Id", "").strip(),
                    date_added=row.get("Date Added", "").strip(),
                ))
    except FileNotFoundError:
        print(f"Error: File not found: {filepath}", file=sys.stderr)
        sys.exit(1)
    except Exception as exc:
        print(f"Error reading CSV: {exc}", file=sys.stderr)
        sys.exit(1)

    if not books:
        print("No books found on your 'to-read' shelf.", file=sys.stderr)
        sys.exit(0)

    return books


# ---------------------------------------------------------------------------
# KCLS / BiblioCommons search
# ---------------------------------------------------------------------------

def build_search_url(query: str, fmt: Format) -> str:
    params = urlencode({
        "query": query,
        "searchType": "smart",
        "f_FORMAT": fmt.code,
    })
    return f"{KCLS_SEARCH_URL}?{params}"


STOP_WORDS = frozenset({
    "a", "an", "the", "of", "in", "at", "by", "for", "and", "or",
    "to", "from", "with", "on", "as", "is", "its",
})

# Minimum title F1 to accept a result as a match
TITLE_F1_THRESHOLD = 0.75


def _normalize(text: str) -> set[str]:
    """Lowercase, strip punctuation, remove stop words, return content word set."""
    text = text.lower()
    text = re.sub(r"'s?\b", "", text)          # strip possessives: ender's → ender
    text = re.sub(r"[^a-z0-9 ]", " ", text)    # replace non-alnum with space
    return {w for w in text.split() if w not in STOP_WORDS and len(w) > 1}


def _f1(query_words: set, candidate_words: set) -> float:
    """Token-level F1: harmonic mean of word-set recall and precision."""
    if not query_words or not candidate_words:
        return 0.0
    overlap = len(query_words & candidate_words)
    recall = overlap / len(query_words)
    precision = overlap / len(candidate_words)
    if recall + precision == 0:
        return 0.0
    return 2 * recall * precision / (recall + precision)


def _match_score(query_title: str, query_author: str,
                 candidate_title: str, candidate_author: str) -> tuple[float, float]:
    """Return (title_f1, author_f1) for a candidate bib vs query."""
    title_f1 = _f1(_normalize(query_title), _normalize(candidate_title))
    author_f1 = _f1(_normalize(query_author), _normalize(candidate_author))
    return title_f1, author_f1


def _extract_page_state(html: str) -> Optional[dict]:
    """
    BiblioCommons embeds the full application state as a large plain JSON blob
    in a <script> tag (not __NEXT_DATA__ — it's their own Redux store).
    Find the script that contains 'entities' and 'bibs' keys.
    """
    for m in re.finditer(r'<script[^>]*>(.*?)</script>', html, re.DOTALL):
        content = m.group(1).strip()
        # The state blob is large and contains these marker keys
        if '"entities"' in content and '"bibs"' in content and '"catalogSearch"' in content:
            try:
                return json.loads(content)
            except json.JSONDecodeError:
                continue
    return None


def _parse_page_state(data: dict) -> tuple[list[str], dict]:
    """
    Extract ordered bib IDs and bib-by-id dict from the BiblioCommons page state.
    Returns (ordered_bib_ids, bibs_by_id).
    """
    # Ordered results live under search.catalogSearch.results
    # Each item: {"representative": "S82C...", "manifestations": ["S82C..."]}
    results = (
        data.get("search", {})
            .get("catalogSearch", {})
            .get("results", [])
    )
    ordered_ids = [
        item["representative"]
        for item in results
        if isinstance(item, dict) and item.get("representative")
    ]

    # Bib objects live under entities.bibs
    bibs_by_id: dict = data.get("entities", {}).get("bibs", {})

    return ordered_ids, bibs_by_id


def _bib_to_result(bib_id: str, bib: dict, original_book: Book) -> Optional[SearchResult]:
    """Convert a raw BiblioCommons bib dict into an SearchResult."""
    brief = bib.get("briefInfo", {})
    title = brief.get("title") or bib.get("title", "")
    authors = brief.get("authors") or []
    author = ", ".join(authors) if authors else ""

    avail = bib.get("availability", {})
    copies_available = avail.get("availableCopies", 0)
    total_copies = avail.get("totalCopies", 0)
    status = avail.get("status", "")
    available = copies_available > 0 or status.upper() == "AVAILABLE"

    return SearchResult(
        book=original_book,
        found=True,
        bib_id=bib_id,
        kcls_title=title,
        kcls_author=author,
        available=available,
        copies_available=int(copies_available),
        total_copies=int(total_copies),
        catalog_url=f"{KCLS_CATALOG_BASE}/v2/record/{bib_id}",
    )


def search_kcls(book: Book, session: requests.Session, fmt: Format,
                debug: bool = False) -> SearchResult:
    """Search KCLS for a downloadable edition of the given book in the given format."""
    result = SearchResult(book=book, found=False)
    query = f"{book.title} {book.author}".strip()
    search_url = build_search_url(query, fmt)

    try:
        resp = session.get(search_url, headers=HEADERS, timeout=20)
        resp.raise_for_status()
    except requests.HTTPError as exc:
        result.error = f"HTTP {exc.response.status_code}"
        return result
    except requests.RequestException as exc:
        result.error = str(exc)
        return result

    html = resp.text

    if debug:
        print(f"\n  [DEBUG] URL: {search_url}")
        print(f"  [DEBUG] Status: {resp.status_code}, Content: {len(html)} bytes")

    page_state = _extract_page_state(html)
    if not page_state:
        if debug:
            print("  [DEBUG] Could not extract page state JSON")
        result.error = "parse error"
        return result

    ordered_ids, bibs_by_id = _parse_page_state(page_state)

    if debug:
        print(f"  [DEBUG] {len(ordered_ids)} results, {len(bibs_by_id)} bibs in state")

    if not ordered_ids:
        return result  # no results for this book

    # Score all candidates; require title F1 >= threshold to accept
    best: Optional[SearchResult] = None
    best_combined = 0.0

    for bib_id in ordered_ids[:15]:
        bib = bibs_by_id.get(str(bib_id))
        if not bib:
            continue
        brief = bib.get("briefInfo", {})
        candidate_title = brief.get("title", "")
        candidate_authors = brief.get("authors") or []
        candidate_author = ", ".join(candidate_authors)

        title_f1, author_f1 = _match_score(book.title, book.author, candidate_title, candidate_author)
        combined = title_f1 + author_f1 * 0.3  # author is a bonus, not a requirement

        if debug:
            print(
                f"  [DEBUG] bib={bib_id} title={candidate_title!r}"
                f" author={candidate_author!r}"
                f" title_f1={title_f1:.2f} author_f1={author_f1:.2f}"
            )

        if title_f1 >= TITLE_F1_THRESHOLD and combined > best_combined:
            best_combined = combined
            best = _bib_to_result(bib_id, bib, book)

    if best:
        return best

    # Nothing met the threshold — book not found in this format at KCLS
    return result


# ---------------------------------------------------------------------------
# Output: terminal table
# ---------------------------------------------------------------------------

def print_terminal_report(results: list[SearchResult], fmt: Format) -> None:
    found = [r for r in results if r.found]
    not_found = [r for r in results if not r.found]
    available = [r for r in found if r.available]
    waitlist = [r for r in found if not r.available]

    found_label = f"Found as downloadable {fmt.noun}"
    label_width = max(len(found_label), 31)

    print()
    print("=" * 72)
    print(f"  KCLS {fmt.title} — {len(results)} books checked")
    print("=" * 72)
    print(f"  {found_label:<{label_width}} : {len(found)}")
    print(f"  {'  ✓ Available now':<{label_width}} : {len(available)}")
    print(f"  {'  ⌛ On waitlist / unavailable':<{label_width}} : {len(waitlist)}")
    print(f"  {'Not found in KCLS catalog':<{label_width}} : {len(not_found)}")
    print("=" * 72)

    if available:
        print("\n── AVAILABLE NOW ──────────────────────────────────────────────────")
        for r in sorted(available, key=lambda x: x.book.title.lower()):
            copies = f"({r.copies_available}/{r.total_copies} copies)" if r.total_copies > 0 else ""
            print(f"\n  ✓ {r.book.title}")
            if r.kcls_title and r.kcls_title.lower() != r.book.title.lower():
                print(f"      KCLS: {r.kcls_title}")
            print(f"      {r.book.author}  {copies}")
            print(f"      {r.catalog_url}")

    if waitlist:
        print("\n── ON WAITLIST / CHECKED OUT ──────────────────────────────────────")
        for r in sorted(waitlist, key=lambda x: x.book.title.lower()):
            copies = f"(0/{r.total_copies} copies available)" if r.total_copies > 0 else ""
            print(f"\n  ⌛ {r.book.title}")
            if r.kcls_title and r.kcls_title.lower() != r.book.title.lower():
                print(f"      KCLS: {r.kcls_title}")
            print(f"      {r.book.author}  {copies}")
            print(f"      {r.catalog_url}")

    if not_found:
        heading = f"── NOT FOUND AS DOWNLOADABLE {fmt.noun.upper()} "
        print("\n" + heading.ljust(68, "─"))
        for r in sorted(not_found, key=lambda x: x.book.title.lower()):
            err = f"  [{r.error}]" if r.error else ""
            print(f"  ✗ {r.book.title} — {r.book.author}{err}")

    print()


# ---------------------------------------------------------------------------
# Output: HTML report
# ---------------------------------------------------------------------------

HTML_TEMPLATE = """\
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>KCLS {title}</title>
  <style>
    *, *::before, *::after {{ box-sizing: border-box; }}
    body {{
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      background: #f5f5f5;
      color: #222;
      margin: 0;
      padding: 2rem 1rem;
    }}
    main {{ max-width: 900px; margin: 0 auto; }}
    h1 {{ color: #1a4a7a; margin-bottom: 0.25rem; }}
    .subtitle {{ color: #666; margin-bottom: 2rem; font-size: 0.9rem; }}
    .stats {{
      display: flex;
      gap: 1rem;
      flex-wrap: wrap;
      margin-bottom: 2rem;
    }}
    .stat-card {{
      background: #fff;
      border-radius: 8px;
      padding: 1rem 1.5rem;
      box-shadow: 0 1px 4px rgba(0,0,0,0.1);
      min-width: 140px;
    }}
    .stat-card .number {{ font-size: 2rem; font-weight: 700; }}
    .stat-card .label {{ font-size: 0.8rem; color: #666; text-transform: uppercase; letter-spacing: 0.05em; }}
    .stat-card.available .number {{ color: #2a7a2a; }}
    .stat-card.waitlist .number {{ color: #b35c00; }}
    .stat-card.notfound .number {{ color: #999; }}
    section {{ margin-bottom: 2rem; }}
    h2 {{
      font-size: 1rem;
      text-transform: uppercase;
      letter-spacing: 0.08em;
      color: #555;
      border-bottom: 2px solid #ddd;
      padding-bottom: 0.4rem;
      margin-bottom: 1rem;
    }}
    h2.available {{ border-color: #2a7a2a; color: #2a7a2a; }}
    h2.waitlist {{ border-color: #b35c00; color: #b35c00; }}
    table {{
      width: 100%;
      border-collapse: collapse;
      background: #fff;
      border-radius: 8px;
      overflow: hidden;
      box-shadow: 0 1px 4px rgba(0,0,0,0.08);
    }}
    th {{
      background: #eef2f7;
      text-align: left;
      padding: 0.6rem 1rem;
      font-size: 0.8rem;
      text-transform: uppercase;
      letter-spacing: 0.05em;
      color: #555;
    }}
    td {{
      padding: 0.7rem 1rem;
      border-top: 1px solid #f0f0f0;
      font-size: 0.9rem;
      vertical-align: top;
    }}
    tr:hover td {{ background: #fafcff; }}
    a {{ color: #1a4a7a; text-decoration: none; }}
    a:hover {{ text-decoration: underline; }}
    .badge {{
      display: inline-block;
      border-radius: 4px;
      padding: 0.15rem 0.5rem;
      font-size: 0.75rem;
      font-weight: 600;
    }}
    .badge-available {{ background: #d4edda; color: #1a5c1a; }}
    .badge-waitlist {{ background: #fde8cc; color: #7a3c00; }}
    .copies {{ font-size: 0.8rem; color: #888; display: block; margin-top: 0.2rem; }}
  </style>
</head>
<body>
  <main>
    <h1>KCLS {title}</h1>
    <p class="subtitle">
      Downloadable {noun}s from your Goodreads to-read list &middot;
      <a href="https://kcls.bibliocommons.com/" target="_blank" rel="noopener">King County Library System</a>
    </p>

    <div class="stats">
      <div class="stat-card available">
        <div class="number">{n_available}</div>
        <div class="label">Available now</div>
      </div>
      <div class="stat-card waitlist">
        <div class="number">{n_waitlist}</div>
        <div class="label">On waitlist</div>
      </div>
      <div class="stat-card notfound">
        <div class="number">{n_not_found}</div>
        <div class="label">Not found</div>
      </div>
      <div class="stat-card">
        <div class="number">{n_total}</div>
        <div class="label">Books checked</div>
      </div>
    </div>

    {available_section}
    {waitlist_section}
    {not_found_section}
  </main>
</body>
</html>
"""

FOUND_SECTION_TEMPLATE = """\
    <section>
      <h2 class="{cls}">{heading}</h2>
      <table>
        <thead>
          <tr>
            <th>Title</th>
            <th>Author (Goodreads)</th>
            <th>Availability</th>
          </tr>
        </thead>
        <tbody>
          {rows}
        </tbody>
      </table>
    </section>
"""

NOT_FOUND_SECTION_TEMPLATE = """\
    <section>
      <h2>Not found as downloadable {noun}</h2>
      <table>
        <thead>
          <tr>
            <th>Title</th>
            <th>Author</th>
          </tr>
        </thead>
        <tbody>
          {rows}
        </tbody>
      </table>
    </section>
"""


def _result_row(r: SearchResult) -> str:
    link = f'<a href="{escape(r.catalog_url)}" target="_blank" rel="noopener">{escape(r.kcls_title or r.book.title)}</a>'
    if r.available:
        badge = '<span class="badge badge-available">Available</span>'
    else:
        badge = '<span class="badge badge-waitlist">Waitlist</span>'
    copies = ""
    if r.total_copies > 0:
        copies = f'<span class="copies">{r.copies_available} of {r.total_copies} copies available</span>'
    return (
        f"<tr>"
        f"<td>{link}</td>"
        f"<td>{escape(r.book.author)}</td>"
        f"<td>{badge}{copies}</td>"
        f"</tr>"
    )


def generate_html_report(results: list[SearchResult], fmt: Format, output_path: str) -> None:
    found = [r for r in results if r.found]
    not_found = [r for r in results if not r.found]
    def _date_key(r: SearchResult) -> str:
        # Sort oldest first; empty date sorts last
        return r.book.date_added if r.book.date_added else "9999/99/99"

    available = sorted([r for r in found if r.available], key=_date_key)
    waitlist = sorted([r for r in found if not r.available], key=_date_key)
    not_found_sorted = sorted(not_found, key=_date_key)

    available_section = (
        FOUND_SECTION_TEMPLATE.format(
            cls="available",
            heading="Available Now",
            rows="\n          ".join(_result_row(r) for r in available),
        )
        if available else ""
    )
    waitlist_section = (
        FOUND_SECTION_TEMPLATE.format(
            cls="waitlist",
            heading="On Waitlist / Checked Out",
            rows="\n          ".join(_result_row(r) for r in waitlist),
        )
        if waitlist else ""
    )
    not_found_rows = "\n          ".join(
        f"<tr><td>{escape(r.book.title)}</td><td>{escape(r.book.author)}</td></tr>"
        for r in not_found_sorted
    )
    not_found_section = (
        NOT_FOUND_SECTION_TEMPLATE.format(noun=fmt.noun, rows=not_found_rows)
        if not_found_sorted else ""
    )

    html = HTML_TEMPLATE.format(
        title=fmt.title,
        noun=fmt.noun,
        n_available=len(available),
        n_waitlist=len(waitlist),
        n_not_found=len(not_found_sorted),
        n_total=len(results),
        available_section=available_section,
        waitlist_section=waitlist_section,
        not_found_section=not_found_section,
    )

    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html)

    print(f"\nHTML report saved to: {output_path}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Find downloadable audiobooks or ebooks at KCLS from your Goodreads to-read list.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "How to get your Goodreads export:\n"
            "  1. Go to https://www.goodreads.com/review/import\n"
            "  2. Click 'Export Library'\n"
            "  3. Download the CSV and pass its path to this script."
        ),
    )
    parser.add_argument(
        "csv",
        metavar="goodreads_export.csv",
        help="Path to your Goodreads library export CSV",
    )
    parser.add_argument(
        "--output", "-o",
        metavar="FILE",
        help="Save an HTML report (e.g. --output results.html)",
    )
    parser.add_argument(
        "--format", "-f",
        choices=sorted(FORMATS),
        default=DEFAULT_FORMAT,
        help=f"Which downloadable format to search for (default: {DEFAULT_FORMAT})",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Print raw search debug info for each book",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=REQUEST_DELAY,
        metavar="SECS",
        help=f"Seconds to wait between requests (default: {REQUEST_DELAY})",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    fmt = FORMATS[args.format]

    print(f"Reading Goodreads export: {args.csv}")
    books = load_to_read_books(args.csv)
    total = len(books)
    print(f"Found {total} book(s) on your to-read shelf.")
    print(f"Searching KCLS for downloadable {fmt.noun}s...\n")

    session = requests.Session()
    results: list[SearchResult] = []
    width = len(str(total))

    for i, book in enumerate(books, 1):
        prefix = f"[{i:>{width}}/{total}]"
        print(f"{prefix} {book.title[:55]:<55}", end=" ", flush=True)

        result = search_kcls(book, session, fmt, debug=args.debug)
        results.append(result)

        if result.found:
            if result.available:
                copies = f"({result.copies_available}/{result.total_copies})" if result.total_copies else ""
                print(f"✓ AVAILABLE {copies}")
            else:
                copies = f"(0/{result.total_copies})" if result.total_copies else ""
                print(f"⌛ waitlist {copies}")
        elif result.error:
            print(f"✗ error: {result.error}")
        else:
            print("— not found")

        if i < total:
            time.sleep(args.delay)

    print_terminal_report(results, fmt)

    if args.output:
        generate_html_report(results, fmt, args.output)


if __name__ == "__main__":
    main()
