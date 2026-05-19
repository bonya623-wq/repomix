"""
goha.ru news parser.

Strategy (in priority order):
  1. RSS/Atom feed  — fast, structured, preferred
  2. HTML scraping  — fallback if no feed found

The parser returns articles sorted oldest-first so the bot can publish
them in chronological order.
"""
import logging
import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Optional

import feedparser
import requests
from bs4 import BeautifulSoup

from .models import Article

logger = logging.getLogger(__name__)

BASE_URL = "https://goha.ru"

# Candidate RSS/Atom feed paths — tried in order
RSS_CANDIDATES = [
    "/rss",
    "/rss.xml",
    "/feed",
    "/feed/rss",
    "/feed.xml",
    "/atom.xml",
    "/news/rss",
]

# HTML fallback selectors (tune if site layout changes)
HTML_SELECTORS = {
    # news card container
    "card": [
        "article",
        ".news-item",
        ".article-item",
        ".post-item",
        "[class*='news']",
        "[class*='article']",
    ],
    "title": ["h1", "h2", "h3", ".title", ".headline"],
    "description": [".description", ".excerpt", ".summary", "p"],
    "image": ["img"],
    "link": ["a"],
    "date": ["time", ".date", ".published", "[datetime]"],
}


def _make_session(user_agent: str, timeout: int) -> requests.Session:
    s = requests.Session()
    s.headers.update(
        {
            "User-Agent": user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
            "Accept-Encoding": "gzip, deflate, br",
            "DNT": "1",
        }
    )
    s.timeout = timeout
    return s


def _parse_rss_date(date_str: Optional[str]) -> datetime:
    if not date_str:
        return datetime.now(timezone.utc)
    try:
        return parsedate_to_datetime(date_str).astimezone(timezone.utc)
    except Exception:
        return datetime.now(timezone.utc)


def _absolute_url(url: str) -> str:
    if url.startswith("http"):
        return url
    return BASE_URL + ("" if url.startswith("/") else "/") + url


def _find_rss_url(session: requests.Session) -> Optional[str]:
    """Try the candidate RSS paths; return the first one that returns valid XML."""
    for path in RSS_CANDIDATES:
        url = BASE_URL + path
        try:
            resp = session.get(url, timeout=session.timeout, allow_redirects=True)
            if resp.status_code == 200 and (
                "xml" in resp.headers.get("Content-Type", "")
                or resp.text.lstrip().startswith("<?xml")
                or "<rss" in resp.text[:500]
                or "<feed" in resp.text[:500]
            ):
                logger.info("RSS feed found at %s", url)
                return url
        except requests.RequestException:
            pass

    # Last resort: parse main page <link> tags
    try:
        resp = session.get(BASE_URL, timeout=session.timeout)
        if resp.status_code == 200:
            soup = BeautifulSoup(resp.text, "html.parser")
            for link in soup.find_all("link", type=re.compile(r"rss|atom")):
                href = link.get("href", "")
                if href:
                    return _absolute_url(href)
    except requests.RequestException:
        pass

    return None


def _articles_from_rss(session: requests.Session, rss_url: str) -> list[Article]:
    try:
        resp = session.get(rss_url, timeout=session.timeout)
        resp.raise_for_status()
    except requests.RequestException as exc:
        logger.error("RSS fetch error: %s", exc)
        return []

    feed = feedparser.parse(resp.text)
    articles: list[Article] = []

    for entry in feed.entries:
        url = entry.get("link", "")
        if not url:
            continue

        title = entry.get("title", "").strip()

        # Description: prefer summary over content
        description = entry.get("summary", "") or entry.get("content", [{}])[0].get("value", "")
        description = BeautifulSoup(description, "html.parser").get_text(" ", strip=True)
        description = description[:300] + ("…" if len(description) > 300 else "")

        # Image: check media_content, enclosures, or img in content
        image_url = ""
        if entry.get("media_content"):
            image_url = entry["media_content"][0].get("url", "")
        elif entry.get("enclosures"):
            for enc in entry["enclosures"]:
                if "image" in enc.get("type", ""):
                    image_url = enc.get("url", "")
                    break
        if not image_url:
            content_html = (entry.get("content") or [{}])[0].get("value", "") or entry.get("summary", "")
            img_match = re.search(r'<img[^>]+src=["\']([^"\']+)["\']', content_html)
            if img_match:
                image_url = img_match.group(1)

        published_at = _parse_rss_date(entry.get("published"))

        articles.append(
            Article(
                url=_absolute_url(url),
                title=title,
                description=description,
                image_url=image_url,
                published_at=published_at,
            )
        )

    # Oldest first
    articles.sort(key=lambda a: a.published_at)
    logger.info("Parsed %d articles from RSS", len(articles))
    return articles


