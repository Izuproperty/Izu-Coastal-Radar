#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Izu Coastal Radar listings generator (Izutaiyo + Maple + Aoba)

Outputs:
  - listings.json
  - buildInfo.json

Updates:
- LOCATIONS: Shimoda, Higashi-Izu, Minami-Izu, AND Kawazu allowed.
- Filters: No Condos, Sea View/Walk required.
- Debugging: Verbose prints retained for troubleshooting.
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

OUT_LISTINGS = "listings.json"
OUT_BUILDINFO = "buildInfo.json"

SLEEP_MIN = 0.50
SLEEP_MAX = 1.00

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

SEA_KEYWORDS = [
    "海", "オーシャン", "海望", "海一望", "相模湾", "駿河湾", 
    "太平洋", "海近", "海岸", "ビーチ", "Sea", "Ocean", "白浜"
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
    if not s:
        return ""
    s = re.sub(r"[ \t\r\f\v]+", " ", s)
    s = re.sub(r"\u3000+", " ", s)
    return s.strip()

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

def year_from_text(text: str) -> Optional[int]:
    t = clean_text(text)
    if not t: return None
    m = re.search(r"(19\d{2}|20\d{2})\s*年", t)
    if m: return int(m.group(1))
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
    """
    Scans text for strict target cities.
    Handles 'Kamo-gun Higashi-Izu-cho' by finding 'Higashi-Izu'.
    """
    if not text: return None
    # Check strictly allowed list only
    if "東伊豆" in text: return "東伊豆町"
    if "南伊豆" in text: return "南伊豆町"
    if "河津" in text: return "河津町"
    if "下田" in text: return "下田市"
    return None

def detect_city_in_text(text: str) -> Optional[str]:
    # Helper for debugging prints
    if "東伊豆" in text: return "東伊豆町"
    if "南伊豆" in text: return "南伊豆町"
    if "河津" in text: return "河津町"
    if "下田" in text: return "下田市"
    if "伊東" in text: return "伊東市"
    if "伊豆市" in text: return "伊豆市"
    if "熱海" in text: return "熱海市"
    return None

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

def scan_izutaiyo_urls(session) -> Set[str]:
    """Grab ALL property URLs first."""
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
        text = clean_text(soup.get_text(" ", strip=True))

        m_id = re.search(r"hp(?:bun)?no=([A-Za-z0-9]+)", url)
        raw_id = m_id.group(1) if m_id else str(abs(hash(url)))
        
        # 1. Title
        title = ""
        h = soup.find(["h1", "h2"])
        if h: title = clean_text(h.get_text())
        
        # 2. Location Check
        city = normalize_city_jp(text)
        if not city:
            found_city = detect_city_in_text(text)
            if found_city:
                print(f"    [Skip IzuTaiyo] City not allowed: {found_city} ({url})")
            else:
                print(f"    [Skip IzuTaiyo] No city detected ({url})")
            return None, True

        # 3. Type Filter (Mansion)
        if "マンション" in title or "マンション" in text:
            is_mansion = False
            for tr in soup.find_all("tr"):
                 if "種目" in tr.get_text() and "マンション" in tr.get_text():
                     is_mansion = True
            if is_mansion:
                print(f"    [Skip IzuTaiyo] Condo detected: {title}")
                return None, True
        
        # 4. Sea View Filter
        sea_score = 0
        if any(k in text for k in SEA_KEYWORDS): sea_score = 4
        if "海まで徒歩" in text or "海へ徒歩" in text: sea_score = max(sea_score, 3)
        
        if sea_score == 0:
             print(f"    [Skip IzuTaiyo] No Sea View keywords found: {title}")
             return None, True

        # Property Type
        ptype = "house"
        if "土地" in title and "戸建" not in title: ptype = "land"
        elif "売地" in title: ptype = "land"

        price = yen_to_int(text)
        if not price:
            for tr in soup.find_all("tr"):
                if "価格" in clean_text(tr.get_text()):
                    price = yen_to_int(clean_text(tr.get_text()))
                    break

        land_sqm = None
        m_l = re.search(r"土地(?:面積)?[:：]?\s*([0-9\.]+)\s*㎡", text)
        if m_l: land_sqm = safe_float(m_l.group(1))

        bldg_sqm = None
        m_b = re.search(r"(?:建物|延床)面積[:：]?\s*([0-9\.]+)\s*㎡", text)
        if m_b: bldg_sqm = safe_float(m_b.group(1))

        year = year_from_text(text)
        age = compute_age(year)

        img = None
        og = soup.find("meta", attrs={"property": "og:image"})
        if og: img = og.get("content")
        if img and img.startswith("/"):
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

    except Exception as e:
        print(f"    [Error IzuTaiyo] {e}")
        return None, False


# ----------------------------
# Maple
# ----------------------------

def parse_maple_detail(session, url, ptype) -> Tuple[Optional[dict], bool]:
    try:
        r = request(session, url)
        soup = BeautifulSoup(r.text, "html.parser")
        text = clean_text(soup.get_text(" ", strip=True))

        city = normalize_city_jp(text)
        if not city: 
            return None, True 

        # Filter: Sea View or Walk
        sea = any(k in text for k in SEA_KEYWORDS)
        walk = any(k in text for k in WALK_KEYWORDS) and any(k in text for k in ["海", "ビーチ"])
        
        if not (sea or walk):
            return None, True

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

        year = year_from_text(text)
        age = compute_age(year)

        tags = []
        if "温泉" in text and any(k in text for k in ["有", "付", "権利", "引込"]):
            if "温泉無" not in text:
                tags.append("Onsen")

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
    except:
        return None, False

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
            for p in list(links)[:5]: 
                try:
                    r2 = request(session, p)
                    s2 = BeautifulSoup(r2.text, "html.parser")
                    for a in s2.find_all("a", href=True):
                        u = a['href']
                        if "/estate_db/" in u and re.search(r"\d+", u):
                             if "tag" not in u and "page" not in u:
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
        text = clean_text(soup.get_text(" ", strip=True))

        city = normalize_city_jp(text)
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

        year = year_from_text(text)
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
    except:
        return None, False

def scrape_aoba(session, now, prev):
    listings = []
    for slug, ptype in {"house": "house", "land": "land"}.items():
        base = f"{AOBA_BASE}/{slug}/"
        print(f"  - Aoba {slug}...")
        try:
            r = request(session, base)
            soup = BeautifulSoup(r.text, "html.parser")
            pages = [base]
            for a in soup.select("a.page-numbers"):
                if a.get("href"): pages.append(urljoin(base, a.get("href")))
            
            detail_urls = set()
            for p in list(set(pages))[:5]:
                try:
                    r2 = request(session, p)
                    s2 = BeautifulSoup(r2.text, "html.parser")
                    for a in s2.select("a[href*='room']"):
                        if a['href'].endswith(".html"):
                            detail_urls.add(urljoin(AOBA_BASE, a['href']))
                except: pass
                sleep_jitter()

            for u in detail_urls:
                item, ok = parse_aoba_detail(session, u, ptype)
                if ok and item:
                    item["firstSeen"] = apply_first_seen(item["id"], now, prev)
                    listings.append(item)
                sleep_jitter()
        except: pass
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
    events = {} 
    
    izutaiyo_urls = scan_izutaiyo_urls(session)
    print(f"  - Found {len(izutaiyo_urls)} IzuTaiyo candidates. Checking details...")
    
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
            "aoba": len([l for l in final_listings if l["source"]=="Aoba Resort"])
        }
    }
    with open(OUT_BUILDINFO, "w", encoding="utf-8") as f:
        json.dump(build, f, ensure_ascii=False, indent=2)

    print(f"\nDone. Saved {len(final_listings)} listings.")
    print(f"  Izu Taiyo: {build['counts']['izutaiyo']}")
    print(f"  Maple:     {build['counts']['maple']}")
    print(f"  Aoba:      {build['counts']['aoba']}")

if __name__ == "__main__":
    main()