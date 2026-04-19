# frcheck

Randomly checks that English and French pages on a website use the same HTML template structure.

## What It Does

- Reads page URLs from sitemap XML files (starting at `https://www.example.com/sitemap.xml`)
- Also checks sitemap URLs listed in `robots.txt` and common sitemap filenames
- Falls back to internal link crawling if no sitemap URLs are found
- Samples random EN pages
- Maps each EN path to the FR path by adding the configured FR prefix (default: `/fr`)
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
  --delay 0.5 \
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

By default, the script writes `findings.csv` and `findings.log` in the current working directory. The log mirrors everything printed to the console during the run. Both files share the same stem as `--csv-output`.

Columns:

- `result` (`PASS` or `FAIL`)
- `finding_type` (`match`, `mismatch`, `missing-fr-page`, `non-html-pair`, `error`)
- `similarity`
- `en_status`
- `fr_status`
- `en_url`
- `en_lang` (from `<html lang>`, otherwise `NA`)
- `en_lang_effective` (best-effort locale from source + CMS hints)
- `en_lang_expected` (inferred from URL locale: `en` or `fr`)
- `en_lang_match` (`yes`, `no`, or `NA`)
- `fr_url`
- `fr_lang` (from `<html lang>`, otherwise `NA`)
- `fr_lang_effective` (best-effort locale from source + CMS hints)
- `fr_lang_expected` (inferred from URL locale: `en` or `fr`)
- `fr_lang_match` (`yes`, `no`, or `NA`)
- `message`

## Notes

- This is a structural comparison, not text-content comparison.
- Some pages can still fail due to intentional template differences or unavailable FR equivalents.
- FR URLs are resolved from `<link rel="alternate" hreflang="fr">` tags in the EN page when present, so translated slugs (e.g. `/parking` → `/fr/stationnement/`) are handled automatically. Path-prefix mapping is used as a fallback when no hreflang is found.
- If your site uses a different EN/FR URL scheme and has no hreflang tags, update `en_to_fr_url()` in `check_en_fr_templates.py`.
- Lang detection reads the server-rendered HTML only. Pages that set `<html lang>` via JavaScript will show the pre-JS value.
