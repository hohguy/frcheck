#!/usr/bin/env python3
"""
Scan random pages on a site and compare EN/FR page templates by HTML structure.

Default assumptions:
- English pages live at: <base-url>/<path>
- French pages live at:  <base-url>/<fr-prefix>/<path>

This script:
1) Discovers pages from sitemap XML files
2) Randomly samples candidate EN URLs
3) Fetches EN + mapped FR pages
4) Computes a structural signature of each HTML page
5) Reports likely template mismatches
"""

from __future__ import annotations

import argparse
import collections
import csv
import random
import re
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from difflib import SequenceMatcher
from html.parser import HTMLParser
from typing import Iterable, List, Optional, Sequence, Set, Tuple


DEFAULT_USER_AGENT = "frcheck-template-scanner/1.0"
DEFAULT_SITEMAP_CANDIDATES = (
    "/sitemap.xml",
    "/sitemap_index.xml",
    "/sitemap-index.xml",
    "/wp-sitemap.xml",
)


@dataclass
class CheckResult:
    en_url: str
    en_lang: str
    fr_url: str
    fr_lang: str
    en_status: Optional[int]
    fr_status: Optional[int]
    similarity: Optional[float]
    ok: bool
    finding_type: str
    message: str


class StructureParser(HTMLParser):
    """Builds a structural token sequence from HTML start/end tags.

    We compare tag order + stable attribute names (not values) to focus on template
    structure and avoid differences caused by translated content.
    """

    # Attributes likely to contain translated or content-specific values.
    _DROP_ATTRS = {
        "alt",
        "content",
        "datetime",
        "href",
        "src",
        "srcset",
        "title",
        "value",
    }

    # Tags that are mostly content containers where internal structure can vary.
    _IGNORE_TAGS = {"script", "style", "noscript"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tokens: List[str] = []
        self._ignore_stack: List[str] = []

    def _stable_attr_names(self, attrs: Sequence[Tuple[str, Optional[str]]]) -> List[str]:
        names: Set[str] = set()
        for name, _ in attrs:
            if not name:
                continue
            lname = name.lower()
            if lname in self._DROP_ATTRS:
                continue
            if lname.startswith("on"):
                continue
            names.add(lname)
        return sorted(names)

    def handle_starttag(self, tag: str, attrs: Sequence[Tuple[str, Optional[str]]]) -> None:
        ltag = tag.lower()
        if self._ignore_stack:
            if ltag in self._IGNORE_TAGS:
                self._ignore_stack.append(ltag)
            return

        if ltag in self._IGNORE_TAGS:
            self._ignore_stack.append(ltag)
            return

        attr_names = self._stable_attr_names(attrs)
        self.tokens.append(f"<{ltag}|{','.join(attr_names)}>")

    def handle_startendtag(self, tag: str, attrs: Sequence[Tuple[str, Optional[str]]]) -> None:
        # Normalize self-closing tags into explicit open+close tokens.
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        ltag = tag.lower()
        if self._ignore_stack:
            if ltag == self._ignore_stack[-1]:
                self._ignore_stack.pop()
            return

        if ltag in self._IGNORE_TAGS:
            return

        self.tokens.append(f"</{ltag}>")


class LinkExtractor(HTMLParser):
    """Extract absolute URLs from anchor tags in an HTML page."""

    def __init__(self, base_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.links: List[str] = []

    def handle_starttag(self, tag: str, attrs: Sequence[Tuple[str, Optional[str]]]) -> None:
        if tag.lower() != "a":
            return

        href: Optional[str] = None
        for name, value in attrs:
            if name and name.lower() == "href":
                href = value
                break

        if not href:
            return

        href = href.strip()
        if not href or href.startswith("#"):
            return
        if href.lower().startswith(("mailto:", "tel:", "javascript:")):
            return

        resolved = urllib.parse.urljoin(self.base_url, href)
        self.links.append(resolved)


def normalize_base_url(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    if not parsed.scheme:
        url = "https://" + url
        parsed = urllib.parse.urlparse(url)
    if not parsed.netloc:
        raise ValueError(f"Invalid base URL: {url}")
    clean = urllib.parse.urlunparse((parsed.scheme, parsed.netloc, "", "", "", ""))
    return clean.rstrip("/")


def fetch_url(
    url: str,
    timeout: float,
    user_agent: str,
) -> Tuple[Optional[int], str, Optional[str], Optional[str]]:
    req = urllib.request.Request(url, headers={"User-Agent": user_agent})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status = getattr(resp, "status", None) or resp.getcode()
            body_bytes = resp.read()
            charset = resp.headers.get_content_charset() or "utf-8"
            content_type = resp.headers.get("Content-Type")
            try:
                body = body_bytes.decode(charset, errors="replace")
            except LookupError:
                body = body_bytes.decode("utf-8", errors="replace")
            return status, body, None, content_type
    except urllib.error.HTTPError as err:
        try:
            body = err.read().decode("utf-8", errors="replace")
        except Exception:
            body = ""
        content_type = err.headers.get("Content-Type") if err.headers else None
        return err.code, body, f"HTTPError: {err.code} {err.reason}", content_type
    except urllib.error.URLError as err:
        reason = getattr(err, "reason", err)
        return None, "", f"URLError: {reason}", None
    except TimeoutError:
        return None, "", "TimeoutError", None
    except socket.timeout:
        return None, "", "socket.timeout", None
    except OSError as err:
        return None, "", f"OSError: {err}", None
    except Exception as err:  # Defensive catch to keep batch runs alive.
        return None, "", f"UnexpectedError: {err}", None


def fetch_xml(
    url: str,
    timeout: float,
    user_agent: str,
) -> Tuple[Optional[int], str, Optional[str], Optional[str]]:
    return fetch_url(url, timeout, user_agent)


def is_html_content_type(content_type: Optional[str]) -> bool:
    if not content_type:
        return True
    lower = content_type.lower()
    return "text/html" in lower or "application/xhtml+xml" in lower


def extract_sitemaps_from_robots(base_url: str, timeout: float, user_agent: str) -> List[str]:
    robots_url = base_url + "/robots.txt"
    status, text, _, _ = fetch_url(robots_url, timeout, user_agent)
    if not status or status >= 400:
        return []

    sitemaps: List[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.lower().startswith("sitemap:"):
            loc = stripped.split(":", 1)[1].strip()
            if loc:
                sitemaps.append(urllib.parse.urljoin(base_url + "/", loc))

    return list(dict.fromkeys(sitemaps))


def discover_sitemap_roots(base_url: str, timeout: float, user_agent: str) -> List[str]:
    roots: List[str] = []

    roots.extend(extract_sitemaps_from_robots(base_url, timeout, user_agent))
    for path in DEFAULT_SITEMAP_CANDIDATES:
        roots.append(base_url + path)

    return list(dict.fromkeys(roots))


def strip_xml_namespace(tag: str) -> str:
    if "}" in tag:
        return tag.split("}", 1)[1]
    return tag


def extract_locs_from_sitemap(xml_text: str) -> List[str]:
    xml_text = xml_text.strip()
    if not xml_text:
        return []

    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []

    locs: List[str] = []
    for elem in root.iter():
        if strip_xml_namespace(elem.tag).lower() != "loc":
            continue
        if elem.text:
            loc = elem.text.strip()
            if loc:
                locs.append(loc)
    return locs


def crawl_sitemaps(
    base_url: str,
    timeout: float,
    user_agent: str,
    max_sitemaps: int,
    initial_sitemaps: Optional[Sequence[str]] = None,
) -> List[str]:
    """Recursively crawl sitemap index files and return discovered URLs."""
    queue: List[str] = list(initial_sitemaps or [base_url + "/sitemap.xml"])
    seen: Set[str] = set()
    discovered_urls: List[str] = []

    while queue and len(seen) < max_sitemaps:
        sitemap_url = queue.pop(0)
        if sitemap_url in seen:
            continue
        seen.add(sitemap_url)

        status, xml_text, _, _ = fetch_xml(sitemap_url, timeout, user_agent)
        if not status or status >= 400:
            continue

        locs = extract_locs_from_sitemap(xml_text)
        if not locs:
            continue

        for loc in locs:
            lower = loc.lower()
            if lower.endswith(".xml"):
                if loc not in seen and loc not in queue:
                    queue.append(loc)
            else:
                discovered_urls.append(loc)

    # Deduplicate while preserving order.
    unique_urls = list(dict.fromkeys(discovered_urls))
    return unique_urls


def extract_internal_links(page_url: str, html: str) -> List[str]:
    parser = LinkExtractor(page_url)
    try:
        parser.feed(html)
        parser.close()
    except Exception:
        pass
    return parser.links


def crawl_internal_links(
    base_url: str,
    timeout: float,
    user_agent: str,
    max_pages: int,
) -> List[str]:
    """Fallback URL discovery when sitemap files are unavailable."""
    parsed_base = urllib.parse.urlparse(base_url)
    netloc = parsed_base.netloc.lower()

    queue: List[str] = [base_url + "/"]
    seen: Set[str] = set()
    discovered: List[str] = []

    while queue and len(seen) < max_pages:
        current = canonicalize_page_url(queue.pop(0))
        if current in seen:
            continue
        seen.add(current)
        discovered.append(current)

        status, html, _, _ = fetch_url(current, timeout, user_agent)
        if not status or status >= 400:
            continue

        for link in extract_internal_links(current, html):
            parsed = urllib.parse.urlparse(link)
            if parsed.netloc.lower() != netloc:
                continue
            # Skip obvious non-HTML assets while crawling.
            if re.search(
                r"\.(pdf|jpg|jpeg|png|gif|svg|webp|zip|docx?|xlsx?|pptx?|css|js)$",
                parsed.path or "",
                flags=re.IGNORECASE,
            ):
                continue
            normalized = canonicalize_page_url(link)
            if normalized not in seen and normalized not in queue:
                queue.append(normalized)

    return discovered


def canonicalize_page_url(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    path = parsed.path or "/"
    if not path.startswith("/"):
        path = "/" + path

    # Keep homepage slash, trim others.
    if path != "/" and path.endswith("/"):
        path = path.rstrip("/")

    # Ignore query/fragment for template checks.
    return urllib.parse.urlunparse((parsed.scheme, parsed.netloc, path, "", "", ""))


def select_effective_base_url(base_url: str, discovered_urls: Sequence[str]) -> str:
    """Pick the best host for checks based on discovered URLs.

    Some domains redirect to a canonical host in sitemap entries.
    """
    base = urllib.parse.urlparse(base_url)
    base_host = base.netloc.lower()

    hosts: List[str] = []
    for raw in discovered_urls:
        parsed = urllib.parse.urlparse(raw)
        if parsed.scheme not in {"http", "https"}:
            continue
        if parsed.netloc:
            hosts.append(parsed.netloc.lower())

    if not hosts:
        return base_url

    counts = collections.Counter(hosts)
    if base_host in counts:
        return base_url

    dominant_host = counts.most_common(1)[0][0]
    return urllib.parse.urlunparse((base.scheme, dominant_host, "", "", "", ""))


def en_to_fr_url(base_url: str, en_url: str, fr_prefix: str) -> Optional[str]:
    parsed_base = urllib.parse.urlparse(base_url)
    parsed_en = urllib.parse.urlparse(en_url)

    if not fr_prefix.startswith("/"):
        fr_prefix = "/" + fr_prefix
    fr_prefix = fr_prefix.rstrip("/") or "/fr"

    # Only compare pages under the same host.
    if parsed_en.netloc.lower() != parsed_base.netloc.lower():
        return None

    path = parsed_en.path or "/"
    if not path.startswith("/"):
        path = "/" + path

    # Skip pages already in FR section.
    if path == fr_prefix or path.startswith(fr_prefix + "/"):
        return None

    fr_path = fr_prefix if path == "/" else f"{fr_prefix}{path}"
    return urllib.parse.urlunparse((parsed_base.scheme, parsed_base.netloc, fr_path, "", "", ""))


def structural_tokens(html: str) -> List[str]:
    # Remove comments first to avoid noise.
    cleaned = re.sub(r"<!--.*?-->", "", html, flags=re.DOTALL)

    parser = StructureParser()
    try:
        parser.feed(cleaned)
        parser.close()
    except Exception:
        # If parsing fails, return whatever was collected.
        pass
    return parser.tokens


def extract_page_lang(html: str) -> str:
    """Extract <html lang="..."> value, returning NA when unavailable."""
    if not html:
        return "NA"

    match = re.search(
        r"<html[^>]*\blang\s*=\s*(?:\"([^\"]+)\"|'([^']+)'|([^\s>]+))",
        html,
        flags=re.IGNORECASE,
    )
    if not match:
        return "NA"

    lang = (match.group(1) or match.group(2) or match.group(3) or "").strip()
    return lang or "NA"


def similarity_score(tokens_a: Sequence[str], tokens_b: Sequence[str]) -> float:
    if not tokens_a and not tokens_b:
        return 1.0
    matcher = SequenceMatcher(a=tokens_a, b=tokens_b)
    return matcher.ratio()


def path_depth(url: str) -> int:
    path = urllib.parse.urlparse(url).path.strip("/")
    if not path:
        return 0
    return len([p for p in path.split("/") if p])


def filter_candidate_en_urls(base_url: str, urls: Iterable[str]) -> List[str]:
    parsed_base = urllib.parse.urlparse(base_url)
    netloc = parsed_base.netloc.lower()

    candidates: List[str] = []
    for raw_url in urls:
        try:
            url = canonicalize_page_url(raw_url)
        except Exception:
            continue
        parsed = urllib.parse.urlparse(url)
        if parsed.netloc.lower() != netloc:
            continue

        path = parsed.path or "/"

        # Ignore obvious non-HTML assets.
        if re.search(r"\.(pdf|jpg|jpeg|png|gif|svg|webp|zip|docx?|xlsx?|pptx?)$", path, flags=re.IGNORECASE):
            continue

        # Skip FR pages; we only sample EN source pages.
        if path == "/fr" or path.startswith("/fr/"):
            continue

        candidates.append(url)

    # Prefer deeper paths a little (more likely to be varied templates), but keep all.
    candidates = list(dict.fromkeys(candidates))
    candidates.sort(key=path_depth)
    return candidates


def check_pair(
    en_url: str,
    fr_url: str,
    timeout: float,
    user_agent: str,
    threshold: float,
) -> CheckResult:
    en_status, en_html, en_error, en_content_type = fetch_url(en_url, timeout, user_agent)
    fr_status, fr_html, fr_error, fr_content_type = fetch_url(fr_url, timeout, user_agent)
    en_lang = extract_page_lang(en_html)
    fr_lang = extract_page_lang(fr_html)

    if not en_status or en_status >= 400:
        reason = en_error or f"status={en_status}"
        return CheckResult(
            en_url=en_url,
            en_lang=en_lang,
            fr_url=fr_url,
            fr_lang=fr_lang,
            en_status=en_status,
            fr_status=fr_status,
            similarity=None,
            ok=False,
            finding_type="error",
            message=f"EN fetch failed ({reason})",
        )

    if not fr_status or fr_status >= 400:
        reason = fr_error or f"status={fr_status}"
        finding_type = "missing-fr-page" if fr_status == 404 and en_status and en_status < 400 else "error"
        return CheckResult(
            en_url=en_url,
            en_lang=en_lang,
            fr_url=fr_url,
            fr_lang=fr_lang,
            en_status=en_status,
            fr_status=fr_status,
            similarity=None,
            ok=False,
            finding_type=finding_type,
            message=f"FR fetch failed ({reason})",
        )

    if not is_html_content_type(en_content_type) or not is_html_content_type(fr_content_type):
        return CheckResult(
            en_url=en_url,
            en_lang=en_lang,
            fr_url=fr_url,
            fr_lang=fr_lang,
            en_status=en_status,
            fr_status=fr_status,
            similarity=None,
            ok=False,
            finding_type="non-html-pair",
            message=(
                "Non-HTML pair "
                f"(EN Content-Type: {en_content_type or 'unknown'}, "
                f"FR Content-Type: {fr_content_type or 'unknown'})"
            ),
        )

    en_tokens = structural_tokens(en_html)
    fr_tokens = structural_tokens(fr_html)

    score = similarity_score(en_tokens, fr_tokens)
    ok = score >= threshold
    msg = "Template match" if ok else "Template mismatch"

    return CheckResult(
        en_url=en_url,
        en_lang=en_lang,
        fr_url=fr_url,
        fr_lang=fr_lang,
        en_status=en_status,
        fr_status=fr_status,
        similarity=score,
        ok=ok,
        finding_type="match" if ok else "mismatch",
        message=msg,
    )


def choose_samples(candidates: Sequence[str], count: int, seed: Optional[int]) -> List[str]:
    if not candidates:
        return []
    rnd = random.Random(seed)
    if count >= len(candidates):
        return list(candidates)
    return rnd.sample(list(candidates), count)


def write_csv_report(csv_output: str, results: Sequence[CheckResult]) -> bool:
    try:
        with open(csv_output, "w", encoding="utf-8", newline="") as fh:
            writer = csv.writer(fh)
            writer.writerow(
                [
                    "result",
                    "finding_type",
                    "similarity",
                    "en_status",
                    "fr_status",
                    "en_url",
                    "en_lang",
                    "fr_url",
                    "fr_lang",
                    "message",
                ]
            )
            for r in results:
                writer.writerow(
                    [
                        "PASS" if r.ok else "FAIL",
                        r.finding_type,
                        "" if r.similarity is None else f"{r.similarity:.6f}",
                        "" if r.en_status is None else str(r.en_status),
                        "" if r.fr_status is None else str(r.fr_status),
                        r.en_url,
                        r.en_lang,
                        r.fr_url,
                        r.fr_lang,
                        r.message,
                    ]
                )
        return True
    except OSError as err:
        print(f"Warning: failed to write CSV output to '{csv_output}': {err}", file=sys.stderr)
        return False


def print_report(results: Sequence[CheckResult]) -> int:
    print("\n=== EN/FR Template Check Report ===")
    print(f"Total checked: {len(results)}")

    ok_count = sum(1 for r in results if r.ok)
    mismatch_count = sum(1 for r in results if r.finding_type == "mismatch")
    missing_fr_count = sum(1 for r in results if r.finding_type == "missing-fr-page")
    non_html_count = sum(1 for r in results if r.finding_type == "non-html-pair")
    error_count = sum(1 for r in results if r.finding_type == "error")
    fail_count = mismatch_count + missing_fr_count + non_html_count + error_count
    print(f"Matches: {ok_count}")
    print(f"Template mismatches: {mismatch_count}")
    print(f"Missing FR pages: {missing_fr_count}")
    print(f"Non-HTML pairs: {non_html_count}")
    print(f"Errors: {error_count}")
    print(f"Overall: {'PASS' if fail_count == 0 else 'FAIL'}")

    if not results:
        return 2

    print("\nDetails:")
    for r in results:
        score_str = f"{r.similarity:.3f}" if r.similarity is not None else "n/a"
        status = "PASS" if r.ok else "FAIL"
        print(f"[{status}] score={score_str} | EN={r.en_status} FR={r.fr_status}")
        print(f"  EN: {r.en_url}")
        print(f"  FR: {r.fr_url}")
        print(f"  {r.message}")

    # Exit non-zero when any mismatch/error is found.
    return 1 if fail_count else 0


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Sample random EN pages from sitemap and compare EN vs FR HTML structure."
    )
    parser.add_argument(
        "--base-url",
        default="https://example.com",
        help="Base site URL (default: https://example.com)",
    )
    parser.add_argument(
        "--fr-prefix",
        default="/fr",
        help="Path prefix for FR pages (default: /fr)",
    )
    parser.add_argument(
        "--sample-size",
        type=int,
        default=20,
        help="Number of EN pages to sample (default: 20)",
    )
    parser.add_argument(
        "--threshold",
        type=float,
        default=0.92,
        help="Similarity threshold for PASS (0-1, default: 0.92)",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=None,
        help="Random seed for reproducible sampling",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=15.0,
        help="HTTP timeout in seconds (default: 15)",
    )
    parser.add_argument(
        "--max-sitemaps",
        type=int,
        default=25,
        help="Maximum sitemap XML files to crawl (default: 25)",
    )
    parser.add_argument(
        "--max-crawl-pages",
        type=int,
        default=150,
        help="Maximum pages to crawl when sitemap discovery fails (default: 150)",
    )
    parser.add_argument(
        "--user-agent",
        default=DEFAULT_USER_AGENT,
        help=f"User-Agent header (default: {DEFAULT_USER_AGENT})",
    )
    parser.add_argument(
        "--csv-output",
        default="findings.csv",
        help="CSV output file path (default: findings.csv)",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str]) -> int:
    try:
        args = parse_args(argv)
    except SystemExit:
        raise
    except Exception as err:
        print(f"Argument parsing failed: {err}", file=sys.stderr)
        return 2

    if args.sample_size <= 0:
        print("--sample-size must be > 0", file=sys.stderr)
        return 2

    if not (0.0 <= args.threshold <= 1.0):
        print("--threshold must be between 0 and 1", file=sys.stderr)
        return 2

    if args.max_crawl_pages <= 0:
        print("--max-crawl-pages must be > 0", file=sys.stderr)
        return 2

    if args.max_sitemaps <= 0:
        print("--max-sitemaps must be > 0", file=sys.stderr)
        return 2

    if args.timeout <= 0:
        print("--timeout must be > 0", file=sys.stderr)
        return 2

    if not args.fr_prefix.strip():
        print("--fr-prefix must not be empty", file=sys.stderr)
        return 2

    if not args.csv_output.strip():
        print("--csv-output must not be empty", file=sys.stderr)
        return 2

    try:
        base_url = normalize_base_url(args.base_url)
    except ValueError as err:
        print(str(err), file=sys.stderr)
        return 2

    try:
        print(f"Base URL: {base_url}")
        print("Discovering pages from sitemap...")
        sitemap_roots = discover_sitemap_roots(base_url, args.timeout, args.user_agent)
        print(f"Sitemap roots to try: {len(sitemap_roots)}")
        all_urls = crawl_sitemaps(
            base_url=base_url,
            timeout=args.timeout,
            user_agent=args.user_agent,
            max_sitemaps=args.max_sitemaps,
            initial_sitemaps=sitemap_roots,
        )

        if not all_urls:
            print("No URLs found via sitemap. Falling back to internal link crawl...")
            all_urls = crawl_internal_links(
                base_url=base_url,
                timeout=args.timeout,
                user_agent=args.user_agent,
                max_pages=args.max_crawl_pages,
            )

        effective_base_url = select_effective_base_url(base_url, all_urls)
        if effective_base_url != base_url:
            print(f"Using canonical host from discovered URLs: {effective_base_url}")

        print(f"Discovered URL count: {len(all_urls)}")
        candidates = filter_candidate_en_urls(effective_base_url, all_urls)
        print(f"Candidate EN pages: {len(candidates)}")

        samples = choose_samples(candidates, args.sample_size, args.seed)
        if not samples:
            print("No candidate EN pages found. Check sitemap availability.", file=sys.stderr)
            return 2

        print(f"Sampling {len(samples)} pages (seed={args.seed})")

        results: List[CheckResult] = []
        start_time = time.time()

        for idx, en_url in enumerate(samples, start=1):
            try:
                fr_url = en_to_fr_url(effective_base_url, en_url, args.fr_prefix)
                if not fr_url:
                    continue

                print(f"[{idx}/{len(samples)}] Checking:")
                print(f"  EN: {en_url}")
                print(f"  FR: {fr_url}")

                result = check_pair(
                    en_url=en_url,
                    fr_url=fr_url,
                    timeout=args.timeout,
                    user_agent=args.user_agent,
                    threshold=args.threshold,
                )
                results.append(result)
            except KeyboardInterrupt:
                print("\nInterrupted by user.", file=sys.stderr)
                break
            except Exception as err:
                results.append(
                    CheckResult(
                        en_url=en_url,
                        en_lang="NA",
                        fr_url=en_to_fr_url(effective_base_url, en_url, args.fr_prefix) or "",
                        fr_lang="NA",
                        en_status=None,
                        fr_status=None,
                        similarity=None,
                        ok=False,
                        finding_type="error",
                        message=f"Unexpected error during pair check: {err}",
                    )
                )

        elapsed = time.time() - start_time
        csv_written = write_csv_report(args.csv_output, results)
        code = print_report(results)
        if csv_written:
            print(f"CSV output: {args.csv_output}")
        print(f"\nCompleted in {elapsed:.1f}s")
        return code
    except KeyboardInterrupt:
        print("\nInterrupted by user.", file=sys.stderr)
        return 130
    except Exception as err:
        print(f"Fatal error: {err}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
