#!/usr/bin/env python3
"""
Config-driven news scraper for the Daleel QSE pipeline.

Source registry lives in news_sources.json — add, remove, or toggle sources
there without touching this file. Each source is tried RSS-first, HTML fallback
second. All outputs are normalized to the same article dict schema.
"""

import json
import re
import sys
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Optional
from urllib.parse import urljoin

import feedparser
import requests
from bs4 import BeautifulSoup

SOURCES_FILE = Path(__file__).parent / "news_sources.json"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ar,en;q=0.9",
}
TIMEOUT = 20
REQUEST_DELAY = 1.5   # seconds between RSS feed requests
BODY_FETCH_DELAY = 1.0  # seconds between full-article fetches (different domains)
BODY_MIN_LENGTH = 200   # chars; bodies shorter than this trigger a full-page fetch

# ---------------------------------------------------------------------------
# Category classifier (keyword-based)
# ---------------------------------------------------------------------------

FINANCE_KEYWORDS = {
    "en": ["stock", "share", "market", "bank", "profit", "revenue", "earnings",
           "gdp", "oil", "gas", "investment", "finance", "economy", "economic",
           "trading", "dividend", "ipo", "quarter", "fiscal", "budget", "inflation",
           "interest rate", "monetary", "qse", "exchange"],
    "ar": ["سهم", "أسهم", "بورصة", "بنك", "ربح", "إيرادات", "اقتصاد", "نفط",
           "غاز", "استثمار", "مالية", "ميزانية", "تداول", "توزيعات", "أرباح",
           "تضخم", "فائدة", "سوق"],
}
REGIONAL_KEYWORDS = {
    "en": ["gulf", "gcc", "saudi", "uae", "kuwait", "bahrain", "oman", "egypt",
           "jordan", "iraq", "iran", "turkey", "qatar"],
    "ar": ["خليج", "مجلس التعاون", "السعودية", "الإمارات", "الكويت", "البحرين",
           "عمان", "مصر", "الأردن", "العراق", "إيران", "تركيا", "قطر"],
}
INTL_KEYWORDS = {
    "en": ["us", "china", "europe", "eu", "global", "world", "international",
           "russia", "ukraine", "fed", "opec", "imf", "world bank", "un"],
    "ar": ["أمريكا", "الصين", "أوروبا", "عالمي", "دولي", "روسيا", "أوبك"],
}


def classify_category(text: str, lang: str = "en") -> str:
    t = text.lower()
    lang_key = "ar" if lang == "ar" else "en"
    if any(kw in t for kw in FINANCE_KEYWORDS.get(lang_key, [])):
        return "finance"
    if any(kw in t for kw in REGIONAL_KEYWORDS.get(lang_key, [])):
        return "regional"
    if any(kw in t for kw in INTL_KEYWORDS.get(lang_key, [])):
        return "international"
    return "politics"


# ---------------------------------------------------------------------------
# QSE entity extraction
# ---------------------------------------------------------------------------

