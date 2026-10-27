#!/usr/bin/env python3
"""
Film Price Checker — compare DVD / Blu-ray / 4K UHD and CD / vinyl / cassette prices across
CeX, musicMagpie, eBay, HMV and Amazon (UK), from one retro-terminal page.

Copyright (c) 2026 MAJ. Licensed under PolyForm Noncommercial 1.0.0 (see LICENSE):
free for personal / non-commercial use; commercial use needs a separate licence.

EASIEST (Windows): double-click install.bat in this folder. It sets everything up
and puts a "Film Price Checker" icon on your desktop. See README.md.

MANUAL:
    python filmprice.py --install      # install the extra packages into this Python
    python filmprice.py                # start it (opens in your browser)
    python filmprice.py --shortcut     # desktop icon (Windows)
    python filmprice.py --startup      # start quietly with Windows, for watchlist alerts (undo: --no-startup)

TROUBLESHOOTING:
    python filmprice.py --selftest     # checks the film database and every shop, saves selftest.txt
    python filmprice.py --headed       # shows the helper browser (to tick a "verify you're human" box)
    python filmprice.py --debug        # saves each shop's raw page to the debug folder

Files it creates next to itself (all private to you, none are uploaded anywhere):
watchlist.json, filmprice.log, filmprice.ico, selftest.txt, browser_profile_* (the helper
browser's own cookies for the shops — never your normal browser's).

Optional: free eBay developer keys make eBay faster and more reliable:
    set EBAY_CLIENT_ID=...  and  set EBAY_CLIENT_SECRET=...   (developer.ebay.com)
"""

import argparse
import base64
import json
import os
import re
import sys
import threading
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from bs4 import BeautifulSoup

CURL_ERR = BROWSER_ERR = None
try:
    from curl_cffi import requests as http  # browser-impersonating client
    IMPERSONATE = {"impersonate": "chrome"}
except Exception as e:  # fall back to plain requests
    CURL_ERR = f"{type(e).__name__}: {e}"
    import requests as http
    IMPERSONATE = {}

VERSION = "3.4.0"
PORT = 8765
TIMEOUT = 20
DEBUG = False
APP_DIR = (os.path.dirname(sys.executable) if getattr(sys, "frozen", False)
           else os.path.dirname(os.path.abspath(__file__)))
DEBUG_DIR = os.path.join(APP_DIR, "debug")

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-GB,en;q=0.9",
}

# Human-facing search pages (also used as "open on site" links).
# {q} is replaced with the URL-encoded query.
SEARCH_URLS = {
    "cex":         "https://uk.webuy.com/search?stext={q}",
    "musicmagpie": "https://www.musicmagpie.co.uk/store/search/?q={q}",
    "ebay":        "https://www.ebay.co.uk/sch/i.html?_nkw={q}&_sacat=11232&LH_PrefLoc=1",
    "hmv":         "https://hmv.com/search?searchtext={q}",
    "amazon":      "https://www.amazon.co.uk/s?k={q}&i=dvd",
    "zavvi":       "https://www.zavvi.com/elysium.search?search={q}",
}
# Official eBay shops used as a fallback when the retailer's own site fails.
EBAY_SHOPS = {"musicmagpie": "musicmagpie", "hmv": "hmv_official_store", "zavvi": "zavvi"}

RETAILERS = [
    ("cex", "CeX"),
    ("musicmagpie", "musicMagpie"),
    ("ebay", "eBay"),
    ("hmv", "HMV"),
    ("amazon", "Amazon"),
    # ("zavvi", "Zavvi"),  # Zavvi entered administration and ceased trading on 12 Oct 2026
]

# --------------------------------------------------------------------------- helpers

PRICE_RE = re.compile(r"£\s?(\d{1,4}(?:,\d{3})*(?:\.\d{1,2})?)")


def parse_price(text):
    if text is None:
        return None
    if isinstance(text, (int, float)):
        return float(text)
    m = PRICE_RE.search(str(text))
    if m:
        return float(m.group(1).replace(",", ""))
    m = re.search(r"(\d+(?:\.\d{1,2})?)", str(text))
    return float(m.group(1)) if m else None


def detect_format(*texts):
    t = " ".join(x for x in texts if x).lower()
    if re.search(r"\b4k\b|uhd|ultra[\s-]?hd", t):
        return "4K"
    if re.search(r"blu[\s-]?ray|bluray", t):
        return "Blu-ray"
    if re.search(r"\bdvd\b", t):
        return "DVD"
    # Cassette: "Cassette", "Cass", "Audio Tape"
    if re.search(r"\bcassettes?\b|\bcass\b|audio tape|music tape", t):
        return "Cassette"
    # Vinyl: "Vinyl", "LP", "2LP", "2 x LP", '12"', "7 inch", "picture disc"
    if re.search(r"\bvinyl\b|\b(?:\d+\s?x?\s?)?lps?\b|(?<![\d.])(?:7|10|12)\s?(?:\"|''|-?inch\b|in\b)|picture disc", t):
        return "Vinyl"
    # CD: "CD", "Audio CD", "2CD", "2 x CD", "[CD]" - but not "CD-ROM"
    if re.search(r"(?:^|[^a-z0-9])(?:\d+\s?x?\s?)?cds?(?![a-z])(?!-?rom)|audio cd|compact disc", t):
        return "CD"
    return "Other"


EDITION_RULES = [
    ("Steelbook", r"steel\s?book"),
    ("Limited", r"limited|collector'?s|special edition|ultimate edition|deluxe|anniversary edition|"
                r"colou?red vinyl|picture disc|splatter|numbered"),
    ("Box set", r"box\s?set|boxset|collection|trilogy|quadrilogy|anthology|saga|\b\d+[- ]?(?:film|movie)s?\b|complete series|seasons?\s*\d+\s*(?:-|to)\s*\d+"),
]
REGION_RULES = [
    ("Region free", r"region[\s-]*(?:free|all|0)\b|all regions|region a\s*,?\s*b\s*,?\s*c|region abc"),
    ("Region B/2", r"region\s*(?:b|2)\b"),
    ("Region A/1", r"region\s*(?:a|1)\b(?!\s*,?\s*b)"),
]


def detect_editions(text):
    t = (text or "").lower()
    return [name for name, rx in EDITION_RULES if re.search(rx, t)]


def detect_region(text):
    t = (text or "").lower()
    return next((name for name, rx in REGION_RULES if re.search(rx, t)), None)


def norm(s):
    return re.sub(r"[^a-z0-9 ]+", " ", (s or "").lower())


STOPWORDS = {"the", "a", "an", "of", "and", "4k", "uhd", "dvd", "bluray", "blu", "ray",
             "cd", "cds", "audio", "album", "vinyl", "lp", "lps", "cassette", "cassettes", "tape"}


def relevant(title, query):
    words = [w for w in norm(query).split() if w not in STOPWORDS]
    t = set(norm(title).split())
    return all(w in t for w in words) if words else True


YEAR_RE = re.compile(r"\b(19[2-9]\d|20[0-4]\d)\b")


def year_ok(title, year):
    """If a listing mentions years, one of them should be close to the film's year
    (cuts out remakes and same-named films). Listings with no year are kept."""
    if not year:
        return True
    years = [int(y) for y in YEAR_RE.findall(title or "")]
    return not years or any(abs(y - int(year)) <= 2 for y in years)


def save_debug(name, text):
    if DEBUG:
        os.makedirs(DEBUG_DIR, exist_ok=True)
        with open(os.path.join(DEBUG_DIR, f"{name}.html"), "w", encoding="utf-8") as f:
            f.write(text or "")


# ---------- fast path: plain HTTP with per-site sessions (keeps cookies)

_sessions = {}
_sess_lock = threading.Lock()


def session_for(url):
    host = urllib.parse.urlparse(url).netloc
    with _sess_lock:
        s = _sessions.get(host)
        if s is None:
            if IMPERSONATE:
                # Let curl_cffi set Chrome's own headers; overriding the User-Agent
                # creates a fingerprint mismatch that bot filters spot instantly.
                s = http.Session(**IMPERSONATE)
                s.headers.update({"Accept-Language": "en-GB,en;q=0.9"})
            else:
                s = http.Session()
                s.headers.update(HEADERS)
            try:  # visit the homepage first to pick up cookies, like a person would
                s.get(f"https://{host}/", timeout=TIMEOUT)
            except Exception:
                pass
            _sessions[host] = s
        return s


BLOCK_MARKERS = ("captcha", "pardon our interruption", "cf-chl", "attention required",
                 "something went wrong on our end", "error page | ebay",
                 "access denied", "are you a robot", "verify you are human")


def looks_blocked(html):
    if not html:
        return True
    if len(html) > 150_000:  # real results pages are big; challenge pages are small
        return False
    low = html.lower()
    return any(m in low for m in BLOCK_MARKERS)


def http_get(url, retailer, headers=None):
    r = session_for(url).get(url, headers=headers or {}, timeout=TIMEOUT)
    save_debug(retailer, r.text)
    if r.status_code >= 400:
        raise RuntimeError(f"HTTP {r.status_code}")
    if looks_blocked(r.text):
        raise RuntimeError("bot check page")
    return r.text


# ---------- slow path: a real Chromium browser (Playwright) for sites that block scripts

try:
    from playwright.sync_api import sync_playwright
    HAVE_BROWSER = True
except Exception as e:
    BROWSER_ERR = f"{type(e).__name__}: {e}"
    HAVE_BROWSER = False

HEADED = False
PROFILE_DIR = os.path.join(APP_DIR, "browser_profile")
BROWSER_HINT = "blocked — browser fallback not installed: stop the app and run  python filmprice.py --install"


CHALLENGE_MARKERS = ("just a moment", "cf-chl", "challenge-platform", "pardon our interruption",
                     "captcha-delivery", "px-captcha", "verify you are human", "are you a robot",
                     "automated access to amazon data", "enter the characters you see below",
                     "something went wrong on our end", "error page | ebay")


def is_challenge(html):
    low = (html or "")[:60000].lower()
    return any(m in low for m in CHALLENGE_MARKERS)


class Browser:
    """One shared browser, driven from a single thread (Playwright's sync API isn't thread-safe).
    Prefers your installed Microsoft Edge or Google Chrome (much harder for bot checks to spot than Playwright's
    test Chromium). It uses its own separate profile folder - never your normal Chrome profile."""

    def __init__(self):
        self.ex = ThreadPoolExecutor(max_workers=1)
        self.ctx = None

    def _context(self):
        if self.ctx is None:
            if getattr(self, "pw", None) is None:
                self.pw = sync_playwright().start()
            # Headless browsers are easy for bot checks (e.g. CeX's Cloudflare) to spot, so run a
            # normal window and, unless --headed was given, park it off-screen where you won't see it.
            args = ["--disable-blink-features=AutomationControlled"]
            if not HEADED:
                args += ["--window-position=-32000,-32000", "--window-size=1366,900"]
            opts = dict(headless=False, locale="en-GB", viewport={"width": 1366, "height": 900},
                        args=args, ignore_default_args=["--enable-automation"])
            # Try your installed Chrome first, then Playwright's Chromium, each with its OWN
            # profile folder (a profile made by one won't open in the other), and finally a
            # throwaway profile. Each candidate must actually open a tab to count as working.
            attempts = [
                ("Microsoft Edge", lambda: self.pw.chromium.launch_persistent_context(
                    PROFILE_DIR + "_edge", channel="msedge", **opts)),
                ("Google Chrome", lambda: self.pw.chromium.launch_persistent_context(
                    PROFILE_DIR + "_chrome", channel="chrome", **opts)),
                ("Playwright Chromium", lambda: self.pw.chromium.launch_persistent_context(
                    PROFILE_DIR + "_chromium", **opts)),
                ("Playwright Chromium (no saved profile)", lambda: self.pw.chromium.launch(
                    headless=False, args=opts["args"], ignore_default_args=opts["ignore_default_args"]
                ).new_context(locale="en-GB", viewport=opts["viewport"])),
            ]
            errors = []
            for name, launch in attempts:
                try:
                    ctx = launch()
                    ctx.new_page().close()  # proves it didn't die on startup
                    self.ctx = ctx
                    print(f"[browser] using {name}")
                    break
                except Exception as e:
                    msg = str(e).splitlines()[0][:160]
                    errors.append(f"{name}: {msg}")
                    print(f"[browser] {name} failed: {msg}")
            if self.ctx is None:
                raise RuntimeError("couldn't start a browser - " + " | ".join(errors))
            self.ctx.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined})")
            self.ctx.on("close", lambda *_: self._reset(close=False))
        return self.ctx

    def _reset(self, close=True):
        """Forget a browser that was closed or crashed so the next search launches a fresh one."""
        ctx, self.ctx = self.ctx, None
        if close and ctx is not None:
            try:
                ctx.close()
            except Exception:
                pass

    def _new_page(self):
        try:
            return self._context().new_page()
        except Exception:  # window closed / browser crashed -> relaunch once
            self._reset()
            return self._context().new_page()

    def _page(self):
        page = getattr(self, "page", None)
        if page is None or page.is_closed() or self.ctx is None:
            self.page = page = self._new_page()
        return page

    def _load(self, url, capture):
        page = self._page()
        responses = []
        handler = (lambda resp: responses.append(resp)
                   if any(c in resp.url for c in capture) else None) if capture else None
        if handler:
            page.on("response", handler)
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=30000)
            deadline = time.time() + 25
            # Give challenge pages (e.g. Cloudflare's "Just a moment") time to clear themselves,
            # and wait for the search data we want rather than a fixed delay.
            clicked = False
            while time.time() < deadline:
                page.wait_for_timeout(500)
                if not clicked and "amazon." in page.url:
                    # Amazon's interstitial: a single "Continue shopping" button, no puzzle.
                    try:
                        btn = page.get_by_role("button", name="Continue shopping")
                        if btn.count():
                            btn.first.click(timeout=2000)
                            clicked = True
                            page.wait_for_load_state("domcontentloaded", timeout=15000)
                    except Exception:
                        pass
                if is_challenge(page.content()):
                    continue
                if capture:
                    if responses:
                        page.wait_for_timeout(1500)  # let any follow-up result calls land
                        break
                else:
                    try:
                        page.wait_for_load_state("networkidle", timeout=6000)
                    except Exception:
                        pass
                    break
            bodies = []
            for resp in responses:
                try:
                    bodies.append(resp.json())
                except Exception:
                    pass
            return page.content(), bodies
        finally:
            if handler:
                try:
                    page.remove_listener("response", handler)
                except Exception:
                    pass

    def get(self, url, capture=None):
        return self.ex.submit(self._load, url, capture).result(timeout=90)


BROWSER = Browser()


def browser_get(url, retailer, capture=None):
    if not HAVE_BROWSER:
        raise RuntimeError(BROWSER_HINT)
    html, bodies = BROWSER.get(url, capture)
    save_debug(retailer + "_browser", html)
    if is_challenge(html) and not bodies:
        raise RuntimeError("blocked by the site's bot check even in browser mode - run once with:  "
                           "python filmprice.py --headed  and tick the check if one appears")
    return html, bodies


