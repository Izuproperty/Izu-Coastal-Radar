#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Izu Coastal Radar listings generator (Izutaiyo + Maple Housing + Aoba Resort)

Fixes vs prior versions:
- Mobile search harvesting restored to the *working* "variant params + pagination" approach
  (notably includes hpfb=1 variants, and follows "next" links).
- Image URL extraction restored to a reliable /bb/... jpg detector + deterministic fallback
  https://www.izutaiyo.co.jp/bb/<prefix>/<hpno_lower>a.jpg
- Search-derived over-inclusion reduced by strict post-filtering on the detail page:
  keep only if (seaViewScore>=4) OR (walk-to-sea true) for items not in Tokusen.
- Title cleaning to avoid marketing/担当者 text bloating titles.

Additional enhancements applied in this version:
- Remove condos/mansions entirely (scrape + output) per user preference.
- EN-only location labels: remove “Town/City” suffixes.
- Maple + Aoba titles: remove “No.xxxx / No.xxxxxxxx” number strings from display titles.
- Maple onsen detection improved (温泉付/温泉付き/温泉権/温泉引込 + negatives).

Outputs: listings.json and buildInfo.json in the schema your current index.html expects.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, List, Optional, Set, Tuple
from urllib.parse import urljoin, urlparse, parse_qs

import requests
from bs4 import BeautifulSoup

BASE = "https://www.izutaiyo.co.jp/"
TOKUSEN_LANDING = "https://www.izutaiyo.co.jp/tokusen.php?hptantou=shimoda"


# Maple Housing
MAPLE_BASE = "https://www.maple-h.co.jp"
MAPLE_ROOT = f"{MAPLE_BASE}/estate_db/"
MAPLE_LISTING_TYPES = {
    "house": "house",     # 戸建
    "land": "estate",     # 土地
}

# Exclude these Maple area buckets entirely
MAPLE_EXCLUDE_AREAS = ["熱海～網代", "宇佐美～伊東", "川奈～富戸"]

# Exclude these Maple cities entirely (to keep the feed focused on Izu South)
MAPLE_EXCLUDE_CITIES_JP = ["伊豆市", "伊東市", "静岡市"]


# Aoba Resort
AOBA_BASE = "https://www.aoba-resort.com"
AOBA_LISTING_TYPES = {
    "house": "house",     # 戸建
    "land": "land",       # 土地
}

# Only interested in these Aoba areas
AOBA_ALLOWED_CITIES = {"下田市", "東伊豆町"}


# ---------------------------
# Shared constants / headers
# ---------------------------

MOBILE_HOME = "https://www.izutaiyo.co.jp/sp/"
MOBILE_SEARCH_RESULTS = "https://www.izutaiyo.co.jp/sp/sa.php"

NEW_ARRIVALS = "https://www.izutaiyo.co.jp/sn.php?hpfb=1"

TARGET_CITIES_JP = ["下田市", "河津町", "東伊豆町", "南伊豆町"]

HEADERS_DESKTOP = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
}

HEADERS_MOBILE = {
    "User-Agent": (
        "Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) "
        "Version/16.6 Mobile/15E148 Safari/604.1"
    ),
    "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
    "Referer": MOBILE_HOME,
}

# EN-only location labels (no Town/City suffix)
CITY_EN_MAP = {
    "下田市": "Shimoda",
    "河津町": "Kawazu",
    "東伊豆町": "Higashi-Izu",
    "南伊豆町": "Minami-Izu",
    "伊東市": "Ito",
    "伊豆市": "Izu",
    "静岡市": "Shizuoka",
}


# ---------------------------
# HTTP helpers
# ---------------------------

def request(session: requests.Session, url: str, *, mobile: bool = False, timeout: int = 25) -> str:
    headers = HEADERS_MOBILE if mobile else HEADERS_DESKTOP
    r = session.get(url, headers=headers, timeout=timeout)
    r.raise_for_status()
    r.encoding = r.apparent_encoding or "utf-8"
    return r.text


def canonical_detail_url(hpno: str) -> str:
    return f"{BASE}d.php?hpno={hpno}"


def hpno_from_url(url: str) -> Optional[str]:
    try:
        q = parse_qs(urlparse(url).query)
        v = q.get("hpno", [None])[0]
        if v:
            return v.strip()
        return None
    except Exception:
        return None