# Static curated aliases for all 56 QSE-listed stocks (English + Arabic).
# Rules:
#   - Keep ALL historical names so old news articles still match after rebrandings.
#   - Arabic names first for each entry where the primary coverage is Arabic press.
#   - KCBK (Al Khalij Commercial Bank) was merged into MARK (Masraf Al Rayan) in 2022;
#     its old aliases are kept under MARK so pre-merger articles still link correctly.
#   - GECO was a duplicate phantom ticker (not a real QSE symbol); removed.
QSE_ALIASES: dict[str, list[str]] = {
    # --- Banking ---
    "QNBK": ["Qatar National Bank", "QNB", "بنك قطر الوطني", "بنك قطر"],
    "CBQK": ["Commercial Bank", "Commercial Bank of Qatar", "البنك التجاري", "CBQ"],
    "DHBK": ["Doha Bank", "بنك الدوحة"],
    "ABQK": ["Ahli Bank", "Ahli Bank Qatar", "المصرف الأهلي"],
    "QIIK": ["Qatar International Islamic Bank", "International Islamic Bank",
             "بنك قطر الدولي الإسلامي", "QIIB"],
    "QIBK": ["Qatar Islamic Bank", "QIB", "بنك قطر الإسلامي"],
    # MARK absorbed KCBK (Al Khalij Commercial Bank) in 2022; both name sets kept
    "MARK": ["Masraf Al Rayan", "Al Rayan", "Rayan", "مصرف الريان",
             "Al Khalij Commercial Bank", "Khalij Commercial Bank", "بنك الخليج التجاري"],
    "QFBQ": ["Lesha Bank", "Qatar First Bank", "QFC Bank", "بنك ليشا", "بنك قطر الأول"],
    "DUBK": ["Dukhan Bank", "بنك دخان"],
    # --- Telecom ---
    "ORDS": ["Ooredoo", "أوريدو", "Qtel", "Qatar Telecom"],
    "VFQS": ["Vodafone Qatar", "فودافون قطر"],
    # --- Insurance ---
    "QATI": ["Qatar Insurance", "Qatar Insurance Company", "QIC", "قطر للتأمين"],
    "DOHI": ["Doha Insurance", "Doha Insurance Group", "التأمين الدوحة"],
    "QGRI": ["Qatar General Insurance", "General Insurance", "قطر للتأمين العام"],
    "AKHI": ["Alkhaleej Takaful", "Al Khaleej Takaful", "الخليج للتكافل"],
    "BEMA": ["Beema", "بيما"],
    # IHGS rebranded to Inma Holding; old name kept for historical coverage
    "IHGS": ["Inma Holding", "Inma", "Islamic Holding Group", "انماء القابضة",
             "المجموعة الإسلامية القابضة"],
    "QISI": ["Qatar Islamic Insurance", "قطر للتأمين الإسلامي"],
    # --- Industrials / Energy ---
    "IQCD": ["Industries Qatar", "إندستريز قطر"],
    "MPHC": ["Mesaieed Petrochemical", "Mesaieed", "مسيعيد للبتروكيماويات", "QAFCO"],
    "QAMC": ["QAMCO", "Qatar Aluminium Manufacturing", "قاتكو", "قطر للألومنيوم"],
    "QIMD": ["Qatar Industrial Manufacturing", "Industries Manufacturing", "الصناعية للإنتاج"],
    # QEWS rebranded to Nebras Energy; old name kept for historical articles
    "QEWS": ["Nebras Energy", "Nebras", "Qatar Electricity", "Qatar Electricity and Water",
             "نبراس للطاقة", "كهرباء قطر ومياهها"],
    "QGMD": ["Qatar German Medical Devices", "Qatar German Co", "QGMD",
             "الشركة القطرية الألمانية للأجهزة الطبية", "قطر الألمانية"],
    # --- Transport / Logistics ---
    "QGTS": ["Nakilat", "ناقلات", "Qatar Gas Transport", "Qatar LNG Transport"],
    "QNNS": ["Qatar Navigation", "Milaha", "الملاحة القطرية", "ميلاها"],
    "GWCS": ["Gulf Warehousing", "Gulf Warehousing Company", "الخليجية للمستودعات"],
    # --- Real Estate ---
    "BRES": ["Barwa Real Estate", "Barwa", "بروة العقارية", "بروة"],
    "UDCD": ["United Development Company", "UDC", "الشركة المتحدة للتطوير"],
    "ERES": ["Ezdan Holding", "Ezdan", "إزدان القابضة"],
    "IGRD": ["Estithmar Holding", "Estithmar", "استثمار القابضة"],
    "MRDS": ["Mazaya Qatar", "Mazaya", "مزايا قطر"],
    # --- Consumer / Retail ---
    "MERS": ["Al Meera", "الميرة", "Al Meera Consumer Goods"],
    "ZHCD": ["Zad Holding", "Zad", "زاد القابضة"],
    "WDAM": ["Widam Food", "Widam", "ودام"],
    "QFLS": ["Qatar Fuel", "Woqod", "وقود", "قطر للوقود"],
    "MCGS": ["Medicare Group", "Medicare", "ميديكير"],
    "QCFS": ["Qatar Cinema", "Qatar Cinema and Film Distribution", "سينما قطر"],
    # --- Conglomerates / Services ---
    "GISS": ["Gulf International Services", "GIS", "الخليج الدولية للخدمات"],
    "MCCS": ["Mannai Corporation", "Mannai Corp", "شركة مناعي"],
    "SIIS": ["Salam International", "سلام الدولية"],
    "NLCS": ["National Leasing", "Alijarah Holding", "التأجير الوطنية", "الإجارة القابضة"],
    "DBIS": ["Dlala Brokerage", "Dlala", "دلالة"],
    "AHCS": ["Aamal Company", "Aamal", "أعمال"],
    "QOIS": ["Qatar Oman Investment", "قطر عمان للاستثمار"],
    "QNCD": ["National Cement", "Qatar National Cement", "الأسمنت الوطنية"],
    "MKDM": ["Mekdam Holding", "Mekdam", "مكدام"],
    "MEZA": ["MEEZA QSTP", "MEEZA", "ميزا"],
    "FALH": ["Faleh Education", "Faleh", "فالح"],
    "MHAR": ["Al Mahhar Holding", "Al Mahhar", "المهار"],
    "MFMS": ["Mosanada Services", "Mosanada", "مسانده"],
    # --- Funds / ETFs ---
    "QETF": ["QE Index ETF", "QSE ETF", "بورصة قطر ETF"],
    "QATR": ["Al Rayan Qatar ETF", "Rayan ETF"],
    # QLMI rebranded to QLM Life & Medical Insurance
    "QLMI": ["QLM Life", "QLM", "QL Investors", "مستثمرو QL", "QLM للتأمين على الحياة"],
    # --- Other ---
    "BLDN": ["Baladna", "بلدنا"],
    "QIGD": ["The Investors", "Investors Group", "مجموعة المستثمرين"],
}

