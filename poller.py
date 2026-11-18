#!/usr/bin/env python3
"""BookMyShow theatre-by-theatre movie booking watcher.

Discovers Coimbatore cinema pages from BookMyShow's venue directory, resolves
venue codes from cinema pages, checks each cinema's requested-date page, and
sends one Telegram alert when the configured movie has at least one enabled Book button.

Important: BookMyShow markup and anti-bot responses can change. The script logs
what it discovers and deliberately does not initialize state when discovery
returns no venues, so a broken scrape cannot silently become a false baseline.
"""

import html
import json
import os
import re
import sys
import time
import urllib.parse
from html.parser import HTMLParser
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = Path(os.environ.get("CONFIG_PATH", ROOT / "config.json"))
STATE_PATH = Path(os.environ.get("STATE_PATH", ROOT / "state.json"))
BASE = "https://in.bookmyshow.com"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-IN,en-US;q=0.9,en;q=0.8",
}


class AnchorCollector(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.anchors = []
        self.current = None

    def handle_starttag(self, tag, attrs):
        if tag.lower() == "a":
            attrs = dict(attrs)
            self.current = {"href": attrs.get("href", ""), "text": []}

    def handle_data(self, data):
        if self.current is not None:
            self.current["text"].append(data)

    def handle_endtag(self, tag):
        if tag.lower() == "a" and self.current is not None:
            self.anchors.append({
                "href": html.unescape(self.current["href"]),
                "text": " ".join(" ".join(self.current["text"]).split()),
            })
            self.current = None


def load_json(path, default=None):
    if not path.exists():
        return default
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def save_json(path, data):
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def load_config():
    cfg = load_json(CONFIG_PATH, {}) or {}
    for env_key, cfg_key in {
        "TELEGRAM_BOT_TOKEN": "telegram_bot_token",
        "TELEGRAM_CHAT_ID": "telegram_chat_id",
        "MOVIE": "movie",
        "REQUESTED_DATE": "requested_date",
    }.items():
        if os.environ.get(env_key):
            cfg[cfg_key] = os.environ[env_key]

    required = ["movie", "requested_date", "telegram_bot_token", "telegram_chat_id"]
    missing = [key for key in required if not cfg.get(key)]
    if missing:
        raise ValueError("Missing config values: " + ", ".join(missing))
    if not re.fullmatch(r"20\d{6}", str(cfg["requested_date"])):
        raise ValueError("requested_date must be YYYYMMDD, e.g. 20261217")

    cfg.setdefault("city_slug", "coim")
    cfg.setdefault("venue_list_url", "https://in.bookmyshow.com/coimbatore/venue-list")
    cfg.setdefault("max_venues", 40)
    cfg.setdefault("request_delay_seconds", 1.0)
    cfg.setdefault("known_venues", [])
    return cfg


def fetch_url(url, cfg, session):
    headers = dict(HEADERS)
    headers.update(cfg.get("headers", {}))
    scraper_key = os.environ.get("SCRAPERAPI_KEY")
    if scraper_key:
        api_url = "https://api.scraperapi.com/?" + urllib.parse.urlencode({
            "api_key": scraper_key,
            "country_code": "in",
            "url": url,
        })
        response = session.get(api_url, headers=HEADERS, timeout=90)
    else:
        proxy = os.environ.get("PROXY_URL")
        proxies = {"http": proxy, "https": proxy} if proxy else None
        response = session.get(url, headers=headers, proxies=proxies, timeout=45)
    response.raise_for_status()
    return response.text


def telegram(cfg, message):
    url = f"https://api.telegram.org/bot{cfg['telegram_bot_token']}/sendMessage"
    response = requests.post(
        url,
        json={"chat_id": cfg["telegram_chat_id"], "text": message,
         "disable_web_page_preview": False},
        timeout=30,
    )
    response.raise_for_status()


def normalize_url(href):
    href = html.unescape(href or "").replace("\\u002F", "/").replace("\\/", "/")
    href = href.replace("&amp;", "&")
    if href.startswith("//"):
        return "https:" + href
    if href.startswith("/"):
        return urllib.parse.urljoin(BASE, href)
    if href.startswith("http"):
        return href
    return ""


def discover_venues(directory_html, cfg, session):
    """Find venue codes using directory anchors, embedded URLs and venue pages."""
    city_paths = {str(cfg["city_slug"]).lower(), "coim", "coimbatore"}
    city_pattern = "(?:" + "|".join(re.escape(x) for x in sorted(city_paths)) + ")"
    booking_re = re.compile(
        rf"/cinemas/({city_pattern})/([^/?#\"' ]+)/buytickets/([A-Za-z0-9_-]+)/\d{{8}}",
        re.IGNORECASE,
    )
    profile_re = re.compile(
        rf"/cinemas/({city_pattern})/([^/?#\"' ]+)(?:[/?#\"']|$)", re.IGNORECASE
    )

    found = {}
    parser = AnchorCollector()
    parser.feed(directory_html)
    candidates = []

    # A venue-list page may expose booking links directly, or profile-page links.
    for anchor in parser.anchors:
        url = normalize_url(anchor["href"])
        if not url or "bookmyshow.com" not in url:
            continue
        candidates.append((url, anchor["text"]))

    # Some BMS pages embed links in JSON/script text rather than visible anchors.
    raw = html.unescape(directory_html).replace("\\/", "/").replace("\\u002F", "/")
    for match in booking_re.finditer(raw):
        city_path, slug, code = match.groups()
        found[code.upper()] = {
            "label": slug.replace("-", " ").title(),
            "slug": slug,
            "code": code.upper(),
            "city_path": city_path,
        }

    for url, label in candidates:
        parsed = urllib.parse.urlparse(url)
        match = booking_re.search(parsed.path)
        if match:
            city_path, slug, code = match.groups()
            found[code.upper()] = {
                "label": label or slug.replace("-", " ").title(),
                "slug": slug,
                "code": code.upper(),
                "city_path": city_path,
            }
            continue
        profile_match = profile_re.search(parsed.path)
        if not profile_match:
            continue
        profile_city_path, slug = profile_match.groups()
        try:
            profile_html = fetch_url(url, cfg, session)
        except requests.RequestException as exc:
            print(f"[discover] could not inspect {slug}: {exc}", flush=True)
            continue
        profile_raw = html.unescape(profile_html).replace("\\/", "/").replace("\\u002F", "/")
        for code_match in booking_re.finditer(profile_raw):
            found_city_path, found_slug, code = code_match.groups()
            found[code.upper()] = {
                "label": label or found_slug.replace("-", " ").title(),
                "slug": found_slug,
                "code": code.upper(),
                "city_path": found_city_path,
            }
        # A profile may contain a venueCode property without a full booking URL.
        code_patterns = [
            rf"{re.escape(slug)}.{{0,500}}?venueCode[\"'\s:=]+([A-Za-z0-9_-]+)",
            rf"venueCode[\"'\s:=]+([A-Za-z0-9_-]+).{{0,500}}?{re.escape(slug)}",
        ]
        for code_pattern in code_patterns:
            code_match = re.search(code_pattern, profile_raw, re.IGNORECASE | re.DOTALL)
            if code_match:
                code = code_match.group(1).upper()
                found[code] = {
                    "label": label or slug.replace("-", " ").title(),
                    "slug": slug,
                    "code": code,
                    "city_path": profile_city_path,
                }
                break

    # Explicit seeds are useful if the directory markup omits a venue link.
    for venue in cfg.get("known_venues", []):
        if venue.get("slug") and venue.get("code"):
            code = str(venue["code"]).upper()
            found[code] = {
                "label": venue.get("label") or venue["slug"].replace("-", " ").title(),
                "slug": venue["slug"],
                "code": code,
                "city_path": venue.get("city_path", cfg["city_slug"]),
            }

    return dict(list(found.items())[: int(cfg["max_venues"])])



def is_expected_venue_page(page_html, venue):
    """Reject generic anti-bot/error pages before treating a request as checked."""
    text = html.unescape(re.sub(r"<[^>]+>", " ", page_html))
    text = re.sub(r"\s+", " ", text).lower()
    label = re.sub(r"\s+", " ", venue.get("label", "")).strip().lower()
    if label and label in text:
        return True
    # Some BMS pages vary punctuation/branding; accept a distinctive slug token.
    ignored = {"cinemas", "cinema", "coimbatore", "mall", "theatres", "theatre", "grandplex"}
    tokens = [t for t in re.split(r"[^a-z0-9]+", venue.get("slug", "").lower())
              if len(t) >= 4 and t not in ignored]
    return any(token in text for token in tokens)


class HtmlNode:
    """Small in-memory HTML tree used to inspect a movie card's showtime buttons."""
    def __init__(self, tag="document", attrs=None, parent=None):
        self.tag = tag
        self.attrs = dict(attrs or [])
        self.parent = parent
        self.children = []
        self.parts = []

    def text_content(self):
        bits = list(self.parts)
        for child in self.children:
            bits.append(child.text_content())
        return " ".join(x for x in bits if x)


class BookingMarkupParser(HTMLParser):
    VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = HtmlNode()
        self.stack = [self.root]

    def handle_starttag(self, tag, attrs):
        node = HtmlNode(tag, attrs, self.stack[-1])
        self.stack[-1].children.append(node)
        if tag.lower() not in self.VOID_TAGS:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        node = HtmlNode(tag, attrs, self.stack[-1])
        self.stack[-1].children.append(node)

    def handle_endtag(self, tag):
        tag = tag.lower()
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag.lower() == tag:
                del self.stack[index:]
                break

    def handle_data(self, data):
        if data and data.strip():
            self.stack[-1].parts.append(data.strip())


def _walk(node):
    yield node
    for child in node.children:
        yield from _walk(child)


def _norm_text(value):
    return re.sub(r"\s+", " ", html.unescape(value or "")).strip().casefold()


def movie_has_showtimes(page_html, cfg):
    """Return true only when the requested movie card has an enabled Book button.

    BookMyShow's page URL is already scoped to the requested date. Within that
    page, identify the movie title anchor and inspect its containing card for
    enabled role=button elements labelled 'Book <time>'. Disabled buttons
    (aria-disabled=true or tabindex=-1) are deliberately ignored.
    """
    parser = BookingMarkupParser()
    parser.feed(page_html)
    movie = _norm_text(cfg["movie"])
    title_anchors = []
    for node in _walk(parser.root):
        if node.tag.lower() == "a" and movie in _norm_text(node.text_content()):
            title_anchors.append(node)

    if not title_anchors:
        return False, []

    # Prefer the movie title link that belongs to a card containing showtime
    # buttons. This avoids matching the footer's generic movie listing.
    time_re = re.compile(r"^Book\s+(?:0?[1-9]|1[0-2]):[0-5]\d\s*(?:AM|PM)$", re.I)
    for anchor in title_anchors:
        ancestor = anchor.parent
        while ancestor is not None:
            buttons = []
            for child in _walk(ancestor):
                attrs = child.attrs
                label = attrs.get("aria-label", "")
                if child.tag.lower() not in {"div", "button"}:
                    continue
                if attrs.get("role", "").lower() != "button" and child.tag.lower() != "button":
                    continue
                if not time_re.match(label.strip()):
                    continue
                disabled = attrs.get("aria-disabled", "").lower() == "true"
                tabindex = attrs.get("tabindex")
                disabled = disabled or tabindex == "-1" or "disabled" in attrs
                if not disabled:
                    buttons.append(label.strip())
            if buttons:
                return True, buttons
            ancestor = ancestor.parent
    return False, []


def debug_page_evidence(page_html, cfg, venue):
    """Print limited page text around the movie title for false-positive diagnosis."""
    text = html.unescape(re.sub(r"<[^>]+>", " ", page_html))
    text = re.sub(r"\s+", " ", text)
    lower = text.lower()
    movie = re.sub(r"\s+", " ", cfg["movie"]).strip().lower()
    pos = lower.find(movie)
    print(f"[debug] {venue.get('label')} ({venue.get('code')}): page_chars={len(page_html)}, title_found={pos >= 0}", flush=True)
    if pos >= 0:
        start = max(0, pos - 250)
        end = min(len(text), pos + 1400)
        print(f"[debug] movie_context: {text[start:end]}", flush=True)
    # Show date-like markers present in the response, without dumping the full page.
    date_patterns = [r"17\s*(?:Dec|December)\s*2026", r"2026[-/]12[-/]17", r"17-12-2026", r"17/12/2026"]
    found = []
    for pattern in date_patterns:
        for match in re.finditer(pattern, text, flags=re.IGNORECASE):
            found.append(text[max(0, match.start()-100):min(len(text), match.end()+180)])
    print(f"[debug] requested_date_markers={len(found)}", flush=True)
    for snippet in found[:4]:
        print(f"[debug] date_context: {snippet}", flush=True)

    # Raw markup is needed to distinguish a real showtime/booking card from
    # generic movie text or a page-wide status legend. Keep output bounded.
    raw_lower = page_html.lower()
    raw_pos = raw_lower.find(movie)
    if raw_pos >= 0:
        raw_start = max(0, raw_pos - 1200)
        raw_end = min(len(page_html), raw_pos + 4200)
        raw_snippet = page_html[raw_start:raw_end]
        # Avoid multiline log spam while preserving the HTML attributes/tags.
        raw_snippet = re.sub(r"\s+", " ", raw_snippet)
        print(f"[debug] raw_movie_markup: {raw_snippet}", flush=True)

    # Show only booking-related links found near the movie title, if any.
    if raw_pos >= 0:
        nearby_raw = page_html[max(0, raw_pos - 1500):min(len(page_html), raw_pos + 6000)]
        links = re.findall(r"<a\b[^>]*href=[\"\']([^\"\']+)[\"\'][^>]*>(.*?)</a>", nearby_raw, flags=re.IGNORECASE | re.DOTALL)
        for href, label in links[:12]:
            clean_label = re.sub(r"<[^>]+>", " ", label)
            clean_label = re.sub(r"\s+", " ", html.unescape(clean_label)).strip()[:100]
            print(f"[debug] nearby_link: label={clean_label!r} href={html.unescape(href)[:300]}", flush=True)

def main():
    cfg = load_config()
    label = f"{cfg['movie']} @ {cfg['requested_date']}"
    session = requests.Session()

    try:
        directory_html = fetch_url(cfg["venue_list_url"], cfg, session)
    except requests.RequestException as exc:
        print(f"[{label}] venue directory fetch failed: {exc}", flush=True)
        return 1

    venues = discover_venues(directory_html, cfg, session)
    print(f"[{label}] discovered {len(venues)} cinema venue(s)", flush=True)
    if not venues:
        print("[discovery] no venue codes extracted; state was not changed.", flush=True)
        return 1

    state = load_json(STATE_PATH, {}) or {}
    old_venues = state.get("venues", {})
    open_now = {}
    checked = 0
    errors = 0
    date = str(cfg["requested_date"])

    for code, venue in venues.items():
        city_path = venue.get("city_path", cfg["city_slug"])
        url = f"https://in.bookmyshow.com/cinemas/{city_path}/{venue['slug']}/buytickets/{code}/{date}"
        try:
            page_html = fetch_url(url, cfg, session)
            if not is_expected_venue_page(page_html, venue):
                errors += 1
                print(f"[venue] unexpected response (possibly blocked/error page): {venue['label']} ({code})", flush=True)
                continue
            checked += 1
            is_open, active_times = movie_has_showtimes(page_html, cfg)
            # Temporary diagnostics for the two venues that produced false alerts.
            if code in {"KARP", "PFSC"}:
                debug_page_evidence(page_html, cfg, venue)
            if is_open:
                open_now[code] = {**venue, "url": url, "showtimes": active_times}
                print(f"[venue] BOOKABLE: {venue['label']} ({code}) — {', '.join(active_times)}", flush=True)
            else:
                print(f"[venue] not bookable yet: {venue['label']} ({code})", flush=True)
        except requests.RequestException as exc:
            errors += 1
            print(f"[venue] check failed for {venue['label']} ({code}): {exc}", flush=True)
        time.sleep(float(cfg["request_delay_seconds"]))

    if checked == 0:
        print(f"[{label}] no venue pages were successfully checked; state not changed.", flush=True)
        return 1

    # On a fresh install, remember currently bookable venues and user-confirmed
    # previously-open venues without sending an initial burst of alerts.
    if not state.get("initialized"):
        baseline = dict(open_now)
        for code in cfg.get("already_open_venue_codes", []):
            code = str(code).upper()
            if code in venues:
                baseline.setdefault(code, {
                    **venues[code],
                    "url": f"https://in.bookmyshow.com/cinemas/{venues[code].get('city_path', cfg['city_slug'])}/{venues[code]['slug']}/buytickets/{code}/{date}",
                    "baseline_source": "user-confirmed already open",
                })
        state = {"initialized": True, "venues": baseline, "checked_at": int(time.time())}
        save_json(STATE_PATH, state)
        print(f"[{label}] baseline initialized: {len(baseline)} already-bookable theatre(s); no initial alerts sent.", flush=True)
        return 0

    # Alert once per theatre. Keep old entries to prevent duplicate alerts if a
    # page temporarily fails or BookMyShow briefly omits a showtime.
    new_venues = [(code, venue) for code, venue in open_now.items() if code not in old_venues]
    pretty_date = f"{date[6:8]}-{date[4:6]}-{date[0:4]}"
    for code, venue in new_venues:
        message = (
            "🎬 Avengers: Doomsday booking is OPEN!\n\n"
            f"Theatre: {venue['label']}\n"
            f"Date: {pretty_date}\n"
            f"Showtimes: {', '.join(venue.get('showtimes', []))}\n\n"
            f"Book here: {venue['url']}"
        )
        telegram(cfg, message)
        print(f"[alert] Telegram sent for {venue['label']} ({code})", flush=True)

    old_venues.update(open_now)
    state.update({"initialized": True, "venues": old_venues, "checked_at": int(time.time())})
    save_json(STATE_PATH, state)
    print(f"[{label}] checked={checked}, bookable={len(open_now)}, new_alerts={len(new_venues)}, errors={errors}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
