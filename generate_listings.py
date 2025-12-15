#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Izu Coastal Radar listings generator (Izutaiyo + Maple Housing)

Fixes vs prior versions:
- Mobile search harvesting restored to the *working* "variant params + pagination" approach
  (notably includes hpfb=1 variants, and follows "next" links).
- Image URL extraction restored to a reliable /bb/... jpg detector + deterministic fallback
  https://www.izutaiyo.co.jp/bb/<prefix>/<hpno_lower>a.jpg
- Search-derived over-inclusion reduced by strict post-filtering on the detail page:
  keep only if (seaViewScore>=4) OR (walk-to-sea true) for items not in Tokusen.
- Title cleaning to avoid marketing/担当者 text bloating titles.

Outputs: listings.json in the schema your current index.html expects.
"""

from __future__ import annotations

import json
import re
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple
from urllib.parse import parse_qs, urljoin, urlparse, unquote

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
    "mansion": "mansion", # マンション
}

# Exclude these Maple area buckets entirely
MAPLE_EXCLUDE_AREAS = ["熱海～網代", "宇佐美～伊東", "川奈～富戸"]

# Exclude these Maple cities entirely (to keep the feed focused on Izu South)
MAPLE_EXCLUDE_CITIES_JP = ["伊豆市", "伊東市", "静岡市"]


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


# ---------------------------
# HTTP helpers
# ---------------------------

def request(
    session: requests.Session,
    url: str,
    *,
    headers: Optional[dict] = None,
    params=None,
    timeout: int = 25,
    retries: int = 4,
    backoff_s: float = 0.8,
) -> requests.Response:
    last_err: Optional[Exception] = None
    for attempt in range(retries):
        try:
            r = session.get(url, headers=headers, params=params, timeout=timeout, allow_redirects=True)
            r.raise_for_status()
            return r
        except Exception as e:
            last_err = e
            time.sleep(backoff_s * (attempt + 1))
    raise RuntimeError(f"GET failed after {retries} tries: {url} ({last_err})")


def canonical_detail_url(hpno: str) -> str:
    return f"{BASE}d.php?hpno={hpno}"


def hpno_from_url(url: str) -> Optional[str]:
    try:
        q = parse_qs(urlparse(url).query)
        hpno = (q.get("hpno") or [None])[0]
        if hpno:
            return hpno.strip()
    except Exception:
        pass
    m = re.search(r"hpno=([A-Z0-9]+)", url)
    return m.group(1) if m else None


def clean_text(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip()


# ---------------------------
# Tokusen: authoritative ID list
# ---------------------------

def extract_tokusen_hpnos(session: requests.Session) -> List[str]:
    """
    Tokusen landing contains a tokusen.php link with hpno=<IDs> embedded.
    Using that avoids sidebars/extra links (the source of '20 links' confusion).
    """
    r = request(session, TOKUSEN_LANDING, headers=HEADERS_DESKTOP)
    soup = BeautifulSoup(r.text or "", "html.parser")

    urls: List[str] = []
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "tokusen.php" in href and "hpno=" in href and "hptantou=shimoda" in href:
            urls.append(urljoin(BASE, href))

    if not urls:
        # last-ditch regex
        m = re.search(r'href="([^"]*tokusen\.php[^"]*hpno=[^"]*hptantou=shimoda[^"]*)"', r.text or "", flags=re.I)
        if m:
            urls.append(urljoin(BASE, m.group(1)))

    if not urls:
        raise RuntimeError("Could not locate tokusen list URL with hpno=... on landing page.")

    list_url = max(urls, key=len)
    q = parse_qs(urlparse(list_url).query)
    hpno_blob = (q.get("hpno") or [""])[0]  # '+' decoded to spaces by parse_qs
    hpnos = [x.strip() for x in re.split(r"\s+", hpno_blob) if x.strip()]

    seen: Set[str] = set()
    out: List[str] = []
    for h in hpnos:
        if h not in seen:
            seen.add(h)
            out.append(h)
    return out


# ---------------------------
# Mobile search: working approach (variants + pagination)
# ---------------------------

def extract_hpnos_from_html(html: str) -> Set[str]:
    return set(re.findall(r"hpno=([A-Z0-9]+)", html or ""))


def find_next_mobile_page(soup: BeautifulSoup, current_url: str) -> Optional[str]:
    """
    Attempt multiple patterns for "next page" links on /sp/sa.php.
    """
    # Common: link text contains 次へ / 次 / Next
    for a in soup.find_all("a", href=True):
        t = clean_text(a.get_text(" ", strip=True))
        if not t:
            continue
        if any(k in t for k in ["次へ", "次", "Next", "next", "＞"]):
            href = a["href"]
            if "sa.php" in href:
                return urljoin(current_url, href)

    # Sometimes pagination is in rel=next
    a = soup.find("a", rel=lambda v: v and "next" in v)
    if a and a.get("href"):
        return urljoin(current_url, a["href"])

    return None


def mobile_search_hpnos(session: requests.Session, city_jp: str, mode: str, max_pages: int = 8) -> Set[str]:
    """
    mode: "sea" or "walk"
    Key detail: include hpfb=1 variants. Without it, the endpoint often returns the form page (0 hpno links).
    """
    assert mode in ("sea", "walk")
    hpnos: Set[str] = set()

    if mode == "sea":
        variants = [
            [("hpcity[]", city_jp), ("hpumi", "1"), ("hpfb", "1")],
            [("hpcity[]", city_jp), ("hpumi", "1")],
        ]
    else:
        variants = [
            [("hpcity[]", city_jp), ("hpumihe", "1"), ("hpfb", "1")],
            [("hpcity[]", city_jp), ("hpumihe", "1")],
            [("hpcity[]", city_jp), ("hpumi_toho", "1"), ("hpfb", "1")],
            [("hpcity[]", city_jp), ("hpumi_toho", "1")],
        ]

    working_start: Optional[str] = None

    # Find a variant that actually yields results links
    for params in variants:
        r = request(session, MOBILE_SEARCH_RESULTS, headers=HEADERS_MOBILE, params=params, retries=4, timeout=25)
        found = extract_hpnos_from_html(r.text or "")
        if found:
            hpnos |= found
            working_start = r.url  # resolved URL with params
            break

    if not working_start:
        # No results for this city/mode (or site changed); return empty safely
        return set()

    # Follow pagination
    url = working_start
    visited: Set[str] = set()
    pages = 1

    while url and url not in visited and pages < max_pages:
        visited.add(url)
        r = request(session, url, headers=HEADERS_MOBILE, params=None, retries=3, timeout=25)
        html = r.text or ""
        hpnos |= extract_hpnos_from_html(html)

        soup = BeautifulSoup(html, "html.parser")
        nxt = find_next_mobile_page(soup, url)
        if not nxt or nxt in visited:
            break
        url = nxt
        pages += 1
        time.sleep(0.7)

    return hpnos




# ---------------------------
# New arrivals ("新着") event dates
# ---------------------------

EVENT_TYPES_JP = ["新規登録", "価格変更", "写真変更", "商談中", "契約済", "成約", "値下げ"]

def _normalize_event_date(d: str) -> Optional[str]:
    """Convert 'YYYY.MM.DD' to 'YYYY-MM-DD'."""
    m = re.match(r"(\d{4})\.(\d{1,2})\.(\d{1,2})", (d or "").strip())
    if not m:
        return None
    y, mo, da = m.group(1), int(m.group(2)), int(m.group(3))
    return f"{y}-{mo:02d}-{da:02d}"

def scrape_new_arrivals_events(session: requests.Session, max_pages: int = 6) -> Dict[str, dict]:
    """Scrape the 新着物件一覧 page(s) and return hpno -> event dict.

    Event dict format:
      { "eventTypeJp": "...", "eventDate": "YYYY-MM-DD" }

    Notes:
    - Detail pages typically do NOT expose listing dates (掲載日/更新日).
    - The 新着 page does, and it includes event type such as 新規登録 / 価格変更.
    - We keep parsing conservative and resilient: we anchor on 'd.php?hpno=' links.
    """
    events: Dict[str, dict] = {}

    for page in range(1, max_pages + 1):
        params = {"page": str(page)} if page > 1 else None
        r = request(session, NEW_ARRIVALS, headers=HEADERS_DESKTOP, params=params, retries=3, timeout=25)
        soup = BeautifulSoup(r.text or "", "html.parser")

        # Find anchors that look like real listing links and whose link text contains the hpno
        anchors = []
        for a in soup.find_all("a", href=True):
            href = a.get("href") or ""
            if "d.php" not in href or "hpno=" not in href:
                continue
            hpno = hpno_from_url(urljoin(BASE, href))
            if not hpno:
                continue
            a_text = clean_text(a.get_text(" ", strip=True))
            if hpno not in a_text:
                continue
            anchors.append((hpno, a))

        if not anchors:
            # No listings found: stop early
            break

        for hpno, a in anchors:
            if hpno in events:
                continue  # already captured on an earlier (more recent) page

            container = (
                a.find_parent("tr")
                or a.find_parent("table")
                or a.find_parent("div")
                or a.parent
            )
            if not container:
                continue

            t = clean_text(container.get_text(" ", strip=True))

            # Try to capture (eventType, date) in either order
            etypes = "|".join(map(re.escape, EVENT_TYPES_JP))
            m = re.search(rf"({etypes})\s*(\d{{4}}\.\d{{1,2}}\.\d{{1,2}})", t)
            if not m:
                m = re.search(rf"(\d{{4}}\.\d{{1,2}}\.\d{{1,2}}).{{0,6}}({etypes})", t)
                if m:
                    d_raw, et = m.group(1), m.group(2)
                else:
                    continue
            else:
                et, d_raw = m.group(1), m.group(2)

            d_norm = _normalize_event_date(d_raw)
            if not d_norm:
                continue

            events[hpno] = {"eventTypeJp": et, "eventDate": d_norm}

        time.sleep(0.25)

    return events


def load_previous_first_seen() -> Dict[str, str]:
    """Load previous firstSeen values from listings.json (if present).

    Backward-compatible: if prior listings.json has no firstSeen fields yet,
    we seed firstSeen from the prior top-level generatedAt to avoid marking
    the entire inventory as NEW on the first run after this enhancement.
    """
    p = Path("listings.json")
    if not p.exists():
        return {}
    try:
        prev = json.loads(p.read_text(encoding="utf-8"))
        seed = (prev.get("generatedAt") or "").strip()
        out: Dict[str, str] = {}
        for it in (prev.get("listings") or []):
            _id = (it.get("id") or "").strip()
            fs = (it.get("firstSeen") or "").strip() or seed
            if _id and fs:
                out[_id] = fs
        return out
    except Exception:
        return {}


# ---------------------------
# Detail parsing
# ---------------------------

SEA_SCORE_MAP = {
    "見えない": 0,
    "望む": 3,
    "遠望": 3,
    "少し": 3,
    "一望": 4,
    "正面": 5,
    "目前": 5,
    "海一望": 4,
    "オーシャン": 5,
}

def parse_sea_fields(page_text: str) -> Tuple[int, bool]:
    """
    Parse tokens after:
      海： <token>   海へ：<token>
    """
    t = (page_text or "").replace("：", ":")
    sea_token = None
    seahe_token = None

    m = re.search(r"海:\s*([^\s]+)", t)
    if m:
        sea_token = m.group(1).strip()

    m = re.search(r"海へ:\s*([^\s]+)", t)
    if m:
        seahe_token = m.group(1).strip()

    score = 0
    if sea_token:
        if sea_token in SEA_SCORE_MAP:
            score = SEA_SCORE_MAP[sea_token]
        else:
            # fuzzy
            for k, v in SEA_SCORE_MAP.items():
                if k in sea_token:
                    score = max(score, v)

    walk = False
    if seahe_token and "徒歩" in seahe_token:
        walk = True

    # Conservative fallback: explicit minutes
    if not walk:
        m = re.search(r"(?:海|海岸|浜|ビーチ)(?:へ|まで).{0,12}徒歩\s*([0-9]{1,2})\s*分", t)
        if m:
            try:
                walk = int(m.group(1)) <= 15
            except Exception:
                walk = True

    return score, walk


def parse_onsen_flag(page_text: str) -> bool:
    """Return True if the listing explicitly indicates onsen is available.

    IMPORTANT: Do not use a broad '不可' exclusion because other fields
    (e.g., ペット 不可, 民泊 不可) are common and unrelated.
    """
    t = (page_text or "").replace("：", ":")

    # Prefer the explicit field "温泉: <value>" (avoid "温泉大浴場: 無" etc.)
    m = re.search(r"温泉:\s*([^\s]+)", t)
    if m:
        v = m.group(1).strip()

        # Negatives must be checked first (e.g., "不可" contains "可")
        if any(k in v for k in ["不可", "無", "なし", "無し"]):
            return False

        if any(k in v for k in ["有", "あり", "有り", "引込可", "引込可能", "引込み可", "可能", "可"]):
            return True

    # Fallback: icon alt-text is often "温泉有"/"温泉無"
    if "温泉有" in t:
        return True
    if any(k in t for k in ["温泉無", "温泉なし", "温泉無し", "温泉不可"]):
        return False

    return False


def clean_title(h1_text: str) -> str:
    """
    Avoid bloated titles like:
      南伊豆 蝶ヶ野（300万円）の土地情報はこちら！下田店 大上 が担当...
    Keep only: 南伊豆 蝶ヶ野
    """
    s = clean_text(h1_text)
    s = s.replace("【", "").replace("】", "")

    # take before "の" when it looks like "〜の土地情報はこちら！" etc.
    m = re.match(r"^(.+?)の", s)
    if m:
        s = m.group(1).strip()

    # remove price parentheses
    s = re.sub(r"（[^）]*?円[^）]*?）", "", s).strip()
    return s


def guess_primary_image_url(hpno: str) -> str:
    prefix = hpno[:2].lower()
    return f"{BASE}bb/{prefix}/{hpno.lower()}a.jpg"


def extract_image_url(html: str, hpno: str) -> str:
    """
    Prefer /bb/... images. If none found, use deterministic a.jpg guess.
    This matches the style that previously worked for you.
    """
    candidates: List[str] = []

    # src/href attributes
    for m in re.finditer(r'(?:src|href)\s*=\s*["\']([^"\']+)["\']', html or "", flags=re.I):
        u = (m.group(1) or "").strip().replace("&amp;", "&")
        if not u:
            continue
        if "/bb/" in u or u.startswith("bb/") or "bb/" in u:
            if re.search(r"\.(jpg|jpeg|png)(\?|$)", u, flags=re.I):
                candidates.append(u)

    # background-image url(...)
    for m in re.finditer(r'url\(\s*["\']?([^"\')]+)["\']?\s*\)', html or "", flags=re.I):
        u = (m.group(1) or "").strip().replace("&amp;", "&")
        if ("/bb/" in u or u.startswith("bb/") or "bb/" in u) and re.search(r"\.(jpg|jpeg|png)(\?|$)", u, flags=re.I):
            candidates.append(u)

    def norm(u: str) -> str:
        if u.startswith("//"):
            u = "https:" + u
        if u.startswith("/"):
            u = urljoin(BASE, u)
        if u.startswith("bb/"):
            u = urljoin(BASE, u)
        u = re.sub(r"^http://", "https://", u)
        return u

    def rank(u: str) -> Tuple[int, int]:
        ul = u.lower()
        s = 0
        if ul.endswith("a.jpg") or ul.endswith("a.jpeg"):
            s += 50
        if "madori" in ul or "floor" in ul:
            s -= 10
        if "icon" in ul or "/img/" in ul:
            s -= 30
        return (s, -len(u))

    if candidates:
        uniq: List[str] = []
        seen: Set[str] = set()
        for u in candidates:
            nu = norm(u)
            if nu not in seen:
                seen.add(nu)
                uniq.append(nu)
        uniq.sort(key=rank, reverse=True)
        return uniq[0]

    return guess_primary_image_url(hpno)


def parse_detail_page(session: requests.Session, hpno: str) -> Optional[dict]:
    url = canonical_detail_url(hpno)
    r = request(session, url, headers=HEADERS_DESKTOP, retries=4, timeout=25)
    html = r.text or ""
    soup = BeautifulSoup(html, "html.parser")

    page_text = clean_text(soup.get_text(" ", strip=True))

    # title
    h1 = soup.find(["h1", "h2"])
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

    # price (JPY)
    price_jpy: Optional[int] = None
    # 億 + 万
    m = re.search(r"([0-9,]+)\s*億\s*([0-9,]+)?\s*万?\s*円", page_text)
    if m:
        oku = int(m.group(1).replace(",", ""))
        man = int((m.group(2) or "0").replace(",", ""))
        price_jpy = oku * 100_000_000 + man * 10_000
    if price_jpy is None:
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
    # The detail pages frequently use the '㎡' symbol rather than spelling out 平方メートル.
    m = re.search(
        r"(?:敷地面積|土地面積|地積|地目|土地)\s*[:：]?\s*([0-9,\.]+)\s*(?:㎡|m²|m2|平方メートル)",
        page_text,
    )
    if m:
        land_sqm = to_float(m.group(1))

    building_sqm = None
    m = re.search(
        r"(?:床面積|延床面積|建物面積|専有面積)\s*[:：]?[^0-9]{0,20}([0-9,\.]+)\s*(?:㎡|m²|m2|平方メートル)",
        page_text,
    )
    if m:
        building_sqm = to_float(m.group(1))

    # sea fields
    sea_score, walk_bool = parse_sea_fields(page_text)

    # year built / age
    year_built: Optional[int] = None
    age = 0.0

    now_year = datetime.now().year

    # Western year formats (e.g., 築年月：1998年4月)
    m = re.search(r"(?:築年月|築年|建築年月|建築年|完成年月|完成年)\s*[:：]?\s*([12]\d{3})\s*年", page_text)
    if m:
        try:
            year_built = int(m.group(1))
        except Exception:
            year_built = None

    # Japanese era formats (e.g., 平成21年3月 / 昭和63年)
    if year_built is None:
        m = re.search(r"(令和|平成|昭和)\s*(元|\d{1,2})\s*年", page_text)
        if m:
            era = m.group(1)
            n_raw = m.group(2)
            try:
                n = 1 if n_raw == "元" else int(n_raw)
                base = {"令和": 2019, "平成": 1989, "昭和": 1926}.get(era)
                if base:
                    year_built = base + n - 1
            except Exception:
                year_built = None

    # Age / years-since-built formats (e.g., 築年数：40.9年)
    m = re.search(r"築年数\s*[:：]?\s*([0-9]{1,3}(?:\.[0-9]+)?)\s*年", page_text)
    if m:
        try:
            age = float(m.group(1))
        except Exception:
            age = 0.0

    # Derive whichever is missing (conservative)
    if year_built and age <= 0:
        age = max(0.0, now_year - year_built)
    elif (year_built is None) and age > 0:
        # Round down to avoid overstating recency
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
        "下田市": "Shimoda City",
        "河津町": "Kawazu Town",
        "東伊豆町": "Higashi-Izu Town",
        "南伊豆町": "Minami-Izu Town",
        "伊東市": "Ito City",
    }
    title_en = title
    if city in city_en_map:
        title_en = re.sub(re.escape(city), city_en_map[city], title_en)
        # If the city token isn't present in the (cleaned) title, prepend it.
        if title_en == title:
            title_en = f"{city_en_map[city]} {title}"

    return {
        "id": f"izutaiyo-{hpno}",
        "sourceUrl": url,
        "title": title,
        "detailTitle": detail_title,
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

CITY_EN_MAP = {
    "下田市": "Shimoda City",
    "河津町": "Kawazu Town",
    "東伊豆町": "Higashi-Izu Town",
    "南伊豆町": "Minami-Izu Town",
    "伊東市": "Ito City",
    "熱海市": "Atami City",
    "伊豆市": "Izu City",
    "伊豆の国市": "Izu-no-Kuni City",
    "函南町": "Kannami Town",
}

def _abs_url(base: str, href: str) -> str:
    href = (href or "").strip()
    if not href:
        return ""
    if href.startswith("//"):
        return "https:" + href
    if href.startswith("http://") or href.startswith("https://"):
        return href
    return urljoin(base, href)

def _parse_price_jpy(text: str) -> Optional[int]:
    """Parse Japanese price strings like '4,800万円', '1億2,500万円', '価格 980 万円'."""
    t = (text or "").replace(",", "").replace(" ", "")
    m = re.search(r"([0-9]+)億([0-9]+)?万?円", t)
    if m:
        oku = int(m.group(1))
        man = int(m.group(2) or "0")
        return oku * 100_000_000 + man * 10_000

    m = re.search(r"([0-9]+)万円", t)
    if m:
        return int(m.group(1)) * 10_000

    # Sometimes: 価格 980 万円
    m = re.search(r"([0-9]+)万?円", t)
    if m and "万" in t:
        return int(m.group(1)) * 10_000

    return None

def _parse_sqm(text: str, key_variants: List[str]) -> Optional[float]:
    t = (text or "").replace(",", "")
    keys = "|".join(map(re.escape, key_variants))
    m = re.search(rf"(?:{keys})\s*[:：]?\s*([0-9]+(?:\.[0-9]+)?)\s*(?:㎡|m²|m2)", t)
    if not m:
        return None
    try:
        return float(m.group(1))
    except Exception:
        return None

def _parse_city(text: str) -> str:
    """Extract 市/町/村 token from the listing's own '所在地' field.

    Maple pages often include the broker's office address in headers/footers (e.g., 伊東市),
    so we intentionally do NOT fall back to scanning the entire page for the first 市/町/村 token.
    We also normalize county-prefixed towns (e.g., 賀茂郡東伊豆町 -> 東伊豆町) so they match Izutaiyo's city keys.
    """
    t = (text or "")

    m = re.search(r"所在地\s*[:：]?\s*([^\s　]+)", t)
    loc = m.group(1) if m else ""
    if not loc:
        return ""

    loc = re.sub(r"^静岡県", "", loc)

    m2 = re.search(r"(.+?(?:市|町|村))", loc)
    if not m2:
        return ""

    city = m2.group(1)

    if "郡" in city and re.search(r"(町|村)$", city):
        city = city.split("郡")[-1]

    return city


def _parse_year_built(text: str) -> Optional[int]:
    t = text or ""
    # Western year
    m = re.search(r"(?:築年月|築年|建築年月|建築年|完成年月|完成年)\s*[:：]?\s*([12]\d{3})\s*年", t)
    if m:
        try:
            y = int(m.group(1))
            if 1800 <= y <= datetime.now().year + 1:
                return y
        except Exception:
            pass

    # Japanese era
    era_map = {"令和": 2018, "平成": 1988, "昭和": 1925, "大正": 1911, "明治": 1867}
    m = re.search(r"(令和|平成|昭和|大正|明治)\s*([0-9]{1,2})\s*年", t)
    if m:
        era, n = m.group(1), int(m.group(2))
        base = era_map.get(era)
        if base:
            y = base + n
            if 1800 <= y <= datetime.now().year + 1:
                return y

    return None

def _maple_has_onsen(text: str) -> bool:
    t = text or ""
    # Prefer explicit field like 温泉：有/無
    m = re.search(r"温泉\s*[:：]\s*([^\s　]+)", t)
    if m:
        v = m.group(1)
        if any(x in v for x in ["有", "あり", "○"]):
            return True
        if any(x in v for x in ["無", "なし", "×"]):
            return False
    # Fallback keyword
    return "温泉" in t and any(x in t for x in ["あり", "有", "源泉", "かけ流し", "掛け流し"])

def _maple_sea_view_and_walk(text: str) -> Tuple[bool, bool]:
    """Heuristic: (sea_view, walk_to_sea). walk_to_sea if <= 20 min on foot or <=1500m."""
    t = text or ""
    sea_view = any(k in t for k in ["海一望", "海が見える", "オーシャンビュー", "海眺望", "海を望む"])
    if not sea_view and ("眺望" in t and "海" in t):
        sea_view = True

    walk_to_sea = False

    # 徒歩-based
    m = re.search(r"(?:海|海岸|浜|ビーチ)(?:へ|まで)?[^\n]{0,24}?徒歩\s*([0-9]{1,2})\s*分", t)
    if m:
        try:
            walk_to_sea = int(m.group(1)) <= 20
        except Exception:
            walk_to_sea = True

    # Meter-based
    if not walk_to_sea:
        m = re.search(r"(?:海|海岸|浜|ビーチ)(?:へ|まで)?[^\n]{0,24}?約\s*([0-9]{1,4})\s*m", t)
        if m:
            try:
                walk_to_sea = int(m.group(1)) <= 1500
            except Exception:
                walk_to_sea = True

    # Qualitative
    if not walk_to_sea and any(k in t for k in ["海まで徒歩圏", "海まで徒歩圏内"]):
        walk_to_sea = True

    return sea_view, walk_to_sea

def _maple_pick_image_url(soup: BeautifulSoup, page_url: str) -> Optional[str]:
    imgs = soup.find_all("img")
    candidates: List[str] = []
    for img in imgs:
        src = (img.get("src") or "").strip()
        if not src:
            continue
        u = _abs_url(page_url, src)
        ul = u.lower()
        if "wp-content/uploads" not in ul:
            continue
        if any(bad in ul for bad in ["logo", "icon", "sprite", "banner", "header", "footer"]):
            continue
        candidates.append(u)
    return candidates[0] if candidates else None

def _maple_context_text(a_tag) -> str:
    """Get a reasonably localized card/row text for a listing link."""
    if not a_tag:
        return ""
    node = a_tag
    for _ in range(5):
        if not node:
            break
        if getattr(node, "name", None) in {"article", "li", "tr"}:
            return clean_text(node.get_text(" ", strip=True))
        node = node.parent
    return clean_text(a_tag.get_text(" ", strip=True))

def _maple_is_excluded_area(text: str) -> bool:
    """Exclude Maple area buckets only (city exclusions are enforced on parsed listing location, not raw page text)."""
    t = text or ""
    return any(a in t for a in MAPLE_EXCLUDE_AREAS)


def _maple_should_fetch_detail(card_text: str) -> bool:
    """Heuristic prefilter to limit detail page fetches."""
    t = card_text or ""
    if _maple_is_excluded_area(t):
        return False
    # Quick city exclusion based on listing card text (reduces detail fetches)
    if any(c in t for c in MAPLE_EXCLUDE_CITIES_JP):
        return False
    if any(k in t for k in ["眺望", "海", "海岸", "浜", "ビーチ", "徒歩", "オーシャン", "海一望"]):
        return True
    return False


def _maple_collect_detail_links(soup: BeautifulSoup, list_url: str, listing_slug: str) -> List[Tuple[str, str]]:
    """Return list of (detail_url, context_text) from a Maple list page."""
    out: List[Tuple[str, str]] = []
    seen: Set[str] = set()

    root_path = f"/estate_db/{listing_slug}".rstrip("/")
    for a in soup.find_all("a", href=True):
        href = a.get("href")
        if not href:
            continue
        u = _abs_url(list_url, href)
        if not u:
            continue

        pu = urlparse(u)
        if pu.netloc and "maple-h.co.jp" not in pu.netloc:
            continue
        if "/estate_db/" not in pu.path:
            continue
        if "/page/" in pu.path:
            continue
        if pu.path.rstrip("/") == root_path or pu.path.rstrip("/") == f"{root_path}/page":
            continue
        # Keep only actual detail pages (reduces wasted fetches & avoids menu/nav links)
        is_detail = False
        if re.match(r"^/estate_db/\d{3,}", pu.path):
            is_detail = True
        else:
            q = parse_qs(pu.query)
            p = (q.get("p") or [None])[0]
            if p and str(p).isdigit():
                is_detail = True
        if not is_detail:
            continue

        u = u.split("#", 1)[0]

        if u in seen:
            continue
        seen.add(u)

        ctx = _maple_context_text(a)
        out.append((u, ctx))

    return out

def _maple_find_max_page(soup: BeautifulSoup) -> int:
    """Best-effort detection of last /page/<n>/ from pagination links."""
    mx = 1
    for a in soup.find_all("a", href=True):
        href = a.get("href") or ""
        m = re.search(r"/page/(\d+)/", href)
        if m:
            try:
                mx = max(mx, int(m.group(1)))
            except Exception:
                pass
    return mx


def _maple_clean_menu_artifact(s: str) -> str:
    if not s:
        return s
    s = clean_text(s)
    # Common Maple mobile-nav artifact
    s = re.sub(r"\s*MENU\s*$", "", s, flags=re.IGNORECASE).strip()
    if s.strip().upper() == "MENU":
        return ""
    # If MENU appears inline, remove the token conservatively
    s = re.sub(r"\bMENU\b", "", s, flags=re.IGNORECASE)
    s = clean_text(s)
    return s

def _maple_extract_title(soup: BeautifulSoup, detail_url: str, page_text: str, no: Optional[str]) -> str:
    """Robust title extraction.

    Some Maple pages have an <h1>/<h2> that resolves to 'MENU' (mobile nav), so we try multiple sources
    and fall back to the URL slug (which usually contains the Japanese title after the listing number).
    """
    candidates: List[str] = []

    # Prefer structured meta titles
    og = soup.find("meta", attrs={"property": "og:title"})
    if og and og.get("content"):
        candidates.append(str(og.get("content")))

    tw = soup.find("meta", attrs={"name": "twitter:title"})
    if tw and tw.get("content"):
        candidates.append(str(tw.get("content")))

    # Common WP title classes
    for sel in ["h1.entry-title", "h1.post-title", "h1.page-title", "h1", "h2.entry-title", "h2", "h3"]:
        el = soup.select_one(sel)
        if el:
            candidates.append(el.get_text(" ", strip=True))

    # <title> tag (often includes site name; we'll trim later)
    if soup.title and soup.title.string:
        candidates.append(str(soup.title.string))

    # URL slug fallback (usually very good on Maple)
    try:
        slug = urlparse(detail_url).path.rstrip("/").split("/")[-1]
        slug = unquote(slug)
        slug = slug.replace("\u3000", " ")
        candidates.append(slug)
    except Exception:
        pass

    # Clean + validate
    for raw in candidates:
        s = _maple_clean_menu_artifact(raw)
        if not s:
            continue
        # Trim site suffix patterns in <title>
        s = re.sub(r"\s*\|\s*[^|]{0,40}$", "", s).strip()
        s = re.sub(r"\s*[-–—]\s*[^-–—]{0,60}$", "", s).strip()

        # Remove leading listing number if present (e.g., 6651：...)
        s = re.sub(r"^\d{3,}\s*[:：]\s*", "", s).strip()

        s = _maple_clean_menu_artifact(s)
        if not s:
            continue
        if len(s) < 3:
            continue
        if s.strip().upper() == "MENU":
            continue
        return s

    return f"Maple Listing {no}" if no else "Maple Listing"


def parse_maple_detail_page(session: requests.Session, detail_url: str, property_type: str) -> Tuple[Optional[dict], bool]:
    """Return (item, kept_flag). kept_flag indicates whether it passed the sea/walk + exclusion rules."""
    r = request(session, detail_url, headers=HEADERS_DESKTOP, retries=4, timeout=25)
    html = r.text or ""
    soup = BeautifulSoup(html, "html.parser")
    page_text = clean_text(soup.get_text(" ", strip=True))

    # Exclusion buckets (enforce on detail too)
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

    # Listing number
    no = None
    m = re.search(r"No\.?\s*([0-9]{3,})", page_text)
    if m:
        no = m.group(1)
    if not no:
        q = parse_qs(urlparse(detail_url).query)
        p = (q.get("p") or [None])[0]
        if p and str(p).isdigit():
            no = str(p)
    if not no:
        m = re.search(r"(\d{3,})", urlparse(detail_url).path)
        if m:
            no = m.group(1)

    if no:
        item_id = f"maple-{no}"
    else:
        pu = urlparse(detail_url)
        stable = (pu.path + ("?" + pu.query if pu.query else "")).strip()
        stable = re.sub(r"[^a-zA-Z0-9]+", "-", stable).strip("-")
        item_id = f"maple-{stable}" if stable else f"maple-{abs(hash(detail_url))}"

    # Title (concise card title; preserve verbose headline separately)
    detail_title = _maple_extract_title(soup, detail_url, page_text, no)

    ptype_jp = {"house": "戸建", "land": "土地", "mansion": "マンション"}.get(property_type, property_type)

    if city and no:
        title = f"{city} {ptype_jp} No.{no}"
    elif city:
        title = f"{city} {ptype_jp}"
    elif no:
        title = f"{ptype_jp} No.{no}"
    else:
        title = f"{ptype_jp}"

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
    sea_score = 0
    if sea_view:
        tags.append("Sea View")
        sea_score = 4
    if walk_to_sea:
        tags.append("Walk to Sea")
    if _maple_has_onsen(page_text):
        tags.append("Onsen")

    image_url = _maple_pick_image_url(soup, detail_url)

    # English title: do not attempt to translate long Japanese marketing copy;
    # keep it consistent and readable in EN mode.
    ptype_en = {"house": "House", "land": "Land", "mansion": "Condo"}.get(property_type, str(property_type).title())
    city_en = CITY_EN_MAP.get(city, city or "Izu")
    title_en = f"{city_en} {ptype_en} No.{no}" if no else f"{city_en} {ptype_en}"
    title_en = clean_text(title_en)

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

    for ptype, slug in MAPLE_LISTING_TYPES.items():
        start_url = f"{MAPLE_ROOT}{slug}/"
        print(f"  - Maple: scanning {ptype} ({start_url})")

        try:
            r0 = request(session, start_url, headers=HEADERS_DESKTOP, retries=4, timeout=25)
            soup0 = BeautifulSoup(r0.text or "", "html.parser")
        except Exception:
            failures.append(start_url)
            continue

        max_page_detected = _maple_find_max_page(soup0)
        page_limit = min(max_pages_per_type, max_page_detected if max_page_detected > 0 else max_pages_per_type)

        seen_detail: Set[str] = set()

        for page in range(1, page_limit + 1):
            list_url = start_url if page == 1 else f"{start_url}page/{page}/"
            try:
                r = request(session, list_url, headers=HEADERS_DESKTOP, retries=3, timeout=25)
            except Exception:
                failures.append(list_url)
                continue

            soup = BeautifulSoup(r.text or "", "html.parser")
            links = _maple_collect_detail_links(soup, list_url, slug)
            if not links:
                break

            # Prefilter to limit detail fetches; loosen if it gets too strict.
            filtered_links = [(u, ctx) for (u, ctx) in links if _maple_should_fetch_detail(ctx)]
            # Cap per-page work to keep the Maple scrape bounded
            filtered_links = filtered_links[:12]

            if len(filtered_links) < 2:
                # Keep a small sample of non-excluded links on that page (helps when list cards are sparse)
                filtered_links = [
                    (u, ctx) for (u, ctx) in links
                    if (not _maple_is_excluded_area(ctx)) and (not any(c in (ctx or "") for c in MAPLE_EXCLUDE_CITIES_JP))
                ][:10]

            for detail_url, ctx in filtered_links:
                if detail_url in seen_detail:
                    continue
                seen_detail.add(detail_url)

                try:
                    item, kept = parse_maple_detail_page(session, detail_url, ptype)
                    if not kept or not item:
                        filtered_out += 1
                        continue

                    item_id = item.get("id")
                    if item_id and item_id in prev_first_seen:
                        item["firstSeen"] = prev_first_seen[item_id]
                    else:
                        item["firstSeen"] = now_iso

                    listings.append(item)

                except Exception:
                    failures.append(detail_url)

                time.sleep(0.25)

            time.sleep(0.15)

    return listings, failures, filtered_out

# ---------------------------
# Main
# ---------------------------

def main() -> None:
    session = requests.Session()

    # Warm-up: establish cookies for /sp/ (helps avoid returning the form page)
    try:
        request(session, MOBILE_HOME, headers=HEADERS_MOBILE, retries=2, timeout=15)
    except Exception:
        pass

    now_iso = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace('+00:00','Z')
    prev_first_seen = load_previous_first_seen()

    print("Step 0: Scraping 新着物件一覧 for event dates...")
    try:
        events_map = scrape_new_arrivals_events(session)
        print(f"  - Captured {len(events_map)} event records.")
    except Exception:
        events_map = {}
        print("  - Warning: could not scrape 新着物件一覧 (continuing without event dates).")

    print("Step 1: Extracting Tokusen IDs...")
    tokusen_hpnos = extract_tokusen_hpnos(session)
    print(f"  - Found {len(tokusen_hpnos)} IDs in URL query.")

    hpnos: Set[str] = set(tokusen_hpnos)
    tokusen_set: Set[str] = set(tokusen_hpnos)
    search_set: Set[str] = set()

    print("Step 2: Scanning Mobile Search...")
    for city in TARGET_CITIES_JP:
        print(f"  - Searching {city} (sea view)...")
        sea = mobile_search_hpnos(session, city, "sea")
        new_sea = sea - hpnos
        hpnos |= new_sea
        search_set |= new_sea
        print(f"    -> Added {len(new_sea)} new properties.")

        print(f"  - Searching {city} (walk-to-sea)...")
        walk = mobile_search_hpnos(session, city, "walk")
        new_walk = walk - hpnos
        hpnos |= new_walk
        search_set |= new_walk
        print(f"    -> Added {len(new_walk)} new properties.")

    # Guardrail: keep only sale listing suffixes (H/M/G)
    hpnos = {h for h in hpnos if len(h) >= 3 and h[-1] in {"H", "M", "G"}}

    print(f"Step 3: Scraping {len(hpnos)} properties...")
    listings: List[dict] = []
    failures: List[str] = []
    filtered_out = 0

    for i, hpno in enumerate(sorted(hpnos), start=1):
        try:
            item = parse_detail_page(session, hpno)
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

            # Persist firstSeen across runs (Pattern A)
            item_id = item.get("id")
            if item_id and item_id in prev_first_seen:
                item["firstSeen"] = prev_first_seen[item_id]
            else:
                item["firstSeen"] = now_iso

            # Attach most-recent event type/date from 新着物件一覧 (if available)
            try:
                hpno = (item.get("id") or "").split("izutaiyo-", 1)[-1]
                if hpno and hpno in events_map:
                    item.update(events_map[hpno])
            except Exception:
                pass

            listings.append(item)

        except Exception:
            failures.append(canonical_detail_url(hpno))

        if i % 10 == 0:
            print(f"  ...{i}/{len(hpnos)}")

    
    print("Step 4: Scraping Maple Housing...")
    maple_listings: List[dict] = []
    maple_failures: List[str] = []
    maple_filtered_out = 0
    try:
        maple_listings, maple_failures, maple_filtered_out = scrape_maple(
            session,
            now_iso=now_iso,
            prev_first_seen=prev_first_seen,
        )
        print(f"  - Maple: kept {len(maple_listings)} listings (filtered out {maple_filtered_out}).")
    except Exception as e:
        print(f"  - Warning: Maple scrape failed (continuing with Izu Taiyo only): {e}")
        print(traceback.format_exc())

    listings.extend(maple_listings)
    failures.extend(maple_failures)

    out = {
        "generatedAt": now_iso,
        "fxRate": 155,
        "listings": listings,
        "stats": {
            "count": len(listings),
            "izutaiyoCount": len(listings) - len(maple_listings),
            "mapleCount": len(maple_listings),
            "tokusenHpnoCount": len(tokusen_set),
            "searchAddedHpnoCount": len(search_set),
            "rawHpnoCount": len(hpnos),
            "filteredOutSearchOnly": filtered_out,
            "mapleFilteredOut": maple_filtered_out,
            "failures": len(failures),
        },
        "failures": failures,
    }

    Path("listings.json").write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    Path("buildInfo.json").write_text(json.dumps({"generatedAt": now_iso}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        f"Done. Saved {len(listings)} valid listings. "
        f"(filtered out {filtered_out} Izutaiyo search-only false positives; "
        f"filtered out {maple_filtered_out} Maple non-qualifiers)"
    )
    if failures:
        print(f"Note: {len(failures)} pages failed.")


if __name__ == "__main__":
    main()