# ---------------------------------------------------------------------------
# Dynamic alias layer — merges live QSE names into QSE_ALIASES once per run
# ---------------------------------------------------------------------------

_aliases_cache: Optional[dict] = None


def get_aliases() -> dict:
    """
    Return QSE_ALIASES merged with current official names from the live QSE scraper.

    Fetched once per process and cached. Falls back to the static QSE_ALIASES if
    the Playwright scraper is unavailable (e.g. during unit tests or offline runs).
    """
    global _aliases_cache
    if _aliases_cache is not None:
        return _aliases_cache

    merged: dict[str, list[str]] = {k: list(v) for k, v in QSE_ALIASES.items()}

    try:
        from qse_scraper import fetch as _qse_fetch
        data = _qse_fetch()
        stocks = (data.get("parsed") or {}).get("stocks", [])
        added = 0
        for s in stocks:
            sym = s.get("symbol", "")
            name = (s.get("name") or "").strip()
            if not sym or not name:
                continue
            if sym not in merged:
                merged[sym] = [name]
                added += 1
            elif not any(name.lower() in a.lower() or a.lower() in name.lower()
                         for a in merged[sym]):
                merged[sym].append(name)
                added += 1
        if added:
            print(f"[aliases] Added {added} live QSE name(s) to alias lookup", file=sys.stderr)
    except Exception:
        pass  # Playwright unavailable or scraper error — static aliases still work

    _aliases_cache = merged
    return _aliases_cache


def extract_entities(text: str) -> list[str]:
    """Return QSE ticker symbols whose company name or ticker appears in text."""
    found = []
    t_lower = text.lower()
    for ticker, aliases in get_aliases().items():
        # Require word boundary for ticker match so "MARK" doesn't fire on "market"
        if re.search(r"\b" + re.escape(ticker.lower()) + r"\b", t_lower):
            found.append(ticker)
            continue
        if any(alias.lower() in t_lower for alias in aliases):
            found.append(ticker)
    return list(set(found))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def detect_language(text: str) -> str:
    arabic_chars = sum(1 for c in text if "؀" <= c <= "ۿ")
    return "ar" if arabic_chars / max(len(text), 1) > 0.2 else "en"


def parse_rss_date(entry) -> Optional[str]:
    for field in ("published", "updated"):
        raw = getattr(entry, field, None) or entry.get(field)
        if raw:
            try:
                dt = parsedate_to_datetime(raw)
                return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            except Exception:
                pass
    return None


def clean_html(html: str) -> str:
    if not html:
        return ""
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script", "style", "nav", "footer", "header", "aside", "form"]):
        tag.decompose()
    return re.sub(r"\s+", " ", soup.get_text(separator=" ")).strip()


def make_article(
    url: str,
    title: str,
    body: str,
    source: str,
    published_at: Optional[str] = None,
    lang: Optional[str] = None,
) -> dict:
    combined = f"{title} {body}"
    detected_lang = lang or detect_language(combined)
    return {
        "url": url,
        "title": title.strip(),
        "body": body[:4000].strip(),
        "source": source,
        "published_at": published_at,
        "language": detected_lang,
        "category": classify_category(combined, detected_lang),
        "entities": extract_entities(combined),
    }


def safe_get(url: str, **kwargs) -> Optional[requests.Response]:
    kwargs.setdefault("timeout", TIMEOUT)
    try:
        r = requests.get(url, headers=HEADERS, **kwargs)
        r.raise_for_status()
        return r
    except Exception as e:
        print(f"  [warn] GET {url}: {e}", file=sys.stderr)
        return None


# ---------------------------------------------------------------------------
# Full article body fetcher
# ---------------------------------------------------------------------------

