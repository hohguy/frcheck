# frcheck

Randomly checks that English and French pages on a website use the same HTML template structure.

## What It Does

- Reads page URLs from sitemap XML files (starting at `https://www.example.com/sitemap.xml`)
- Also checks sitemap URLs listed in `robots.txt` and common sitemap filenames
- Falls back to internal link crawling if no sitemap URLs are found
- Samples random EN pages
- Maps each EN path to the FR path by adding `/fr` prefix
- Compares EN vs FR HTML structure (tag sequence + stable attribute names)
- Extracts each page's `<html lang>` value when available
- Reports pass/fail based on a similarity threshold
- Writes findings to a CSV file

## Usage

```bash
python3 check_en_fr_templates.py
```

Note: the default `--base-url` is `https://example.com`, so for real checks you should pass your site URL.

Example with a generic site:

```bash
python3 check_en_fr_templates.py --base-url https://example.com --fr-prefix /fr --sample-size 20
```

Useful options:

```bash
python3 check_en_fr_templates.py \
  --base-url https://example.com \
  --fr-prefix /fr \
  --sample-size 30 \
  --threshold 0.90 \
  --seed 42 \
  --timeout 20 \
  --max-sitemaps 25 \
  --max-crawl-pages 200 \
  --csv-output findings.csv
```

## Exit Codes

- `0`: all sampled pairs matched threshold
- `1`: at least one template mismatch, missing FR page, non-HTML pair, or fetch/runtime error
- `2`: invalid input or no candidates found

## Console Summary

The report includes:

- total checked
- matches
- template mismatches
- missing FR pages
- non-HTML pairs
- errors
- overall pass/fail

## CSV Output

By default, the script writes `findings.csv` in the current working directory.

Columns:

- `result` (`PASS` or `FAIL`)
- `finding_type` (`match`, `mismatch`, `missing-fr-page`, `non-html-pair`, `error`)
- `similarity`
- `en_status`
- `fr_status`
- `en_url`
- `en_lang` (from `<html lang>`, otherwise `NA`)
- `fr_url`
- `fr_lang` (from `<html lang>`, otherwise `NA`)
- `message`

## Notes

- This is a structural comparison, not text-content comparison.
- Some pages can still fail due to intentional template differences or unavailable FR equivalents.
- If your site uses a different EN/FR URL scheme, update `en_to_fr_url()` in `check_en_fr_templates.py`.