def _articles_from_html(session: requests.Session) -> list[Article]:
    """Fallback HTML scraper for goha.ru/news."""
    news_url = f"{BASE_URL}/news"
    try:
        resp = session.get(news_url, timeout=session.timeout)
        resp.raise_for_status()
    except requests.RequestException as exc:
        logger.error("HTML fetch error: %s", exc)
        return []

    soup = BeautifulSoup(resp.text, "html.parser")
    articles: list[Article] = []

    # Try each card selector
    cards = []
    for sel in HTML_SELECTORS["card"]:
        cards = soup.select(sel)
        if cards:
            logger.debug("HTML: found %d cards with selector '%s'", len(cards), sel)
            break

    if not cards:
        logger.warning("HTML: no article cards found, trying generic <a> links")
        # Ultra-fallback: grab all links that look like article paths
        pattern = re.compile(r"/\d{4}/\d{2}|\/(articles|news|post)s?/")
        for a in soup.find_all("a", href=pattern):
            url = _absolute_url(a["href"])
            title = a.get_text(strip=True)
            if title:
                articles.append(
                    Article(url=url, title=title, description="", image_url="")
                )
        articles = articles[:20]
        return articles

    for card in cards[:20]:
        # URL + title
        link_el = card.find("a")
        if not link_el:
            continue
        url = _absolute_url(link_el.get("href", ""))
        if not url or url == BASE_URL:
            continue

        title = ""
        for sel in HTML_SELECTORS["title"]:
            el = card.select_one(sel)
            if el:
                title = el.get_text(strip=True)
                break
        if not title:
            title = link_el.get_text(strip=True)

        # Description
        description = ""
        for sel in HTML_SELECTORS["description"]:
            el = card.select_one(sel)
            if el and el.get_text(strip=True) != title:
                description = el.get_text(strip=True)[:300]
                break

        # Image
        image_url = ""
        img = card.find("img")
        if img:
            image_url = img.get("src") or img.get("data-src") or img.get("data-lazy-src", "")
            if image_url:
                image_url = _absolute_url(image_url)

        # Date
        published_at = datetime.now(timezone.utc)
        for sel in HTML_SELECTORS["date"]:
            el = card.select_one(sel)
            if el:
                dt_str = el.get("datetime") or el.get_text(strip=True)
                try:
                    published_at = parsedate_to_datetime(dt_str).astimezone(timezone.utc)
                except Exception:
                    pass
                break

        articles.append(
            Article(
                url=url,
                title=title,
                description=description,
                image_url=image_url,
                published_at=published_at,
            )
        )

    articles.sort(key=lambda a: a.published_at)
    logger.info("Parsed %d articles from HTML", len(articles))
    return articles


class GohаParser:
    """
    Stateful parser that remembers the RSS URL after it is found once.
    Thread-safe for single-threaded asyncio scheduler use.
    """

    def __init__(self, user_agent: str, timeout: int) -> None:
        self._session = _make_session(user_agent, timeout)
        self._rss_url: Optional[str] = None
        self._rss_checked = False

    def fetch_articles(self) -> list[Article]:
        # Try RSS on first call; skip discovery on subsequent calls if not found
        if not self._rss_checked:
            self._rss_url = _find_rss_url(self._session)
            self._rss_checked = True

        if self._rss_url:
            articles = _articles_from_rss(self._session, self._rss_url)
            if articles:
                return articles
            # RSS returned empty — fall through to HTML
            logger.warning("RSS returned no articles, falling back to HTML")

        return _articles_from_html(self._session)
