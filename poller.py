#!/usr/bin/env python3
"""
Ticket-booking watcher.

Polls a URL (a BookMyShow / District showtimes page, or an internal API
request you grabbed from your browser's DevTools) and sends a Telegram
message the moment a given theatre appears with booking open.

State is tracked in state.json so you get alerted on the *transition*
to "available" instead of on every run.

Everything is driven by config.json (and/or environment variables), so
nothing site-specific is hardcoded -- if BookMyShow/District change their
markup you only edit config, not code.
"""

import json
import html
import os
import re
import sys
import time
import urllib.parse
from collections import Counter
from html.parser import HTMLParser
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = Path(os.environ.get("CONFIG_PATH", ROOT / "config.json"))
STATE_PATH = Path(os.environ.get("STATE_PATH", ROOT / "state.json"))

# Look like a real Chrome on Windows -- BMS rejects obvious bots.
DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,*/*;q=0.8"
    ),
    "Accept-Language": "en-IN,en-US;q=0.9,en;q=0.8",
    "Upgrade-Insecure-Requests": "1",
    "sec-ch-ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "same-origin",
    "Sec-Fetch-User": "?1",
}


def load_json(path, default=None):
    if not path.exists():
        return default
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)


def load_config():
    cfg = load_json(CONFIG_PATH, default={}) or {}

    # Environment variables override the file (used by GitHub Actions secrets).
    env_map = {
        "TARGET_URL": "target_url",
        "THEATRE": "theatre",
        "MOVIE": "movie",
        "REQUESTED_DATE": "requested_date",
        "TELEGRAM_BOT_TOKEN": "telegram_bot_token",
        "TELEGRAM_CHAT_ID": "telegram_chat_id",
    }
    for env_key, cfg_key in env_map.items():
        if os.environ.get(env_key):
            cfg[cfg_key] = os.environ[env_key]

    if os.environ.get("HEADERS_JSON"):
        cfg["headers"] = json.loads(os.environ["HEADERS_JSON"])

    # The BMS date is embedded in the URL, so build the URL from the template
    # and the (possibly overridden) requested_date. Set REQUESTED_DATE=20260717
    # to point everything at the 17th for a live end-to-end test.
    if cfg.get("url_template") and cfg.get("requested_date"):
        cfg["target_url"] = cfg["url_template"].format(date=cfg["requested_date"])

    required = ["target_url", "telegram_bot_token", "telegram_chat_id"]
    detector = cfg.get("detector")
    if detector in ("bms_date", "venue_date", "bms_theatres"):
        required.append("requested_date")
    elif detector != "venue_date":
        required.append("theatre")
    if detector == "venue_date" and not (cfg.get("venue_code") or cfg.get("venue_codes")):
        sys.exit("venue_date detector needs 'venue_code' or 'venue_codes'")
    missing = [k for k in required if not cfg.get(k)]
    if missing:
        sys.exit(f"Missing required config: {', '.join(missing)}")
    return cfg


def send_telegram(token, chat_id, text):
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    resp = requests.post(
        url,
        json={"chat_id": chat_id, "text": text, "disable_web_page_preview": False},
        timeout=30,
    )
    resp.raise_for_status()


def fetch(cfg):
    """
    Fetch the target URL from an India egress when configured.

    BookMyShow blocks non-India / datacenter IPs (e.g. GitHub's US runners),
    so a plain request from CI gets a 403. Two ways to route through India:

    * SCRAPERAPI_KEY  -- routes via ScraperAPI with country_code=in and solves
                         anti-bot. Easiest for CI. Set it as a repo secret.
    * PROXY_URL       -- a standard http(s) proxy string, e.g.
                         "http://user:pass@in-proxy-host:port".

    With neither set, it makes a direct request with browser headers plus a
    cookie warm-up -- enough only when running from an India IP.
    """
    headers = dict(DEFAULT_HEADERS)
    headers.update(cfg.get("headers", {}))

    scraper_key = os.environ.get("SCRAPERAPI_KEY")
    if scraper_key:
        api_url = "https://api.scraperapi.com/?" + urllib.parse.urlencode(
            {"api_key": scraper_key, "country_code": "in", "url": cfg["target_url"]}
        )
        resp = requests.get(api_url, timeout=90)
        resp.raise_for_status()
        return resp.text

    proxy = os.environ.get("PROXY_URL")
    proxies = {"http": proxy, "https": proxy} if proxy else None

    session = requests.Session()
    session.headers.update(headers)

    # Warm-up: hit the homepage first to pick up cookies (helps soft bot checks).
    try:
        session.get("https://in.bookmyshow.com/", timeout=30, proxies=proxies)
    except requests.RequestException:
        pass

    resp = session.get(
        cfg["target_url"],
        timeout=30,
        proxies=proxies,
        headers={"Referer": "https://in.bookmyshow.com/explore/movies-chennai"},
    )
    resp.raise_for_status()
    return resp.text


def is_available_bms_date(page_text, cfg):
    """
    BookMyShow-specific detector for "a given date has opened for booking".

    BMS only renders showtimes for the date currently being displayed, and it
    silently falls back to the nearest available date when you request a date
    that hasn't opened yet. So the requested date (e.g. 20260720) sits at a
    low ~3 count (just the date-strip navigation) until it opens, at which
    point its showtimes render and it becomes the *dominant* date token.

    Rule: open when the requested date is the most-referenced date token on
    the page and it clears a small floor (well above strip-only noise).
    """
    requested = cfg["requested_date"]  # e.g. "20260720"
    floor = cfg.get("min_references", 10)

    tokens = re.findall(r"20\d{6}", page_text)
    if not tokens:
        return False

    counts = Counter(tokens)
    top_date, _ = counts.most_common(1)[0]
    requested_count = counts.get(requested, 0)

    return top_date == requested and requested_count >= floor


def is_available_venue_date(page_text, cfg):
    """
    Theatre-specific detector: is a given venue bookable on a given date?

    BMS renders a per-venue booking link like
        /cinemas/chennai/<slug>/buytickets/<venueCode>/<date>
    only when that venue has live shows for that exact date. Because the date
    is baked into the link, it can't be confused with the silent fallback
    (a fallback page carries /<code>/<fallbackDate>, not /<code>/<ourDate>).

    Set venue_code (one) or venue_codes (list). With a list, it's open when
    ANY of them is bookable for the date.
    """
    date = cfg["requested_date"]
    codes = cfg.get("venue_codes") or [cfg["venue_code"]]
    return any("/{}/{}".format(code, date) in page_text for code in codes)



class AnchorCollector(HTMLParser):
    """Collect anchor hrefs and their visible text from the BookMyShow page."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.anchors = []
        self._current = None

    def handle_starttag(self, tag, attrs):
        if tag.lower() == "a":
            attrs = dict(attrs)
            self._current = {"href": attrs.get("href", ""), "text": []}

    def handle_data(self, data):
        if self._current is not None:
            self._current["text"].append(data)

    def handle_endtag(self, tag):
        if tag.lower() == "a" and self._current is not None:
            self.anchors.append({
                "href": html.unescape(self._current["href"]),
                "text": " ".join(" ".join(self._current["text"]).split())
            })
            self._current = None


def find_open_venues(page_text, cfg):
    """
    Find theatre booking links for the exact requested date.

    Expected link shape:
      /cinemas/<city>/<theatre-slug>/buytickets/<venue-code>/<YYYYMMDD>
    """
    requested_date = cfg["requested_date"]
    expected_city = cfg.get("city_slug", "coimbatore").strip("/").lower()

    parser = AnchorCollector()
    parser.feed(page_text)

    pattern = re.compile(
        r"/cinemas/([^/?#]+)/([^/?#]+)/buytickets/([^/?#]+)/(\d{8})",
        re.IGNORECASE
    )
    venues = {}

    for anchor in parser.anchors:
        match = pattern.search(anchor["href"])
        if not match:
            continue

        city_slug, theatre_slug, venue_code, link_date = match.groups()
        if city_slug.lower() != expected_city or link_date != requested_date:
            continue

        label = anchor["text"] or theatre_slug.replace("-", " ").title()
        booking_url = urllib.parse.urljoin(
            "https://in.bookmyshow.com", anchor["href"]
        )
        venues[venue_code.upper()] = {
            "label": label,
            "url": booking_url,
            "slug": theatre_slug
        }

    return venues


def is_available(page_text, cfg):
    detector = cfg.get("detector")
    if detector == "venue_date":
        return is_available_venue_date(page_text, cfg)
    if detector == "bms_date":
        return is_available_bms_date(page_text, cfg)
    if detector == "bms_theatres":
        return find_open_venues(page_text, cfg)
    return is_available_generic(page_text, cfg)


def is_available_generic(page_text, cfg):
    """
    Booking is considered OPEN for the target theatre when the theatre name
    is present AND at least one 'booking is live' signal is present.

    Matching is case-insensitive and ignores extra whitespace so small
    formatting differences don't cause misses.
    """
    haystack = re.sub(r"\s+", " ", page_text).lower()

    theatre = re.sub(r"\s+", " ", cfg["theatre"]).lower().strip()
    if theatre not in haystack:
        return False

    # If the movie name is configured, require it too (avoids false hits when
    # the theatre is listed for other movies).
    movie = cfg.get("movie")
    if movie:
        if re.sub(r"\s+", " ", movie).lower().strip() not in haystack:
            return False

    # Signals that booking is actually live rather than "coming soon".
    open_signals = cfg.get(
        "open_signals",
        ["book tickets", "book now", '"showtimes"', "showtime", "select seats"],
    )
    # Signals that it's NOT yet open -- if present near-exclusively, treat as closed.
    closed_signals = cfg.get("closed_signals", ["notify me", "coming soon"])

    has_open = any(s.lower() in haystack for s in open_signals)
    only_closed = any(s.lower() in haystack for s in closed_signals) and not has_open

    return has_open and not only_closed


def main():
    cfg = load_config()
    detector = cfg.get("detector")
    state = load_json(STATE_PATH, default={}) or {}

    target_desc = cfg.get("theatre") or cfg.get("requested_date", "target")
    label = f"{cfg.get('movie', 'movie')} @ {target_desc}"

    try:
        page = fetch(cfg)
    except requests.RequestException as exc:
        print(f"[{label}] fetch failed: {exc}")
        return 0

    if detector == "bms_theatres":
        current_venues = find_open_venues(page, cfg)
        known_venues = state.get("venues", {})

        # First successful run establishes a baseline, avoiding alerts for
        # theatres that were already open when monitoring began.
        if not state.get("initialized"):
            if not current_venues:
                print(
                    f"[{label}] no matching theatre booking links found; "
                    "baseline was NOT initialized. Check the fetched page/URL "
                    "before relying on notifications."
                )
                return 0

            state = {
                "initialized": True,
                "venues": current_venues,
                "checked_at": int(time.time())
            }
            save_json(STATE_PATH, state)
            print(
                f"[{label}] baseline initialized with "
                f"{len(current_venues)} currently bookable theatre(s): "
                f"{', '.join(v['label'] for v in current_venues.values())}"
            )
            return 0

        new_venues = [
            (code, venue)
            for code, venue in current_venues.items()
            if code not in known_venues
        ]

        rd = cfg["requested_date"]
        pretty = f"{rd[6:8]}-{rd[4:6]}-{rd[0:4]}"

        for code, venue in new_venues:
            msg = (
                "🎬 Booking just OPENED!\n\n"
                f"{cfg.get('movie', 'Movie')}\n"
                f"Theatre: {venue['label']}\n"
                f"Date: {pretty}\n\n"
                f"Book here: {venue['url']}"
            )
            send_telegram(
                cfg["telegram_bot_token"],
                cfg["telegram_chat_id"],
                msg
            )
            print(f"[{label}] new theatre notification sent: {venue['label']} ({code})")

        # Retain all previously seen venues to avoid duplicate alerts if a
        # theatre link temporarily disappears from a later page response.
        known_venues.update(current_venues)
        state["initialized"] = True
        state["venues"] = known_venues
        state["checked_at"] = int(time.time())
        save_json(STATE_PATH, state)

        print(
            f"[{label}] theatres currently detected={len(current_venues)}, "
            f"new theatres={len(new_venues)}"
        )
        return 0

    # Original single-availability behaviour for other detector modes.
    if not state:
        state = {"available": False}

    available = is_available(page, cfg)
    print(f"[{label}] available={available} (was {state.get('available')})")

    if available and not state.get("available"):
        if detector in ("bms_date", "venue_date"):
            rd = cfg["requested_date"]
            pretty = f"{rd[6:8]}-{rd[4:6]}-{rd[0:4]}"
            venue = cfg.get("venue_label") or cfg.get("venue_code") or ""
            venue_line = f"Theatre: {venue}\n" if venue else ""
            msg = (
                f"🎬 Booking just OPENED!\n\n"
                f"{cfg.get('movie', 'Movie')}\n"
                f"{venue_line}"
                f"Date: {pretty}\n\n"
                f"Book here: {cfg['target_url']}"
            )
        else:
            msg = (
                f"🎬 Booking is OPEN!\n\n"
                f"{cfg.get('movie', 'Movie')}\n"
                f"Theatre: {cfg['theatre']}\n\n"
                f"Book here: {cfg['target_url']}"
            )
        send_telegram(cfg["telegram_bot_token"], cfg["telegram_chat_id"], msg)
        print(f"[{label}] notification sent")

    if available != state.get("available"):
        state["available"] = available
        state["checked_at"] = int(time.time())
        save_json(STATE_PATH, state)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
