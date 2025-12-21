#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Izu Coastal Radar listings generator
Sources: Izu Taiyo + Maple + Aoba + Tokai Yajima

Outputs:
  - listings.json
  - buildInfo.json

Updates:
- FIX: Restored missing 'scrape_izutaiyo_event_records' function.
- FIX: Izu Taiyo -> Uses "Footer Stripping" to ignore company branch addresses.
- FIX: Aoba/Tokai/Maple -> Uses "First Match" address detection.
- FIX: Aoba -> Scans all area codes (b1-b4).
"""

from __future__ import annotations

import datetime as dt
import json
import os
import random
import re
import time
from typing import Dict, List, Optional, Set, Tuple
from urllib.parse import urljoin, quote

import requests
from bs4 import BeautifulSoup
import urllib3

# Suppress SSL warnings for legacy sites
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ----------------------------
# Global config
# ----------------------------

IZUTAIYO_BASE = "https://www.izutaiyo.co.jp"
MAPLE_BASE = "https://www.maple-h.co.jp"
AOBA_BASE = "https://www.aoba-resort.com"
TOKAI_BASE = "https://tokaiyajima.com"

OUT_LISTINGS = "listings.json"
OUT_BUILDINFO = "buildInfo.json"

SLEEP_MIN = 1.00
SLEEP_MAX = 2.00

DEFAULT_RETRIES = 3

HEADERS_DESKTOP = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/123.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
}

# STRICT LOCATION SCOPE (4 LOCATIONS)
ALLOWED_CITIES = {"下田市", "東伊豆町", "南伊豆町", "河津町"}

CITY_EN_MAP = {
    "下田市": "Shimoda",
    "河津町": "Kawazu",
    "東伊豆町": "Higashi-Izu",
    "南伊豆町": "Minami-Izu",
    "伊東市": "Ito",
    "伊豆市": "Izu",
    "静岡市": "Shizuoka",
}

# Expanded keywords to catch Minami Izu specific spots & "View" (展望)
SEA_KEYWORDS = [
    "海", "オーシャン", "海望", "海一望", "相模湾", "駿河湾", 
    "太平洋", "海近", "海岸", "ビーチ", "Sea", "Ocean", "白浜",
    "弓ヶ浜", "ヒリゾ", "外浦", "吉佐美", "入田", "多々戸", "展望"
]
WALK_KEYWORDS = ["徒歩", "歩", "近", "Walk"]

# ----------------------------
# Helpers
# ----------------------------

def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()

def sleep_jitter():
    time.sleep(random.uniform(SLEEP_MIN, SLEEP_MAX))

def clean_text(s: str) -> str:
    if not s: return ""
    s = re.sub(r"[ \t\r\f\v]+", " ", s)
    s = re.sub(r"\u3000+", " ", s)
    return s.strip()

def safe_float(s: Optional[str]) -> Optional[float]:
    if not s: return None
    s2 = re.sub(r"[^\d\.]", "", str(s))
    if not s2: return None
    try:
        return float(s2)
    except Exception: return None

def yen_to_int(text: str) -> Optional[int]:
    t = clean_text(text)
    if not t: return None
    if re.match(r"^[0-9,]+$", t):
        return int(t.replace(",", ""))
    
    oku = 0.0
    man = 0.0
    m_oku = re.search(r"(\d+(?:\.\d+)?)\s*億", t)
    if m_oku: oku = float(m_oku.group(1))
    m_man = re.search(r"(\d+(?:\.\d+)?)\s*万", t)
    if m_man: man = float(m_man.group(1))

    if oku == 0 and man == 0:
        m_raw = re.search(r"([0-9,]+)\s*円", t)
        if m_raw: return int(m_raw.group(1).replace(",", ""))
        m_raw_man = re.search(r"([0-9,]+)\s*万円", t)
        if m_raw_man: return int(float(m_raw_man.group(1).replace(",", "")) * 10000)
        return None

    total = int(round(oku * 100_000_000 + man * 10_000))
    return total if total > 0 else None

def extract_year_built(text: str) -> Optional[int]:
    """
    Handles both "Built: 1990" and "Age: 7.5 years".
    Strictly looks for '築' to avoid grabbing '2025' from update date.
    """
    t = clean_text(text)
    if not t: return None
    
    # Pattern 1: Absolute Year (築1990年)
    m_year = re.search(r"築(?:年月)?\s*[:：]?\s*(?:西暦)?([0-9]{4})年", t)
    if m_year:
        return int(m_year.group(1))
        
    # Pattern 2: Relative Age (築年数：7.5年)
    m_age = re.search(r"築年数\s*[:：]?\s*([0-9\.]+)年", t)
    if m_age:
        age_val = float(m_age.group(1))
        current_year = dt.datetime.now().year
        return int(current_year - age_val)

    return None

def compute_age(year_built: Optional[int]) -> Optional[float]:
    if not year_built: return None
    y = dt.datetime.now().year
    return float(max(0, y - year_built))

def request(session: requests.Session, url: str, *, headers: dict = HEADERS_DESKTOP, retries: int = DEFAULT_RETRIES) -> requests.Response:
    last_exc = None
    for i in range(retries):
        try:
            r = session.get(url, headers=headers, timeout=20, verify=False)
            if r.status_code in (403, 429, 500, 502, 503) and i < retries - 1:
                sleep_jitter()
                continue
            r.raise_for_status()
            return r
        except Exception as e:
            last_exc = e
            sleep_jitter()
    raise last_exc  # type: ignore

def normalize_city_jp(text: str) -> Optional[str]:
    if not text: return None
    if "東伊豆" in text: return "東伊豆町"
    if "南伊豆" in text: return "南伊豆町"
    if "河津" in text: return "河津町"
    if "下田" in text: return "下田市"
    return None

def remove_footer(soup: BeautifulSoup, markers: List[str]) -> BeautifulSoup:
    """
    Destructively removes footer elements containing specific markers.
    This prevents 'Shimoda Branch' in the footer from triggering a location match.
    """
    for f in soup.find_all("footer"):
        f.decompose()
    
    for marker in markers:
        # Find text nodes containing the marker
        matches = soup.find_all(string=re.compile(marker))
        for m in matches:
            # Climb up to find the container block and remove it
            parent = m.find_parent(["div", "table", "section", "ul"])
            if parent:
                parent.decompose()
    return soup

def get_address_first_match(soup: BeautifulSoup, raw_text: str) -> str:
    """
    Finds the FIRST occurrence of an address block.
    Property details are always at the top, company info at bottom.
    """
    # 1. Structural Search
    for tag in soup.find_all(["th", "dt", "td", "div"]):
        txt = clean_text(tag.get_text())
        if txt == "所在地" or txt == "住所" or "所在地" in txt[:5]:
            sib = tag.find_next_sibling(["td", "dd", "div"])
            if sib: return clean_text(sib.get_text())
            if tag.name == "th":
                sib_td = tag.find_next_sibling("td")
                if sib_td: return clean_text(sib_td.get_text())

    # 2. Raw Text Fallback
    m = re.search(r"所在地\s*[:：]?\s*([^\s<]+)", raw_text)
    if m: return m.group(1)
    
    return ""

def load_prev_first_seen(path: str = OUT_LISTINGS) -> Dict[str, str]:
    if not os.path.exists(path): return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            j = json.load(f)
        out = {}
        for it in j.get("listings", []):
            if it.get("id") and it.get("firstSeen"):
                out[str(it["id"])] = str(it["firstSeen"])
        return out
    except:
        return {}

def apply_first_seen(listing_id: str, now: str, prev: Dict[str, str]) -> str:
    return prev.get(listing_id, now)


# ----------------------------
# Izu Taiyo
# ----------------------------

def scrape_izutaiyo_event_records(session) -> Dict[str, Tuple[str, str]]:
    """Scrape 'New' page for event dates/types."""
    url = f"{IZUTAIYO_BASE}/new.php"
    records = {}
    try:
        r = request(session, url)
        soup = BeautifulSoup(r.text, "html.parser")
        for row in soup.find_all("tr"):
            txt = clean_text(row.get_text())
            # Find date
            m_date = re.search(r"(20\d{2})[\/\.-](\d{1,2})[\/\.-](\d{1,2})", txt)
            date_iso = None
            if m_date:
                date_iso = f"{m_date.group(1)}-{m_date.group(2).zfill(2)}-{m_date.group(3).zfill(2)}"
            
            # Find ID
            hpno = None
            a = row.find("a", href=True)
            if a:
                m_id = re.search(r"hpno=([A-Za-z0-9]+)", a['href'])
                if not m_id:
                    m_id = re.search(r"hpbunno=([A-Za-z0-9]+)", a['href'])
                if m_id:
                    hpno = m_id.group(1)
            
            # Event Type
            etype = None
            for k in ["新規登録", "価格変更", "写真変更", "情報更新", "商談中", "成約"]:
                if k in txt:
                    etype = k
                    break
            
            if hpno and (etype or date_iso):
                records[hpno] = (etype, date_iso)
    except:
        pass
    return records

def scan_izutaiyo_urls(session) -> Set[str]:
    found_urls = set()
    print("  - Scanning Tokusen...")
    try:
        r = request(session, f"{IZUTAIYO_BASE}/tokusen.php")
        soup = BeautifulSoup(r.text, "html.parser")
        for a in soup.find_all("a", href=True):
            if "hpno=" in a['href'] or "hpbunno=" in a['href']:
                found_urls.add(urljoin(IZUTAIYO_BASE, a['href']))
    except: pass

    print("  - Scanning New Listings...")
    try:
        r = request(session, f"{IZUTAIYO_BASE}/sn.php?hpfb=1")
        soup = BeautifulSoup(r.text, "html.parser")
        for a in soup.find_all("a", href=True):
             if "hpno=" in a['href'] or "hpbunno=" in a['href']:
                found_urls.add(urljoin(IZUTAIYO_BASE, a['href']))
    except: pass

    print("  - Scanning Main List (sp/sa.php)...")
    for page in range(1, 4): 
        url = f"{IZUTAIYO_BASE}/sp/sa.php?page={page}"
        try:
            r = request(session, url)
            soup = BeautifulSoup(r.text, "html.parser")
            for a in soup.find_all("a", href=True):
                if "hpbunno=" in a['href'] or "hpno=" in a['href']:
                    found_urls.add(urljoin(url, a['href']))
            sleep_jitter()
        except: break
    return found_urls

def parse_izutaiyo_detail(session, url, event_records) -> Tuple[Optional[dict], bool]:
    try:
        r = request(session, url)
        soup = BeautifulSoup(r.text, "html.parser")
        
        # STRIP FOOTER TO FIX LOCATION BUG
        soup = remove_footer(soup, ["伊豆太陽ホーム情報", "会社概要"])
        text = clean_text(soup.get_text(" ", strip=True))

        m_id = re.search(r"hp(?:bun)?no=([A-Za-z0-9]+)", url)
        raw_id = m_id.group(1) if m_id else str(abs(hash(url)))
        
        title = ""
        h = soup.find(["h1", "h2"])
        if h: 
            raw_title = clean_text(h.get_text())
            title = re.sub(r"(の(?:家|マンション)情報はこちら|物件情報).*$", "", raw_title).strip()
            title = re.sub(r"（\d+万円）", "", title).strip()
        
        # 1. Location Check
        city = normalize_city_jp(text)
        if not city: return None, True

        # 2. Condo Check
        if "マンション" in title or "マンション" in text:
            is_mansion = False
            for tr in soup.find_all("tr"):
                 if "種目" in tr.get_text() and "マンション" in tr.get_text():
                     is_mansion = True
            if is_mansion: return None, True

        # 3. Status Check
        if "商談中" in text or "成約" in text:
            if "商談中" in title or "成約" in title: return None, True
            for tr in soup.find_all("tr"):
                row_text = clean_text(tr.get_text())
                if "価格" in row_text and ("商談中" in row_text or "成約" in row_text):
                    return None, True
        
        # 4. NEGATIVE SEA CHECK
        sea_score = 0
        if any(k in text for k in SEA_KEYWORDS): sea_score = 4
        if "海まで徒歩" in text or "海へ徒歩" in text: sea_score = max(sea_score, 3)
        
        # Explicit rejection for "Sea: Not Visible"
        for tr in soup.find_all("tr"):
            row = clean_text(tr.get_text())
            if "海" in row and ("見えない" in row or "無" in row):
                # Only reject if we don't have a strong 'Walk' signal
                if "一望" not in row and "有" not in row:
                    return None, True

        if sea_score == 0: return None, True

        ptype = "house"
        if "土地" in title and "戸建" not in title: ptype = "land"
        elif "売地" in title: ptype = "land"

        price = yen_to_int(text)
        if not price:
            for tr in soup.find_all("tr"):
                if "価格" in clean_text(tr.get_text()):
                    price = yen_to_int(clean_text(tr.get_text()))
                    break
        if not price or price == 0: return None, True

        land_sqm = None
        m_l = re.search(r"土地(?:面積)?[:：]?\s*([0-9\.]+)\s*㎡", text)
        if m_l: land_sqm = safe_float(m_l.group(1))

        bldg_sqm = None
        m_b = re.search(r"(?:建物|延床)面積[:：]?\s*([0-9\.]+)\s*㎡", text)
        if m_b: bldg_sqm = safe_float(m_b.group(1))

        year = extract_year_built(text)
        age = compute_age(year)

        img = None
        og = soup.find("meta", attrs={"property": "og:image"})
        if og: img = og.get("content")
        if not img:
            for sel in ["img#main_img", "img.main-img", ".item_img img"]:
                tag = soup.select_one(sel)
                if tag and tag.get("src"):
                    img = tag.get("src")
                    break

        if img and not img.startswith("http"):
             img = urljoin(IZUTAIYO_BASE, img)

        etype, edate = event_records.get(raw_id, (None, None))

        item = {
            "id": f"izutaiyo-{raw_id}",
            "source": "Izu Taiyo",
            "sourceUrl": url,
            "title": title or f"Izu Taiyo {raw_id}",
            "titleEn": f"{CITY_EN_MAP.get(city, city)} {ptype.capitalize()}",
            "propertyType": ptype,
            "city": city,
            "priceJpy": price,
            "landSqm": land_sqm,
            "buildingSqm": bldg_sqm,
            "yearBuilt": year,
            "age": round(age, 1) if age else 0,
            "lastUpdated": None,
            "seaViewScore": sea_score,
            "imageUrl": img,
            "highlightTags": [],
            "eventTypeJp": etype,
            "eventDate": edate,
        }
        return item, True
    except: return None, False


# ----------------------------
# Maple
# ----------------------------

def parse_maple_detail(session, url, ptype) -> Tuple[Optional[dict], bool]:
    try:
        r = request(session, url)
        soup = BeautifulSoup(r.text, "html.parser")
        
        soup = remove_footer(soup, ["Copyright", "会社概要"])
        text = clean_text(soup.get_text(" ", strip=True))

        addr = get_address_first_match(soup, text)
        city = normalize_city_jp(addr)
        if not city: city = normalize_city_jp(text)
        if not city: return None, True 

        sea = any(k in text for k in SEA_KEYWORDS)
        walk = any(k in text for k in WALK_KEYWORDS) and any(k in text for k in ["海", "ビーチ"])
        if not (sea or walk): return None, True

        title = ""
        og_t = soup.find("meta", attrs={"property": "og:title"})
        if og_t: title = clean_text(og_t.get("content"))
        if not title or "物件" in title:
            title = f"{city} {('戸建' if ptype=='house' else '土地')}"
        
        price = yen_to_int(text)
        land_sqm = None
        m_l = re.search(r"(?:土地面積|土地)\s*[:：]?\s*([0-9\.]+)\s*㎡", text)
        if m_l: land_sqm = safe_float(m_l.group(1))

        bldg_sqm = None
        m_b = re.search(r"(?:建物面積|延床面積)\s*[:：]?\s*([0-9\.]+)\s*㎡", text)
        if m_b: bldg_sqm = safe_float(m_b.group(1))

        year = extract_year_built(text)
        age = compute_age(year)

        tags = []
        if "温泉" in text and any(k in text for k in ["有", "付", "権利", "引込"]):
            if "温泉無" not in text: tags.append("Onsen")

        img = None
        og_img = soup.find("meta", attrs={"property": "og:image"})
        if og_img: img = og_img.get("content")

        m_id = re.search(r"/estate_db/(\d+)", url)
        mid = m_id.group(1) if m_id else str(abs(hash(url)))

        item = {
            "id": f"maple-{mid}",
            "source": "Maple Housing",
            "sourceUrl": url,
            "title": title,
            "titleEn": f"{CITY_EN_MAP.get(city, city)} {ptype.capitalize()}",
            "propertyType": ptype,
            "city": city,
            "priceJpy": price,
            "landSqm": land_sqm,
            "buildingSqm": bldg_sqm,
            "yearBuilt": year,
            "age": round(age, 1) if age else 0,
            "lastUpdated": None,
            "seaViewScore": 4 if sea else 3,
            "imageUrl": img,
            "highlightTags": tags,
        }
        return item, True
    except: return None, False

def scrape_maple(session, now, prev):
    listings = []
    for slug, ptype in {"house": "house", "estate": "land"}.items():
        base = f"{MAPLE_BASE}/estate_db/{slug}/"
        print(f"  - Maple {slug}...")
        try:
            r = request(session, base)
            soup = BeautifulSoup(r.text, "html.parser")
            links = set([base])
            for a in soup.select("a.page-numbers"):
                if a.get("href"): links.add(urljoin(base, a.get("href")))
            
            detail_urls = set()
            for p in list(links)[:3]:
                try:
                    r2 = request(session, p)
                    s2 = BeautifulSoup(r2.text, "html.parser")
                    for a in s2.select("a[href*='/estate_db/']"):
                        u = a.get("href")
                        if re.search(r"/estate_db/\d+", u):
                            detail_urls.add(urljoin(MAPLE_BASE, u).split("#")[0])
                except: pass
                sleep_jitter()

            for u in detail_urls:
                item, ok = parse_maple_detail(session, u, ptype)
                if ok and item:
                    item["firstSeen"] = apply_first_seen(item["id"], now, prev)
                    listings.append(item)
                sleep_jitter()
        except: pass
    return listings


# ----------------------------
# Aoba
# ----------------------------

def parse_aoba_detail(session, url, ptype) -> Tuple[Optional[dict], bool]:
    try:
        r = request(session, url)
        soup = BeautifulSoup(r.text, "html.parser")
        
        soup = remove_footer(soup, ["Copyright", "会社概要", "お問い合わせ先"])
        text = clean_text(soup.get_text(" ", strip=True))

        addr = get_address_first_match(soup, text)
        city = normalize_city_jp(addr)
        if not city: city = normalize_city_jp(text)
        if not city: return None, True

        sea = any(k in text for k in SEA_KEYWORDS)
        walk = any(k in text for k in WALK_KEYWORDS) and any(k in text for k in ["海", "ビーチ", "海岸"])
        if not (sea or walk): return None, True

        price = yen_to_int(text)
        title = f"{city} {('戸建' if ptype=='house' else '土地')}"
        
        land_sqm = None
        m_l = re.search(r"(?:土地|敷地)面積[:：]?\s*([0-9\.]+)\s*㎡", text)
        if m_l: land_sqm = safe_float(m_l.group(1))

        bldg_sqm = None
        m_b = re.search(r"(?:建物|延床)面積[:：]?\s*([0-9\.]+)\s*㎡", text)
        if m_b: bldg_sqm = safe_float(m_b.group(1))

        year = extract_year_built(text)
        age = compute_age(year)

        img = None
        og = soup.find("meta", attrs={"property": "og:image"})
        if og: img = og.get("content")

        m_id = re.search(r"room(\d+)\.html", url)
        rid = m_id.group(1) if m_id else str(abs(hash(url)))

        item = {
            "id": f"aoba-{rid}",
            "source": "Aoba Resort",
            "sourceUrl": url,
            "title": title,
            "titleEn": f"{CITY_EN_MAP.get(city, city)} {ptype.capitalize()}",
            "propertyType": ptype,
            "city": city,
            "priceJpy": price,
            "landSqm": land_sqm,
            "buildingSqm": bldg_sqm,
            "yearBuilt": year,
            "age": round(age, 1) if age else 0,
            "lastUpdated": None,
            "seaViewScore": 4 if sea else 3,
            "imageUrl": img,
            "highlightTags": [],
        }
        return item, True
    except: return None, False

def scrape_aoba(session, now, prev):
    listings = []
    
    targets = [
        f"{AOBA_BASE}/house/",
        f"{AOBA_BASE}/land/",
        f"{AOBA_BASE}/area-b1/", 
        f"{AOBA_BASE}/area-b2/", 
        f"{AOBA_BASE}/area-b3/", 
        f"{AOBA_BASE}/area-b4/", 
    ]

    print(f"  - Aoba: Scanning {len(targets)} entry points...")
    detail_urls = set()

    for base in targets:
        try:
            r = request(session, base)
            soup = BeautifulSoup(r.text, "html.parser")
            
            pages = set([base])
            for a in soup.select("a.page-numbers"):
                if a.get("href"): pages.add(urljoin(base, a.get("href")))
            
            for p in list(pages)[:5]:
                try:
                    r2 = request(session, p)
                    s2 = BeautifulSoup(r2.text, "html.parser")
                    for a in s2.select("a[href*='room']"):
                        if a['href'].endswith(".html"):
                            detail_urls.add(urljoin(AOBA_BASE, a['href']))
                except: pass
                sleep_jitter()
        except: pass

    print(f"    Found {len(detail_urls)} Aoba detail URLs.")
    
    for u in detail_urls:
        ptype = "house" if "house" in u or "area" in u else "land"
        if "land" in u: ptype = "land"

        item, ok = parse_aoba_detail(session, u, ptype)
        if ok and item:
            item["firstSeen"] = apply_first_seen(item["id"], now, prev)
            listings.append(item)
        sleep_jitter()

    return listings


# ----------------------------
# Tokai Yajima
# ----------------------------

def parse_tokai_detail(session, url) -> Tuple[Optional[dict], bool]:
    try:
        r = request(session, url)
        r.encoding = r.apparent_encoding
        soup = BeautifulSoup(r.text, "html.parser")
        
        soup = remove_footer(soup, ["会社概要", "お問い合わせ", "Copyright"])
        text = clean_text(soup.get_text(" ", strip=True))

        addr = get_address_first_match(soup, text)
        city = normalize_city_jp(addr)
        if not city: city = normalize_city_jp(text)
        if not city: return None, True

        title = ""
        h = soup.find(["h1", "h2"])
        if h: title = clean_text(h.get_text())
        
        if "マンション" in title or "マンション" in text:
             if "種別" in text and "マンション" in text:
                 return None, True

        sea = any(k in text for k in SEA_KEYWORDS)
        walk = any(k in text for k in WALK_KEYWORDS) and any(k in text for k in ["海", "ビーチ"])
        if not (sea or walk): return None, True

        price = yen_to_int(text)
        if not price:
             p_tag = soup.find(class_=re.compile("price"))
             if p_tag: price = yen_to_int(p_tag.get_text())

        ptype = "house"
        if "土地" in title or "売地" in title: ptype = "land"

        land_sqm = None
        m_l = re.search(r"土地(?:面積)?[:：]?\s*([0-9\.]+)\s*㎡", text)
        if m_l: land_sqm = safe_float(m_l.group(1))

        bldg_sqm = None
        m_b = re.search(r"(?:建物|延床)面積[:：]?\s*([0-9\.]+)\s*㎡", text)
        if m_b: bldg_sqm = safe_float(m_b.group(1))

        year = extract_year_built(text)
        age = compute_age(year)
        
        tags = []
        if "温泉" in text and any(k in text for k in ["有", "付", "権利", "引込"]):
            if "温泉無" not in text: tags.append("Onsen")

        img = None
        main_img = soup.find("img", class_=re.compile("main|detail"))
        if main_img:
             img = urljoin(TOKAI_BASE, main_img.get("src"))
        if not img:
             og = soup.find("meta", attrs={"property": "og:image"})
             if og: img = urljoin(TOKAI_BASE, og.get("content"))

        m_id = re.search(r"/(?:bukken|fudo)/(\d+)", url)
        if not m_id:
            m_id = re.search(r"bukken_id=(\d+)", url)
        
        raw_id = m_id.group(1) if m_id else str(abs(hash(url)))

        item = {
            "id": f"tokai-{raw_id}",
            "source": "Tokai Yajima",
            "sourceUrl": url,
            "title": title or "Tokai Property",
            "titleEn": f"{CITY_EN_MAP.get(city, city)} {ptype.capitalize()}",
            "propertyType": ptype,
            "city": city,
            "priceJpy": price,
            "landSqm": land_sqm,
            "buildingSqm": bldg_sqm,
            "yearBuilt": year,
            "age": round(age, 1) if age else 0,
            "lastUpdated": None,
            "seaViewScore": 4 if sea else 3,
            "imageUrl": img,
            "highlightTags": tags,
        }
        return item, True

    except Exception as e:
        return None, False

def scrape_tokai(session, now, prev):
    listings = []
    print("  - Tokai Yajima: Scanning...")
    
    start_url = f"{TOKAI_BASE}/search2/buy_area"
    city_links = set()
    
    try:
        r = request(session, start_url)
        r.encoding = r.apparent_encoding
        soup = BeautifulSoup(r.text, "html.parser")
        
        for a in soup.find_all("a", href=True):
            txt = clean_text(a.get_text())
            if any(c in txt for c in ALLOWED_CITIES):
                city_links.add(urljoin(start_url, a['href']))
    except:
        pass
    
    if not city_links:
        city_links.add(f"{TOKAI_BASE}/search2/buy_result")

    found_urls = set()
    for list_url in city_links:
        try:
            r = request(session, list_url)
            r.encoding = r.apparent_encoding
            soup = BeautifulSoup(r.text, "html.parser")
            
            for a in soup.find_all("a", href=True):
                h = a['href']
                if "/bukken/" in h or "/fudo/" in h or "bukken_id" in h:
                     found_urls.add(urljoin(list_url, h))
        except: pass
        sleep_jitter()

    print(f"    Found {len(found_urls)} potential Tokai URLs.")
    
    for u in found_urls:
        item, ok = parse_tokai_detail(session, u)
        if ok and item:
             item["firstSeen"] = apply_first_seen(item["id"], now, prev)
             listings.append(item)
        sleep_jitter()
        
    return listings


# ----------------------------
# Main
# ----------------------------

def main():
    session = requests.Session()
    prev_seen = load_prev_first_seen()
    now = now_iso()
    all_listings = []

    print("Scraping Izu Taiyo...")
    events = scrape_izutaiyo_event_records(session)
    izutaiyo_urls = scan_izutaiyo_urls(session)
    print(f"  - Found {len(izutaiyo_urls)} IzuTaiyo candidates.")
    
    it_valid = 0
    for url in izutaiyo_urls:
        item, ok = parse_izutaiyo_detail(session, url, events)
        if ok and item:
            item["firstSeen"] = apply_first_seen(item["id"], now, prev_seen)
            all_listings.append(item)
            it_valid += 1
        sleep_jitter()
    print(f"  - Izu Taiyo valid: {it_valid}")

    print("Scraping Maple...")
    maple_list = scrape_maple(session, now, prev_seen)
    all_listings.extend(maple_list)

    print("Scraping Aoba...")
    aoba_list = scrape_aoba(session, now, prev_seen)
    all_listings.extend(aoba_list)
    
    print("Scraping Tokai Yajima...")
    tokai_list = scrape_tokai(session, now, prev_seen)
    print(f"  - Tokai valid: {len(tokai_list)}")
    all_listings.extend(tokai_list)

    # FINAL FILTER: No mansions
    final_listings = [l for l in all_listings if l["propertyType"] != "mansion"]

    data = {
        "generatedAt": now,
        "fxRateUsd": 155.0,
        "fxRateCny": 20.0,
        "fxSource": "Manual",
        "listings": final_listings
    }
    
    with open(OUT_LISTINGS, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    build = {
        "generatedAt": now,
        "counts": {
            "total": len(final_listings),
            "izutaiyo": len([l for l in final_listings if l["source"]=="Izu Taiyo"]),
            "maple": len([l for l in final_listings if l["source"]=="Maple Housing"]),
            "aoba": len([l for l in final_listings if l["source"]=="Aoba Resort"]),
            "tokai": len([l for l in final_listings if l["source"]=="Tokai Yajima"])
        }
    }
    with open(OUT_BUILDINFO, "w", encoding="utf-8") as f:
        json.dump(build, f, ensure_ascii=False, indent=2)

    print(f"\nDone. Saved {len(final_listings)} listings.")
    print(f"  Izu Taiyo: {build['counts']['izutaiyo']}")
    print(f"  Maple:     {build['counts']['maple']}")
    print(f"  Aoba:      {build['counts']['aoba']}")
    print(f"  Tokai:     {build['counts']['tokai']}")

if __name__ == "__main__":
    main()