def fetch(url, retailer, headers=None):
    """Fast HTTP first; if the site blocks it, load the page in the real browser."""
    try:
        return http_get(url, retailer, headers)
    except Exception as e:
        if not HAVE_BROWSER:
            raise RuntimeError(f"{e} — {BROWSER_HINT}")
        return browser_get(url, retailer)[0]


def item(title, price, url, retailer, condition=None, image=None, stock=None, extra=None):
    return {
        "title": (title or "").strip(),
        "price": price,
        "url": url,
        "format": detect_format(title, extra),
        "editions": detect_editions(f"{title} {extra or ''}"),
        "region": detect_region(f"{title} {extra or ''}"),
        "sell": None,
        "condition": condition,
        "image": image,
        "in_stock": stock,
        "retailer": retailer,
    }


def img_url(img, base=None):
    if img is None:
        return None
    for attr in ("data-src", "data-lazy-src", "data-original", "src"):
        v = img.get(attr)
        if v and not v.startswith("data:"):
            return absolute(base, v) if base else v
    srcset = img.get("data-srcset") or img.get("srcset")
    if srcset:
        first = srcset.split(",")[0].strip().split(" ")[0]
        return absolute(base, first) if base else first
    return None


def absolute(base, href):
    return urllib.parse.urljoin(base, href) if href else None


# ---------- generic extractors (used for HMV / musicMagpie, whose markup shifts)

def from_json_ld(soup, base, retailer):
    out = []

    def walk(node):
        if isinstance(node, list):
            for n in node:
                walk(n)
        elif isinstance(node, dict):
            typ = node.get("@type")
            if typ == "Product" or (isinstance(typ, list) and "Product" in typ):
                offers = node.get("offers") or {}
                if isinstance(offers, list):
                    offers = offers[0] if offers else {}
                price = parse_price(offers.get("price") or offers.get("lowPrice"))
                if node.get("name") and price:
                    img = node.get("image")
                    if isinstance(img, list):
                        img = img[0] if img else None
                    out.append(item(node["name"], price, absolute(base, node.get("url") or offers.get("url")),
                                    retailer, image=img if isinstance(img, str) else None,
                                    stock=("InStock" in str(offers.get("availability", ""))) or None))
            for v in node.values():
                if isinstance(v, (dict, list)):
                    walk(v)

    for s in soup.find_all("script", type="application/ld+json"):
        try:
            walk(json.loads(s.string or ""))
        except (json.JSONDecodeError, TypeError):
            continue
    return out


def from_embedded_json(soup, base, retailer):
    """Next.js / Nuxt style state blobs: look for dicts that have a name and a price."""
    out = []
    blobs = []
    for s in soup.find_all("script"):
        txt = s.string or ""
        if s.get("id") == "__NEXT_DATA__" or s.get("type") == "application/json":
            blobs.append(txt)
    name_keys = ("name", "title", "productName", "displayName")
    price_keys = ("price", "salePrice", "sellPrice", "currentPrice", "priceValue", "finalPrice")

    def walk(node, depth=0):
        if depth > 40:
            return
        if isinstance(node, list):
            for n in node:
                walk(n, depth + 1)
        elif isinstance(node, dict):
            name = next((node[k] for k in name_keys if isinstance(node.get(k), str)), None)
            raw = next((node[k] for k in price_keys if k in node), None)
            if isinstance(raw, dict):
                raw = raw.get("value") or raw.get("amount") or raw.get("current")
            price = parse_price(raw) if raw is not None else None
            if name and price and 0 < price < 1000:
                url = node.get("url") or node.get("href") or node.get("slug")
                out.append(item(name, price, absolute(base, url) if url else None, retailer))
            for v in node.values():
                if isinstance(v, (dict, list)):
                    walk(v, depth + 1)

    for b in blobs:
        try:
            walk(json.loads(b))
        except (json.JSONDecodeError, TypeError):
            continue
    return out


def from_cards(soup, base, retailer, href_hint=None):
    """Heuristic: product links whose nearby container shows a £ price."""
    out, seen = [], set()
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if href_hint and not re.search(href_hint, href):
            continue
        title = (a.get("title") or a.get_text(" ", strip=True) or "").strip()
        if len(title) < 3 or title.lower() in ("view", "buy now", "add to basket"):
            continue
        node, price = a, None
        for _ in range(5):
            node = node.parent
            if node is None:
                break
            m = PRICE_RE.search(node.get_text(" ", strip=True))
            if m:
                price = float(m.group(1).replace(",", ""))
                break
        if not price:
            continue
        url = absolute(base, href)
        if url in seen:
            continue
        seen.add(url)
        img = node.find("img") if node is not None else None
        ctx = node.get_text(" ", strip=True)[:200] if node is not None else ""
        it = item(title, price, url, retailer, image=img_url(img, base), extra=ctx or None)
        it["_ctx"] = ctx
        out.append(it)
    return out


def generic_scrape(url, retailer, href_hint=None):
    html = fetch(url, retailer)
    soup = BeautifulSoup(html, "lxml")
    for fn in (from_json_ld, from_embedded_json):
        res = fn(soup, url, retailer)
        if res:
            return res
    return from_cards(soup, url, retailer, href_hint)


# --------------------------------------------------------------------------- retailers

def cex_boxes(data, music=False):
    """Find CeX 'box' records anywhere in a JSON blob (old v3 API or the newer Algolia search)."""
    out, seen = [], set()

    def walk(n):
        if isinstance(n, list):
            for x in n:
                walk(x)
        elif isinstance(n, dict):
            if "boxName" in n and "sellPrice" in n:
                bid = n.get("boxId") or n.get("objectID")
                if bid in seen:
                    return
                seen.add(bid)
                cat = n.get("categoryFriendlyName") or n.get("categoryName") or ""
                if isinstance(cat, list):
                    cat = " ".join(map(str, cat))
                imgs = n.get("imageUrls") or {}
                img = imgs.get("medium") or imgs.get("large") if isinstance(imgs, dict) else (imgs[0] if imgs else None)
                # CeX reports several stock counts (online, click & collect, in store...).
                # Count it as in stock if ANY of them is above zero; only call it out of
                # stock when CeX itself says so and no count shows stock anywhere.
                qtys = [v for k, v in n.items() if "quantity" in k.lower() and isinstance(v, (int, float))]
                if any(v > 0 for v in qtys):
                    stock = True
                elif n.get("outOfStock"):
                    stock = False
                else:
                    stock = None  # unknown: don't strike it through
                it = item(n.get("boxName"), parse_price(n.get("sellPrice")),
                          f"https://uk.webuy.com/product-detail?id={bid}", "cex",
                          condition="Used (CeX graded)", image=img, stock=stock)
                it["format"] = detect_format(str(cat), n.get("boxName"))
                supercat = str(n.get("superCatFriendlyName") or n.get("superCatName") or "")
                if music and it["format"] == "Other" and MUSIC_WORDS.search(f"{cat} {supercat}") \
                        and not re.search(r"vinyl|\blp\b|cassette|tape|merch", f"{cat} {n.get('boxName')}", re.I):
                    it["format"] = "CD"  # CeX's music section is CDs unless it says otherwise
                cash = n.get("cashPriceCalculated", n.get("cashPrice"))
                voucher = n.get("exchangePriceCalculated", n.get("exchangePrice"))
                if cash is not None or voucher is not None:
                    it["sell"] = {"cash": parse_price(cash), "voucher": parse_price(voucher), "by": "CeX"}
                out.append(it)
            else:
                for v in n.values():
                    if isinstance(v, (dict, list)):
                        walk(v)

    walk(data)
    return out


MAX_ITEMS = 1000   # per shop, after filtering
FORMAT_QUERY = {"DVD": "dvd", "Blu-ray": "blu-ray", "4K": "4k blu-ray",
                "CD": "cd", "Vinyl": "vinyl", "Cassette": "cassette"}
MEDIA_FORMATS = {"film": ["DVD", "Blu-ray", "4K"], "music": ["CD", "Vinyl", "Cassette"]}
MUSIC_WORDS = re.compile(r"music|\bcds?\b|album|rock|pop|soundtrack|jazz|classical|dance|hip.?hop|\brap\b|"
                         r"country|metal|soul|reggae|indie|folk|blues|easy listening|r&b|electronic", re.I)


def price_ok(p, o):
    return (p is not None and p > 0 and (o.get("pmin") is None or p >= o["pmin"])
            and (o.get("pmax") is None or p <= o["pmax"]))


def site_url(key, q, o, page=1):
    """Search page URL for a shop, with price range + page number where the shop supports it."""
    u = SEARCH_URLS[key].format(q=urllib.parse.quote_plus(q))
    lo, hi = o.get("pmin"), o.get("pmax")
    if o.get("media") == "music":  # search the shop's music section instead of films
        u = u.replace("_sacat=11232", "_sacat=11233")    # eBay: Music (CDs, vinyl, cassettes)
        u = u.replace("&i=dvd", "&i=popular")            # Amazon: CDs & Vinyl (cassettes too)
    if key == "ebay":
        u += "&_ipg=240"  # 240 listings per page
        if lo is not None:
            u += f"&_udlo={lo:g}"
        if hi is not None:
            u += f"&_udhi={hi:g}"
        if page > 1:
            u += f"&_pgn={page}"
    elif key == "amazon":
        if lo is not None or hi is not None:  # Amazon's price filter is in pence
            u += "&rh=" + urllib.parse.quote(f"p_36:{int((lo or 0) * 100)}-{int(hi * 100) if hi is not None else ''}")
        if page > 1:
            u += f"&page={page}"
    elif key == "cex":
        if lo is not None or hi is not None:
            u += f"&sellPrice={lo or 0:g}:{hi if hi is not None else 9999:g}"
        if page > 1:
            u += f"&page={page}"
    elif key == "zavvi":
        if page > 1:
            u += f"&pageNumber={page}"
    elif page > 1:
        u += f"&page={page}"
    return u


def paged(fetch_page, pages):
    """Call fetch_page(1..pages), stopping early when a page adds nothing new."""
    out, seen = [], set()
    for n in range(1, pages + 1):
        try:
            got = fetch_page(n) or []
        except Exception:
            if n == 1:
                raise
            break  # a later page failing shouldn't lose the results we already have
        new = [r for r in got if (r.get("url"), r.get("format")) not in seen]
        if not new:
            break
        for r in new:
            seen.add((r.get("url"), r.get("format")))
        out += new
    return out


# ---------- CeX

def search_cex(q, o):
    music = o.get("media") == "music"
    # 1) Unofficial JSON API — fast when it isn't blocked. superCatIds=[2] = Film & TV;
    #    music (and barcode look-ups) search everything and sort it out by category.
    supercat = "&superCatIds=[2]" if o.get("media", "film") == "film" else ""
    try:
        def api_page(n):
            api = ("https://wss2.cex.uk.webuy.io/v3/boxes?q=" + urllib.parse.quote(q) +
                   f"{supercat}&firstRecord={1 + (n - 1) * 50}&count=50&sortBy=relevance&sortOrder=desc")
            return cex_boxes(json.loads(http_get(api, "cex", headers={
                "Accept": "application/json", "Origin": "https://uk.webuy.com", "Referer": "https://uk.webuy.com/"})),
                music=music)
        res = paged(api_page, o["pages"])
        if res:
            return res
    except Exception as e:
        if not HAVE_BROWSER:
            raise RuntimeError(f"{e} — {BROWSER_HINT}")

    # 2) Open the real CeX search page and capture the search data it downloads.
    def browser_page(n):
        html, bodies = browser_get(site_url("cex", q, o, n), "cex", capture=("webuy.io", "algolia"))
        res = cex_boxes(bodies, music=music)
        if not res:
            res = from_cards(BeautifulSoup(html, "lxml"), "https://uk.webuy.com", "cex", href_hint=r"product-detail")
            if music:  # card text carries CeX's category, e.g. "Music CDs | Oasis - Definitely Maybe"
                for r in res:
                    ctx = r.get("_ctx", "")
                    if r["format"] == "Other" and MUSIC_WORDS.search(ctx) and not re.search(r"vinyl|cassette|\blp\b", ctx, re.I):
                        r["format"] = "CD"
        return res
    return paged(browser_page, o["pages"])


# ---------- eBay

_ebay_token = {"value": None, "exp": 0}
_ebay_lock = threading.Lock()


def ebay_token():
    cid, secret = os.environ.get("EBAY_CLIENT_ID"), os.environ.get("EBAY_CLIENT_SECRET")
    if not (cid and secret):
        return None
    with _ebay_lock:
        if _ebay_token["value"] and time.time() < _ebay_token["exp"] - 60:
            return _ebay_token["value"]
        auth = base64.b64encode(f"{cid}:{secret}".encode()).decode()
        r = http.post("https://api.ebay.com/identity/v1/oauth2/token",
                      headers={"Authorization": f"Basic {auth}",
                               "Content-Type": "application/x-www-form-urlencoded"},
                      data="grant_type=client_credentials&scope=https%3A%2F%2Fapi.ebay.com%2Foauth%2Fapi_scope",
                      timeout=TIMEOUT)
        r.raise_for_status()
        j = r.json()
        _ebay_token.update(value=j["access_token"], exp=time.time() + j.get("expires_in", 7200))
        return _ebay_token["value"]


def parse_ebay(html, label):
    soup = BeautifulSoup(html, "lxml")
    if not soup.select("li.s-item, li.s-card, div.s-item, div.s-card") and not re.search(
            r"\b0 results\b|no exact matches", html, re.I):
        raise RuntimeError("eBay sent back an error/blocked page instead of results - try again shortly")
    out = []
    for c in soup.select("li.s-item, li.s-card, div.s-item, div.s-card"):
        t = c.select_one(".s-item__title, .s-card__title")
        p = c.select_one(".s-item__price, .s-card__price")
        a = c.select_one("a.s-item__link, a.su-link, a[href*='/itm/']")
        if not (t and p and a):
            continue
        title = t.get_text(" ", strip=True).replace("New listing", "").strip()
        if title.lower().startswith("shop on ebay"):
            continue
        text = c.get_text(" ", strip=True)
        # Condition: eBay's own wording, not the seller's subtitle/tagline
        m = re.search(r"\b(Brand new|Pre-owned|Used|New \(other\)|New|Opened[^·|]*|Very good|Good|Acceptable)\b",
                      text.replace(title, ""), re.I)
        condition = m.group(1).strip().capitalize() if m else None
        # Postage: add it so totals are comparable with shops that include delivery
        price = parse_price(p.get_text())
        post_m = re.search(r"\+\s*£\s?(\d+(?:\.\d{1,2})?)\s*(?:postage|delivery|shipping)", text, re.I)
        if post_m and price:
            postage = float(post_m.group(1))
            price = round(price + postage, 2)
            condition = f"{condition + ' · ' if condition else ''}inc. £{postage:.2f} postage"
        elif re.search(r"free (postage|delivery|shipping)", text, re.I):
            condition = f"{condition + ' · ' if condition else ''}free postage"
        out.append(item(title, price, a["href"].split("?")[0], label, condition=condition,
                        image=img_url(c.select_one("img")), stock=True))
    return out


