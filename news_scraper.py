#!/usr/bin/env python3
"""
News scrapers for: QNA, Al Jazeera (EN+AR), Qatar TV, Al Watan.
Strategy: RSS first, HTML fallback. All outputs are dicts with standard fields.
"""

import re
import sys
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Optional

import feedparser
import requests
from bs4 import BeautifulSoup

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ar,en;q=0.9",
}
TIMEOUT = 20
REQUEST_DELAY = 1.5  # seconds between requests, be respectful

# ---------------------------------------------------------------------------
# Category classifier (keyword-based)
# ---------------------------------------------------------------------------

FINANCE_KEYWORDS = {
    "en": ["stock", "share", "market", "bank", "profit", "revenue", "earnings",
           "gdp", "oil", "gas", "investment", "finance", "economy", "economic",
           "trading", "dividend", "ipo", "quarter", "fiscal", "budget", "inflation",
           "interest rate", "monetary", "fiscal", "qse", "exchange"],
    "ar": ["سهم", "أسهم", "بورصة", "بنك", "ربح", "إيرادات", "اقتصاد", "نفط",
           "غاز", "استثمار", "مالية", "ميزانية", "تداول", "توزيعات", "أرباح",
           "تضخم", "فائدة", "سوق"],
}
REGIONAL_KEYWORDS = {
    "en": ["gulf", "gcc", "saudi", "uae", "kuwait", "bahrain", "oman", "egypt",
           "jordan", "iraq", "iran", "turkey", "syria", "lebanon"],
    "ar": ["خليج", "مجلس التعاون", "السعودية", "الإمارات", "الكويت", "البحرين",
           "عمان", "مصر", "الأردن", "العراق", "إيران", "تركيا"],
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

# Common aliases for QSE-listed companies (English + Arabic)
QSE_ALIASES: dict[str, list[str]] = {
    "QNBK": ["Qatar National Bank", "QNB", "بنك قطر الوطني"],
    "ORDS":  ["Ooredoo", "أوريدو", "Qtel"],
    "QIBK":  ["Qatar Islamic Bank", "QIB", "بنك قطر الإسلامي"],
    "MARK":  ["Masraf Al Rayan", "مصرف الريان", "Al Rayan"],
    "CBQK":  ["Commercial Bank", "البنك التجاري", "CBQ"],
    "BRES":  ["Barwa Real Estate", "بروة العقارية", "Barwa"],
    "IQCD":  ["Industries Qatar", "إندستريز قطر"],
    "QEWS":  ["Qatar Electricity", "كهرباء قطر ومياهها"],
    "GISS":  ["Gulf International Services", "الخليج الدولية للخدمات", "GIS"],
    "QGTS":  ["Nakilat", "ناقلات", "Qatar Gas Transport"],
    "VFQS":  ["Vodafone Qatar", "فودافون قطر"],
    "DHBK":  ["Doha Bank", "بنك الدوحة"],
    "ABQK":  ["Ahli Bank", "المصرف الأهلي"],
    "QIIK":  ["Qatar International Islamic Bank", "بنك قطر الدولي الإسلامي", "QIIB"],
    "MCCS":  ["Mannai Corporation", "شركة مناعي"],
    "UDCD":  ["United Development Company", "الشركة المتحدة للتطوير", "UDC"],
    "MPHC":  ["Mesaieed Petrochemical", "مسيعيد للبتروكيماويات", "QAFCO"],
    "QFBQ":  ["Qatar First Bank", "بنك قطر الأول"],
    "KCBK":  ["Al Khalij Commercial Bank", "بنك الخليج التجاري"],
    "IHGS":  ["Islamic Holding Group", "المجموعة الإسلامية القابضة"],
    "BLDN":  ["Baladna", "بلدنا"],
    "QLMI":  ["QL Investors", "مستثمرو QL"],
    "MERS":  ["Al Meera", "الميرة"],
    "GECO":  ["Gulf Warehousing", "خليجية"],
    "QISI":  ["Qatar Insurance", "قطر للتأمين"],
}


def extract_entities(text: str) -> list[str]:
    found = []
    t_lower = text.lower()
    for ticker, aliases in QSE_ALIASES.items():
        if ticker.lower() in t_lower:
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
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script", "style", "nav", "footer", "header", "aside", "form"]):
        tag.decompose()
    return re.sub(r"\s+", " ", soup.get_text(separator=" ")).strip()


def make_article(url, title, body, source, published_at=None, lang=None) -> dict:
    combined = f"{title} {body}"
    detected_lang = lang or detect_language(combined)
    return {
        "url": url,
        "title": title.strip(),
        "body": body[:4000].strip(),  # cap body to keep DB lean
        "source": source,
        "published_at": published_at,
        "language": detected_lang,
        "category": classify_category(combined, detected_lang),
        "entities": extract_entities(combined),
    }


def safe_get(url: str, **kwargs) -> Optional[requests.Response]:
    try:
        r = requests.get(url, headers=HEADERS, timeout=TIMEOUT, **kwargs)
        r.raise_for_status()
        return r
    except Exception as e:
        print(f"  [warn] GET {url}: {e}", file=sys.stderr)
        return None


# ---------------------------------------------------------------------------
# QNA (Qatar News Agency)
# ---------------------------------------------------------------------------

QNA_RSS_FEEDS = [
    ("https://www.qna.org.qa/en/rss/latestNews", "en"),
    ("https://www.qna.org.qa/en/rss/economy", "en"),
    ("https://www.qna.org.qa/ar/rss/latestNews", "ar"),
    ("https://www.qna.org.qa/ar/rss/economy", "ar"),
]
QNA_HTML_URL = "https://www.qna.org.qa/en/News-Area/News"


def scrape_qna() -> list[dict]:
    articles = []
    seen_urls: set[str] = set()

    for feed_url, lang in QNA_RSS_FEEDS:
        try:
            feed = feedparser.parse(feed_url)
            for entry in feed.entries[:30]:
                url = entry.get("link", "")
                if not url or url in seen_urls:
                    continue
                seen_urls.add(url)
                title = entry.get("title", "")
                body = clean_html(entry.get("summary", "") or entry.get("content", [{}])[0].get("value", ""))
                art = make_article(url, title, body, "qna", parse_rss_date(entry), lang)
                articles.append(art)
            time.sleep(REQUEST_DELAY)
        except Exception as e:
            print(f"  [qna rss] {feed_url}: {e}", file=sys.stderr)

    # HTML fallback if RSS yielded nothing
    if not articles:
        r = safe_get(QNA_HTML_URL)
        if r:
            soup = BeautifulSoup(r.text, "lxml")
            for a_tag in soup.select("a[href]")[:40]:
                href = a_tag.get("href", "")
                if "/News-Area/News/" in href:
                    full_url = href if href.startswith("http") else f"https://www.qna.org.qa{href}"
                    if full_url in seen_urls:
                        continue
                    seen_urls.add(full_url)
                    title = a_tag.get_text(strip=True)
                    if len(title) > 10:
                        articles.append(make_article(full_url, title, "", "qna"))

    print(f"  QNA: {len(articles)} articles", file=sys.stderr)
    return articles


# ---------------------------------------------------------------------------
# Al Jazeera (English + Arabic)
# ---------------------------------------------------------------------------

AJ_RSS_FEEDS = [
    ("https://www.aljazeera.com/xml/rss/all.xml", "en"),
    ("https://arabic.aljazeera.net/xml/rss/all.xml", "ar"),
]
AJ_FINANCE_FEEDS = [
    ("https://www.aljazeera.com/xml/rss/economy.xml", "en"),
    ("https://arabic.aljazeera.net/xml/rss/economy.xml", "ar"),
]


def scrape_aljazeera() -> list[dict]:
    articles = []
    seen_urls: set[str] = set()

    for feed_url, lang in AJ_RSS_FEEDS + AJ_FINANCE_FEEDS:
        try:
            feed = feedparser.parse(feed_url)
            for entry in feed.entries[:40]:
                url = entry.get("link", "")
                if not url or url in seen_urls:
                    continue
                seen_urls.add(url)
                title = entry.get("title", "")
                body = clean_html(
                    entry.get("summary", "")
                    or (entry.get("content") or [{}])[0].get("value", "")
                )
                art = make_article(url, title, body, "aljazeera", parse_rss_date(entry), lang)
                articles.append(art)
            time.sleep(REQUEST_DELAY)
        except Exception as e:
            print(f"  [aj rss] {feed_url}: {e}", file=sys.stderr)

    print(f"  Al Jazeera: {len(articles)} articles", file=sys.stderr)
    return articles


# ---------------------------------------------------------------------------
# Qatar TV (QTV)
# ---------------------------------------------------------------------------

QTV_RSS_URL = "https://www.qtv.com.qa/feeds/latest"
QTV_HTML_URLS = [
    "https://www.qtv.com.qa/news",
    "https://www.qtv.com.qa/en/news",
]


def scrape_qtv() -> list[dict]:
    articles = []
    seen_urls: set[str] = set()

    # Try RSS
    try:
        feed = feedparser.parse(QTV_RSS_URL)
        if feed.entries:
            for entry in feed.entries[:30]:
                url = entry.get("link", "")
                if not url or url in seen_urls:
                    continue
                seen_urls.add(url)
                title = entry.get("title", "")
                body = clean_html(entry.get("summary", ""))
                art = make_article(url, title, body, "qtv", parse_rss_date(entry))
                articles.append(art)
            time.sleep(REQUEST_DELAY)
    except Exception as e:
        print(f"  [qtv rss]: {e}", file=sys.stderr)

    # HTML fallback
    if not articles:
        for base_url in QTV_HTML_URLS:
            r = safe_get(base_url)
            if not r:
                continue
            soup = BeautifulSoup(r.text, "lxml")
            for a_tag in soup.select("article a, .news-item a, .card a, h2 a, h3 a"):
                href = a_tag.get("href", "")
                if not href or href in seen_urls:
                    continue
                full_url = href if href.startswith("http") else f"https://www.qtv.com.qa{href}"
                if full_url in seen_urls:
                    continue
                seen_urls.add(full_url)
                title = a_tag.get_text(strip=True)
                if len(title) > 10:
                    articles.append(make_article(full_url, title, "", "qtv"))
            if articles:
                break
            time.sleep(REQUEST_DELAY)

    print(f"  Qatar TV: {len(articles)} articles", file=sys.stderr)
    return articles


# ---------------------------------------------------------------------------
# Al Watan Qatar (Arabic newspaper)
# ---------------------------------------------------------------------------

ALWATAN_RSS_URLS = [
    "https://www.alwatan.com.qa/rss",
    "https://www.alwatan.com.qa/rss.xml",
    "https://www.alwatan.com.qa/feed",
]
ALWATAN_HTML_URL = "https://www.alwatan.com.qa"


def scrape_alwatan() -> list[dict]:
    articles = []
    seen_urls: set[str] = set()

    # Try RSS variants
    for rss_url in ALWATAN_RSS_URLS:
        try:
            feed = feedparser.parse(rss_url)
            if feed.entries:
                for entry in feed.entries[:30]:
                    url = entry.get("link", "")
                    if not url or url in seen_urls:
                        continue
                    seen_urls.add(url)
                    title = entry.get("title", "")
                    body = clean_html(entry.get("summary", ""))
                    art = make_article(url, title, body, "alwatan", parse_rss_date(entry), "ar")
                    articles.append(art)
                time.sleep(REQUEST_DELAY)
                break
        except Exception as e:
            print(f"  [alwatan rss] {rss_url}: {e}", file=sys.stderr)

    # HTML fallback
    if not articles:
        r = safe_get(ALWATAN_HTML_URL)
        if r:
            soup = BeautifulSoup(r.text, "lxml")
            selectors = [
                "article a", ".news a", ".article-title a",
                "h2 a", "h3 a", ".post-title a",
            ]
            for sel in selectors:
                for a_tag in soup.select(sel)[:40]:
                    href = a_tag.get("href", "")
                    if not href:
                        continue
                    full_url = href if href.startswith("http") else f"https://www.alwatan.com.qa{href}"
                    if full_url in seen_urls or "alwatan.com.qa" not in full_url:
                        continue
                    seen_urls.add(full_url)
                    title = a_tag.get_text(strip=True)
                    if len(title) > 10:
                        art = make_article(full_url, title, "", "alwatan", lang="ar")
                        articles.append(art)
                if articles:
                    break

    print(f"  Al Watan: {len(articles)} articles", file=sys.stderr)
    return articles


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def scrape_all() -> list[dict]:
    """Run all scrapers and return combined deduplicated list."""
    print("[scraper] Starting all news sources...", file=sys.stderr)
    all_articles: list[dict] = []
    seen_urls: set[str] = set()

    for fn, name in [
        (scrape_qna, "QNA"),
        (scrape_aljazeera, "Al Jazeera"),
        (scrape_qtv, "Qatar TV"),
        (scrape_alwatan, "Al Watan"),
    ]:
        try:
            batch = fn()
            for art in batch:
                if art["url"] not in seen_urls:
                    seen_urls.add(art["url"])
                    all_articles.append(art)
        except Exception as e:
            print(f"  [scraper] {name} failed: {e}", file=sys.stderr)

    print(f"[scraper] Total: {len(all_articles)} unique articles", file=sys.stderr)
    return all_articles


if __name__ == "__main__":
    import json
    articles = scrape_all()
    print(json.dumps(articles[:3], ensure_ascii=False, indent=2))
    print(f"\nTotal: {len(articles)}")