def clean_text(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").replace("\u3000", " ")).strip()


# ---------------------------
# Izutaiyo harvesting
# ---------------------------

def extract_tokusen_hpnos(session: requests.Session) -> List[str]:
    html = request(session, TOKUSEN_LANDING, mobile=False)
    soup = BeautifulSoup(html, "html.parser")
    ids: List[str] = []

    for a in soup.find_all("a", href=True):
        hpno = hpno_from_url(a["href"])
        if hpno and hpno not in ids:
            ids.append(hpno)

    return ids


def extract_hpnos_from_html(html: str) -> Set[str]:
    hpnos: Set[str] = set()
    for m in re.finditer(r"hpno=([A-Za-z0-9]+)", html):
        hpnos.add(m.group(1))
    return hpnos


def find_next_mobile_page(soup: BeautifulSoup, current_url: str) -> Optional[str]:
    # Prefer explicit "次へ" links when present
    for a in soup.find_all("a", href=True):
        if "次へ" in a.get_text(strip=True):
            return urljoin(current_url, a["href"])
    # Fallback: rel=next
    link = soup.find("a", rel=lambda v: v and "next" in v)
    if link and link.get("href"):
        return urljoin(current_url, link["href"])
    return None


def mobile_search_hpnos(session: requests.Session, city_jp: str, mode: str, max_pages: int = 8) -> Set[str]:
    """
    mode: 'sea' or 'walk'
    Uses the proven variant param approach (including hpfb=1) and follows pagination.
    """
    hpnos: Set[str] = set()

    # These query variations were empirically necessary on Izutaiyo mobile search.
    variants = []
    if mode == "sea":
        variants = [
            {"hpch": city_jp, "hpnms": "1", "hpfb": "0"},
            {"hpch": city_jp, "hpnms": "1", "hpfb": "1"},
        ]
    else:
        variants = [
            {"hpch": city_jp, "hpnms": "2", "hpfb": "0"},
            {"hpch": city_jp, "hpnms": "2", "hpfb": "1"},
        ]

    for params in variants:
        qs = "&".join([f"{k}={requests.utils.quote(str(v))}" for k, v in params.items()])
        url = f"{MOBILE_SEARCH_RESULTS}?{qs}"
        pages = 0
        while url and pages < max_pages:
            pages += 1
            try:
                html = request(session, url, mobile=True)
            except Exception:
                break
            hpnos |= extract_hpnos_from_html(html)
            soup = BeautifulSoup(html, "html.parser")

            nxt = find_next_mobile_page(soup, url)
            if not nxt or nxt == url:
                break
            url = nxt

    return hpnos


def _normalize_event_date(d: str) -> Optional[str]:
    d = clean_text(d)
    if not d:
        return None
    # Accept YYYY/MM/DD or YYYY-MM-DD
    m = re.match(r"(\d{4})[/-](\d{1,2})[/-](\d{1,2})", d)
    if not m:
        return None
    y = int(m.group(1))
    mo = int(m.group(2))
    da = int(m.group(3))
    return f"{y:04d}-{mo:02d}-{da:02d}"


def scrape_new_arrivals_events(session: requests.Session, max_pages: int = 6) -> Dict[str, dict]:
    """
    Scrape 新着物件一覧 (sn.php) to capture event types and dates per hpno.
    Returns: { hpno: {eventTypeJp, eventDate} }
    """
    events: Dict[str, dict] = {}
    url = NEW_ARRIVALS
    pages = 0

    while url and pages < max_pages:
        pages += 1
        try:
            html = request(session, url, mobile=False)
        except Exception:
            break

        soup = BeautifulSoup(html, "html.parser")

        # Each listing tends to be in a <tr> with date/type/hpno link
        for tr in soup.find_all("tr"):
            ttxt = clean_text(tr.get_text(" ", strip=True))
            a = tr.find("a", href=True)
            if not a:
                continue
            hpno = hpno_from_url(a["href"])
            if not hpno:
                continue

            # Try to locate a date near the row
            date = None
            for td in tr.find_all("td"):
                d = _normalize_event_date(td.get_text(" ", strip=True))
                if d:
                    date = d
                    break
            if not date:
                # fallback: scan row text
                m = re.search(r"(\d{4}[/-]\d{1,2}[/-]\d{1,2})", ttxt)
                if m:
                    date = _normalize_event_date(m.group(1))

            # Event type: look for known keywords in the row
            event_type = None
            for key in ["新規登録", "価格変更", "写真変更", "情報更新", "商談中", "成約"]:
                if key in ttxt:
                    event_type = key
                    break

            if date and event_type:
                events[hpno] = {"eventTypeJp": event_type, "eventDate": date}

        # Pagination: look for "次へ"
        nxt = None
        for a in soup.find_all("a", href=True):
            if "次へ" in a.get_text(strip=True):
                nxt = urljoin(url, a["href"])
                break
        url = nxt

    return events


def load_previous_first_seen() -> Dict[str, str]:
    """
    Carry forward firstSeen timestamps so 'NEW!' can behave as intended across daily runs.
    """
    try:
        with open("listings.json", "r", encoding="utf-8") as f:
            j = json.load(f)
        prev = {}
        for it in (j.get("listings") or []):
            if it.get("id") and it.get("firstSeen"):
                prev[it["id"]] = it["firstSeen"]
        return prev
    except Exception:
        return {}


def parse_sea_fields(page_text: str) -> Tuple[int, bool]:
    """
    Returns: (seaViewScore, walk_to_sea)
    seaViewScore: 4 if sea view, else 0 (or 3 for partial heuristics if needed).
    walk_to_sea: True if indicates walking access to sea / beach.
    """
    t = page_text or ""
    sea_score = 0
    walk = False

    # Dedicated “眺望：海” field if present
    if re.search(r"眺望\s*[:：]\s*海", t):
        sea_score = 4
    # Also accept “海一望”, “海が見える”, “オーシャンビュー”
    if sea_score < 4 and re.search(r"(海一望|海が見える|オーシャンビュー|海を望む)", t):
        sea_score = 4

    # Walk-to-sea heuristics (徒歩xx分 + 海/海岸/ビーチ)
    # Ex: “海まで徒歩10分”
    m = re.search(r"(海|海岸|ビーチ)[^0-9]{0,10}徒歩\s*(\d{1,2})\s*分", t)
    if m:
        try:
            mins = int(m.group(2))
            if mins <= 20:
                walk = True
        except Exception:
            pass

    return sea_score, walk


def parse_onsen_flag(page_text: str) -> bool:
    """
    Izutaiyo onsen detector (already working for that source).
    """
    t = page_text or ""
    if re.search(r"温泉\s*[:：]\s*(有|あり|○)", t):
        return True
    if re.search(r"(温泉引込|温泉付|温泉付き|源泉|かけ流し|掛け流し)", t):
        return True
    return False


def clean_title(h1_text: str) -> str:
    """
    Strip marketing fragments and footer/header bleed (e.g., 担当者/物件詳細/MENU).
    Keep it conservative to avoid breaking legitimate Japanese titles.
    """
    t = clean_text(h1_text)
    if not t:
        return t
    # Remove trailing "MENU" artifacts
    t = re.sub(r"(?:\s*MENU\s*)+$", "", t).strip()

    # Remove common boilerplate after separators
    for sep in ["｜", "|", "】", "）", ")"]:
        if sep in t and ("担当" in t or "物件" in t or "伊豆" in t):
            left = t.split(sep, 1)[0].strip()
            if len(left) >= 6:
                t = left
                break
    return t


def guess_primary_image_url(hpno: str) -> str:
    """
    Deterministic fallback used when a direct /bb/... image URL isn't discoverable.
    """
    hpno = (hpno or "").strip()
    if not hpno:
        return ""
    pref = hpno[:2].lower()
    return f"{BASE}bb/{pref}/{hpno.lower()}a.jpg"


def extract_image_url(html: str, hpno: str) -> str:
    """
    Prefer any /bb/... jpg in the HTML. Else deterministic fallback.
    """
    m = re.search(r"(https?://[^\s\"']+/bb/[^\s\"']+?\.jpe?g)", html, flags=re.I)
    if m:
        return m.group(1)
    m = re.search(r"(/bb/[^\s\"']+?\.jpe?g)", html, flags=re.I)
    if m:
        return urljoin(BASE, m.group(1))
    return guess_primary_image_url(hpno)


def parse_detail_page(session: requests.Session, hpno: str) -> Optional[dict]:
    url = canonical_detail_url(hpno)
    html = request(session, url, mobile=False)
    soup = BeautifulSoup(html, "html.parser")
    page_text = clean_text(soup.get_text("\n", strip=True))

    # title
    h1 = soup.find("h1")
    title = clean_title(h1.get_text(" ", strip=True) if h1 else hpno)

    # city
    city = ""
    m = re.search(r"所在地】.*?(下田市|河津町|東伊豆町|南伊豆町|伊東市)", page_text)
    if m:
        city = m.group(1)
    else:
        pref = hpno[:2].upper()
        city = {"SM": "下田市", "KW": "河津町", "HI": "東伊豆町", "MI": "南伊豆町"}.get(pref, "")

    # property type
    # IMPORTANT: the site-wide header/footer includes copy like
    # "伊豆のマンション購入するなら..." which can cause naive substring checks
    # to classify *everything* as "mansion".
    #
    # Most Izutaiyo hpno identifiers encode the type as the last character:
    #   ...H = house, ...M = mansion, ...G = land
    # We treat this as authoritative when available.
    ptype = "house"
    suffix = (hpno or "").strip()[-1:].upper()
    if suffix == "M":
        ptype = "mansion"
    elif suffix == "G":
        ptype = "land"
    elif suffix == "H":
        ptype = "house"
    else:
        # fallback heuristic (avoid generic marketing headers where possible)
        if "売土地" in page_text:
            ptype = "land"
        elif "売マンション" in page_text:
            ptype = "mansion"
        elif re.search(r"物件種目[:：\s]*マンション", page_text):
            ptype = "mansion"

    # Exclude condos/mansions entirely (user preference)
    if ptype == "mansion":
        return {"__skip__": True}

    # price (JPY)
    price_jpy: Optional[int] = None
    # 億 + 万
    m = re.search(r"([0-9,]+)\s*億\s*([0-9,]+)?\s*万?\s*円", page_text)
    if m:
        oku = int(m.group(1).replace(",", ""))
        man = int((m.group(2) or "0").replace(",", ""))
        price_jpy = oku * 100_000_000 + man * 10_000
    else:
        # 万円 only
        m = re.search(r"([0-9,]+)\s*万\s*円", page_text)
        if m:
            price_jpy = int(m.group(1).replace(",", "")) * 10_000

    # land/building sqm
    def to_float(s: str) -> Optional[float]:
        try:
            return float(s.replace(",", "").strip())
        except Exception:
            return None

    land_sqm = None
    m = re.search(
        r"(?:敷地面積|土地面積|地積|地目|土地)\s*[:：]?\s*([0-9,\.]+)\s*(?:㎡|m²|m2|平方メートル)",
        page_text,
    )
    if m:
        land_sqm = to_float(m.group(1))

    building_sqm = None
    m = re.search(
        r"(?:建物面積|延床面積|床面積|専有面積)\s*[:：]?\s*([0-9,\.]+)\s*(?:㎡|m²|m2|平方メートル)",
        page_text,
    )
    if m:
        building_sqm = to_float(m.group(1))

    # sea view / walk to sea
    sea_score, walk_bool = parse_sea_fields(page_text)

    # age / year built
    year_built: Optional[int] = None
    m = re.search(r"(?:築年数|築年月|築)\s*[:：]?\s*(\d{4})\s*年", page_text)
    if m:
        try:
            year_built = int(m.group(1))
        except Exception:
            year_built = None

    # If year built not directly, attempt to compute from 築xx年
    age = 0.0
    now_year = datetime.now().year
    m = re.search(r"築\s*(\d{1,3})\s*年", page_text)
    if m:
        try:
            age = float(m.group(1))
        except Exception:
            age = 0.0

    # Derive whichever is missing (conservative)
    if year_built and age <= 0:
        age = max(0.0, now_year - year_built)
    elif (year_built is None) and age > 0:
        year_built = max(1800, now_year - int(age))

    tags: List[str] = []
    if sea_score >= 4:
        tags.append("Sea View")
    if walk_bool:
        tags.append("Walk to Sea")
    if parse_onsen_flag(page_text):
        tags.append("Onsen")

    image_url = extract_image_url(html, hpno)

    # minimal EN title: translate only the city token (no external translation)
    city_en_map = {
        "下田市": "Shimoda",
        "河津町": "Kawazu",
        "東伊豆町": "Higashi-Izu",
        "南伊豆町": "Minami-Izu",
        "伊東市": "Ito",
    }
    title_en = title
    if city in city_en_map:
        title_en = re.sub(re.escape(city), city_en_map[city], title_en)
        if title_en == title:
            title_en = f"{city_en_map[city]} {title}"

    return {
        "id": f"izutaiyo-{hpno}",
        "sourceUrl": url,
        "title": title,
        "titleEn": title_en,
        "propertyType": ptype,
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


# ---------------------------
# Maple Housing scraping
# ---------------------------

def _maple_has_onsen(text: str) -> bool:
    """Detect onsen availability on Maple Housing detail pages.

    Maple commonly expresses onsen as:
      - 温泉：有/無
      - 温泉付 / 温泉付き / 温泉つき
      - 温泉権
      - 温泉引込 / 温泉引き込み（済/可/可能）
    """
    t = text or ""

    # Prefer explicit field like 温泉：有/無
    m = re.search(r"温泉\s*[:：]\s*([^\s　]+)", t)
    if m:
        v = m.group(1)
        if any(x in v for x in ["有", "あり", "○", "可", "可能", "付", "付き"]):
            return True
        if any(x in v for x in ["無", "なし", "×", "不可"]):
            return False

    # Strong positives that often appear outside the explicit field
    strong_pos = [
        "温泉付", "温泉付き", "温泉つき",
        "温泉権",
        "温泉引込", "温泉引き込み", "温泉引込み",
        "源泉", "かけ流し", "掛け流し",
    ]
    if any(k in t for k in strong_pos):
        # If the page explicitly negates onsen near the keyword, treat as no.
        if re.search(r"温泉[^\n]{0,12}(?:無|なし|不可|×)", t):
            return False
        return True

    # Explicit negatives (guarded so we don't match unrelated “不可”)
    if re.search(r"温泉[^\n]{0,12}(?:無|なし|不可|×)", t):
        return False

    # Generic fallback
    return "温泉" in t and any(x in t for x in ["あり", "有", "○"])


def _maple_sea_view_and_walk(text: str) -> Tuple[bool, bool]:
    """Heuristic: (sea_view, walk_to_sea). walk_to_sea if <= 20 min on foot or <=1500m."""
    t = text or ""
    sea = False
    walk = False

    if re.search(r"(眺望|景観)\s*[:：]?\s*海", t):
        sea = True
    if re.search(r"(海一望|海が見える|オーシャンビュー|海を望む|伊豆諸島|大島)", t):
        sea = True

    # Walk: “海まで徒歩xx分”, “海岸まで徒歩xx分”
    m = re.search(r"(海|海岸|ビーチ)[^0-9]{0,12}徒歩\s*(\d{1,2})\s*分", t)
    if m:
        try:
            mins = int(m.group(2))
            if mins <= 20:
                walk = True
        except Exception:
            pass

    # Walk: “海までxxxm”
    m = re.search(r"(海|海岸|ビーチ)[^0-9]{0,12}(\d{3,4})\s*m", t)
    if m:
        try:
            meters = int(m.group(2))
            if meters <= 1500:
                walk = True
        except Exception:
            pass

    return sea, walk


def _maple_pick_image_url(soup: BeautifulSoup, detail_url: str) -> Optional[str]:
    # Prefer og:image
    og = soup.find("meta", property="og:image")
    if og and og.get("content"):
        return urljoin(detail_url, og["content"])
    # Otherwise first meaningful image
    for img in soup.find_all("img"):
        src = img.get("src") or img.get("data-src")
        if not src:
            continue
        src = src.strip()
        if not src:
            continue
        if any(x in src.lower() for x in ["logo", "icon", "common", "spacer", "blank"]):
            continue
        return urljoin(detail_url, src)
    return None


def _parse_city(text: str) -> str:
    """
    Normalize “賀茂郡東伊豆町” to “東伊豆町” etc.
    """
    t = text or ""
    if "賀茂郡東伊豆町" in t:
        return "東伊豆町"
    for c in ["下田市", "河津町", "東伊豆町", "南伊豆町", "伊東市", "伊豆市", "静岡市"]:
        if c in t:
            return c
    return ""


def _maple_is_excluded_area(text: str) -> bool:
    """
    Exclude broad Maple "area bucket" regions (e.g., 熱海～網代).
    Note: this must NOT check cities by naive substring against full page text, because
    Maple pages include the company's office address in the footer (e.g., 伊東市...), which
    would incorrectly exclude every listing.
    """
    t = text or ""
    for a in MAPLE_EXCLUDE_AREAS:
        if a and a in t:
            return True
    return False


def _maple_extract_title_near_link(a_tag) -> str:
    """
    Extract a compact heading for a listing card from list pages.
    """
    # Prefer closest containing element
    node = a_tag
    for _ in range(5):
        if not node:
            break
        if getattr(node, "name", None) in {"article", "li", "tr"}:
            return clean_text(node.get_text(" ", strip=True))
        node = node.parent
    return clean_text(a_tag.get_text(" ", strip=True))


def _parse_price_jpy(s: str) -> Optional[int]:
    t = clean_text(s)
    if not t:
        return None
    # e.g. "1億2,300万円" or "2,980万円" or "980万"
    m = re.search(r"([0-9,]+)\s*億\s*([0-9,]+)?\s*万?\s*円?", t)
    if m:
        oku = int(m.group(1).replace(",", ""))
        man = int((m.group(2) or "0").replace(",", ""))
        return oku * 100_000_000 + man * 10_000
    m = re.search(r"([0-9,]+)\s*万\s*円?", t)
    if m:
        return int(m.group(1).replace(",", "")) * 10_000
    m = re.search(r"([0-9,]+)\s*万円", t)
    if m:
        return int(m.group(1).replace(",", "")) * 10_000
    return None


def _parse_sqm(text: str, keys: List[str]) -> Optional[float]:
    t = text or ""
    for k in keys:
        m = re.search(rf"{re.escape(k)}\s*[:：]?\s*([0-9,\.]+)\s*(?:㎡|m²|m2)", t)
        if m:
            try:
                return float(m.group(1).replace(",", ""))
            except Exception:
                pass
    return None


def _parse_year_built(text: str) -> Optional[int]:
    t = text or ""
    m = re.search(r"(築年月|築年数|築)\s*[:：]?\s*(\d{4})\s*年", t)
    if m:
        try:
            return int(m.group(2))
        except Exception:
            return None
    return None


def _maple_parse_detail(
    session: requests.Session,
    detail_url: str,
    property_type: str,
    *,
    now_iso: str,
    prev_first_seen: Dict[str, str],
) -> Tuple[Optional[dict], bool]:
    """
    Returns (item, kept_bool). kept_bool indicates whether it passed *qualification* filters.
    item=None indicates fetch/parse failure.
    """
    try:
        html = request(session, detail_url, mobile=False)
    except Exception:
        return None, False

    soup = BeautifulSoup(html, "html.parser")
    page_text = clean_text(soup.get_text("\n", strip=True))

    # Exclude entire area buckets
    if _maple_is_excluded_area(page_text):
        return None, False

    # City-based exclusion (enforce on detail too)
    city = _parse_city(page_text)
    if city in MAPLE_EXCLUDE_CITIES_JP:
        return None, False

    # Sea view / walk to sea rules
    sea_view, walk_to_sea = _maple_sea_view_and_walk(page_text)
    if not (sea_view or walk_to_sea):
        return None, False

    # Listing number (keep for ID uniqueness, but do NOT show in title)
    no = None
    m = re.search(r"No\.?\s*([0-9]{3,})", page_text)
    if m:
        no = m.group(1)
    if not no:
        q = parse_qs(urlparse(detail_url).query)
        cand = q.get("p", [None])[0]
        if cand and re.match(r"^\d{3,}$", cand):
            no = cand

    item_id = f"maple-{no or abs(hash(detail_url))}"

    # Titles (clean + compact; do not include No.)
    type_jp = "戸建" if property_type == "house" else "土地"
    # Exclude condos/mansions entirely (user preference)
    if property_type == "mansion":
        return None, False

    type_en = "House" if property_type == "house" else "Land"

    title_city = city or "伊豆"
    title = f"{title_city} {type_jp}".strip()

    title_en_city = CITY_EN_MAP.get(city, city) if city else "Maple"
    title_en = f"{title_en_city} {type_en}".strip()

    # Price
    price_jpy = None
    m = re.search(r"価格[^0-9]{0,10}([0-9,]+\s*億\s*[0-9,]*\s*万?\s*円|[0-9,]+\s*万\s*円|[0-9,]+\s*万円)", page_text)
    if m:
        price_jpy = _parse_price_jpy(m.group(1))
    if price_jpy is None:
        price_jpy = _parse_price_jpy(page_text)

    # Areas
    land_sqm = _parse_sqm(page_text, ["土地面積", "敷地面積", "地積"])
    building_sqm = _parse_sqm(page_text, ["建物面積", "延床面積", "専有面積"])

    # Year built / age
    year_built = _parse_year_built(page_text)
    age = 0.0
    now_year = datetime.now().year
    if year_built:
        age = max(0.0, now_year - year_built)

    tags: List[str] = []
    sea_score = 4 if sea_view else (3 if walk_to_sea else 0)
    if sea_view:
        tags.append("Sea View")
    if walk_to_sea:
        tags.append("Walk to Sea")
    if _maple_has_onsen(page_text):
        tags.append("Onsen")

    image_url = _maple_pick_image_url(soup, detail_url)

    item = {
        "id": item_id,
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

    # firstSeen
    item["firstSeen"] = prev_first_seen.get(item_id, now_iso)

    return item, True


def scrape_maple(
    session: requests.Session,
    *,
    now_iso: str,
    prev_first_seen: Dict[str, str],
    max_pages_per_type: int = 12,
) -> Tuple[List[dict], List[str], int]:
    """Scrape Maple Housing and return (listings, failures, filtered_out_count)."""
    listings: List[dict] = []
    failures: List[str] = []
    filtered_out = 0

    seen_detail_urls: Set[str] = set()

    for ptype, slug in MAPLE_LISTING_TYPES.items():
        root_url = f"{MAPLE_ROOT}{slug}/"
        print(f"  - Maple: scanning {ptype} ({root_url})")

        # Pagination format: /estate_db/house/page/2/
        for page in range(1, max_pages_per_type + 1):
            page_url = root_url if page == 1 else f"{root_url}page/{page}/"
            try:
                html = request(session, page_url, mobile=False)
            except Exception:
                # Stop if pages start failing for this type
                if page == 1:
                    failures.append(page_url)
                break

            soup = BeautifulSoup(html, "html.parser")

            # Collect candidate detail URLs
            links = []
            for a in soup.find_all("a", href=True):
                u = urljoin(page_url, a["href"])
                pu = urlparse(u)

                # Keep Maple internal estate_db detail links (avoid pagination links)
                if pu.netloc and "maple-h.co.jp" not in pu.netloc:
                    continue
                if "/estate_db/" not in pu.path:
                    continue
                if "/page/" in pu.path:
                    continue

                # remove anchors
                u = u.split("#", 1)[0]

                # Skip list roots
                root_path = urlparse(root_url).path.rstrip("/")
                if pu.path.rstrip("/") == root_path:
                    continue

                # Keep only likely "detail" links: /estate_db/<digits...> or ?p=<digits>
                if not re.search(r"/estate_db/\d", pu.path) and not re.search(r"(?:^|&)p=\d{3,}(?:&|$)", pu.query):
                    continue

                links.append(u)

            # De-dupe per page
            for u in dict.fromkeys(links).keys():
                if u in seen_detail_urls:
                    continue
                seen_detail_urls.add(u)

                try:
                    item, kept = _maple_parse_detail(
                        session, u, ptype,
                        now_iso=now_iso,
                        prev_first_seen=prev_first_seen,
                    )
                except Exception:
                    item, kept = None, False

                if item is None:
                    failures.append(u)
                    continue
                if not kept:
                    filtered_out += 1
                    continue
                listings.append(item)

    return listings, failures, filtered_out


# ---------------------------
# Aoba Resort scraping
# ---------------------------

def _aoba_city_from_text(text: str) -> str:
    t = text or ""
    if "賀茂郡東伊豆町" in t:
        return "東伊豆町"
    if "下田市" in t:
        return "下田市"
    if "東伊豆町" in t:
        return "東伊豆町"
    return ""


def _aoba_has_sea_or_walk(text: str) -> Tuple[bool, bool]:
    t = text or ""
    sea = False
    walk = False

    if re.search(r"(海一望|海が見える|オーシャンビュー|海を望む|伊豆諸島|大島)", t):
        sea = True
    if re.search(r"(眺望|景観)\s*[:：]?\s*海", t):
        sea = True

    m = re.search(r"(海|海岸|ビーチ)[^0-9]{0,12}徒歩\s*(\d{1,2})\s*分", t)
    if m:
        try:
            mins = int(m.group(2))
            if mins <= 20:
                walk = True
        except Exception:
            pass

    m = re.search(r"(海|海岸|ビーチ)[^0-9]{0,12}(\d{3,4})\s*m", t)
    if m:
        try:
            meters = int(m.group(2))
            if meters <= 1500:
                walk = True
        except Exception:
            pass

    return sea, walk


def _aoba_pick_image_url(soup: BeautifulSoup, detail_url: str) -> Optional[str]:
    og = soup.find("meta", property="og:image")
    if og and og.get("content"):
        return urljoin(detail_url, og["content"])
    for img in soup.find_all("img"):
        src = img.get("data-original") or img.get("data-src") or img.get("src")
        if not src:
            continue
        src = src.strip()
        if not src:
            continue
        if any(x in src.lower() for x in ["logo", "icon", "common", "spacer", "blank"]):
            continue
        return urljoin(detail_url, src)
    return None


def _aoba_parse_detail(
    session: requests.Session,
    detail_url: str,
    property_type: str,
    *,
    now_iso: str,
    prev_first_seen: Dict[str, str],
) -> Tuple[Optional[dict], bool]:
    """Return (item, kept). kept False means filtered out by criteria."""
    # Property type (passed in)
    if property_type == "mansion":
        return None, False
    try:
        html = request(session, detail_url, mobile=False)
    except Exception:
        return None, False

    soup = BeautifulSoup(html, "html.parser")
    text = clean_text(soup.get_text("\n", strip=True))

    city = _aoba_city_from_text(text)
    if city not in AOBA_ALLOWED_CITIES:
        return None, False

    sea, walk = _aoba_has_sea_or_walk(text)
    if not (sea or walk):
        return None, False

    # Room ID from URL: .../room98036165.html
    room_id = None
    m = re.search(r"room(\d+)\.html", detail_url)
    if m:
        room_id = m.group(1)

    item_id = f"aoba-{room_id or abs(hash(detail_url))}"

    type_jp = "戸建" if property_type == "house" else "土地"
    title = f"{city} {type_jp}".strip()

    title_en_city = {"下田市": "Shimoda", "東伊豆町": "Higashi-Izu"}.get(city, city)
    if property_type == "house":
        title_en = f"{title_en_city} House".strip()
    else:
        title_en = f"{title_en_city} Land".strip()

    # Price
    price_jpy = None
    m = re.search(r"(価格|販売価格)\s*[:：]?\s*([0-9,]+)\s*万\s*円", text)
    if m:
        try:
            price_jpy = int(m.group(2).replace(",", "")) * 10_000
        except Exception:
            price_jpy = None

    # Areas
    land_sqm = None
    m = re.search(r"(土地面積|敷地面積)\s*[:：]?\s*([0-9,\.]+)\s*(㎡|m²|m2)", text)
    if m:
        try:
            land_sqm = float(m.group(2).replace(",", ""))
        except Exception:
            land_sqm = None

    building_sqm = None
    m = re.search(r"(建物面積|延床面積)\s*[:：]?\s*([0-9,\.]+)\s*(㎡|m²|m2)", text)
    if m:
        try:
            building_sqm = float(m.group(2).replace(",", ""))
        except Exception:
            building_sqm = None

    # Year built
    year_built = None
    m = re.search(r"(築年数|築年月|築)\s*[:：]?\s*(\d{4})\s*年", text)
    if m:
        try:
            year_built = int(m.group(2))
        except Exception:
            year_built = None

    # Age
    age = 0.0
    now_year = datetime.now().year
    if year_built:
        age = max(0.0, now_year - year_built)

    tags: List[str] = []
    sea_score = 4 if sea else (3 if walk else 0)
    if sea:
        tags.append("Sea View")
    if walk:
        tags.append("Walk to Sea")

    image_url = _aoba_pick_image_url(soup, detail_url)

    item = {
        "id": item_id,
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

    item["firstSeen"] = prev_first_seen.get(item_id, now_iso)

    return item, True


def scrape_aoba(
    session: requests.Session,
    *,
    now_iso: str,
    prev_first_seen: Dict[str, str],
    max_pages_per_type: int = 20,
) -> Tuple[List[dict], List[str], int]:
    """Scrape Aoba Resort and return (listings, failures, filtered_out_count)."""
    listings: List[dict] = []
    failures: List[str] = []
    filtered_out = 0

    for ptype, slug in AOBA_LISTING_TYPES.items():
        root_url = f"{AOBA_BASE}/{slug}/"
        print(f"  - Aoba: scanning {ptype} ({root_url})")

        for page in range(1, max_pages_per_type + 1):
            page_url = root_url if page == 1 else f"{root_url}?pg={page}"
            try:
                html = request(session, page_url, mobile=False)
            except Exception:
                if page == 1:
                    failures.append(page_url)
                break

            soup = BeautifulSoup(html, "html.parser")

            links = []
            for a in soup.find_all("a", href=True):
                href = a["href"]
                if not href:
                    continue
                u = urljoin(page_url, href)
                pu = urlparse(u)
                if pu.netloc and "aoba-resort.com" not in pu.netloc:
                    continue
                if not pu.path.endswith(".html"):
                    continue
                if "/room" not in pu.path:
                    continue
                u = u.split("#", 1)[0]
                links.append(u)

            # De-dupe
            for u in dict.fromkeys(links).keys():
                try:
                    item, kept = _aoba_parse_detail(
                        session, u, ptype,
                        now_iso=now_iso,
                        prev_first_seen=prev_first_seen,
                    )
                except Exception:
                    item, kept = None, False

                if item is None:
                    failures.append(u)
                    continue
                if not kept:
                    filtered_out += 1
                    continue
                listings.append(item)

    return listings, failures, filtered_out


# ---------------------------
# Main
# ---------------------------

def main() -> None:
    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
    prev_first_seen = load_previous_first_seen()

    session = requests.Session()

    # Step 0: event dates
    print("Step 0: Scraping 新着物件一覧 for event dates...")
    events = scrape_new_arrivals_events(session, max_pages=6)
    print(f"  - Captured {len(events)} event records.")

    # Step 1: tokusen
    print("Step 1: Extracting Tokusen IDs...")
    tokusen = extract_tokusen_hpnos(session)
    tokusen_set = set(tokusen)
    print(f"  - Found {len(tokusen)} IDs in URL query.")

    # Step 2: mobile search
    print("Step 2: Scanning Mobile Search...")
    search_set: Set[str] = set()
    for city in TARGET_CITIES_JP:
        print(f"  - Searching {city} (sea view)...")
        before = len(search_set)
        search_set |= mobile_search_hpnos(session, city, "sea", max_pages=8)
        print(f"    -> Added {len(search_set)-before} new properties.")
        print(f"  - Searching {city} (walk-to-sea)...")
        before = len(search_set)
        search_set |= mobile_search_hpnos(session, city, "walk", max_pages=8)
        print(f"    -> Added {len(search_set)-before} new properties.")

    # Combine
    hpnos = tokusen_set | search_set

    # Step 3: detail scrape
    print(f"Step 3: Scraping {len(hpnos)} properties...")
    listings: List[dict] = []
    failures: List[str] = []
    filtered_out = 0

    for i, hpno in enumerate(sorted(hpnos), start=1):
        try:
            item = parse_detail_page(session, hpno)

            if item and isinstance(item, dict) and item.get("__skip__"):
                # Skip (e.g., condo/mansion) without treating as a failure
                continue

            if not item:
                failures.append(canonical_detail_url(hpno))
                continue

            is_search_only = (hpno in search_set) and (hpno not in tokusen_set)
            if is_search_only:
                sv_ok = (item.get("seaViewScore") or 0) >= 4
                walk_ok = "Walk to Sea" in (item.get("highlightTags") or [])
                if not (sv_ok or walk_ok):
                    filtered_out += 1
                    continue

            # Attach event info if present
            ev = events.get(hpno)
            if ev:
                item["eventTypeJp"] = ev.get("eventTypeJp")
                item["eventDate"] = ev.get("eventDate")

            # firstSeen
            if item.get("id"):
                item["firstSeen"] = prev_first_seen.get(item["id"], now_iso)

            item["source"] = "Izu Taiyo"
            listings.append(item)
        except Exception:
            failures.append(canonical_detail_url(hpno))

        if i % 10 == 0:
            print(f"  ...{i}/{len(hpnos)}")
        time.sleep(0.12)

    # Step 4: Maple
    print("Step 4: Scraping Maple Housing...")
    maple_failures: List[str] = []
    try:
        maple_listings, maple_failures, maple_filtered_out = scrape_maple(
            session,
            now_iso=now_iso,
            prev_first_seen=prev_first_seen,
            max_pages_per_type=12,
        )
        print(f"  - Maple: kept {len(maple_listings)} listings (filtered out {maple_filtered_out}).")
        listings.extend(maple_listings)
        filtered_out += maple_filtered_out
    except Exception:
        print("  - Warning: Maple scrape failed (continuing with Izu Taiyo only).")

    # Step 5: Aoba
    print("Step 5: Scraping Aoba Resort...")
    aoba_failures: List[str] = []
    try:
        aoba_listings, aoba_failures, aoba_filtered_out = scrape_aoba(
            session,
            now_iso=now_iso,
            prev_first_seen=prev_first_seen,
            max_pages_per_type=20,
        )
        print(f"  - Aoba: kept {len(aoba_listings)} listings (filtered out {aoba_filtered_out}).")
        listings.extend(aoba_listings)
        filtered_out += aoba_filtered_out
    except Exception:
        print("  - Warning: Aoba scrape failed (continuing).")

    # Final sort: price desc by default in UI, but keep deterministic output
    listings.sort(key=lambda x: (x.get("priceJpy") or 0), reverse=True)

    # FX (manual defaults; UI can override)
    out = {
        "generatedAt": now_iso,
        "fxRateUsd": 155,
        "fxRateCny": 20,
        "fxSource": "Manual",
        "listings": listings,
    }

    with open("listings.json", "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)

    with open("buildInfo.json", "w", encoding="utf-8") as f:
        json.dump({"generatedAt": now_iso}, f, ensure_ascii=False, indent=2)

    print(f"Done. Saved {len(listings)} valid listings. (filtered out {filtered_out} total)")
    if failures or maple_failures or aoba_failures:
        all_fail = failures + maple_failures + aoba_failures
        print(f"Note: {len(all_fail)} pages failed.")

if __name__ == "__main__":
    main()
