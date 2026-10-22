# goodreads-kcls-radar

Reads your Goodreads library export and checks every book on your **to-read** shelf against the [King County Library System (KCLS)](https://kcls.bibliocommons.com/) catalog for **downloadable audiobooks or ebooks**. It reports which titles you can borrow now and which have a waitlist, with a link to each catalog record. Results print in the terminal and can also be saved as an HTML report.

Example output (illustrative):

```
[ 1/42] Project Hail Mary                                       ⌛ waitlist (0/283)
[ 2/42] The Left Hand of Darkness                               ✓ AVAILABLE (2/14)
[ 3/42] Some Obscure Title                                      — not found
```

## Requirements

- Python 3.9+
- `requests` and `beautifulsoup4` (see `requirements.txt`)

## Setup

**With [Flox](https://flox.dev/)** (sets up Python and a virtualenv, and installs the requirements):

```sh
flox activate
```

**Without Flox:**

```sh
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Getting your Goodreads export

1. Go to <https://www.goodreads.com/review/import>.
2. Click **Export Library**.
3. Download the CSV once it's ready.

## Usage

```sh
# Audiobooks (the default), terminal output only
python radar.py export-csv-files/goodreads_export.csv

# Save an HTML report as well
python radar.py export-csv-files/goodreads_export.csv \
  --output results-html-files/results.html

# Search for ebooks instead
python radar.py export-csv-files/goodreads_export.csv --format ebook \
  --output results-html-files/ebook-results.html
```

### Options

| Option | Description |
|---|---|
| `--format`, `-f` | `audiobook` (default) or `ebook` |
| `--output`, `-o FILE` | Also save an HTML report to `FILE` |
| `--delay SECS` | Seconds to wait between requests (default: `1.5`) |
| `--debug` | Print the search URL and match scores for each candidate |

## Organizing your files

The repo includes two empty folders you can use for your own files. Their contents are git-ignored, so your library data stays local.

```
export-csv-files/      # Goodreads exports, e.g. goodreads_export_20oct2026.csv
results-html-files/    # HTML reports, e.g. results-20oct2026.html
```

Your Goodreads export includes your ratings, reviews and private notes, so don't commit it anywhere public.

## How it works

- For each to-read book, it searches the KCLS BiblioCommons catalog with the title and author, filtered by format (`AB` for eAudiobook, `EBOOK` for eBook).
- It reads the results from the JSON state embedded in the search page.
- A result counts as a match when its title scores at least 0.75 on word-level similarity with the Goodreads title, after dropping stop words. A matching author is a bonus, but not required. This filters out translations and spinoffs that only share part of the title, so a book whose only KCLS editions are translations shows as "not found".
- A match is "available" if KCLS reports at least one copy available now. Otherwise it's on the waitlist.

## Being polite to KCLS

The script waits 1.5 seconds between requests by default. KCLS's [`robots.txt`](https://kcls.bibliocommons.com/robots.txt) sets a `Crawl-delay` of 120 seconds. That directive is aimed at crawlers, but if you want to follow it strictly, use `--delay 120`. At that pace, 100 books take about 3.3 hours, so start it and let it run.

This project isn't affiliated with KCLS, BiblioCommons or Goodreads. It depends on the current layout of the KCLS catalog pages, so it may stop working if those pages change.

## License

[MIT](LICENSE)
