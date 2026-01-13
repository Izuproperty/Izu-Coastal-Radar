#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Izu Coastal Radar - Generator v16 (Failsafe & Context Trust)
"""

from __future__ import annotations
import datetime as dt
import json
import os
import random
import re
import time
from urllib.parse import urljoin, parse_qs, urlparse
import requests
from bs4 import BeautifulSoup
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# --- CONFIG ---
OUT_LISTINGS = "listings.json"
OUT_BUILDINFO = "buildInfo.json"

# We only want these areas
TARGET_CITIES_JP = ["下田", "河津", "東伊豆", "南伊豆", "賀茂郡"]

CITY_EN_MAP = {
    "下田": "Shimoda",
    "河津": "Kawazu",
    "東伊豆": "Higashi-Izu",
    "南伊豆": "Minami-Izu",
    "賀茂郡": "Minami-Izu"
}

# "Sea View" Validation
# If a property has these, it gets a high score.
SEA_KEYWORDS = [
    "オーシャン", "海一望", "海を望", "海望", "海近", "ビーチ", "Sea", "Ocean", 
    "白浜", "吉佐美", "入田", "多々戸", "眺望", "景色", "相模湾", "太平洋", "海岸", "伊豆七島",
    "海 ：一望", "海： 一望", "海 ： 一望", "海が見え", "海見え"
]

# Keywords to Identify House vs Land
HOUSE_KEYWORDS = ["戸建", "家", "建物", "LDK", "House", "Room", "築"]
LAND_KEYWORDS = ["売地", "土地", "Land", "建築条件"]

# Keywords to EXCLUDE (Mansions/Condos)
MANSION_KEYWORDS = ["マンション", "mansion", "condo"]

# Status Keywords (Exclude Sold)
CONTRACTED_KEYWORDS = ["成約", "商談中", "予約", "Sold", "Contracted", "Reserved", "済"]

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
}

# --- STATS ---
STATS = {
    "scanned": 0,
    "saved": 0,
    "skipped_mansion": 0,
    "skipped_sold": 0,
    "skipped_loc": 0,
    "error": 0
}

# --- HELPERS ---

def sleep_jitter():
    time.sleep(random.uniform(0.5, 1.5))

def clean_text(s):
    return re.sub(r"\s+", " ", s or "").strip()

def safe_int(s):
    try: return int(re.sub(r"[^\d]", "", s))
    except: return 0

def normalize_city(text):
    """
    Scans text for target city names.
    Returns the first match found.
    """
    if not text: return None
    for c in TARGET_CITIES_JP:
        if c in text: return c
    return None

def extract_price(text):
    if not text: return 0
    t = clean_text(text)
    try:
        # Pattern: 1億 2800万
        if "億" in t:
            parts = t.split("億")
            oku = safe_int(parts[0]) * 100000000
            man = safe_int(parts[1]) * 10000 if len(parts)>1 else 0
            return oku + man
        # Pattern: 3500万円
        m = re.search(r"([\d,]+)万", t)
        if m: return safe_int(m.group(1)) * 10000
        # Pattern: 12000000円
        m = re.search(r"([\d,]+)円", t)
        if m: return safe_int(m.group(1))
    except: pass
    return 0

def is_contracted(title, text):
    """Checks Title and sticky header text for Sold status"""
    combined = (title + " " + text[:200]).replace(" ", "")
    for k in CONTRACTED_KEYWORDS:
        if k in combined: return True
    return False

def determine_type(title, text):
    combined = (title + " " + text).lower()
    # If explicitly Land
    if any(k in title for k in ["売地", "土地"]): return "land"
    # If explicitly House
    if any(k in combined for k in HOUSE_KEYWORDS): return "house"
    # Fallback default
    return "house"

def get_best_image(soup, url):
    """Robust image finder: OG Tag -> Main ID -> First Large Image"""
    # 1. Meta Tag (Best quality usually)
    og = soup.find("meta", attrs={"property": "og:image"})
    if og and og.get("content"):
        return urljoin(url, og.get("content"))

    # 2. Known ID/Classes
    selectors = ["#main_img", ".main_img", ".wp-post-image", ".item_img img", ".swiper-slide img"]
    for sel in selectors:
        el = soup.select_one(sel)
        if el and el.get("src"):
            return urljoin(url, el.get("src"))
    
    # 3. Fallback: First image that looks like a photo (jpg/png) and not a logo
    for img in soup.find_all("img"):
        src = img.get("src", "")
        if not src: continue
        lower = src.lower()
        if "logo" in lower or "icon" in lower or "map" in lower: continue
        if ".jpg" in lower or ".jpeg" in lower or ".png" in lower:
            return urljoin(url, src)
    
    return ""

def get_location_trust(soup, full_text, context_city=None):
    """
    Determines city.
    1. If context_city is provided (from search URL), use it.
    2. Search Address Table.
    3. Search Title.
    4. Search Body.
    """
    # 1. Trust the search context!
    if context_city: return context_city

    # 2. Address Table
    markers = ["所在地", "住所", "Location"]
    for marker in markers:
        for tag in soup.find_all(["th", "td", "dt", "dd"]):
            if marker in tag.get_text():
                # Check this tag and next siblings
                candidates = [tag.get_text()]
                sib = tag.find_next_sibling()
                if sib: candidates.append(sib.get_text())
                
                for c in candidates:
                    city = normalize_city(c)
                    if city: return city

    # 3. Title
    h1 = soup.find("h1")
    if h1:
        city = normalize_city(h1.get_text())
        if city: return city

    # 4. Full Text scan
    return normalize_city(full_text[:500])

# --- SCRAPERS ---

class BaseScraper:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update(HEADERS)
        self.items = []

    def fetch(self, url):
        try:
            r = self.session.get(url, timeout=15, verify=False)
            if r.status_code != 200: return None
            r.encoding = r.apparent_encoding
            return BeautifulSoup(r.text, "html.parser")
        except:
            STATS["error"] += 1
            return None

    def add_item(self, item):
        self.items.append(item)
        print(f"  [SAVED] {item['source']}: {item['city']} - {item['title'][:30]}")

class IzuTaiyo(BaseScraper):
    def run(self):
        print("--- Scanning Izu Taiyo ---")
        # Map City Codes to Names for Context Trust
        # 22219=Shimoda, 22301=Kawazu, 22302=Higashi, 22304=Minami
        city_map = {
            "22219": "下田市",
            "22301": "河津町",
            "22302": "東伊豆町",
            "22304": "南伊豆町"
        }
        
        # We scan these specific city lists
        base_url = "https://www.izutaiyo.co.jp/tokusen.php?hpcity[]={}&hpkind=0"
        
        found_links = {} # url -> city_context

        for code, city_name in city_map.items():
            url = base_url.format(code)
            soup = self.fetch(url)
            if not soup: continue
            
            # Find detail links
            for a in soup.find_all("a", href=True):
                href = a['href']
                full = urljoin("https://www.izutaiyo.co.jp", href)
                
                # Check for ID
                if "hpno=" in full or "hpbunno=" in full:
                    # If it's a multi-ID list in URL param
                    if "tokusen.php" in full:
                        qs = parse_qs(urlparse(full).query)
                        if "hpno" in qs:
                            ids = re.split(r'[ +]+', qs["hpno"][0])
                            for i in ids:
                                if i.strip():
                                    d_link = f"https://www.izutaiyo.co.jp/d.php?hpno={i.strip()}"
                                    found_links[d_link] = city_name
                    else:
                        found_links[full] = city_name

        print(f"  > Processing {len(found_links)} unique listings...")
        
        for link, city_ctx in found_links.items():
            self.parse_detail(link, city_ctx)
            sleep_jitter()

    def parse_detail(self, url, city_ctx):
        STATS["scanned"] += 1
        soup = self.fetch(url)
        if not soup: return

        # Remove footer (common source of "Atami" false flags in old versions)
        for f in soup.find_all("footer"): f.decompose()
        
        title = clean_text(soup.find("h1").get_text()) if soup.find("h1") else "Izu Taiyo Property"
        full_text = clean_text(soup.get_text())

        # 1. Sold?
        if is_contracted(title, full_text):
            STATS["skipped_sold"] += 1
            return

        # 2. Mansion?
        if any(k in title for k in MANSION_KEYWORDS):
            STATS["skipped_mansion"] += 1
            return

        # 3. Location (Trust Context if extraction fails)
        city = get_location_trust(soup, full_text, city_ctx)
        if not city:
            STATS["skipped_loc"] += 1
            return

        # 4. Sea View (Soft Filter: Low score ok, but calculate it)
        sea_score = 4 if any(k in full_text for k in SEA_KEYWORDS) else 0
        if "海は見えません" in full_text: sea_score = 0
        # Boost score if "Walk to Sea"
        if sea_score == 0 and any(k in full_text for k in ["海", "ビーチ"]) and any(k in full_text for k in ["歩", "近", "分"]):
            sea_score = 3

        # Extract
        price = 0
        for tr in soup.find_all("tr"):
            if "価格" in tr.get_text():
                price = extract_price(tr.get_text())
                break
        
        img = get_best_image(soup, url)
        ptype = determine_type(title, full_text)

        self.add_item({
            "id": f"izutaiyo-{abs(hash(url))}",
            "source": "Izu Taiyo",
            "sourceUrl": url,
            "title": title,
            "titleEn": f"{CITY_EN_MAP.get(normalize_city(city), city)} Property",
            "propertyType": ptype,
            "city": city,
            "priceJpy": price,
            "seaViewScore": sea_score,
            "imageUrl": img
        })

class Maple(BaseScraper):
    def run(self):
        print("--- Scanning Maple ---")
        urls = [
            "https://www.maple-h.co.jp/estate_db/house/",
            "https://www.maple-h.co.jp/estate_db/estate/"
        ]
        candidates = set()
        for u in urls:
            soup = self.fetch(u)
            if not soup: continue
            for a in soup.select("a"):
                full = urljoin(u, a.get("href", ""))
                # Only listings
                if "maple-h.co.jp/estate_db/" in full and re.search(r"\d+", full):
                    candidates.add(full)

        print(f"  > Processing {len(candidates)} candidates...")
        # Cap at 50 to avoid timeouts
        for link in list(candidates)[:50]:
            self.parse_detail(link)
            sleep_jitter()

    def parse_detail(self, url):
        STATS["scanned"] += 1
        soup = self.fetch(url)
        if not soup: return

        # Title extraction
        h1 = soup.select_one("h1.entry-title")
        title = clean_text(h1.get_text()) if h1 else ""
        if not title:
            meta = soup.find("title")
            title = clean_text(meta.get_text()).split("|")[0] if meta else "Maple Property"

        full_text = clean_text(soup.get_text())

        if is_contracted(title, full_text):
            STATS["skipped_sold"] += 1
            return

        if any(k in title for k in MANSION_KEYWORDS):
            STATS["skipped_mansion"] += 1
            return

        city = get_location_trust(soup, full_text)
        if not city:
            STATS["skipped_loc"] += 1
            return

        sea_score = 4 if any(k in full_text for k in SEA_KEYWORDS) else 0
        price = extract_price(full_text)
        img = get_best_image(soup, url)
        ptype = determine_type(title, full_text)

        self.add_item({
            "id": f"maple-{abs(hash(url))}",
            "source": "Maple Housing",
            "sourceUrl": url,
            "title": title,
            "titleEn": f"{CITY_EN_MAP.get(normalize_city(city), city)} Property",
            "propertyType": ptype,
            "city": city,
            "priceJpy": price,
            "seaViewScore": sea_score,
            "imageUrl": img
        })

class Aoba(BaseScraper):
    def run(self):
        print("--- Scanning Aoba ---")
        urls = ["https://www.aoba-resort.com/house/", "https://www.aoba-resort.com/land/"]
        candidates = set()
        for u in urls:
            soup = self.fetch(u)
            if not soup: continue
            for a in soup.select("a[href*='room']"):
                if a['href'].endswith(".html"):
                    candidates.add(urljoin("https://www.aoba-resort.com", a['href']))

        print(f"  > Processing {len(candidates)} candidates...")
        for link in list(candidates)[:40]:
            self.parse_detail(link)
            sleep_jitter()

    def parse_detail(self, url):
        STATS["scanned"] += 1
        soup = self.fetch(url)
        if not soup: return

        h2 = soup.find("h2")
        title = clean_text(h2.get_text()) if h2 else "Aoba Property"
        full_text = clean_text(soup.get_text())

        if is_contracted(title, full_text):
            STATS["skipped_sold"] += 1
            return

        if any(k in title for k in MANSION_KEYWORDS):
            STATS["skipped_mansion"] += 1
            return

        city = get_location_trust(soup, full_text)
        if not city:
            STATS["skipped_loc"] += 1
            return

        sea_score = 4 if any(k in full_text for k in SEA_KEYWORDS) else 0
        price = extract_price(full_text)
        img = get_best_image(soup, url)
        ptype = determine_type(title, full_text)

        self.add_item({
            "id": f"aoba-{abs(hash(url))}",
            "source": "Aoba Resort",
            "sourceUrl": url,
            "title": title,
            "titleEn": f"{CITY_EN_MAP.get(normalize_city(city), city)} Property",
            "propertyType": ptype,
            "city": city,
            "priceJpy": price,
            "seaViewScore": sea_score,
            "imageUrl": img
        })

# --- MAIN ---

def main():
    scrapers = [IzuTaiyo(), Maple(), Aoba()]
    all_data = []
    
    for s in scrapers:
        s.run()
        all_data.extend(s.items)
        STATS["saved"] += len(s.items)

    out = {
        "generatedAt": dt.datetime.now().isoformat(),
        "listings": all_data
    }
    with open(OUT_LISTINGS, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)

    counts = {}
    for i in all_data:
        counts[i['source']] = counts.get(i['source'], 0) + 1
    
    with open(OUT_BUILDINFO, "w", encoding="utf-8") as f:
        json.dump({"counts": counts, "generatedAt": out["generatedAt"]}, f)

    print("\n-------------------------")
    print(" SCAN SUMMARY")
    print("-------------------------")
    print(f" Total Scanned:      {STATS['scanned']}")
    print(f" SAVED:              {STATS['saved']}")
    print(f" Skipped (Location): {STATS['skipped_loc']}")
    print(f" Skipped (Sold):     {STATS['skipped_sold']}")
    print(f" Skipped (Mansion):  {STATS['skipped_mansion']}")
    print("-------------------------")
    print(counts)

if __name__ == "__main__":
    main()