def search_ebay(q, o, seller=None, label="ebay", debug_name=None):
    tok = ebay_token()
    if tok:  # official Browse API
        def api_page(n):
            flt = "itemLocationCountry:GB,buyingOptions:{FIXED_PRICE}"
            if o.get("pmin") is not None or o.get("pmax") is not None:
                flt += f",price:[{o.get('pmin') or 0:g}..{'' if o.get('pmax') is None else format(o['pmax'], 'g')}],priceCurrency:GBP"
            if seller:
                flt += f",sellers:{{{seller}}}"
            r = http.get("https://api.ebay.com/buy/browse/v1/item_summary/search",
                         params={"q": q, "category_ids": "11233" if o.get("media") == "music" else "11232",
                                 "limit": "200",
                                 "offset": str((n - 1) * 200), "filter": flt},
                         timeout=TIMEOUT,
                         headers={"Authorization": f"Bearer {tok}", "X-EBAY-C-MARKETPLACE-ID": "EBAY_GB"})
            r.raise_for_status()
            return [item(it.get("title"), parse_price((it.get("price") or {}).get("value")), it.get("itemWebUrl"),
                         label, condition=it.get("condition"),
                         image=(it.get("image") or {}).get("imageUrl"), stock=True)
                    for it in r.json().get("itemSummaries", [])]
        return paged(api_page, o["pages"])

    def html_page(n):
        url = site_url("ebay", q, o, n) + "&LH_BIN=1"
        if seller:
            url += "&_ssn=" + urllib.parse.quote(seller)
        return parse_ebay(fetch(url, debug_name or label), label)
    return paged(html_page, o["pages"])


# ---------- Amazon

FORMAT_LABEL_RE = re.compile(r"^\s*(dvd|blu-?ray|4k|4k uhd|4k ultra hd|ultra hd|uhd|4k\s*\+\s*blu-?ray|"
                             r"audio cd|audio cassette|cd|vinyl|lp|mp3 music|mp3|streaming|cassette)\b", re.I)


def amazon_dp(href):
    href = absolute("https://www.amazon.co.uk", href)
    m = re.search(r"/dp/([A-Z0-9]{10})", href or "")
    return f"https://www.amazon.co.uk/dp/{m.group(1)}" if m else href


def amazon_byline(card):
    """The "by <artist>" line under an Amazon result (music cards keep the artist there)."""
    for el in card.select(".a-row.a-color-secondary, .a-row .a-color-secondary, .a-row.a-size-base"):
        m = re.match(r"by\s+(.+?)(?:\s*\|.*)?$", el.get_text(" ", strip=True), re.I)
        if m and 1 < len(m.group(1)) < 80:
            return m.group(1).strip(" ,")
    return None


def parse_amazon(html, music=False):
    soup = BeautifulSoup(html, "lxml")
    cards = soup.select("div[data-component-type='s-search-result']")
    if not cards and not re.search(r"no results for|did not match any products", html, re.I):
        raise RuntimeError("Amazon didn't return a results page (likely its bot check) - try again in a minute")
    out = []
    for c in cards:
        t = c.select_one("h2 span") or c.select_one("h2")
        a = c.select_one("a[href*='/dp/']")
        if not (t and a):
            continue
        title = t.get_text(" ", strip=True)
        if music:  # "Definitely Maybe" + "by Oasis" -> "Oasis - Definitely Maybe"
            by = amazon_byline(c)
            if by and by.lower() not in title.lower():
                title = f"{by} - {title}"
        img = c.select_one("img.s-image")
        image = img.get("src") if img else None
        # A card can list several formats, each with its own price, e.g.
        #   Blu-ray £39.99 | DVD £23.99 | 4K £63.99
        # Give each format label the first (non-struck-through) price after it, before the next label.
        order = {id(el): i for i, el in enumerate(c.find_all(True))}
        labels = [el for el in c.select("a.a-text-bold, span.a-text-bold, a.a-link-normal")
                  if FORMAT_LABEL_RE.match(el.get_text(" ", strip=True) or "")
                  and len(el.get_text(" ", strip=True)) < 40]
        prices = [el for el in c.select("span.a-price") if not el.get("data-a-strike")]
        added = False
        for n, lab in enumerate(labels):
            lo = order[id(lab)]
            hi = order[id(labels[n + 1])] if n + 1 < len(labels) else 10**9
            pr = next((p for p in prices if lo < order[id(p)] < hi), None)
            off = pr.select_one("span.a-offscreen") if pr else None
            it = item(title, parse_price(off.get_text()) if off else None,
                      amazon_dp(lab.get("href") or a["href"]), "amazon", image=image, stock=bool(off))
            it["format"] = detect_format(lab.get_text(" ", strip=True))
            out.append(it)
            added = True
        if not added:  # single-format card: format from the title, first price
            off = c.select_one("span.a-price:not([data-a-strike]) span.a-offscreen")
            out.append(item(title, parse_price(off.get_text()) if off else None, amazon_dp(a["href"]),
                            "amazon", image=image, stock=bool(off)))
    return out


def search_amazon(q, o):
    def page(n):
        url = site_url("amazon", q, o, n)
        try:
            return parse_amazon(fetch(url, "amazon"), music=o.get("media") == "music")
        except RuntimeError:
            # Amazon sometimes answers the quick request with a page that isn't a recognisable
            # bot check but has no results either - give the real browser a go before giving up.
            if not HAVE_BROWSER:
                raise
            return parse_amazon(browser_get(url, "amazon")[0], music=o.get("media") == "music")
    return paged(page, o["pages"])


# ---------- HMV / musicMagpie (own site, falling back to their official eBay shops)

def own_site_then_ebay(q, o, key, label, href_hint):
    errors = []
    try:
        def page(n):
            res = generic_scrape(site_url(key, q, o, n), key, href_hint=href_hint)
            # Only trust real matches with a disc format; banners, menus and
            # "you may also like" links don't count.
            ctx = (lambda r: " " + r.get("_ctx", "")) if o.get("media") == "music" else (lambda r: "")
            return [r for r in res if r.get("price") and r["format"] != "Other" and relevant(r["title"] + ctx(r), q)]
        good = paged(page, o["pages"])
        if good:
            return good
        errors.append(f"{label} site: no matching products on page")
    except Exception as e:
        errors.append(f"{label} site: {e}")
    try:
        res = search_ebay(q, o, seller=EBAY_SHOPS[key], label=key, debug_name=f"{key}_ebayshop")
    except Exception as e:
        errors.append(f"{label} eBay shop: {e}")
        raise RuntimeError(" | ".join(errors))
    for r in res:
        r["condition"] = ((r.get("condition") or "") + f" · via {label} eBay shop").lstrip(" ·")
    return res


def search_hmv(q, o):
    return own_site_then_ebay(q, o, "hmv", "hmv", r"/store/")


def search_musicmagpie(q, o):
    return own_site_then_ebay(q, o, "musicmagpie", "musicMagpie", r"/store/|/product")


def parse_thg(html, base="https://www.zavvi.com"):
    """Zavvi runs on The Hut Group's platform: product cards ('productBlock') with the
    name, a price element (often with a content="24.99" attribute) and a /<id>.html link."""
    soup = BeautifulSoup(html, "lxml")
    out, seen = [], set()
    cards = soup.select(".productBlock, li.productListProducts_product, [data-product-id], "
                        "[class*='productCard'], [class*='product-card']")
    for c in cards:
        a = c.select_one("a[href$='.html'], a[href*='.html?']")
        t = c.select_one(".productBlock_productName, [class*='productName'], [class*='product-name'], "
                         "[class*='ProductName'], h3, h2")
        p = c.select_one("[class*='priceValue'], [class*='price-value'], [class*='Price_value'], "
                         "[class*='productPrice'], [class*='price']")
        if not (a and (t or a.get("title"))):
            continue
        title = (t.get_text(" ", strip=True) if t else a.get("title", "")).strip()
        price = None
        if p is not None:
            price = parse_price(p.get("content") or p.get("data-price") or p.get_text(" ", strip=True))
        if price is None:
            price = parse_price(c.get("data-product-price") or c.get_text(" ", strip=True))
        url = absolute(base, a["href"].split("?")[0])
        if not title or not price or url in seen:
            continue
        seen.add(url)
        text = c.get_text(" ", strip=True)
        stock = False if re.search(r"out of stock|sold out|currently unavailable", text, re.I) else True
        out.append(item(title, price, url, "zavvi", image=img_url(c.select_one("img"), base), stock=stock,
                        extra=text[:200]))
    return out


def search_zavvi(q, o):
    errors = []
    try:
        def page(n):
            html = fetch(site_url("zavvi", q, o, n), "zavvi")
            soup = BeautifulSoup(html, "lxml")
            res = parse_thg(html)
            for fn in (from_json_ld, from_embedded_json):  # other ways the page may carry products
                if not res:
                    res = fn(soup, "https://www.zavvi.com", "zavvi")
            if not res:
                res = from_cards(soup, "https://www.zavvi.com", "zavvi", href_hint=r"/\d{5,}\.html")
            ctx = (lambda r: " " + r.get("_ctx", "")) if o.get("media") == "music" else (lambda r: "")
            return [r for r in res if r.get("price") and r["format"] != "Other" and relevant(r["title"] + ctx(r), q)]
        good = paged(page, o["pages"])
        if good:
            return good
        errors.append("Zavvi site: no matching products on page")
        print("[zavvi] site page had no matching products - see debug/zavvi*.html")
    except Exception as e:
        errors.append(f"Zavvi site: {e}")
    try:
        res = search_ebay(q, o, seller=EBAY_SHOPS["zavvi"], label="zavvi", debug_name="zavvi_ebayshop")
    except Exception as e:
        errors.append(f"Zavvi eBay shop: {e}")
        raise RuntimeError(" | ".join(errors))
    for r in res:
        r["condition"] = ((r.get("condition") or "") + " · via Zavvi eBay shop").lstrip(" ·")
    return res


SEARCHERS = {
    "cex": search_cex,
    "musicmagpie": search_musicmagpie,
    "ebay": search_ebay,
    "hmv": search_hmv,
    "amazon": search_amazon,
    # "zavvi": search_zavvi,  # ceased trading Oct 2026 - kept in case the brand is relaunched
}


def run_search(q, formats, strict, retailers, pmin=None, pmax=None, pages=1, in_stock_only=False,
               editions=None, region=None, year=None, media="film"):
    """q may be blank: then each selected format is browsed on its own ("dvd", "blu-ray", "4k blu-ray")
    and everything inside the price range is returned."""
    media = media if media in MEDIA_FORMATS else "film"
    o = {"pmin": pmin, "pmax": pmax, "pages": max(1, min(int(pages or 1), 10)), "media": media}
    # only ever show this department's formats (keeps soundtracks out of film results and vice versa)
    formats = [f for f in (formats or []) if f in MEDIA_FORMATS[media]] or MEDIA_FORMATS[media]
    queries = [q] if q else [FORMAT_QUERY[f] for f in formats]
    results = {}

    def one(key):
        t0 = time.time()
        try:
            items, seen, errors = [], set(), []
            for qq in queries:
                try:
                    for i in SEARCHERS[key](qq, o) or []:
                        k = (i.get("url"), i.get("format"), i.get("price"))
                        if k not in seen:
                            seen.add(k)
                            items.append(i)
                except Exception as e:
                    errors.append(str(e))
            if errors and not items:
                raise RuntimeError(errors[0])
            items = [i for i in items if price_ok(i.get("price"), o)]
            if strict and q:
                # music: shops often show the artist beside the album title, not in it
                items = [i for i in items if relevant(i["title"] + (" " + i.get("_ctx", "") if media == "music" else ""), q)]
            if formats:
                items = [i for i in items if i["format"] in formats]
            if in_stock_only:
                items = [i for i in items if i.get("in_stock") is not False]
            if year and q:
                items = [i for i in items if year_ok(i["title"], year)]
            if editions:
                items = [i for i in items if all(e in i["editions"] for e in editions)]
            if region:
                items = [i for i in items if i.get("region") == region]
            items.sort(key=lambda i: (i.get("in_stock") is False, i["price"]))
            for i in items:
                i.pop("_ctx", None)
            return key, {"ok": True, "items": items[:MAX_ITEMS], "total": len(items),
                         "ms": int((time.time() - t0) * 1000)}
        except Exception as e:
            return key, {"ok": False, "error": f"{type(e).__name__}: {e}"[:400], "items": [],
                         "ms": int((time.time() - t0) * 1000)}

    with ThreadPoolExecutor(max_workers=len(retailers)) as ex:
        for fut in as_completed([ex.submit(one, k) for k in retailers]):
            k, v = fut.result()
            v["search_url"] = site_url(k, queries[0], o)
            results[k] = v
    return results


# --------------------------------------------------------------------------- director / actor lookup (Wikidata)

# Wikimedia asks automated tools for a descriptive User-Agent and blocks generic library ones.
WIKIDATA_UA = f"FilmPriceChecker/{VERSION} (personal desktop film price-comparison tool; low volume)"
_person_cache = {}


class FilmDBError(Exception):
    pass


def _wd_get(url, params, tries=3):
    import urllib.error
    import urllib.request
    req = urllib.request.Request(url + "?" + urllib.parse.urlencode(params),
                                 headers={"User-Agent": WIKIDATA_UA, "Accept": "application/json"})
    last = None
    for attempt in range(tries):
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            last = f"HTTP {e.code} from {urllib.parse.urlparse(url).netloc}"
            if e.code not in (429, 500, 502, 503, 504):
                break  # not a temporary problem - retrying won't help
            try:
                wait = int(e.headers.get("Retry-After") or 0) if e.headers else 0
            except ValueError:
                wait = 0
            if wait > 10:
                break  # asked to back off for a while - fail now instead of hanging the search
            time.sleep(max(wait, 1.5 * (attempt + 1)))
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last = f"{type(e).__name__}: {getattr(e, 'reason', e)}"
            time.sleep(2 * (attempt + 1))
    print(f"[filmdb] {last}")
    raise FilmDBError(last)


ROLE_WORDS = {
    "film": re.compile(r"actor|actress|director|film|screenwriter|producer|comedian|cinematograph|animator", re.I),
    "music": re.compile(r"band|singer|musician|rapper|songwriter|group|duo|composer|guitarist|drummer|"
                        r"pianist|vocalist|\bdj\b|record producer|music|orchestra|ensemble|trio", re.I),
}


def find_people(name, role="actor"):
    """Possible matches for a name, the most likely film people (or musicians, for artist
    search) first."""
    res = _wd_get("https://www.wikidata.org/w/api.php", {
        "action": "wbsearchentities", "search": name, "language": "en", "uselang": "en",
        "type": "item", "limit": 10, "format": "json"})
    people = [{"id": x["id"], "label": x.get("label", ""), "description": x.get("description", "")}
              for x in res.get("search", [])]
    words = ROLE_WORDS["music" if role == "artist" else "film"]
    return sorted(people, key=lambda p: 0 if words.search(p["description"]) else 1)