# Tried in order: named containers first (most reliable), structural fallbacks last.
# Covers English and Arabic news site conventions.
_ARTICLE_BODY_SELECTORS = [
    # --- Explicit named containers ---
    ".article-body", ".article-content", ".articleBody",
    ".story-body", ".story-content",
    ".post-body", ".post-content",
    ".entry-content", ".entry-body",
    ".news-body", ".news-content", ".news-details-content",
    ".content-body", ".main-content",
    ".wysiwyg",
    # --- Arabic-site patterns (Drupal / WordPress portals) ---
    ".field-items", ".article-text", ".node__content",
    # --- Schema.org structured markup ---
    "[itemprop='articleBody']",
    # --- Class-name pattern matching ---
    "[class*='article-body']", "[class*='articleBody']",
    "[class*='story-body']", "[class*='post-content']",
    "[class*='entry-content']",
    # --- Structural fallbacks (broad but imprecise) ---
    "article p", "main p",
]

# Tags and selectors stripped before body extraction to remove boilerplate.
_STRIP_TAGS = [
    "script", "style", "nav", "footer", "header", "aside",
    "form", "figure", "figcaption", "iframe",
]
_STRIP_SELECTORS = [
    ".ad", ".ads", ".advertisement",
    "[class*='ad-']", "[id*='-ad']",
    "[class*='social']", "[class*='share']",
    "[class*='related']", "[class*='recommended']", "[class*='popular']",
    "#comments", ".comments", "[class*='comment']",
    ".newsletter", ".subscribe", ".paywall",
]


def fetch_full_article(url: str) -> str:
    """
    Fetch a full article page and extract the body text.

    Returns clean text capped at 4000 chars, or empty string on any failure.
    The caller should check the returned length before replacing the RSS body.
    """
    r = safe_get(url, timeout=15)
    if not r:
        return ""

    try:
        soup = BeautifulSoup(r.text, "lxml")

        for tag_name in _STRIP_TAGS:
            for el in soup.find_all(tag_name):
                el.decompose()
        for sel in _STRIP_SELECTORS:
            for el in soup.select(sel):
                el.decompose()

        for selector in _ARTICLE_BODY_SELECTORS:
            elements = soup.select(selector)
            if not elements:
                continue
            text = " ".join(el.get_text(separator=" ") for el in elements)
            text = re.sub(r"\s+", " ", text).strip()
            if len(text) >= BODY_MIN_LENGTH:
                return text[:4000]

    except Exception as e:
        print(f"  [body] parse error {url}: {e}", file=sys.stderr)

    return ""


def _enrich_bodies(articles: list[dict]) -> list[dict]:
    """
    For each article whose body is shorter than BODY_MIN_LENGTH, fetch the full
    article page and replace the body. Re-derives category and entities from the
    richer text. Modifies the list in-place and returns it.
    """
    to_fetch = [a for a in articles if len(a.get("body", "")) < BODY_MIN_LENGTH]
    if not to_fetch:
        return articles

    print(
        f"  [body] Fetching full text for {len(to_fetch)}/{len(articles)} articles...",
        file=sys.stderr,
    )
    enriched = 0

    for art in to_fetch:
        full_body = fetch_full_article(art["url"])
        # Only replace if we actually got more content than the RSS summary
        if full_body and len(full_body) > len(art.get("body", "")):
            combined = f"{art['title']} {full_body}"
            art["body"] = full_body[:4000]
            art["category"] = classify_category(combined, art["language"])
            art["entities"] = extract_entities(combined)
            enriched += 1
        time.sleep(BODY_FETCH_DELAY)

    print(
        f"  [body] Enriched {enriched}/{len(to_fetch)} articles with full body",
        file=sys.stderr,
    )
    return articles


# ---------------------------------------------------------------------------
# Source config loader
# ---------------------------------------------------------------------------

def load_sources() -> list[dict]:
    """Load and return all sources from news_sources.json."""
    try:
        raw = SOURCES_FILE.read_text(encoding="utf-8")
        data = json.loads(raw)
        sources = []
        for entry in data.get("sources", []):
            # Skip comment-only entries (no 'id' field)
            if "id" not in entry:
                continue
            sources.append(entry)
        return sources
    except Exception as e:
        print(f"[scraper] ERROR reading {SOURCES_FILE}: {e}", file=sys.stderr)
        return []


# ---------------------------------------------------------------------------
# Generic scraper (works for any source config)
# ---------------------------------------------------------------------------

