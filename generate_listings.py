#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Izu Coastal Radar listings generator (Izutaiyo + Maple + Aoba)

Outputs:
  - listings.json
  - buildInfo.json

Notes:
- Izu Taiyo scrape portion is treated as "frozen" in behavior.
- Maple: onsen detection expanded; titles cleaned (no "No.xxxx").
- Aoba: stricter area filtering + more robust keyword detection; titles cleaned (no listing numbers).
- Condos/mansions are excluded from Maple/Aoba scan (house + land only).
"""

from __future__ import annotations

import csv
import datetime as dt
import json
import math
import os
import random
import re
import sys
import time
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple
from urllib.parse import urljoin, urlparse, parse_qs, quote

import requests
from bs4 import BeautifulSoup


# ----------------------------
# Global config
# ----------------------------

IZUTAIYO_BASE = "https://www.izutaiyo.co.jp"
MAPLE_BASE = "https://www.maple-h.co.jp"
AOBA_BASE = "https://www.aoba-resort.com"

OUT_LISTINGS = "listings.json"
OUT_BUILDINFO = "buildInfo.json"

# Conservative throttling
SLEEP_MIN = 0.20
SLEEP_MAX = 0.55

# Retries
DEFAULT_RETRIES = 3

# Debug toggles (optional)
AOBA_DEBUG = bool(int(os.environ.get("AOBA_DEBUG", "0")))
MAPLE_DEBUG = bool(int(os.environ.get("MAPLE_DEBUG", "0")))

# Currency FX defaults (overridden if you wire to a real source)
DEFAULT_FX_USDJPY = 155.0
DEFAULT_FX_CNYJPY = 20.0
DEFAULT_FX_SOURCE = "Manual"

HEADERS_DESKTOP = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/123.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
}

HEADERS_MOBILE = {
    "User-Agent": (
        "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) "
        "Version/17.0 Mobile/15E148 Safari/604.1"
    ),
    "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
}

# Target cities (canonical JP keys used by index.html)
ALLOWED_CITIES = {"下田市", "河津町", "東伊豆町", "南伊豆町"}

# EN mapping used for titleEn (index.html does final location display mapping)
CITY_EN_MAP = {
    "下田市": "Shimoda City",
    "河津町": "Kawazu Town",
    "東伊豆町": "Higashi-Izu Town",
    "南伊豆町": "Minami-Izu Town",
    "伊東市": "Ito City",
    "伊豆市": "Izu City",
    "静岡市": "Shizuoka City",
}

# Maple: include all areas except user excludes + extra excludes:
#   EXCLUDE 熱海～網代, 宇佐美～伊東, 川奈～富戸
#   EXCLUDE Izu City, Ito City, Shizuoka City
MAPLE_EXCLUDED_AREA_LABELS = {"熱海～網代", "宇佐美～伊東", "川奈～富戸"}
MAPLE_EXCLUDED_CITIES = {"伊豆市", "伊東市", "静岡市"}

# Aoba: only these cities
AOBA_ALLOWED_CITIES = {"下田市", "東伊豆町"}  # 東伊豆町 includes 賀茂郡東伊豆町 normalization


# ----------------------------
# Helpers
# ----------------------------

def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()

def sleep_jitter():
    time.sleep(random.uniform(SLEEP_MIN, SLEEP_MAX))

def clean_text(s: str) -> str:
    if not s:
        return ""
    s = re.sub(r"[ \t\r\f\v]+", " ", s)
    s = re.sub(r"\u3000+", " ", s)
    return s.strip()

def safe_int(s: Optional[str]) -> Optional[int]:
    if not s:
        return None
    s2 = re.sub(r"[^\d]", "", str(s))
    if not s2:
        return None
    try:
        return int(s2)
    except Exception:
        return None

def safe_float(s: Optional[str]) -> Optional[float]:
    if not s:
        return None
    s2 = re.sub(r"[^\d\.]", "", str(s))
    if not s2:
        return None
    try:
        return float(s2)
    except Exception:
        return None

def yen_to_int(text: str) -> Optional[int]:
    """
    Parse Japanese price strings like:
      - "1,980万円"
      - "2億3,000万円"
      - "3,500万"
      - "980万円"
      - "1,980,000円"
    Returns JPY int.
    """
    t = clean_text(text)
    if not t:
        return None

    # If already "円"
    if "円" in t and ("万" not in t and "億" not in t):
        v = safe_int(t)
        return v

    oku = 0
    man = 0

    m_oku = re.search(r"(\d+(?:\.\d+)?)\s*億", t)
    if m_oku:
        oku = float(m_oku.group(1))

    m_man = re.search(r"(\d+(?:\.\d+)?)\s*万", t)
    if m_man:
        man = float(m_man.group(1))
    else:
        # Some pages omit "万" but still indicate in context; ignore
        pass

    if oku == 0 and man == 0:
        # fallback: digits
        v = safe_int(t)
        return v

    total = int(round(oku * 100_000_000 + man * 10_000))
    return total if total > 0 else None

def sqm_from_text(text: str) -> Optional[float]:
    """
    Parse "123.45㎡" or "123.45 m2".
    """
    t = clean_text(text)
    if not t:
        return None
    m = re.search(r"(\d+(?:\.\d+)?)\s*(?:㎡|m2|m²)", t, flags=re.IGNORECASE)
    if not m:
        return None
    try:
        return float(m.group(1))
    except Exception:
        return None

def year_from_text(text: str) -> Optional[int]:
    t = clean_text(text)
    if not t:
        return None
    m = re.search(r"(19\d{2}|20\d{2})\s*年", t)
    if m:
        return int(m.group(1))
    return None

def compute_age(year_built: Optional[int]) -> Optional[float]:
    if not year_built:
        return None
    y = dt.datetime.now().year
    age = y - year_built
    return float(age) if age >= 0 else None

def request(session: requests.Session, url: str, *, headers: dict, retries: int = DEFAULT_RETRIES, timeout: int = 25) -> requests.Response:
    last_exc = None
    for i in range(retries):
        try:
            r = session.get(url, headers=headers, timeout=timeout)
            # Some sites respond 403 unless we retry with slightly different headers
            if r.status_code in (403, 429) and i < retries - 1:
                sleep_jitter()
                continue
            r.raise_for_status()
            return r
        except Exception as e:
            last_exc = e
            sleep_jitter()
    raise last_exc  # type: ignore

def normalize_city_jp(city: Optional[str]) -> Optional[str]:
    if not city:
        return None
    c = clean_text(city)
    if c == "賀茂郡東伊豆町":
        return "東伊豆町"
    return c


# ----------------------------
# FirstSeen persistence
# ----------------------------

def load_prev_first_seen(path: str = OUT_LISTINGS) -> Dict[str, str]:
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            j = json.load(f)
        out: Dict[str, str] = {}
        for it in j.get("listings", []):
            if isinstance(it, dict):
                _id = it.get("id")
                fs = it.get("firstSeen")
                if _id and fs:
                    out[str(_id)] = str(fs)
        return out
    except Exception:
        return {}

def apply_first_seen(listing_id: str, now_iso_str: str, prev_first_seen: Dict[str, str]) -> str:
    return prev_first_seen.get(listing_id, now_iso_str)


# ----------------------------
# Event records (Izu Taiyo)
# ----------------------------

def scrape_event_records(session: requests.Session) -> Dict[str, Tuple[str, str]]:
    """
    Scrape Izu Taiyo 新着物件一覧 for event type/date.
    Returns mapping: hpno -> (eventTypeJp, eventDate YYYY-MM-DD)
    """
    url = f"{IZUTAIYO_BASE}/new.php"
    records: Dict[str, Tuple[str, str]] = {}
    try:
        r = request(session, url, headers=HEADERS_DESKTOP, retries=4, timeout=25)
        soup = BeautifulSoup(r.text or "", "html.parser")
        # The list is typically a table; robust parse:
        for a in soup.select("a[href*='hpno=']"):
            href = a.get("href") or ""
            m = re.search(r"hpno=([A-Za-z0-9]+)", href)
            if not m:
                continue
            hpno = m.group(1)
            row = a.find_parent(["tr", "li", "div"])
            if not row:
                continue
            row_text = clean_text(row.get_text(" ", strip=True))
            # Look for date: YYYY/MM/DD
            mdate = re.search(r"(20\d{2})[\/\.-](\d{1,2})[\/\.-](\d{1,2})", row_text)
            if not mdate:
                continue
            y, mo, d = mdate.group(1), mdate.group(2).zfill(2), mdate.group(3).zfill(2)
            date_iso = f"{y}-{mo}-{d}"

            # Look for event type tokens
            # Common: 新規登録 価格変更 写真変更 情報更新 商談中 成約
            etype = None
            for key in ["新規登録", "価格変更", "写真変更", "情報更新", "商談中", "成約"]:
                if key in row_text:
                    etype = key
                    break
            if etype:
                records[hpno] = (etype, date_iso)
    except Exception:
        return records

    return records


# ----------------------------
# Izu Taiyo scraping (frozen behavior)
# ----------------------------

TOKUSEN_URL = f"{IZUTAIYO_BASE}/tokusen.php"

# Mobile search endpoints: querystring-driven
MOBILE_SEARCH_BASE = f"{IZUTAIYO_BASE}/msearch.php"

# Izu Taiyo: we exclude mansions/condos from final output by filtering propertyType == "mansion" at the end.
# NOTE: the rest of Izu Taiyo logic is unchanged from prior stable behavior.

CITY_SEARCH_QUERIES = [
    ("下田市", "眺望 海"),
    ("下田市", "海まで徒歩"),
    ("河津町", "眺望 海"),
    ("河津町", "海まで徒歩"),
    ("東伊豆町", "眺望 海"),
    ("東伊豆町", "海まで徒歩"),
    ("南伊豆町", "眺望 海"),
    ("南伊豆町", "海まで徒歩"),
]


def extract_tokusei_hpnos(session: requests.Session) -> List[str]:
    r = request(session, TOKUSEN_URL, headers=HEADERS_DESKTOP, retries=4, timeout=25)
    # IDs are in URL query like hpno=XXXX
    ids = re.findall(r"hpno=([A-Za-z0-9]+)", r.text or "")
    out = list(dict.fromkeys(ids))
    return out


def scan_mobile_search(session: requests.Session) -> Set[str]:
    """
    Scan Izu Taiyo mobile search for targeted city + keyword pairs.
    Returns a set of hpno IDs discovered.
    """
    found: Set[str] = set()
    for city, kw in CITY_SEARCH_QUERIES:
        # City appears as "city=" or free text; Izu Taiyo uses internal search param "free="
        # We keep the same pattern as the stable version: free query includes city + keyword.
        q = f"{city} {kw}"
        params = {"free": q}
        url = MOBILE_SEARCH_BASE + "?" + "&".join(f"{k}={quote(v)}" for k, v in params.items())
        print(f"  - Searching {city} ({'sea view' if '眺望' in kw else 'walk-to-sea'})...")
        try:
            r = request(session, url, headers=HEADERS_MOBILE, retries=3, timeout=25)
            hpnos = set(re.findall(r"hpno=([A-Za-z0-9]+)", r.text or ""))
            before = len(found)
            found |= hpnos
            print(f"    -> Added {len(found) - before} new properties.")
        except Exception:
            print(f"    -> Warning: mobile search failed for query: {q}")
        sleep_jitter()
    return found


def parse_izutaiyo_detail(session: requests.Session, hpno: str, event_records: Dict[str, Tuple[str, str]]) -> Tuple[Optional[dict], bool]:
    """
    Parse Izu Taiyo detail page into listing dict.
    Returns (item, ok).
    ok False means "failed to scrape" (counts as failure).
    item None means "filtered out" (not a match / no longer qualifies).
    """
    url = f"{IZUTAIYO_BASE}/d.php?hpno={hpno}"
    try:
        r = request(session, url, headers=HEADERS_DESKTOP, retries=4, timeout=25)
        soup = BeautifulSoup(r.text or "", "html.parser")
        page_text = clean_text(soup.get_text(" ", strip=True))

        # Determine property type (house/land/mansion)
        # Stable heuristic:
        property_type = None
        if "土地" in page_text and ("建物" not in page_text and "延床" not in page_text):
            property_type = "land"
        elif "マンション" in page_text:
            property_type = "mansion"
        else:
            property_type = "house"

        # Title (stable)
        title = None
        h = soup.find(["h1", "h2"])
        if h:
            title = clean_text(h.get_text(" ", strip=True))
        if not title:
            title = f"伊豆太陽物件 {hpno}"

        # City (stable): search for canonical tokens
        city = None
        for c in ["下田市", "河津町", "東伊豆町", "南伊豆町", "伊東市", "伊豆市"]:
            if c in page_text:
                city = c
                break

        # Price
        price_jpy = None
        # Prefer explicit "価格" line
        m_price = re.search(r"価格[:：]\s*([0-9,\.]+)\s*(億|万)?", page_text)
        if m_price:
            # rebuild a small token and parse
            token = m_price.group(1) + (m_price.group(2) or "")
            price_jpy = yen_to_int(token)
        if not price_jpy:
            # fallback: any "万円" near
            m2 = re.search(r"([0-9,]+)\s*万円", page_text)
            if m2:
                price_jpy = yen_to_int(m2.group(1) + "万円")

        # Areas
        land_sqm = None
        building_sqm = None
        # land: "土地面積"
        m_land = re.search(r"土地面積[:：]?\s*([0-9\.]+)\s*㎡", page_text)
        if m_land:
            land_sqm = safe_float(m_land.group(1))
        m_bld = re.search(r"(?:建物面積|延床面積)[:：]?\s*([0-9\.]+)\s*㎡", page_text)
        if m_bld:
            building_sqm = safe_float(m_bld.group(1))

        # Year built
        year_built = year_from_text(page_text)
        age = compute_age(year_built)

        # Sea view scoring (stable heuristic)
        sea_view_score = 0
        if "眺望" in page_text and "海" in page_text:
            sea_view_score = 4
        if "海まで徒歩" in page_text or "海まで" in page_text and "徒歩" in page_text:
            sea_view_score = max(sea_view_score, 3)

        # Image: try og:image
        img = None
        og = soup.find("meta", attrs={"property": "og:image"})
        if og and og.get("content"):
            img = og.get("content")
        if img and img.startswith("/"):
            img = urljoin(IZUTAIYO_BASE, img)

        # Event
        event_type, event_date = event_records.get(hpno, (None, None))

        item = {
            "id": f"izutaiyo-{hpno}",
            "source": "Izu Taiyo",
            "sourceUrl": url,
            "title": title,
            "titleEn": None,
            "titleCn": None,
            "propertyType": property_type,
            "city": city,
            "priceJpy": price_jpy,
            "landSqm": land_sqm,
            "buildingSqm": building_sqm,
            "yearBuilt": year_built,
            "age": round(age, 1) if age is not None else 0,
            "lastUpdated": None,
            "seaViewScore": sea_view_score,
            "imageUrl": img,
            "highlightTags": [],
            "eventTypeJp": event_type,
            "eventDate": event_date,
        }

        return item, True

    except Exception:
        return None, False


# ----------------------------
# Maple scraping
# ----------------------------

MAPLE_LISTING_TYPES = {
    "house": "house",
    "land": "estate",
}

# Maple keyword rules
MAPLE_SEA_KEYWORDS = ["海", "オーシャン", "海望", "海を望", "海一望", "相模湾", "駿河湾"]
MAPLE_WALK_KEYWORDS = ["海まで徒歩", "海へ徒歩", "海まで", "徒歩", "歩", "ビーチ", "海岸"]

def _maple_has_onsen(text: str) -> bool:
    """Detect onsen availability on Maple pages.

    Maple uses multiple phrasings:
      - 温泉：有/無 (explicit field)
      - 温泉付き / 温泉付 / 温泉権 / 温泉引込(み)
    We also guard against common negatives (温泉なし/無/不可).
    """
    t = text or ""

    # Fast negative guard
    if any(ng in t for ng in ["温泉なし", "温泉無", "温泉無し", "温泉不可", "温泉なしです"]):
        return False

    # Prefer explicit field like 温泉：有/無
    m = re.search(r"温泉\s*[:：]\s*([^\s　]+)", t)
    if m:
        v = m.group(1)
        if any(x in v for x in ["有", "あり", "○", "可"]):
            return True
        if any(x in v for x in ["無", "なし", "×", "不可"]):
            return False

    # Common affirmative patterns
    if any(k in t for k in ["温泉付き", "温泉付", "温泉権", "温泉引込", "温泉引き込み", "温泉引込み"]):
        return True

    # Fallback keyword (older phrasing)
    return "温泉" in t and any(x in t for x in ["あり", "有", "源泉", "かけ流し", "掛け流し", "引湯", "引き湯"])

def _maple_sea_view_and_walk(text: str) -> Tuple[bool, bool]:
    t = text or ""
    sea = any(k in t for k in MAPLE_SEA_KEYWORDS) or ("眺望" in t and "海" in t)
    walk = any(k in t for k in MAPLE_WALK_KEYWORDS) and ("海" in t or "ビーチ" in t or "海岸" in t)
    return sea, walk

def _maple_pick_image_url(soup: BeautifulSoup, base_url: str) -> Optional[str]:
    og = soup.find("meta", attrs={"property": "og:image"})
    if og and og.get("content"):
        u = og.get("content")
        if u.startswith("/"):
            u = urljoin(base_url, u)
        return u
    img = soup.select_one("img")
    if img and img.get("src"):
        u = img.get("src")
        if u.startswith("/"):
            u = urljoin(base_url, u)
        return u
    return None

def _maple_parse_city(page_text: str) -> Optional[str]:
    t = page_text or ""
    # Canonical first
    for c in ["下田市", "河津町", "東伊豆町", "南伊豆町", "伊東市", "伊豆市", "静岡市"]:
        if c in t:
            return c
    # Some Maple pages include 郡 prefix
    if "賀茂郡東伊豆町" in t:
        return "東伊豆町"
    return None

def parse_maple_detail_page(session: requests.Session, detail_url: str, property_type: str) -> Tuple[Optional[dict], bool]:
    try:
        r = request(session, detail_url, headers=HEADERS_DESKTOP, retries=4, timeout=25)
        soup = BeautifulSoup(r.text or "", "html.parser")
        page_text = clean_text(soup.get_text(" ", strip=True))

        # City detection + normalization
        city = normalize_city_jp(_maple_parse_city(page_text))

        # Apply city excludes (new requirement)
        if city in MAPLE_EXCLUDED_CITIES:
            return None, True

        # Sea / walk filters
        sea_view, walk_to_sea = _maple_sea_view_and_walk(page_text)
        if not (sea_view or walk_to_sea):
            return None, True

        # Derive ID from URL path
        m = re.search(r"/estate_db/(\d+)", detail_url)
        maple_id = m.group(1) if m else str(abs(hash(detail_url)))

        # Area label (for excluded Maple regional labels) – often present in list pages; here do best-effort
        # If page text contains any excluded area label, drop
        for lbl in MAPLE_EXCLUDED_AREA_LABELS:
            if lbl in page_text:
                return None, True

        # Title: prefer a real heading, but skip the common "MENU" artifact
        def _pick_maple_title() -> str:
            for tag in soup.find_all(["h1", "h2", "h3"]):
                t = clean_text(tag.get_text(" ", strip=True))
                if not t:
                    continue
                up = t.strip().upper()
                if up == "MENU":
                    continue
                if "MENU" in up and len(t) <= 8:
                    continue
                return t
            return ""

        raw_title = _pick_maple_title()

        type_jp = {"house": "戸建", "land": "土地"}.get(property_type, "物件")
        type_en = {"house": "House", "land": "Land"}.get(property_type, "Listing")

        title_city = city or "Maple"
        title = raw_title if raw_title else f"{title_city} {type_jp}"

        title_en_city = CITY_EN_MAP.get(city, city) if city else "Maple"
        title_en = f"{title_en_city} {type_en}"

        # Price
        price_jpy = yen_to_int(page_text)

        # Areas
        land_sqm = None
        building_sqm = None
        m_land = re.search(r"(?:土地面積|土地)\s*[:：]?\s*([0-9\.]+)\s*㎡", page_text)
        if m_land:
            land_sqm = safe_float(m_land.group(1))
        m_bld = re.search(r"(?:建物面積|延床面積|床面積)\s*[:：]?\s*([0-9\.]+)\s*㎡", page_text)
        if m_bld:
            building_sqm = safe_float(m_bld.group(1))

        # Year built
        year_built = year_from_text(page_text)
        age = compute_age(year_built)

        # Sea score
        sea_score = 0
        if sea_view:
            sea_score = 4
        elif walk_to_sea:
            sea_score = 3

        # Tags
        tags: List[str] = []
        if _maple_has_onsen(page_text + " " + detail_url):
            tags.append("Onsen")

        # Image
        image_url = _maple_pick_image_url(soup, MAPLE_BASE)

        item = {
            "id": f"maple-{maple_id}",
            "source": "Maple Housing",
            "sourceUrl": detail_url,
            "title": title,
            "titleEn": title_en,
            "propertyType": property_type,
            "city": city,
            "priceJpy": price_jpy,
            "landSqm": land_sqm,
            "buildingSqm": building_sqm,
            "yearBuilt": year_built,
            "age": round(age, 1) if age else 0,
            "lastUpdated": None,
            "seaViewScore": sea_score,
            "imageUrl": image_url,
            "highlightTags": tags,
        }

        return item, True

    except Exception:
        return None, False


def scrape_maple(
    session: requests.Session,
    *,
    now_iso_str: str,
    prev_first_seen: Dict[str, str],
    max_pages_per_type: int = 40,
) -> Tuple[List[dict], List[str], int]:
    listings: List[dict] = []
    failures: List[str] = []
    filtered_out = 0

    for ptype, slug in MAPLE_LISTING_TYPES.items():
        base_url = f"{MAPLE_BASE}/estate_db/{slug}/"
        print(f"  - Maple: scanning {ptype} ({base_url})")

        try:
            r0 = request(session, base_url, headers=HEADERS_DESKTOP, retries=4, timeout=25)
            soup0 = BeautifulSoup(r0.text or "", "html.parser")
        except Exception:
            failures.append(base_url)
            continue

        # Collect detail links from first page; then paginate if present
        page_urls = [base_url]
        # look for pagination links
        for a in soup0.select("a[href]"):
            href = a.get("href") or ""
            if not href:
                continue
            if "page=" in href or "/page/" in href:
                full = urljoin(base_url, href)
                page_urls.append(full)
        page_urls = list(dict.fromkeys(page_urls))

        # Limit pages
        page_urls = page_urls[:max_pages_per_type]

        detail_links: Set[str] = set()
        for pu in page_urls:
            try:
                r = request(session, pu, headers=HEADERS_DESKTOP, retries=3, timeout=25)
                soup = BeautifulSoup(r.text or "", "html.parser")
                for a in soup.select("a[href*='/estate_db/']"):
                    href = a.get("href") or ""
                    if not href:
                        continue
                    if "/estate_db/" not in href:
                        continue
                    if href.startswith("/"):
                        href = urljoin(MAPLE_BASE, href)
                    # Skip category index pages
                    if href.rstrip("/").endswith(f"/estate_db/{slug}"):
                        continue
                    # Only detail pages: they start with /estate_db/NNNN
                    if re.search(r"/estate_db/\d+", href):
                        detail_links.add(href.split("#")[0])
            except Exception:
                failures.append(pu)
            sleep_jitter()

        for durl in sorted(detail_links):
            item, ok = parse_maple_detail_page(session, durl, ptype)
            if not ok:
                failures.append(durl)
                continue
            if not item:
                filtered_out += 1
                continue

            # firstSeen
            item["firstSeen"] = apply_first_seen(item["id"], now_iso_str, prev_first_seen)

            listings.append(item)
            sleep_jitter()

    return listings, failures, filtered_out


# ----------------------------
# Aoba scraping (house + land only)
# ----------------------------

AOBA_LISTING_TYPES = {
    "house": "house",
    "land": "land",
}

# bknarea mapping (strict); only allow listed areas
AOBA_BKNAREA_TO_CITY = {
    "ao14384": None,       # excluded area (example)
    "ao22219": None,       # excluded
    "ao22301": "東伊豆町",  # target area cluster
    "ao00000": None,
}

def _aoba_room_id_from_url(url: str) -> Optional[str]:
    m = re.search(r"room(\d+)\.html", url)
    return m.group(1) if m else None

def _aoba_city_from_url(url: str) -> Optional[str]:
    """
    Extract bknarea token from URL and map to city.
    Example:
      https://www.aoba-resort.com/area-b2/bknarea-ao22301/room99255589.html
    """
    m = re.search(r"bknarea-(ao\d+)", url)
    if not m:
        return None
    code = m.group(1)
    city = AOBA_BKNAREA_TO_CITY.get(code)
    if city:
        return city
    return None

def _aoba_allowed_city(url_city: Optional[str], soup: BeautifulSoup, page_text: str) -> Optional[str]:
    """
    Determine city for Aoba detail page, but only allow the user's requested cities.
    We primarily trust the bknarea mapping; if missing, fallback to text scan.
    """
    if url_city in AOBA_ALLOWED_CITIES:
        return url_city

    # fallback text scan (rare)
    t = page_text or ""
    if "下田市" in t:
        return "下田市"
    if "賀茂郡東伊豆町" in t or "東伊豆町" in t:
        return "東伊豆町"
    return None

AOBA_SEA_KEYWORDS = [
    "海", "海岸", "ビーチ", "オーシャン", "海一望", "海を望", "海望",
    "相模湾", "駿河湾",
    "伊豆諸島", "伊豆大島", "大島", "利島", "新島", "神津島", "三宅島", "御蔵島", "八丈島",
    "須崎", "白浜",
]
AOBA_WALK_KEYWORDS = [
    "海まで徒歩", "海へ徒歩", "海まで", "徒歩", "歩いて", "徒歩圏", "海岸まで",
    "ビーチまで", "浜まで",
]

def _aoba_sea_view_and_walk(text: str) -> Tuple[bool, bool]:
    t = text or ""
    sea = any(k in t for k in AOBA_SEA_KEYWORDS) or ("眺望" in t and ("海" in t or "伊豆" in t))
    walk = any(k in t for k in AOBA_WALK_KEYWORDS) and ("海" in t or "ビーチ" in t or "海岸" in t or "浜" in t)
    return sea, walk

def _aoba_pick_image_url(scope: BeautifulSoup, detail_url: str, *, room_id: Optional[str], og_image: Optional[str]) -> Optional[str]:
    # Prefer og:image
    if og_image:
        u = og_image
        if u.startswith("/"):
            u = urljoin(AOBA_BASE, u)
        return u

    # Otherwise, pick a reasonable image element
    for sel in ["img", "figure img", ".slide img", ".swiper img"]:
        img = scope.select_one(sel)
        if img and img.get("src"):
            u = img.get("src")
            if u.startswith("/"):
                u = urljoin(AOBA_BASE, u)
            # Reject obvious chrome
            ul = u.lower()
            if any(bad in ul for bad in ["logo", "icon", "sprite", "noimage", "loading"]):
                continue
            return u
    return None

def parse_aoba_detail_page(session: requests.Session, detail_url: str, property_type: str) -> Tuple[Optional[dict], bool]:
    try:
        r = request(session, detail_url, headers=HEADERS_DESKTOP, retries=4, timeout=25)
        html = r.text or ""
        soup = BeautifulSoup(html, "html.parser")

        page_text = clean_text(soup.get_text(" ", strip=True))

        # City: strict by URL bknarea mapping, fallback to page scan
        url_city = _aoba_city_from_url(detail_url)
        city = _aoba_allowed_city(url_city, soup, page_text)
        city = normalize_city_jp(city)

        if city not in AOBA_ALLOWED_CITIES:
            # filter out all non-target cities
            return None, True

        # Build a robust "signal" text: include meta and a truncated raw HTML
        parts: List[str] = []
        try:
            parts.append(clean_text(soup.title.get_text(" ", strip=True)) if soup.title else "")
        except Exception:
            pass
        for prop in ["og:title", "og:description", "description"]:
            try:
                if prop == "description":
                    mtag = soup.find("meta", attrs={"name": "description"})
                else:
                    mtag = soup.find("meta", attrs={"property": prop})
                if mtag and mtag.get("content"):
                    parts.append(clean_text(mtag.get("content")))
            except Exception:
                pass
        parts.append(page_text)

        signal_text = clean_text(" ".join(p for p in parts if p))

        # Aoba pages sometimes hide key descriptive terms in inline scripts; include a truncated raw HTML view.
        try:
            signal_text = clean_text(signal_text + " " + (html[:200000] if html else ""))
        except Exception:
            pass

        sea_view, walk_to_sea = _aoba_sea_view_and_walk(signal_text)

        if AOBA_DEBUG:
            print(f"    [Aoba dbg] city={city} sea={sea_view} walk={walk_to_sea} url={detail_url}")

        if not (sea_view or walk_to_sea):
            return None, True

        # Price
        price_jpy = yen_to_int(signal_text)

        # Areas
        land_sqm = None
        building_sqm = None
        # Try label patterns
        m_land = re.search(r"(?:土地面積|敷地面積)\s*[:：]?\s*([0-9\.]+)\s*㎡", signal_text)
        if m_land:
            land_sqm = safe_float(m_land.group(1))
        m_bld = re.search(r"(?:建物面積|延床面積|専有面積)\s*[:：]?\s*([0-9\.]+)\s*㎡", signal_text)
        if m_bld:
            building_sqm = safe_float(m_bld.group(1))

        # Year built
        year_built = year_from_text(signal_text)
        age = compute_age(year_built)

        # Sea score
        sea_score = 0
        if sea_view:
            sea_score = 4
        elif walk_to_sea:
            sea_score = 3

        # Image: prefer og:image; then fallback
        og_image = None
        try:
            og = soup.find("meta", attrs={"property": "og:image"})
            if og and og.get("content"):
                og_image = og.get("content")
        except Exception:
            og_image = None

        room_id = _aoba_room_id_from_url(detail_url)

        image_url = (
            _aoba_pick_image_url(soup, detail_url, room_id=room_id, og_image=og_image)
        )

        # Drop obvious chrome/placeholder
        if image_url:
            ul = image_url.lower()
            if any(bad in ul for bad in ["page_top", "logo", "icon", "sprite", "noimage", "loading"]):
                image_url = None

        # Titles (keep simple and consistent with other sources)
        type_jp = {"house": "戸建", "land": "土地"}.get(property_type, "物件")
        type_en = {"house": "House", "land": "Land"}.get(property_type, "Listing")

        title = f"{city} {type_jp}".strip()
        title_en_city = CITY_EN_MAP.get(city, city) if city else "Aoba"
        title_en = f"{title_en_city} {type_en}".strip()

        tags: List[str] = []
        # (Aoba onsen tagging not requested; keep blank)

        item = {
            "id": f"aoba-{room_id}" if room_id else f"aoba-{abs(hash(detail_url))}",
            "source": "Aoba Resort",
            "sourceUrl": detail_url,
            "title": title,
            "titleEn": title_en,
            "propertyType": property_type,
            "city": city,
            "priceJpy": price_jpy,
            "landSqm": land_sqm,
            "buildingSqm": building_sqm,
            "yearBuilt": year_built,
            "age": round(age, 1) if age else 0,
            "lastUpdated": None,
            "seaViewScore": sea_score,
            "imageUrl": image_url,
            "highlightTags": tags,
        }

        return item, True

    except Exception:
        return None, False


def scrape_aoba(
    session: requests.Session,
    *,
    now_iso_str: str,
    prev_first_seen: Dict[str, str],
    max_pages_per_type: int = 25,
) -> Tuple[List[dict], List[str], int]:
    listings: List[dict] = []
    failures: List[str] = []
    filtered_out = 0

    for ptype, slug in AOBA_LISTING_TYPES.items():
        base_url = f"{AOBA_BASE}/{slug}/"
        print(f"  - Aoba: scanning {ptype} ({base_url})")

        # Collect paginated list pages
        page_urls: List[str] = [base_url]
        try:
            r0 = request(session, base_url, headers=HEADERS_DESKTOP, retries=4, timeout=25)
            soup0 = BeautifulSoup(r0.text or "", "html.parser")

            # pagination anchors
            for a in soup0.select("a[href]"):
                href = a.get("href") or ""
                if not href:
                    continue
                if "page" in href or "/page/" in href:
                    page_urls.append(urljoin(base_url, href))
        except Exception:
            failures.append(base_url)
            continue

        page_urls = list(dict.fromkeys(page_urls))[:max_pages_per_type]

        detail_links: Set[str] = set()
        for pu in page_urls:
            try:
                r = request(session, pu, headers=HEADERS_DESKTOP, retries=3, timeout=25)
                soup = BeautifulSoup(r.text or "", "html.parser")
                for a in soup.select("a[href*='room']"):
                    href = a.get("href") or ""
                    if not href:
                        continue
                    if not href.endswith(".html"):
                        continue
                    if href.startswith("/"):
                        href = urljoin(AOBA_BASE, href)
                    # Only allow URL bknarea codes that map to our target cities
                    url_city = _aoba_city_from_url(href)
                    if url_city not in AOBA_ALLOWED_CITIES:
                        continue
                    detail_links.add(href.split("#")[0])
            except Exception:
                failures.append(pu)
            sleep_jitter()

        for durl in sorted(detail_links):
            item, ok = parse_aoba_detail_page(session, durl, ptype)
            if not ok:
                failures.append(durl)
                continue
            if not item:
                filtered_out += 1
                continue

            item["firstSeen"] = apply_first_seen(item["id"], now_iso_str, prev_first_seen)
            listings.append(item)
            sleep_jitter()

    return listings, failures, filtered_out


# ----------------------------
# Main
# ----------------------------

def main():
    session = requests.Session()

    prev_first_seen = load_prev_first_seen(OUT_LISTINGS)
    now_iso_str = now_iso()

    print("Step 0: Scraping 新着物件一覧 for event dates...")
    event_records = scrape_event_records(session)
    print(f"  - Captured {len(event_records)} event records.")

    print("Step 1: Extracting Tokusen IDs...")
    tokusen_ids = extract_tokusei_hpnos(session)
    print(f"  - Found {len(tokusen_ids)} IDs in URL query.")

    print("Step 2: Scanning Mobile Search...")
    mobile_ids = scan_mobile_search(session)

    # Merge
    candidate_ids = list(dict.fromkeys(tokusen_ids + sorted(mobile_ids)))

    print(f"Step 3: Scraping {len(candidate_ids)} properties...")
    izu_listings: List[dict] = []
    failures: List[str] = []
    izu_filtered_false = 0

    for i, hpno in enumerate(candidate_ids, start=1):
        item, ok = parse_izutaiyo_detail(session, hpno, event_records)
        if not ok:
            failures.append(f"{IZUTAIYO_BASE}/d.php?hpno={hpno}")
            continue
        if not item:
            izu_filtered_false += 1
            continue

        # firstSeen
        item["firstSeen"] = apply_first_seen(item["id"], now_iso_str, prev_first_seen)

        izu_listings.append(item)
        if i % 10 == 0:
            print(f"  ...{i}/{len(candidate_ids)}")
        sleep_jitter()

    # Maple
    print("Step 4: Scraping Maple Housing...")
    try:
        maple_listings, maple_failures, maple_filtered = scrape_maple(
            session,
            now_iso_str=now_iso_str,
            prev_first_seen=prev_first_seen,
            max_pages_per_type=40,
        )
        print(f"  - Maple: kept {len(maple_listings)} listings (filtered out {maple_filtered}).")
        failures.extend(maple_failures)
    except Exception:
        print("  - Warning: Maple scrape failed (continuing with Izu Taiyo only).")
        maple_listings, maple_filtered = [], 0

    # Aoba
    print("Step 5: Scraping Aoba Resort...")
    try:
        aoba_listings, aoba_failures, aoba_filtered = scrape_aoba(
            session,
            now_iso_str=now_iso_str,
            prev_first_seen=prev_first_seen,
            max_pages_per_type=25,
        )
        print(f"  - Aoba: kept {len(aoba_listings)} listings (filtered out {aoba_filtered}).")
        failures.extend(aoba_failures)
    except Exception:
        print("  - Warning: Aoba scrape failed (continuing without Aoba).")
        aoba_listings, aoba_filtered = [], 0

    # Combine
    listings = izu_listings + maple_listings + aoba_listings

    # Global exclusions: remove mansions/condos everywhere (including Izu Taiyo)
    listings = [l for l in listings if l.get("propertyType") != "mansion"]

    # Normalize city (Maple/Aoba occasional)
    for l in listings:
        l["city"] = normalize_city_jp(l.get("city"))

    # Output JSON
    payload = {
        "generatedAt": now_iso_str,
        "fxRateUsd": DEFAULT_FX_USDJPY,
        "fxRateCny": DEFAULT_FX_CNYJPY,
        "fxSource": DEFAULT_FX_SOURCE,
        "listings": listings,
    }

    with open(OUT_LISTINGS, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    build = {
        "generatedAt": now_iso_str,
        "counts": {
            "total": len(listings),
            "izutaiyo": len(izu_listings),
            "maple": len(maple_listings),
            "aoba": len(aoba_listings),
        },
    }
    with open(OUT_BUILDINFO, "w", encoding="utf-8") as f:
        json.dump(build, f, ensure_ascii=False, indent=2)

    print(
        f"Done. Saved {len(listings)} valid listings. "
        f"(filtered out {izu_filtered_false} Izutaiyo search-only false positives; "
        f"filtered out {maple_filtered} Maple non-qualifiers; "
        f"filtered out {aoba_filtered} Aoba non-qualifiers)"
    )

    if failures:
        print(f"Note: {len(failures)} pages failed.")


if __name__ == "__main__":
    main()