# Wikidata item types that count as a film (direct "instance of" values).
FILM_TYPES = ["Q11424", "Q24869", "Q202866", "Q29168811", "Q506240", "Q93204", "Q226730", "Q20650540"]
# ...and an album: album, studio album, live album, compilation album, EP, soundtrack album
ALBUM_TYPES = ["Q482994", "Q208569", "Q209939", "Q222910", "Q169930", "Q4176708"]
# role -> (Wikidata property linking the work to the person, work types that count)
ROLES = {"director": ("P57", FILM_TYPES), "actor": ("P161", FILM_TYPES), "artist": ("P175", ALBUM_TYPES)}
WD_API = "https://www.wikidata.org/w/api.php"


FILM_ROUTE = {}  # last route that worked, per role - shown in the self-test


def _wd_search_ids(srsearch, cap=600):
    ids, offset = [], 0
    while True:
        res = _wd_get(WD_API, {"action": "query", "list": "search", "format": "json", "srnamespace": 0,
                               "srsearch": srsearch, "srlimit": 500, "sroffset": offset,
                               "srinfo": "", "srprop": ""}, tries=2)
        ids += [h["title"] for h in res.get("query", {}).get("search", []) if re.fullmatch(r"Q\d+", h.get("title", ""))]
        nxt = res.get("continue", {}).get("sroffset")
        if not nxt or len(ids) >= cap:
            return ids
        offset = nxt


def _wd_backlinks(person_id, cap=3000):
    """Every item that mentions the person anywhere (films, awards, family...)."""
    ids, cont = [], {}
    while True:
        res = _wd_get(WD_API, dict({"action": "query", "list": "backlinks", "bltitle": person_id,
                                    "blnamespace": 0, "bllimit": 500, "format": "json"}, **cont), tries=2)
        ids += [x["title"] for x in res.get("query", {}).get("backlinks", []) if re.fullmatch(r"Q\d+", x.get("title", ""))]
        cont = res.get("continue", {})
        if not cont.get("blcontinue") or len(ids) >= cap:
            return ids
        cont = {"blcontinue": cont["blcontinue"]}


def _wd_entities(ids):
    out = {}
    for i in range(0, len(ids), 50):
        out.update(_wd_get(WD_API, {"action": "wbgetentities", "ids": "|".join(ids[i:i + 50]), "format": "json",
                                    "props": "labels|claims", "languages": "en|en-gb", "languagefallback": 1},
                           tries=2).get("entities", {}))
    return out


def _claim_ids(entity, prop):
    vals = []
    for c in entity.get("claims", {}).get(prop, []):
        v = ((c.get("mainsnak") or {}).get("datavalue") or {}).get("value")
        if isinstance(v, dict) and v.get("id"):
            vals.append(v["id"])
    return vals


def _as_films(ents, person_id, prop, need_type, types=FILM_TYPES):
    films = []
    for qid, e in ents.items():
        if person_id not in _claim_ids(e, prop):
            continue  # must really have this person as director / cast member
        if need_type and not set(_claim_ids(e, "P31")) & set(types):
            continue
        labels = e.get("labels", {})
        title = (labels.get("en") or labels.get("en-gb") or next(iter(labels.values()), {})).get("value")
        years = []
        for c in e.get("claims", {}).get("P577", []):
            t = (((c.get("mainsnak") or {}).get("datavalue") or {}).get("value") or {}).get("time", "")
            m = re.match(r"[+-]?(\d{4})", t)
            if m:
                years.append(int(m.group(1)))
        films.append({"title": title, "year": min(years) if years else None, "id": qid})
    return films


def _filmography_api(person_id, prop, work_types=FILM_TYPES):
    """Uses Wikidata's normal API (the same one person search uses), not the stricter,
    rate-limited query service. Tries progressively broader routes until one finds films
    (or albums, for artists)."""
    types = "|".join(f"P31={t}" for t in work_types)
    # artists are "performer" on every song and single too, so allow a bigger haul to filter
    cap = 1500 if prop == "P175" else 600
    # Directors: Wikidata's search never matches the combined director + film-type query
    # (seen in testing), so start with the next route for them.
    routes = [] if prop == "P57" else [
        ("search: person + type", lambda: _wd_search_ids(f"haswbstatement:{prop}={person_id} haswbstatement:{types}"), True)]
    routes += [
        ("search: person only", lambda: _wd_search_ids(f"haswbstatement:{prop}={person_id}", cap=cap), True),
        ("links to person", lambda: _wd_backlinks(person_id, cap=1500 if prop == "P175" else 3000), True),
    ]
    tried = []
    for name, get_ids, need_type in routes:
        ids = get_ids()
        films = _as_films(_wd_entities(ids), person_id, prop, need_type, work_types) if ids else []
        tried.append(f"{name}: {len(ids)} items -> {len(films)} {'albums' if prop == 'P175' else 'films'}")
        if films:
            FILM_ROUTE[prop] = "; ".join(tried)
            return films
    FILM_ROUTE[prop] = "; ".join(tried)
    return []


def _filmography_sparql(person_id, prop, work_type="Q11424"):
    """Backup: Wikidata's query service (stricter rate limits, so only used if the API fails)."""
    query = f"""
SELECT ?film ?filmLabel (MIN(YEAR(?date)) AS ?year) WHERE {{
  ?film wdt:{prop} wd:{person_id} ; wdt:P31 wd:{work_type} .
  OPTIONAL {{ ?film wdt:P577 ?date . }}
  SERVICE wikibase:label {{ bd:serviceParam wikibase:language "en". }}
}} GROUP BY ?film ?filmLabel LIMIT 300"""
    data = _wd_get("https://query.wikidata.org/sparql", {"query": query, "format": "json"}, tries=1)
    out = []
    for b in data["results"]["bindings"]:
        y = b.get("year", {}).get("value")
        out.append({"title": b.get("filmLabel", {}).get("value"), "year": int(y) if y and y.isdigit() else None,
                    "id": b["film"]["value"].rsplit("/", 1)[-1]})
    return out


def filmography(person_id, role):
    """Films a person directed (role='director') or appeared in (role='actor'), or albums by an
    artist (role='artist'), newest first."""
    if not re.fullmatch(r"Q\d+", person_id or ""):
        raise ValueError("bad person id")
    key = (person_id, role)
    if key in _person_cache and time.time() - _person_cache[key][0] < 86400:
        return _person_cache[key][1]
    prop, work_types = ROLES.get(role, ROLES["actor"])
    first = None
    try:
        raw = _filmography_api(person_id, prop, work_types)
    except FilmDBError as e:
        raw, first = [], e
    if not raw:  # last resort: the query service (rate-limited, but worth one try)
        try:
            raw = _filmography_sparql(person_id, prop, work_types[0])
            FILM_ROUTE[prop] = FILM_ROUTE.get(prop, "") + "; query service: ok"
        except FilmDBError:
            if first:
                raise first
    films, seen = [], set()
    for f in raw:
        title = f.get("title")
        if not title or re.fullmatch(r"Q\d+", title):
            continue  # no title on record
        k = (title.lower(), f.get("year"))
        if k in seen:
            continue
        seen.add(k)
        films.append(f)
    films.sort(key=lambda f: (f["year"] is None, -(f["year"] or 0), f["title"].lower()))
    _person_cache[key] = (time.time(), films)
    return films


def person_lookup(name, role, person_id=None):
    role = role if role in ROLES else "actor"
    people = find_people(name, role) if name else []
    if not person_id:
        if not people:
            raise ValueError(f"no one called '{name}' found")
        person_id = people[0]["id"]
    person = next((p for p in people if p["id"] == person_id), {"id": person_id, "label": name, "description": ""})
    return {"person": person, "role": role, "alternates": [p for p in people if p["id"] != person_id][:6],
            "films": filmography(person_id, role)}


# --------------------------------------------------------------------------- sell prices

def sell_lookup(barcode):
    """Trade-in prices for a disc barcode. CeX's product IDs for films are usually the barcode."""
    bc = re.sub(r"\D", "", barcode or "")
    if len(bc) < 8:
        raise ValueError("enter the 8-13 digit barcode from the back of the case")
    out = {"barcode": bc, "cex": [], "cex_error": None,
           "musicmagpie_url": "https://www.musicmagpie.co.uk/",
           "cex_sell_url": f"https://uk.webuy.com/sell/search?stext={bc}"}
    try:
        res = search_cex(bc, {"pmin": None, "pmax": None, "pages": 1, "media": "any"})
        out["cex"] = [r for r in res if r.get("sell")] or res
    except Exception as e:
        out["cex_error"] = str(e)[:300]
    return out


# --------------------------------------------------------------------------- watchlist + alerts

WATCH_FILE = os.path.join(APP_DIR, "watchlist.json")
_watch_lock = threading.RLock()
_watch_state = {"checking": None, "next_run": 0}


def load_watch():
    with _watch_lock:
        try:
            with open(WATCH_FILE, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            data = {}
        data.setdefault("interval_hours", 6)
        data.setdefault("items", [])
        data.setdefault("alerts", [])
        return data


def save_watch(data):
    with _watch_lock:
        tmp = WATCH_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, WATCH_FILE)


def notify(title, message):
    """Windows toast notification (no extra installs needed); prints elsewhere."""
    print(f"[ALERT] {title}: {message}")
    if os.name != "nt":
        return
    import subprocess
    ps = r"""
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] > $null
$t = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02)
$x = $t.GetElementsByTagName('text')
$x.Item(0).AppendChild($t.CreateTextNode($env:FPC_TITLE)) > $null
$x.Item(1).AppendChild($t.CreateTextNode($env:FPC_MSG)) > $null
$n = [Windows.UI.Notifications.ToastNotification]::new($t)
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe').Show($n)
"""
    env = dict(os.environ, FPC_TITLE=str(title)[:120], FPC_MSG=str(message)[:250])  # passed as data, never as code
    try:
        subprocess.Popen(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps], env=env,
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as e:
        print(f"[ALERT] notification failed: {e}")


def check_watch_item(w):
    """Search all shops for one watched title and raise an alert if it's at or under target."""
    with _watch_lock:
        _watch_state["checking"] = w["title"]
    try:
        res = run_search(w["title"], w.get("formats") or [], True, list(SEARCHERS), pages=1,
                         in_stock_only=True, editions=w.get("editions") or None, region=w.get("region") or None,
                         media=w.get("media", "film"))
    finally:
        with _watch_lock:
            _watch_state["checking"] = None
    names = dict(RETAILERS)
    best = None
    for key, r in res.items():
        for it in r.get("items") or []:
            if best is None or it["price"] < best["price"]:
                best = dict(it, shop=names.get(key, key))
    now = time.time()
    with _watch_lock:
        data = load_watch()
        cur = next((x for x in data["items"] if x["id"] == w["id"]), None)
        if cur is None:
            return None
        cur["last_checked"] = now
        cur["best"] = ({k: best.get(k) for k in ("price", "shop", "url", "title", "format")} if best else None)
        if best:
            cur.setdefault("history", []).append({"t": int(now), "price": best["price"], "shop": best["shop"]})
            cur["history"] = cur["history"][-60:]
        hit = best is not None and best["price"] <= float(cur["target"]) and (
            cur.get("alerted_price") is None or best["price"] < cur["alerted_price"])
        if hit:
            cur["alerted_price"] = best["price"]
            msg = f"£{best['price']:.2f} at {best['shop']} ({best['format']}) - target £{float(cur['target']):.2f}"
            data["alerts"].insert(0, {"t": int(now), "title": cur["title"], "msg": msg, "url": best.get("url"),
                                      "seen": False})
            data["alerts"] = data["alerts"][:50]
        elif best is not None and cur.get("alerted_price") is not None and best["price"] > float(cur["target"]):
            cur["alerted_price"] = None  # price went back up: alert again next time it drops
        save_watch(data)
    if hit:
        notify(f"Price alert: {w['title']}", msg)
    return best


def watch_loop():
    """Background checker: runs every interval_hours while the app is open."""
    time.sleep(20)
    while True:
        try:
            data = load_watch()
            interval = max(0.5, float(data.get("interval_hours") or 6)) * 3600
            due = [w for w in data["items"] if time.time() - (w.get("last_checked") or 0) >= interval]
            for w in due:
                try:
                    check_watch_item(w)
                except Exception as e:
                    print(f"[watch] {w.get('title')}: {e}")
            oldest = min([w.get("last_checked") or 0 for w in load_watch()["items"]] or [time.time()])
            _watch_state["next_run"] = oldest + interval
        except Exception as e:
            print(f"[watch] {e}")
        time.sleep(60)


def watch_api(action, body):
    data = load_watch()
    if action == "add":
        title = (body.get("title") or "").strip()
        try:
            target = round(float(body.get("target")), 2)
        except (TypeError, ValueError):
            raise ValueError("set a target price")
        if not title:
            raise ValueError("enter a title to watch")
        w = {"id": f"w{int(time.time() * 1000)}", "title": title[:120],
             "media": body.get("media") if body.get("media") in MEDIA_FORMATS else "film",
             "formats": [f for f in body.get("formats") or [] if f in FORMAT_QUERY],
             "editions": [e for e in body.get("editions") or [] if e in dict(EDITION_RULES)],
             "region": body.get("region") if body.get("region") in dict(REGION_RULES) else None,
             "target": target, "created": int(time.time()), "last_checked": 0, "best": None,
             "alerted_price": None, "history": []}
        with _watch_lock:
            data = load_watch()
            data["items"].append(w)
            save_watch(data)
        threading.Thread(target=check_watch_item, args=(w,), daemon=True).start()
    elif action == "delete":
        with _watch_lock:
            data = load_watch()
            data["items"] = [x for x in data["items"] if x["id"] != body.get("id")]
            save_watch(data)
    elif action == "target":
        with _watch_lock:
            data = load_watch()
            for x in data["items"]:
                if x["id"] == body.get("id"):
                    x["target"] = round(float(body.get("target")), 2)
                    x["alerted_price"] = None
            save_watch(data)
    elif action == "check":
        items = [x for x in data["items"] if body.get("id") in (None, "", "all") or x["id"] == body.get("id")]

        def run():
            for x in items:
                try:
                    check_watch_item(x)
                except Exception as e:
                    print(f"[watch] {x.get('title')}: {e}")
        threading.Thread(target=run, daemon=True).start()
    elif action == "settings":
        with _watch_lock:
            data = load_watch()
            data["interval_hours"] = max(0.5, min(float(body.get("interval_hours") or 6), 168))
            save_watch(data)
    elif action == "alerts_seen":
        with _watch_lock:
            data = load_watch()
            for a in data["alerts"]:
                a["seen"] = True
            save_watch(data)
    elif action == "alerts_clear":
        with _watch_lock:
            data = load_watch()
            data["alerts"] = []
            save_watch(data)
    data = load_watch()
    data["checking"] = _watch_state["checking"]
    data["next_run"] = _watch_state["next_run"]
    return data


# --------------------------------------------------------------------------- web UI

PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Film Price Checker</title>
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=VT323&family=Anton&display=swap" rel="stylesheet">
<style>
:root{
  --navy:#0b1f6b; --navy2:#071548; --ink:#030a26; --screen:#040b2c;
  --yellow:#ffd21f; --amber:#ffb000; --cyan:#7fe7ff; --phos:#c8ffd8;
  --text:#e9ecff; --mute:#8d97c9; --line:#2a3a8f; --good:#6dff8a; --bad:#ff5a5a;
  --dvd:#e3262b; --br:#1f6dff; --k4:#ffd21f;
  --mono:'VT323',ui-monospace,Consolas,monospace; --display:'Anton',Impact,'Arial Narrow Bold',sans-serif;
}
*{box-sizing:border-box}
html,body{background:var(--navy2)}
body{margin:0;color:var(--text);font:20px/1.15 var(--mono);min-height:100vh;
  background:radial-gradient(ellipse at 50% 0%, #1636a8 0%, var(--navy) 35%, var(--navy2) 75%, #030b30 100%) fixed}
/* CRT scanlines + vignette over everything */
body::after{content:"";position:fixed;inset:0;pointer-events:none;z-index:50;
  background:repeating-linear-gradient(to bottom, rgba(0,0,0,.18) 0 1px, transparent 1px 3px),
             radial-gradient(ellipse at center, transparent 55%, rgba(0,0,0,.45) 100%)}
a{color:inherit}
::selection{background:var(--yellow);color:var(--ink)}

/* ---- store sign ---- */
.sign{background:var(--yellow);color:var(--navy2);border-bottom:6px solid var(--ink);
  box-shadow:0 6px 0 var(--navy2), 0 14px 30px rgba(0,0,0,.45)}
.sign-in{max-width:1400px;margin:auto;padding:14px 20px 12px;display:flex;align-items:flex-end;gap:18px;flex-wrap:wrap}
.logo{font:400 52px/0.9 var(--display);letter-spacing:.02em;text-transform:uppercase;margin:0;
  transform:skewX(-8deg);text-shadow:3px 3px 0 rgba(3,10,38,.25)}
.logo span{display:inline-block;background:var(--navy2);color:var(--yellow);padding:2px 12px 0;margin-left:6px}
.tagline{font:22px var(--mono);letter-spacing:.08em;text-transform:uppercase;padding-bottom:4px}
.tagline b{background:var(--navy2);color:var(--yellow);padding:0 6px;font-weight:400}
.sign-right{margin-left:auto;text-align:right;font-size:18px;text-transform:uppercase;letter-spacing:.06em;padding-bottom:4px}

/* ---- terminal frame ---- */
.wrap{max-width:1400px;margin:auto;padding:22px 20px 70px}
.crt{background:var(--screen);border:3px solid #1b2a74;border-radius:14px;
  box-shadow:inset 0 0 0 2px #000, inset 0 0 40px rgba(127,231,255,.08), 0 0 0 6px #0a154d, 0 0 0 8px #000, 0 18px 40px rgba(0,0,0,.5);
  padding:16px 18px;position:relative}
.dept{display:flex;gap:8px;align-items:center}
.dept button{font:400 18px var(--display);letter-spacing:.1em;text-transform:uppercase;transform:none;box-shadow:none;
  background:#000;color:var(--yellow);border:2px solid var(--yellow);padding:2px 14px 0;cursor:pointer}
.dept button.on{background:var(--yellow);color:var(--navy2)}
.dept button:hover:not(.on){background:#1a1a00}
body.music .film-only,body.film .music-only{display:none!important}
.crt-title{display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap;color:var(--cyan);font-size:18px;letter-spacing:.12em;
  text-transform:uppercase;border-bottom:1px dashed var(--line);padding-bottom:8px;margin-bottom:12px;text-shadow:0 0 6px rgba(127,231,255,.5)}

form{display:flex;flex-wrap:wrap;gap:12px;align-items:center}
.prompt{flex:1 1 380px;display:flex;align-items:center;gap:8px;border:2px solid var(--yellow);background:#000;padding:6px 10px;
  box-shadow:0 0 12px rgba(255,210,31,.25)}
.prompt select{background:#000;color:var(--yellow);border:0;font:22px var(--mono);cursor:pointer;
  text-shadow:0 0 6px rgba(255,210,31,.6);padding:0;outline:none}
.prompt select option{background:#000}
.fg{width:100%;font-size:19px}
.fg td{white-space:normal;vertical-align:middle;padding:4px 10px}
.fg td.t{color:var(--text);text-transform:uppercase;cursor:pointer}
.fg td.t:hover{color:var(--yellow);text-decoration:underline}
.fg td.y{color:var(--mute);width:60px}
.fg .best a{color:var(--good);text-decoration:none}
.fg .best .hint{font-size:15px}
.fg .lowest a{text-shadow:0 0 8px rgba(109,255,138,.6)}
.who{margin:4px 0 10px;text-transform:uppercase;color:var(--phos)}
.who .y{color:var(--yellow)}
.who a{color:var(--cyan);cursor:pointer;text-decoration:underline}
.prompt label{color:var(--yellow);white-space:nowrap;text-shadow:0 0 6px rgba(255,210,31,.6)}
input[type=text],input[type=number]{background:transparent;border:0;outline:0;color:var(--yellow);font:24px var(--mono);
  caret-color:var(--yellow);text-transform:uppercase;width:100%;text-shadow:0 0 6px rgba(255,210,31,.5)}
input::placeholder{color:#7a6a2a;text-transform:uppercase}
.range{display:flex;align-items:center;gap:8px;color:var(--mute);text-transform:uppercase}
.range .box{display:flex;align-items:center;border:2px solid var(--yellow);background:#000;padding:6px 8px;width:118px}
.range .box b{color:var(--yellow);font-weight:400;margin-right:4px}
input[type=number]::-webkit-inner-spin-button{opacity:.4}

.chips{display:flex;gap:14px;flex-wrap:wrap;text-transform:uppercase;color:var(--phos)}
.chips label{display:flex;gap:6px;align-items:center;cursor:pointer;user-select:none}
.chips input[type=checkbox],.fg input[type=checkbox]{appearance:none;-webkit-appearance:none;width:18px;height:18px;margin:0;border:2px solid var(--phos);
  background:#000;display:grid;place-content:center;cursor:pointer}
.chips input[type=checkbox]:checked::before,.fg input[type=checkbox]:checked::before{content:"X";color:var(--ink);font:18px/1 var(--mono)}
.chips input[type=checkbox]:checked,.fg input[type=checkbox]:checked{background:var(--phos)}
.chips select{background:#000;color:var(--yellow);border:2px solid var(--yellow);font:20px var(--mono);padding:0 4px}

button{font:400 26px/1 var(--display);letter-spacing:.06em;text-transform:uppercase;cursor:pointer;
  background:var(--yellow);color:var(--navy2);border:3px solid var(--ink);padding:10px 22px 8px;
  box-shadow:4px 4px 0 var(--ink);transform:skewX(-6deg);transition:transform .05s, box-shadow .05s}
button:hover{background:#ffe066}
button:active{transform:skewX(-6deg) translate(3px,3px);box-shadow:1px 1px 0 var(--ink)}
button:disabled{filter:grayscale(.6) brightness(.8);cursor:wait}
.fkey{font:18px var(--mono);background:var(--navy2);color:var(--yellow);padding:0 5px;margin-right:6px;vertical-align:3px}

/* ---- results ledger ---- */
#best{margin:22px 0 22px;overflow-x:auto}
table{border-collapse:collapse;width:100%;font-size:21px}
th,td{padding:6px 12px;text-align:left;white-space:nowrap;border-bottom:1px dashed var(--line)}
th{color:var(--navy2);background:var(--yellow);font:400 18px var(--display);letter-spacing:.1em;text-transform:uppercase;padding:8px 12px}
td:first-child{color:var(--cyan);text-transform:uppercase}
td a{text-decoration:none}
td.low a{color:var(--good);text-shadow:0 0 8px rgba(109,255,138,.6)}
td.low a::before{content:"★ "}
td.none{color:#3d4c96}

/* ---- shop windows ---- */
.grid{display:grid;grid-template-columns:repeat(var(--cols,5),minmax(0,1fr));gap:14px}
@media (max-width:1150px){.grid{grid-template-columns:repeat(3,minmax(0,1fr))}}
@media (max-width:820px){.grid{grid-template-columns:repeat(2,minmax(0,1fr))}}
@media (max-width:620px){.grid{grid-template-columns:1fr}}
.col{background:var(--screen);border:3px solid #000;border-radius:10px;display:flex;flex-direction:column;min-height:150px;
  box-shadow:0 0 0 3px #1b2a74, 0 12px 26px rgba(0,0,0,.45);overflow:hidden}
.col h2{margin:0;padding:8px 12px 6px;background:var(--yellow);color:var(--navy2);display:flex;justify-content:space-between;align-items:baseline;gap:8px}
.col h2 .nm{font:400 22px var(--display);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;letter-spacing:.05em;text-transform:uppercase}
.col h2 a{font:17px var(--mono);white-space:nowrap;flex:none;text-transform:uppercase;text-decoration:none;background:var(--navy2);color:var(--yellow);padding:0 6px}
.col h2 a:hover{background:#000}
.stat{display:flex;justify-content:space-between;font-size:17px;color:var(--mute);padding:4px 12px;border-bottom:1px dashed var(--line);text-transform:uppercase}
.list{padding:4px 6px 8px;display:flex;flex-direction:column;max-height:72vh;overflow:auto;scrollbar-color:var(--yellow) #000}
.it{display:grid;grid-template-columns:46px 1fr auto;column-gap:10px;row-gap:2px;padding:7px 6px;text-decoration:none;color:inherit;border-bottom:1px dotted #17256a}
.it:hover{background:var(--yellow);color:var(--navy2)}
.it:hover .m,.it:hover .p{color:var(--navy2);text-shadow:none}
.it img{grid-row:span 3;align-self:start;width:46px;height:62px;object-fit:cover;background:#0d1a55;border:1px solid #000;image-rendering:auto}
.it .t{font-size:20px;text-transform:uppercase;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.it .m{font-size:16px;color:var(--mute);margin-top:4px;display:flex;gap:6px;flex-wrap:wrap;align-items:center;text-transform:uppercase}
.it .p{grid-column:3;grid-row:1;line-height:1;font-size:26px;color:var(--yellow);white-space:nowrap;text-shadow:0 0 8px rgba(255,210,31,.45)}
/* rental-sticker format tags */
.tag{font:400 13px/1 var(--display);letter-spacing:.08em;padding:3px 6px 2px;color:#fff;transform:rotate(-3deg);
  border:2px solid #000;box-shadow:2px 2px 0 #000;text-transform:uppercase}
.t-Vinyl{background:repeating-radial-gradient(circle at 50% 50%,#111 0 2px,#2a2a2a 2px 3px);color:#fff}
.t-Cassette{background:#f2e6c9;color:#3a2a10;border-style:dashed}
.t-CD{background:linear-gradient(135deg,#f4f6fb,#b9c0d0 45%,#eef1f7 60%,#aab2c4);color:#000}
.t-DVD{background:var(--dvd)}.t-Blu-ray{background:var(--br)}.t-4K{background:var(--k4);color:#000}.t-Other{background:#555}
.oos{opacity:.45}.oos .p{text-decoration:line-through}
.err{color:var(--bad);padding:12px;text-transform:uppercase;font-size:18px}
.err::before{content:"!! SYSTEM ERROR !!";display:block;color:#000;background:var(--bad);padding:0 6px;margin-bottom:6px;width:max-content}
.empty{color:var(--mute);padding:14px 12px;text-transform:uppercase}
.empty::before{content:"> ";color:var(--yellow)}
.spin{display:inline-block;font:20px var(--mono);color:var(--navy2);animation:blink 1s steps(1) infinite}
.spin::after{content:"LOADING ▮"}
.loading{padding:0;color:var(--cyan);text-transform:uppercase}
.loading::after{content:"▮";animation:blink 1s steps(1) infinite;color:var(--yellow)}
@keyframes blink{50%{opacity:0}}
.splash{padding:28px 6px 8px;color:var(--phos);text-transform:uppercase;font-size:22px;line-height:1.35}
.splash .y{color:var(--yellow)}

/* ---- status bar ---- */
.status{position:fixed;left:0;right:0;bottom:0;z-index:40;background:#000;border-top:3px solid var(--yellow);
  color:var(--yellow);font-size:19px;text-transform:uppercase;letter-spacing:.06em}
.status-in{max-width:1400px;margin:auto;padding:5px 20px;display:flex;gap:22px;flex-wrap:wrap}
.status .k{background:var(--yellow);color:#000;padding:0 5px;margin-right:4px}
.status .r{margin-left:auto}
.dot{color:var(--good);animation:blink 1.4s steps(1) infinite}

/* ---- edition stickers, trade-in, panels ---- */
.ed{font:400 12px/1 var(--display);letter-spacing:.06em;text-transform:uppercase;padding:3px 5px 2px;border:2px solid #000;
  box-shadow:2px 2px 0 #000;background:#c0c6d8;color:#000;transform:rotate(2deg)}
.ed.Steelbook{background:linear-gradient(135deg,#eef1f6,#9aa3b5 55%,#e4e8ef)}
.ed.Limited{background:#b678ff}.ed.Box{background:#ff8a3d}.ed.Region{background:#000;color:var(--cyan);border-color:var(--cyan)}
.sell{grid-column:2/4;font-size:16px;color:var(--good);text-transform:uppercase;margin-top:-2px}
.sell b{font-weight:400;color:#000;background:var(--good);padding:0 4px;margin-right:4px}
.it:hover .sell{color:var(--navy2)}
.row2{display:flex;gap:14px;flex-wrap:wrap;align-items:center;flex-basis:100%;text-transform:uppercase;color:var(--mute)}
.row2 .lbl{color:var(--cyan)}
.btn2{font:20px var(--mono);text-transform:uppercase;background:#000;color:var(--yellow);border:2px solid var(--yellow);
  padding:3px 10px;cursor:pointer;box-shadow:none;transform:none}
.btn2:hover{background:var(--yellow);color:#000}
.btn2.red{color:var(--bad);border-color:var(--bad)}.btn2.red:hover{background:var(--bad);color:#000}
.panels{display:grid;grid-template-columns:2fr 1fr;gap:18px;margin-top:24px}
@media (max-width:1000px){.panels{grid-template-columns:1fr}}
.panel h3{margin:0 0 10px;font:400 26px var(--display);letter-spacing:.06em;color:var(--yellow);text-transform:uppercase;
  display:flex;justify-content:space-between;align-items:center;gap:10px;flex-wrap:wrap}
.panel h3 small{font:18px var(--mono);color:var(--mute);letter-spacing:.04em}
.wl{width:100%;font-size:19px}
.wl td{white-space:normal;vertical-align:top}
.wl .hit{color:var(--good)}
.wl input{width:80px;background:#000;border:1px solid var(--yellow);color:var(--yellow);font:19px var(--mono);padding:0 4px}
.spark{display:block;margin-top:2px}
.alerts{margin:0 0 12px;padding:0;list-style:none}
.alerts li{background:var(--good);color:#000;padding:4px 10px;margin-bottom:6px;text-transform:uppercase;font-size:19px;
  display:flex;gap:10px;justify-content:space-between}
.alerts li.seen{background:#123b2a;color:var(--good)}
.alerts a{color:inherit}
.sellbox{display:flex;gap:8px;margin-bottom:10px}
.sellbox .prompt{flex:1}
.sellres{font-size:19px;text-transform:uppercase}
.sellres .card{border-bottom:1px dashed var(--line);padding:6px 0}
.sellres .v{color:var(--good)}
.hint{color:var(--mute);font-size:17px;text-transform:uppercase}

.grid.c5 .it .p{font-size:23px}
.grid.narrow .it{grid-template-columns:46px 1fr}
.grid.narrow .it .p{grid-column:2;grid-row:auto;order:-1}
.grid.narrow .it img{order:-2}
.grid.narrow .sell{grid-column:2}
@media (max-width:700px){.logo{font-size:38px}.sign-right{display:none}body{font-size:18px}}
@media (prefers-reduced-motion:reduce){*{animation:none!important}}
</style></head><body>
<div class="sign"><div class="sign-in">
  <h1 class="logo">Film Price<span>Checker</span></h1>
  <div class="tagline">Video &amp; <b>Music</b></div>
  <div class="sign-right">Price lookup system<br>v__VERSION__ · Be kind, rewind</div>
</div></div>

<div class="wrap">
  <div class="crt">
    <div class="crt-title"><span>&gt;&gt; Stock enquiry terminal 01</span>
      <span class="dept" title="Which department to search"><span>Dept:</span>
        <button type="button" data-media="film" class="on">Video</button><button type="button" data-media="music">Music</button></span>
      <span id="clock"></span></div>
    <form id="f">
      <div class="prompt"><select id="mode" title="What to search by">
          <option value="title">TITLE&gt;</option><option value="director">DIRECTOR&gt;</option><option value="actor">ACTOR&gt;</option></select>
        <input type="text" id="q" placeholder="Enter title, or blank to browse price range" autofocus autocomplete="off"></div>
      <div class="range">
        <div class="box"><b>£</b><input type="number" id="pmin" placeholder="Min" min="0" step="0.01"></div>
        <span>to</span>
        <div class="box"><b>£</b><input type="number" id="pmax" placeholder="Max" min="0" step="0.01"></div>
      </div>
      <button id="go"><span class="fkey">F1</span>Search</button>
      <div class="chips" style="flex-basis:100%">
        <label class="film-only"><input type="checkbox" name="fmt" data-media="film" value="DVD" checked>DVD</label>
        <label class="film-only"><input type="checkbox" name="fmt" data-media="film" value="Blu-ray" checked>Blu-ray</label>
        <label class="film-only"><input type="checkbox" name="fmt" data-media="film" value="4K" checked>4K UHD</label>
        <label class="music-only"><input type="checkbox" name="fmt" data-media="music" value="CD" checked>CD</label>
        <label class="music-only"><input type="checkbox" name="fmt" data-media="music" value="Vinyl" checked>Vinyl</label>
        <label class="music-only"><input type="checkbox" name="fmt" data-media="music" value="Cassette" checked>Cassette</label>
        <label title="Only keep results whose title contains every word you typed"><input type="checkbox" id="strict" checked>Exact-ish match</label>
        <label><input type="checkbox" id="instock">In stock only</label>
        <label title="More pages = more results, but slower">Pages <select id="pages"><option>1</option><option selected>2</option><option>3</option><option>5</option><option>10</option></select></label>
        <label title="How many shops side by side">Columns <select id="cols"><option>3</option><option>4</option><option selected>5</option></select></label>
      </div>
      <div class="chips row2">
        <span class="lbl">Edition:</span>
        <label class="film-only"><input type="checkbox" name="ed" value="Steelbook">Steelbook</label>
        <label><input type="checkbox" name="ed" value="Limited"><span class="film-only">Limited / Collector's</span><span class="music-only">Limited / Deluxe</span></label>
        <label><input type="checkbox" name="ed" value="Box set">Box set</label>
        <span class="lbl film-only">Region:</span>
        <select id="region" class="film-only"><option value="">Any</option><option>Region B/2</option><option>Region free</option><option>Region A/1</option></select>
        <span class="hint" style="margin-left:auto">Editions<span class="film-only"> &amp; regions</span> only match when the listing says so</span>
        <button class="btn2" id="watchbtn" type="button" title="Save this search to your watchlist with a target price">+ Watch</button>
      </div>
    </form>
    <div id="best"><div class="splash">&gt; System ready.<br>&gt; Checking <span class="y">CeX · musicMagpie · eBay · HMV · Amazon</span><br>&gt; Enter a title and press <span class="y">F1</span> or Enter._</div></div>
  </div>

  <div class="grid" id="grid" style="margin-top:24px"></div>

  <div class="panels">
    <div class="crt panel">
      <h3>Watchlist <small id="wl-meta"></small>
        <span style="display:flex;gap:8px;align-items:center">
          <small>Check every</small>
          <select id="wl-int" class="btn2" style="padding:1px 4px"><option value="1">1h</option><option value="3">3h</option><option value="6">6h</option><option value="12">12h</option><option value="24">24h</option></select>
          <button class="btn2" type="button" id="wl-check">Check now</button></span></h3>
      <ul class="alerts" id="alerts"></ul>
      <div id="wl"></div>
      <p class="hint">Alerts pop up in Windows while the app is running. Use "Start with Windows" (see help) so it keeps checking in the background.</p>
    </div>
    <div class="crt panel">
      <h3>Sell price <small>trade-in</small></h3>
      <form class="sellbox" id="sellf">
        <div class="prompt"><label for="bc">BARCODE&gt;</label><input type="text" id="bc" inputmode="numeric" placeholder="5051..." autocomplete="off"></div>
        <button class="btn2" type="submit">Check</button>
      </form>
      <div id="sellres" class="sellres"><span class="hint">Type the barcode from the back of the case to see what CeX will pay. CeX trade-in prices also show on CeX results.</span></div>
    </div>
  </div>
</div>

<div class="status"><div class="status-in">
  <span><span class="k">F1</span>Search</span><span><span class="k">ESC</span>Clear</span>
  <span id="st-msg">Ready</span>
  <span class="r"><span class="dot">●</span> Online · 5 suppliers</span>
  <span><button class="btn2 red" id="quit" type="button" title="Stop the app">Quit</button></span>
</div></div>
<script>
const R = __RETAILERS__;
const gbp = n => '£' + Number(n).toFixed(2);
const esc = s => String(s ?? '').replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
// ---- department (Video / Music)
let MEDIA = 'film';
const MEDIA_FMTS = {film: ['DVD', 'Blu-ray', '4K'], music: ['CD', 'Vinyl', 'Cassette']};
const MODES = {film: [['title', 'TITLE>'], ['director', 'DIRECTOR>'], ['actor', 'ACTOR>']],
               music: [['title', 'TITLE>'], ['artist', 'ARTIST>']]};
const PLACEHOLDERS = {
  film: {title: 'Enter title, or blank to browse price range', director: 'Director name, e.g. Christopher Nolan', actor: 'Actor name, e.g. Tom Cruise'},
  music: {title: 'Artist and/or album, e.g. Oasis Definitely Maybe', artist: 'Band or artist, e.g. Oasis'}};
const selectedFormats = () => { const f = [...document.querySelectorAll(`input[name=fmt][data-media=${MEDIA}]:checked`)].map(x => x.value);
  return f.length ? f : MEDIA_FMTS[MEDIA]; };
const selectedEditions = () => [...document.querySelectorAll('input[name=ed]:checked')]
  .filter(x => MEDIA === 'film' || !x.closest('.film-only')).map(x => x.value);
const selectedRegion = () => MEDIA === 'film' ? document.getElementById('region').value : '';
const noun = (n) => (MEDIA === 'music' ? 'album' : 'film') + (n === 1 ? '' : 's');
function setMedia(m, quiet) {
  MEDIA = m === 'music' ? 'music' : 'film';
  document.body.classList.toggle('music', MEDIA === 'music'); document.body.classList.toggle('film', MEDIA === 'film');
  document.querySelectorAll('.dept button').forEach(b => b.classList.toggle('on', b.dataset.media === MEDIA));
  const sel = document.getElementById('mode'), was = sel.value;
  sel.innerHTML = MODES[MEDIA].map(([v, l]) => `<option value="${v}">${esc(l)}</option>`).join('');
  sel.value = MODES[MEDIA].some(([v]) => v === was) ? was : 'title';
  document.getElementById('q').placeholder = PLACEHOLDERS[MEDIA][sel.value];
  try { localStorage.setItem('fpc-media', MEDIA); } catch (e) {}
  if (!quiet) {
    document.getElementById('grid').innerHTML = '';
    document.getElementById('best').innerHTML = `<div class="splash">&gt; ${MEDIA === 'music' ? 'Music' : 'Video'} department.<br>&gt; Enter ${MEDIA === 'music' ? 'an artist or album' : 'a title'} and press <span class="y">F1</span> or Enter._</div>`;
    document.getElementById('st-msg').textContent = (MEDIA === 'music' ? 'Music' : 'Video') + ' department';
    document.getElementById('q').focus();
  }
}
document.querySelectorAll('.dept button').forEach(b => b.addEventListener('click', () => setMedia(b.dataset.media)));
try { setMedia(localStorage.getItem('fpc-media') || 'film', true); } catch (e) { setMedia('film', true); }

document.getElementById('f').addEventListener('submit', async e => {
  e.preventDefault();
  const q = document.getElementById('q').value.trim();
  const mode = document.getElementById('mode').value;
  if (mode !== 'title') { if (q) personSearch(q, mode); return; }
  const pmin = document.getElementById('pmin').value, pmax = document.getElementById('pmax').value;
  if (!q && !pmin && !pmax) { alert('Type a title, or set a price range to browse everything in it.'); return; }
  const pages = document.getElementById('pages').value, instock = document.getElementById('instock').checked ? 1 : 0;
  const fmts = selectedFormats(), eds = selectedEditions(), region = selectedRegion();
  const strict = document.getElementById('strict').checked;
  const btn = document.getElementById('go'); btn.disabled = true;
  document.getElementById('best').innerHTML = '';
  const grid = document.getElementById('grid');
  grid.innerHTML = R.map(([k, n]) => `<div class="col" id="c-${k}"><h2><span class="nm">${n}</span><span class="spin"></span></h2><div class="loading" style="padding:14px 12px">Contacting supplier</div></div>`).join('');
  document.getElementById('st-msg').textContent = 'Searching ' + (q ? '"' + q + '"' : 'price range') + '...';
  const all = {};
  await Promise.all(R.map(async ([k, n]) => {
    const p = new URLSearchParams({q, formats: fmts.join(','), strict: strict ? 1 : 0, retailers: k, pmin, pmax, pages, instock,
      editions: eds.join(','), region, media: MEDIA});
    let res;
    try { res = (await (await fetch('/api/search?' + p)).json())[k]; }
    catch (err) { res = {ok:false, error:String(err), items:[], search_url:'#'}; }
    all[k] = res;
    const el = document.getElementById('c-' + k);
    let body;
    if (!res.ok) body = `<div class="err">${esc(res.error)}</div>`;
    else if (!res.items.length) body = `<div class="empty">No titles found</div>`;
    else body = '<div class="list">' + res.items.map(i => `
      <a class="it ${i.in_stock === false ? 'oos' : ''}" href="${esc(i.url)}" target="_blank" rel="noopener">
        ${i.image ? `<img src="${esc(i.image)}" loading="lazy" referrerpolicy="no-referrer">` : '<img alt="">'}
        <div style="min-width:0"><div class="t">${esc(i.title)}</div>
          <div class="m"><span class="tag t-${esc(i.format)}">${i.format === '4K' ? '4K UHD' : esc(i.format)}</span>${stickers(i)}${[i.condition, i.in_stock === false ? 'out of stock' : ''].filter(Boolean).map(esc).join(' · ')}</div></div>
        <div class="p">${gbp(i.price)}</div>${sellLine(i)}</a>`).join('') + '</div>';
    el.innerHTML = `<h2><span class="nm">${n}</span><a href="${esc(res.search_url)}" target="_blank" rel="noopener" title="Open this search on the shop's site">↗</a></h2>
      <div class="stat"><span>${res.ok ? String(res.total ?? res.items.length).padStart(3, '0') + ' titles' : 'Offline'}</span><span>${((res.ms ?? 0)/1000).toFixed(1)}s</span></div>` + body;
    renderBest(all, fmts);
  }));
  btn.disabled = false;
  document.getElementById('st-msg').textContent = 'Search complete';
});
function stickers(i) {
  const cls = e => e === 'Box set' ? 'Box' : e;
  return (i.editions || []).map(e => `<span class="ed ${cls(e)}">${esc(e)}</span>`).join('') +
         (i.region ? `<span class="ed Region">${esc(i.region)}</span>` : '');
}
function sellLine(i) {
  if (!i.sell || (i.sell.cash == null && i.sell.voucher == null)) return '';
  const parts = [];
  if (i.sell.cash != null) parts.push(gbp(i.sell.cash) + ' cash');
  if (i.sell.voucher != null) parts.push(gbp(i.sell.voucher) + ' voucher');
  return `<div class="sell" title="What ${esc(i.sell.by)} pays if you sell it to them"><b>Sell</b>${parts.join(' / ')}</div>`;
}
const api = async (path, body) => {
  const r = await fetch(path, body === undefined ? {} : {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)});
  const j = await r.json(); if (!r.ok) throw new Error(j.error || r.status); return j;
};

// ---- director / actor search
const qInput = document.getElementById('q');
document.getElementById('mode').addEventListener('change', e => { qInput.placeholder = PLACEHOLDERS[MEDIA][e.target.value]; qInput.focus(); });
let FG = null;   // current filmography
async function personSearch(name, role, id) {
  const best = document.getElementById('best');
  document.getElementById('grid').innerHTML = '';
  best.innerHTML = `<div class="loading" style="padding:14px 6px">Looking up ${esc(name)}'s ${noun()}</div>`;
  document.getElementById('st-msg').textContent = 'Database lookup...';
  try {
    const d = await api('/api/person?' + new URLSearchParams({name, role, id: id || ''}));
    FG = d;
    const alts = d.alternates.length ? `<br><span class="hint">Not them? </span>` + d.alternates.map(p =>
      `<a onclick="personSearch(${esc(JSON.stringify(name))}, '${role}', '${p.id}')">${esc(p.label)}${p.description ? ' (' + esc(p.description) + ')' : ''}</a>`).join(' · ') : '';
    const head = `<div class="who">&gt; <span class="y">${esc(d.person.label)}</span>${d.person.description ? ' — ' + esc(d.person.description) : ''}
      · ${d.films.length} ${noun(d.films.length)} as ${role}${alts}</div>`;
    if (!d.films.length) { best.innerHTML = head + `<div class="empty">No ${noun()} on record for this ${role === 'artist' ? 'artist' : 'person in that role'}</div>`; return; }
    const cols = fmtCols();
    best.innerHTML = head + `<div style="display:flex;gap:10px;flex-wrap:wrap;margin-bottom:10px">
        <button class="btn2" type="button" onclick="checkFilms(true)">Check prices: ticked</button>
        <button class="btn2" type="button" onclick="checkFilms(false, 10)">Check newest 10</button>
        <button class="btn2" type="button" onclick="checkFilms(false, 9999)">Check all ${d.films.length}</button>
        <button class="btn2" type="button" onclick="document.querySelectorAll('.fgc').forEach(c => c.checked = !c.checked)">Invert ticks</button>
        <span class="hint" style="align-self:center">Click a title for full results · each ${noun(1)} checks all 5 shops</span></div>
      <table class="fg"><thead><tr><th></th><th>${MEDIA === 'music' ? 'Album' : 'Film'}</th><th>Year</th>${cols.map(c => `<th>${c === '4K' ? '4K UHD' : c}</th>`).join('')}</tr></thead><tbody>` +
      d.films.map((f, n) => `<tr id="fg-${n}"><td><input type="checkbox" class="fgc" data-n="${n}"></td>
        <td class="t" onclick="openFilm(${n})">${esc(f.title)}</td><td class="y">${f.year || ''}</td>
        ${cols.map(c => `<td class="best" data-f="${c}"><span class="hint">--</span></td>`).join('')}</tr>`).join('') + '</tbody></table>';
    document.getElementById('st-msg').textContent = d.films.length + ' films found';
  } catch (err) { best.innerHTML = `<div class="err">${esc(err.message)}</div>`; }
}
function fmtCols() { return selectedFormats(); }
// albums are searched as "artist album" - album titles alone ("Greatest Hits") are too vague
const workQuery = f => FG && FG.role === 'artist' ? `${FG.person.label} ${f.title}` : f.title;
function openFilm(n) {
  const f = FG.films[n];
  document.getElementById('mode').value = 'title'; qInput.placeholder = PLACEHOLDERS[MEDIA].title;
  qInput.value = workQuery(f); document.getElementById('f').requestSubmit();
}
let fgRun = 0;
async function checkFilms(tickedOnly, limit) {
  if (!FG) return;
  const run = ++fgRun;
  let idx = tickedOnly ? [...document.querySelectorAll('.fgc:checked')].map(c => +c.dataset.n) : FG.films.map((_, n) => n).slice(0, limit);
  if (!idx.length) { alert('Tick some films first.'); return; }
  if (idx.length > 25 && !confirm(`Check ${idx.length} films? That's a lot of searches and may take several minutes.`)) return;
  const cols = fmtCols(), names = Object.fromEntries(R);
  const eds = selectedEditions().join(',');
  const opts = {formats: cols.join(','), strict: 1, pages: 1, instock: 1, editions: eds, region: selectedRegion(), media: MEDIA,
                pmin: document.getElementById('pmin').value, pmax: document.getElementById('pmax').value};
  let done = 0;
  const one = async n => {
    const f = FG.films[n], row = document.getElementById('fg-' + n);
    row.querySelectorAll('.best').forEach(td => td.innerHTML = '<span class="loading"></span>');
    let res = {};
    try { res = await api('/api/search?' + new URLSearchParams({...opts, q: workQuery(f), year: MEDIA === 'film' ? (f.year || '') : ''})); } catch (e) {}
    if (run !== fgRun) return;
    const prices = [];
    row.querySelectorAll('.best').forEach(td => {
      let b = null;
      for (const [k, r] of Object.entries(res)) for (const i of (r.items || []))
        if (i.format === td.dataset.f && (!b || i.price < b.price)) b = {...i, shop: names[k]};
      td.innerHTML = b ? `<a href="${esc(b.url)}" target="_blank" rel="noopener">${gbp(b.price)}</a> <span class="hint">${esc(b.shop)}</span>` : '<span class="hint">none</span>';
      if (b) prices.push([td, b.price]);
    });
    if (prices.length) { const lo = Math.min(...prices.map(p => p[1])); prices.forEach(([td, p]) => td.classList.toggle('lowest', p === lo)); }
    document.getElementById('st-msg').textContent = `Checked ${++done} of ${idx.length} films`;
  };
  // two films at a time keeps it quick without hammering the shops
  const queue = [...idx];
  await Promise.all([0, 1].map(async () => { while (queue.length && run === fgRun) await one(queue.shift()); }));
  if (run === fgRun) document.getElementById('st-msg').textContent = 'Filmography check complete';
}

// ---- watchlist
const ago = t => { if (!t) return 'never'; const m = Math.round((Date.now()/1000 - t) / 60);
  return m < 1 ? 'just now' : m < 60 ? m + 'm ago' : m < 1440 ? Math.round(m/60) + 'h ago' : Math.round(m/1440) + 'd ago'; };
function spark(h) {
  if (!h || h.length < 2) return '';
  const p = h.map(x => x.price), lo = Math.min(...p), hi = Math.max(...p), w = 120, ht = 22;
  const pts = p.map((v, n) => `${(n/(p.length-1)*w).toFixed(1)},${(ht - 2 - (hi === lo ? 0.5 : (v-lo)/(hi-lo)) * (ht-4)).toFixed(1)}`).join(' ');
  return `<svg class="spark" width="${w}" height="${ht}" viewBox="0 0 ${w} ${ht}"><polyline fill="none" stroke="#7fe7ff" stroke-width="1.5" points="${pts}"/></svg>`;
}
let lastAlertCount = null;
function renderWatch(d) {
  document.getElementById('wl-int').value = String(d.interval_hours);
  document.getElementById('wl-meta').textContent = d.checking ? 'Checking: ' + d.checking + '...' : d.items.length + ' title' + (d.items.length === 1 ? '' : 's');
  const fresh = d.alerts.filter(a => !a.seen);
  document.getElementById('alerts').innerHTML = d.alerts.slice(0, 6).map(a => `<li class="${a.seen ? 'seen' : ''}">
      <span>★ ${esc(a.title)} — ${a.url ? `<a href="${esc(a.url)}" target="_blank" rel="noopener">${esc(a.msg)}</a>` : esc(a.msg)}</span><span>${ago(a.t)}</span></li>`).join('') +
    (d.alerts.length ? '<li class="seen" style="justify-content:flex-end"><button class="btn2" type="button" onclick="watchAct(\'alerts_clear\')">Clear alerts</button></li>' : '');
  if (fresh.length) { document.title = `(${fresh.length}) Price alert! - Film Price Checker`;
    document.getElementById('st-msg').textContent = '★ ' + fresh.length + ' new price alert' + (fresh.length > 1 ? 's' : ''); }
  else document.title = 'Film Price Checker';
  if (!d.items.length) { document.getElementById('wl').innerHTML = '<p class="hint">Nothing watched yet. Search for a title, set Max £ as your target, then press + Watch.</p>'; return; }
  document.getElementById('wl').innerHTML = `<table class="wl"><thead><tr><th>Title</th><th>Target</th><th>Best now</th><th>Checked</th><th></th></tr></thead><tbody>` +
    d.items.map(w => { const b = w.best, hit = b && b.price <= w.target;
      const filt = [...(w.formats || []), ...(w.editions || []), w.region].filter(Boolean).join(' · ');
      return `<tr><td>${esc(w.title)}<div class="hint">${esc(filt || 'any format')}</div></td>
        <td>£<input type="number" step="0.01" min="0" value="${w.target}" onchange="watchAct('target', {id:'${w.id}', target:this.value})"></td>
        <td class="${hit ? 'hit' : ''}">${b ? `<a href="${esc(b.url)}" target="_blank" rel="noopener">${gbp(b.price)}</a> <span class="hint">${esc(b.shop)} · ${esc(b.format)}</span>` : '<span class="hint">--</span>'}${spark(w.history)}</td>
        <td class="hint">${ago(w.last_checked)}</td>
        <td style="white-space:nowrap"><button class="btn2" type="button" onclick="watchAct('check', {id:'${w.id}'})">↻</button>
            <button class="btn2 red" type="button" onclick="if(confirm('Stop watching this title?'))watchAct('delete', {id:'${w.id}'})">✕</button></td></tr>`; }).join('') +
    '</tbody></table>';
}
async function watchAct(action, body = {}) {
  try { renderWatch(await api('/api/watch/' + action, body)); if (action === 'check') setTimeout(loadWatch, 4000); }
  catch (e) { alert(e.message); }
}
async function loadWatch() { try { const d = await api('/api/watch'); renderWatch(d);
  if (d.alerts.some(a => !a.seen) && document.hasFocus()) setTimeout(() => api('/api/watch/alerts_seen', {}), 8000); } catch (e) {} }
loadWatch(); setInterval(loadWatch, 30000);
document.getElementById('wl-int').addEventListener('change', e => watchAct('settings', {interval_hours: e.target.value}));
document.getElementById('wl-check').addEventListener('click', () => watchAct('check', {id: 'all'}));
document.getElementById('watchbtn').addEventListener('click', () => {
  const title = document.getElementById('q').value.trim();
  if (!title) { alert('Type the film title to watch first.'); return; }
  let target = document.getElementById('pmax').value;
  if (!target) target = prompt('Alert me when "' + title + '" is at or under (£):', '10');
  if (!target) return;
  watchAct('add', {title, target,
    formats: selectedFormats(), editions: selectedEditions(), region: selectedRegion(), media: MEDIA});
  document.getElementById('st-msg').textContent = 'Added to watchlist: ' + title;
});

// ---- sell price lookup
document.getElementById('sellf').addEventListener('submit', async e => {
  e.preventDefault();
  const out = document.getElementById('sellres'), bc = document.getElementById('bc').value.trim();
  out.innerHTML = '<div class="loading">Checking trade-in prices</div>';
  try {
    const d = await api('/api/sell?' + new URLSearchParams({barcode: bc}));
    const cex = d.cex.length ? d.cex.slice(0, 4).map(i => `<div class="card">${esc(i.title)}<br>
        ${i.sell ? `<span class="v">CeX pays ${gbp(i.sell.cash)} cash / ${gbp(i.sell.voucher)} voucher</span>` : ''}
        <span class="hint"> · sells for ${gbp(i.price)}</span></div>`).join('')
      : `<div class="card hint">${d.cex_error ? 'CeX: ' + esc(d.cex_error) : 'CeX doesn\'t list this barcode.'}</div>`;
    out.innerHTML = cex + `<div class="card"><a href="${esc(d.cex_sell_url)}" target="_blank" rel="noopener">CeX sell page ↗</a> ·
      <a href="${esc(d.musicmagpie_url)}" target="_blank" rel="noopener">musicMagpie quote ↗</a>
      <div class="hint">musicMagpie: paste the barcode on their page for an instant quote.</div></div>`;
    try { await navigator.clipboard.writeText(d.barcode); } catch (e) {}
  } catch (err) { out.innerHTML = `<div class="err">${esc(err.message)}</div>`; }
});

// ---- quit
document.getElementById('quit').addEventListener('click', async () => {
  if (!confirm('Stop Film Price Checker? Watchlist alerts pause until you open it again.')) return;
  try { await api('/api/quit', {}); } catch (e) {}
  document.body.innerHTML = '<div class="wrap"><div class="crt splash">&gt; System shut down.<br>&gt; Be kind, rewind._</div></div>';
});

// columns preference (remembered in this browser)
const colSel = document.getElementById('cols');
const applyCols = n => { const g = document.getElementById('grid'); g.style.setProperty('--cols', n); g.classList.toggle('narrow', +n >= 6); g.classList.toggle('c5', +n === 5); };
try { const saved = localStorage.getItem('fpc-cols2'); if (saved) colSel.value = saved; } catch (e) {}
applyCols(colSel.value);
colSel.addEventListener('change', () => { applyCols(colSel.value); try { localStorage.setItem('fpc-cols2', colSel.value); } catch (e) {} });

// clock, F1 / Esc keys
const clock = () => { const d = new Date(); document.getElementById('clock').textContent =
  d.toLocaleDateString('en-GB') + ' ' + d.toLocaleTimeString('en-GB', {hour: '2-digit', minute: '2-digit'}); };
clock(); setInterval(clock, 15000);
document.addEventListener('keydown', e => {
  if (e.key === 'F1') { e.preventDefault(); document.getElementById('f').requestSubmit(); }
  if (e.key === 'Escape') { ['q','pmin','pmax'].forEach(id => document.getElementById(id).value = '');
    document.getElementById('q').focus(); }
});
function renderBest(all, fmts) {
  const cols = fmts.length ? fmts : MEDIA_FMTS[MEDIA];
  const rows = R.map(([k, n]) => {
    const r = all[k]; const cells = cols.map(f => {
      if (!r) return null;
      const c = (r.items || []).filter(i => i.format === f && i.in_stock !== false);
      return c.length ? c.reduce((a, b) => a.price <= b.price ? a : b) : null;
    });
    return {k, n, cells, done: !!r};
  });
  const mins = cols.map((_, j) => Math.min(...rows.map(r => r.cells[j]?.price ?? Infinity)));
  document.getElementById('best').innerHTML = `<table><thead><tr><th>Cheapest in stock</th>${cols.map(c => `<th>${c === '4K' ? '4K UHD' : c}</th>`).join('')}</tr></thead><tbody>` +
    rows.map(r => `<tr><td>${r.n}</td>${r.cells.map((c, j) => !r.done ? '<td class="none"><span class="loading"></span></td>' :
      c ? `<td class="${c.price === mins[j] ? 'low' : ''}"><a href="${esc(c.url)}" target="_blank" rel="noopener">${gbp(c.price)}</a></td>` : '<td class="none">-- --</td>').join('')}</tr>`).join('') +
    '</tbody></table>';
}
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    server_version = "FilmPriceChecker"

    def log_message(self, fmt, *args):
        try:
            sys.stderr.write("[%s] %s\n" % (time.strftime("%H:%M:%S"), fmt % args))
        except Exception:
            pass

    def _send(self, code, body, ctype="application/json"):
        data = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _json(self, obj, code=200):
        return self._send(code, json.dumps(obj))

    def _allowed(self):
        """Only answer the app's own page: blocks other websites (and DNS-rebinding tricks)
        from driving the app or reading its data while it's running."""
        port = self.server.server_address[1]
        ok_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
        if self.headers.get("Host", "") not in ok_hosts:
            return False
        origin = self.headers.get("Origin")
        if origin and urllib.parse.urlparse(origin).netloc not in ok_hosts:
            return False
        return True

    def do_GET(self):
        if not self._allowed():
            return self._send(403, "forbidden", "text/plain")
        u = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(u.query)
        arg = lambda name, default="": (qs.get(name) or [default])[0].strip()
        if u.path == "/":
            return self._send(200, PAGE.replace("__RETAILERS__", json.dumps(RETAILERS)).replace("__VERSION__", VERSION),
                              "text/html; charset=utf-8")
        if u.path == "/api/ping":
            return self._json({"app": "film-price-checker", "version": VERSION})
        if u.path == "/api/search":
            q = arg("q")

            def num(name):
                try:
                    v = arg(name)
                    return float(v) if v else None
                except ValueError:
                    return None
            pmin, pmax = num("pmin"), num("pmax")
            if not q and pmin is None and pmax is None:
                return self._json({"error": "give a title or a price range"}, 400)
            formats = [f for f in arg("formats").split(",") if f in FORMAT_QUERY]
            media = arg("media") if arg("media") in MEDIA_FORMATS else "film"
            editions = [e for e in arg("editions").split(",") if e in dict(EDITION_RULES)]
            region = arg("region") if arg("region") in dict(REGION_RULES) else None
            wanted = [r for r in arg("retailers").split(",") if r in SEARCHERS] or list(SEARCHERS)
            year = int(num("year")) if num("year") else None
            return self._json(run_search(q, formats, arg("strict", "1") == "1", wanted, pmin, pmax,
                                         int(num("pages") or 1), arg("instock") == "1", editions, region, year,
                                         media))
        if u.path == "/api/person":
            try:
                return self._json(person_lookup(arg("name"), arg("role"), arg("id") or None))
            except ValueError as e:
                return self._json({"error": str(e)}, 400)
            except FilmDBError as e:
                return self._json({"error": f"film database (Wikidata) unavailable: {e} - try again shortly"}, 502)
            except Exception as e:
                return self._json({"error": f"film lookup failed: {type(e).__name__}: {e}"[:300]}, 502)
        if u.path == "/api/watch":
            return self._json(watch_api("get", {}))
        if u.path == "/api/sell":
            try:
                return self._json(sell_lookup(arg("barcode")))
            except ValueError as e:
                return self._json({"error": str(e)}, 400)
        self._send(404, "not found", "text/plain")

    def do_POST(self):
        if not self._allowed() or "application/json" not in self.headers.get("Content-Type", ""):
            return self._send(403, "forbidden", "text/plain")
        u = urllib.parse.urlparse(self.path)
        try:
            n = min(int(self.headers.get("Content-Length") or 0), 100_000)
            body = json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            return self._json({"error": "bad request"}, 400)
        if u.path.startswith("/api/watch/"):
            try:
                return self._json(watch_api(u.path.rsplit("/", 1)[1], body))
            except (ValueError, TypeError) as e:
                return self._json({"error": str(e)}, 400)
        if u.path == "/api/quit":
            self._json({"ok": True})
            threading.Thread(target=lambda: (time.sleep(0.5), os._exit(0)), daemon=True).start()
            return
        self._send(404, "not found", "text/plain")


# --------------------------------------------------------------------------- launcher helpers

def make_icon(path):
    """Write a small film-strip .ico (yellow + navy) without any image libraries."""
    import struct
    import zlib
    W = H = 64
    Y, N, D = (255, 210, 31, 255), (11, 31, 107, 255), (4, 11, 44, 255)
    px = []
    for y in range(H):
        row = []
        for x in range(W):
            c = Y
            if 18 <= y <= 45:
                c = N
                if (y in range(21, 26) or y in range(38, 43)) and (x % 9) in (2, 3, 4, 5):
                    c = Y
                if 28 <= y <= 35 and 8 <= x <= 55:
                    c = D if (x // 12) % 2 == 0 else (23, 54, 168, 255)
            if x < 3 or y < 3 or x >= W - 3 or y >= H - 3:
                c = D
            row.append(bytes(c))
        px.append(b"\x00" + b"".join(row))

    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xffffffff)
    png = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", W, H, 8, 6, 0, 0, 0)) +
           chunk(b"IDAT", zlib.compress(b"".join(px), 9)) + chunk(b"IEND", b""))
    ico = struct.pack("<HHH", 0, 1, 1) + struct.pack("<BBBBHHII", W, H, 0, 0, 1, 32, len(png), 22) + png
    with open(path, "wb") as f:
        f.write(ico)
    return path


def launch_target(background=False):
    """What a shortcut should run: the .exe itself, or pythonw.exe (no console window) + this script."""
    if getattr(sys, "frozen", False):
        return sys.executable, ("--background" if background else "")
    exe = sys.executable
    pyw = os.path.join(os.path.dirname(exe), "pythonw.exe")
    target = pyw if os.path.exists(pyw) else exe
    args = f'"{os.path.abspath(__file__)}"' + (" --background" if background else "")
    return target, args


def make_shortcut(folder_name, name, background=False):
    if os.name != "nt":
        raise RuntimeError("shortcuts are only supported on Windows")
    import subprocess
    icon = make_icon(os.path.join(APP_DIR, "filmprice.ico"))
    target, args = launch_target(background)
    ps = r"""
$dir = [Environment]::GetFolderPath($env:FPC_FOLDER)
$lnk = Join-Path $dir $env:FPC_NAME
$s = (New-Object -ComObject WScript.Shell).CreateShortcut($lnk)
$s.TargetPath = $env:FPC_TARGET; $s.Arguments = $env:FPC_ARGS
$s.WorkingDirectory = $env:FPC_WD; $s.IconLocation = $env:FPC_ICON
$s.Description = 'Film Price Checker'
$s.Save(); Write-Output $lnk
"""
    env = dict(os.environ, FPC_FOLDER=folder_name, FPC_NAME=name + ".lnk", FPC_TARGET=target,
               FPC_ARGS=args, FPC_WD=APP_DIR, FPC_ICON=icon)
    out = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps], env=env,
                         capture_output=True, text=True)
    if out.returncode != 0:
        raise RuntimeError(out.stderr.strip()[:300])
    return out.stdout.strip()


def remove_startup():
    import subprocess
    ps = "$p = Join-Path ([Environment]::GetFolderPath('Startup')) 'Film Price Checker (watchlist).lnk'; " \
         "if (Test-Path $p) { Remove-Item $p; 'removed' } else { 'not found' }"
    return subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, text=True).stdout.strip()


def build_exe():
    import subprocess
    py = sys.executable
    icon = make_icon(os.path.join(APP_DIR, "filmprice.ico"))
    steps = [
        [py, "-m", "pip", "install", "--upgrade", "pyinstaller"],
        [py, "-m", "PyInstaller", "--noconfirm", "--onedir", "--windowed", "--name", "FilmPriceChecker",
         "--icon", icon, "--collect-all", "curl_cffi", "--collect-all", "playwright",
         "--distpath", os.path.join(APP_DIR, "dist"), "--workpath", os.path.join(APP_DIR, "build"),
         "--specpath", os.path.join(APP_DIR, "build"), os.path.abspath(__file__)],
    ]
    for cmd in steps:
        print(">", " ".join(cmd))
        if subprocess.call(cmd) != 0:
            print("\nThat step failed - copy the error above and send it over.")
            return
    print("\nBuilt:", os.path.join(APP_DIR, "dist", "FilmPriceChecker", "FilmPriceChecker.exe"))
    print("Keep the whole FilmPriceChecker folder together. It uses your installed Edge/Chrome for blocked sites.")


def self_test():
    """Real lookups against the film database and every shop; prints a pass/fail report
    and saves it to selftest.txt so it can be shared."""
    lines = []

    def out(msg=""):
        print(msg)
        lines.append(msg)

    def check(name, fn):
        t0 = time.time()
        try:
            detail = fn()
            out(f"  PASS  {name}  ({time.time() - t0:.1f}s)  {detail}")
            return True
        except Exception as e:
            msg = str(e) if isinstance(e, RuntimeError) else f"{type(e).__name__}: {e}"
            out(f"  FAIL  {name}  ({time.time() - t0:.1f}s)  {msg[:220]}")
            return False

    out(f"Film Price Checker v{VERSION} self-test  {time.strftime('%Y-%m-%d %H:%M')}")
    out(f"Python {sys.version.split()[0]}  curl_cffi={'yes' if IMPERSONATE else 'NO'}  playwright={'yes' if HAVE_BROWSER else 'NO'}")
    out("\nFilm database (director / actor search):")

    def people():
        p = find_people("Christopher Nolan")
        assert p, "no matches returned"
        return f"top match: {p[0]['label']} - {p[0]['description']}"

    def directed():
        pid = find_people("Christopher Nolan")[0]["id"]
        films = filmography(pid, "director")
        titles = [x["title"] for x in films]
        assert any("Inception" in t for t in titles), f"Inception missing from {titles[:8]} [{FILM_ROUTE.get('P57')}]"
        return f"{len(films)} films, e.g. {', '.join(titles[:4])}  [route: {FILM_ROUTE.get('P57')}]"

    def acted():
        pid = find_people("Tom Cruise")[0]["id"]
        films = filmography(pid, "actor")
        titles = [x["title"] for x in films]
        assert any("Top Gun" in t or "Mission: Impossible" in t for t in titles), f"unexpected: {titles[:8]}"
        return f"{len(films)} films, e.g. {', '.join(titles[:4])}  [route: {FILM_ROUTE.get('P161')}]"

    def albums():
        pid = find_people("Oasis", "artist")[0]["id"]
        found = filmography(pid, "artist")
        titles = [x["title"] for x in found]
        assert any("Definitely Maybe" in t for t in titles), f"Definitely Maybe missing from {titles[:8]} [{FILM_ROUTE.get('P175')}]"
        return f"{len(found)} albums, e.g. {', '.join(titles[:4])}  [route: {FILM_ROUTE.get('P175')}]"

    check("find person", people)
    check("director filmography (Christopher Nolan)", directed)
    check("actor filmography (Tom Cruise)", acted)
    check("artist albums (Oasis)", albums)

    out("\nShops - video (searching 'Inception', 1 page each):")
    names = dict(RETAILERS)
    for key in SEARCHERS:
        def shop(key=key):
            r = run_search("inception", [], True, [key], pages=1)[key]
            if not r["ok"]:
                raise RuntimeError(r["error"])
            assert r["items"], "no results (blocked page or layout change?)"
            cheapest = min(r["items"], key=lambda i: i["price"])
            return f"{r['total']} results, cheapest {cheapest['format']} £{cheapest['price']:.2f}"
        check(names.get(key, key), shop)

    out("\nShops - music (searching 'Oasis Definitely Maybe' on CD / vinyl / cassette, 1 page each):")
    for key in SEARCHERS:
        def shop(key=key):
            r = run_search("oasis definitely maybe", [], True, [key], pages=1, media="music")[key]
            if not r["ok"]:
                raise RuntimeError(r["error"])
            assert r["items"], "no music results (blocked page or layout change?)"
            cheapest = min(r["items"], key=lambda i: i["price"])
            fmts = sorted({i["format"] for i in r["items"]})
            return f"{r['total']} results ({', '.join(fmts)}), cheapest {cheapest['format']} £{cheapest['price']:.2f}"
        check(names.get(key, key), shop)
    path = os.path.join(APP_DIR, "selftest.txt")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    out(f"\nSaved to {path}")


def already_running(port):
    try:
        import urllib.request
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/ping", timeout=2) as r:
            return json.load(r).get("app") == "film-price-checker"
    except Exception:
        return False


def main():
    global DEBUG, HEADED
    if sys.stdout is None or sys.stderr is None:  # started with pythonw / as the .exe: log to a file
        log = open(os.path.join(APP_DIR, "filmprice.log"), "a", encoding="utf-8", buffering=1)
        sys.stdout = sys.stderr = log
    ap = argparse.ArgumentParser(description="Film price checker")
    ap.add_argument("--port", type=int, default=PORT)
    ap.add_argument("--debug", action="store_true", help="save raw retailer HTML to ./debug/")
    ap.add_argument("--install", action="store_true",
                    help="install curl_cffi + Playwright/Chromium into THIS Python, then exit")
    ap.add_argument("--headed", action="store_true", help="show the fallback browser window (helps with stubborn bot checks)")
    ap.add_argument("--background", action="store_true", help="run without opening the browser (for the watchlist)")
    ap.add_argument("--shortcut", action="store_true", help="put a 'Film Price Checker' icon on your desktop")
    ap.add_argument("--startup", action="store_true", help="start quietly when Windows starts, so watchlist alerts keep working")
    ap.add_argument("--no-startup", action="store_true", help="stop starting with Windows")
    ap.add_argument("--build-exe", action="store_true", help="build a stand-alone FilmPriceChecker.exe (Windows)")
    ap.add_argument("--version", action="version", version=f"Film Price Checker {VERSION}")
    ap.add_argument("--selftest", action="store_true", help="test the film database and every shop, then exit")
    ap.add_argument("--cli", metavar="TITLE", help="search from the command line instead of starting the web UI")
    a = ap.parse_args()
    DEBUG, HEADED = a.debug, a.headed
    if a.install:
        import subprocess
        py = sys.executable
        print(f"Installing into: {py}\n")
        steps = [[py, "-m", "pip", "install", "--upgrade", "requests", "beautifulsoup4", "lxml", "curl_cffi", "playwright"],
                 [py, "-m", "playwright", "install", "chromium"]]
        for cmd in steps:
            print(">", " ".join(cmd))
            if subprocess.call(cmd) != 0:
                print("\nThat step failed - copy the error above and send it over.")
                return
        print("\nAll done. Now run:  python filmprice.py")
        return
    if a.shortcut or a.startup:
        try:
            if a.shortcut:
                print("Desktop icon created:", make_shortcut("Desktop", "Film Price Checker"))
            if a.startup:
                print("Will start with Windows:", make_shortcut("Startup", "Film Price Checker (watchlist)", background=True))
        except Exception as e:
            print("Couldn't create the shortcut:", e)
        return
    if a.no_startup:
        print("Startup shortcut:", remove_startup())
        return
    if a.build_exe:
        return build_exe()

    print(f"Film Price Checker v{VERSION}   Python: {sys.executable}")
    if not IMPERSONATE:
        print(f"curl_cffi not loaded:  {CURL_ERR}")
    if not HAVE_BROWSER:
        print(f"Playwright not loaded: {BROWSER_ERR}")
    if a.selftest:
        return self_test()
    if a.cli:
        res = run_search(a.cli, [], True, list(SEARCHERS))
        for k, n in RETAILERS:
            r = res[k]
            print(f"\n== {n} ==")
            if not r["ok"]:
                print("  ERROR:", r["error"])
            for i in r["items"][:8]:
                print(f"  £{i['price']:>7.2f}  [{i['format']:<7}] {i['title'][:80]}")
        return
    url = f"http://localhost:{a.port}"
    try:
        srv = ThreadingHTTPServer(("127.0.0.1", a.port), Handler)
    except OSError:
        if already_running(a.port):  # double-clicked again: just show the running app
            print("Already running - opening it.")
            if not a.background:
                import webbrowser
                webbrowser.open(url)
            return
        print(f"\n*** Port {a.port} is in use by something else.")
        print(f"*** Run on another port:  python filmprice.py --port {a.port + 1}\n")
        return
    threading.Thread(target=watch_loop, daemon=True).start()
    print(f"Film Price Checker running at {url}  (Ctrl+C or the QUIT button to stop)")
    if not a.background:
        import webbrowser
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