def scrape_source(source: dict) -> list[dict]:
    """
    RSS-first + HTML-fallback scraper driven by a source config dict.

    RSS: tries each URL in rss_urls in order, collects from all that succeed.
    HTML: used only if RSS yields zero articles; extracts links via
          html_article_selectors and stores title-only articles (no body).
          Full body fetching is handled separately (see issue 1b).
    """
    articles: list[dict] = []
    seen_urls: set[str] = set()
    source_id = source["id"]
    default_lang: Optional[str] = source.get("default_language")

    # --- Stage 1: RSS ---
    for rss_url in source.get("rss_urls") or []:
        try:
            feed = feedparser.parse(rss_url)
            if not feed.entries:
                continue

            for entry in feed.entries[:40]:
                url = entry.get("link", "")
                if not url or url in seen_urls:
                    continue
                seen_urls.add(url)

                title = entry.get("title", "").strip()
                # Prefer full content over summary when available
                content_list = entry.get("content") or []
                body_raw = (
                    content_list[0].get("value", "") if content_list else ""
                ) or entry.get("summary", "")
                body = clean_html(body_raw)

                art = make_article(
                    url, title, body, source_id, parse_rss_date(entry), default_lang
                )
                articles.append(art)

            time.sleep(REQUEST_DELAY)

        except Exception as e:
            print(f"  [{source_id} rss] {rss_url}: {e}", file=sys.stderr)

    # --- Stage 2: HTML fallback (title-only) ---
    if not articles:
        html_url = source.get("html_fallback_url")
        if html_url:
            r = safe_get(html_url)
            if r:
                soup = BeautifulSoup(r.text, "lxml")
                selectors: list[str] = source.get("html_article_selectors") or [
                    "article a", ".article-title a", "h2 a", "h3 a", ".news a",
                ]
                for selector in selectors:
                    for a_tag in soup.select(selector)[:50]:
                        href = a_tag.get("href", "")
                        if not href:
                            continue
                        full_url = urljoin(html_url, href)
                        if full_url in seen_urls:
                            continue
                        title = a_tag.get_text(strip=True)
                        if len(title) <= 10:
                            continue
                        seen_urls.add(full_url)
                        articles.append(
                            make_article(full_url, title, "", source_id, lang=default_lang)
                        )
                    if articles:
                        break

    status = "✓" if articles else "✗"
    print(
        f"  {status} [{source['tier']}] {source['name']}: {len(articles)} articles",
        file=sys.stderr,
    )
    return articles


# ---------------------------------------------------------------------------
# Pipeline entry point
# ---------------------------------------------------------------------------

def scrape_all() -> list[dict]:
    """
    Run all enabled sources from news_sources.json.
    Returns a deduplicated list of article dicts.
    """
    all_sources = load_sources()
    enabled = [s for s in all_sources if s.get("enabled", True)]
    disabled = len(all_sources) - len(enabled)

    print(
        f"[scraper] {len(enabled)} sources enabled, {disabled} disabled "
        f"(edit news_sources.json to change)",
        file=sys.stderr,
    )

    all_articles: list[dict] = []
    seen_urls: set[str] = set()

    for source in enabled:
        try:
            batch = scrape_source(source)
            # Fetch full article bodies for sources that allow it (default: yes).
            # Set fetch_body: false in news_sources.json for paywalled sources.
            if batch and source.get("fetch_body", True):
                batch = _enrich_bodies(batch)
            for art in batch:
                if art["url"] not in seen_urls:
                    seen_urls.add(art["url"])
                    all_articles.append(art)
        except Exception as e:
            print(f"  [scraper] {source['id']} failed: {e}", file=sys.stderr)

    # Summary by tier and language
    by_tier: dict[int, int] = {}
    by_lang: dict[str, int] = {}
    for art in all_articles:
        t = 0  # unknown tier without lookup; skip for simplicity
        lang = art.get("language", "?")
        by_lang[lang] = by_lang.get(lang, 0) + 1

    lang_str = ", ".join(f"{lang}:{cnt}" for lang, cnt in sorted(by_lang.items()))
    print(
        f"[scraper] Total: {len(all_articles)} unique articles ({lang_str})",
        file=sys.stderr,
    )
    return all_articles


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Run all configured news scrapers")
    ap.add_argument("--list-sources", action="store_true", help="Print source list and exit")
    args = ap.parse_args()

    if args.list_sources:
        sources = load_sources()
        print(f"{'ID':<25} {'Tier':<5} {'En?':<5} {'Name'}")
        print("-" * 70)
        for s in sources:
            print(
                f"{s['id']:<25} {s['tier']:<5} {'yes' if s.get('enabled', True) else 'no':<5} {s['name']}"
            )
        print(f"\nTotal: {len(sources)}  Enabled: {sum(1 for s in sources if s.get('enabled', True))}")
    else:
        import json as _json
        articles = scrape_all()
        print(_json.dumps(articles[:3], ensure_ascii=False, indent=2))
        print(f"\nTotal: {len(articles)}")
