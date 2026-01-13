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
    "海 ：一望", "海： 一望", "海 ： 一望", "海が見え", "海見え", "オーシャンビュー",
    "海の見え", "海側", "海前", "海沿い", "シービュー", "ベイビュー", "海眺望",
    "海を一望", "海が一望", "オーシャンフロント", "ウォーターフロント"
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
    Handles whitespace variations.
    """
    if not text: return None
    # Normalize whitespace for better matching
    normalized = re.sub(r"\s+", "", text)
    for c in TARGET_CITIES_JP:
        # Also remove whitespace from target cities for comparison
        c_normalized = re.sub(r"\s+", "", c)
        if c_normalized in normalized: return c
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

    # 2. Address Table - Check with whitespace normalization
    markers = ["所在地", "住所", "Location", "物件所在地", "エリア"]
    for tag in soup.find_all(["th", "td", "dt", "dd", "div", "span"]):
        tag_text = tag.get_text()
        # Normalize whitespace for matching
        tag_normalized = re.sub(r"\s+", "", tag_text)

        for marker in markers:
            marker_normalized = re.sub(r"\s+", "", marker)
            if marker_normalized in tag_normalized:
                # Check this tag and next siblings
                candidates = [tag_text]
                sib = tag.find_next_sibling()
                if sib: candidates.append(sib.get_text())
                # Also check parent row if in table
                parent = tag.find_parent("tr")
                if parent: candidates.append(parent.get_text())

                for c in candidates:
                    city = normalize_city(c)
                    if city: return city

    # 3. Title
    h1 = soup.find("h1")
    if h1:
        city = normalize_city(h1.get_text())
        if city: return city

    # Also check h2 tags
    h2 = soup.find("h2")
    if h2:
        city = normalize_city(h2.get_text())
        if city: return city

    # 4. Full Text scan (expanded to first 1000 chars)
    return normalize_city(full_text[:1000])

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
            "22219": "下田",
            "22301": "河津",
            "22302": "東伊豆",
            "22304": "南伊豆"
        }

        found_links = {} # url -> city_context

        # Strategy: Scan their main pages directly - they use onclick JavaScript
        # Let's try the search result pages and parse onclick attributes
        # hpkind: 0=Mansion(skip), 1=House, 2=Land
        property_types = [1, 2]  # We want Houses and Land only

        for code, city_name in city_map.items():
            for hpkind in property_types:
                search_url = f"https://www.izutaiyo.co.jp/tokusen.php?hpcity[]={code}&hpkind={hpkind}"
                type_name = "House" if hpkind == 1 else "Land"
                print(f"  Fetching {city_name} {type_name}...")
                soup = self.fetch(search_url)
                if not soup:
                    print(f"  [WARNING] Failed to fetch {city_name} {type_name}")
                    continue

                # Look for onclick handlers with property IDs
                for tag in soup.find_all(True, onclick=True):
                    onclick = tag.get("onclick", "")
                    # Extract hpno from onclick like: location.href='d.php?hpno=12345'
                    match = re.search(r"d\.php\?hpno=(\d+)", onclick)
                    if match:
                        prop_id = match.group(1)
                        d_link = f"https://www.izutaiyo.co.jp/d.php?hpno={prop_id}"
                        found_links[d_link] = city_name

                    # Also check for hpbunno
                    match = re.search(r"d\.php\?hpbunno=([^'\"&]+)", onclick)
                    if match:
                        prop_id = match.group(1).strip()
                        d_link = f"https://www.izutaiyo.co.jp/d.php?hpbunno={prop_id}"
                        found_links[d_link] = city_name

                # Also try direct links
                for a in soup.find_all("a", href=True):
                    href = a['href']
                    if "d.php" in href and ("hpno=" in href or "hpbunno=" in href):
                        full = urljoin("https://www.izutaiyo.co.jp", href)
                        # Extract the city context
                        found_links[full] = city_name

        print(f"  > Processing {len(found_links)} unique listings...")

        for link, city_ctx in found_links.items():
            self.parse_detail(link, city_ctx)
            sleep_jitter()

    def parse_detail(self, url, city_ctx):
        STATS["scanned"] += 1
        soup = self.fetch(url)
        if not soup: return

        # Remove footer and nav (can contain misleading location info)
        for tag in soup.find_all(["footer", "nav", ".footer", ".navigation"]):
            tag.decompose()

        title = clean_text(soup.find("h1").get_text()) if soup.find("h1") else "Izu Taiyo Property"
        full_text = clean_text(soup.get_text())

        # 1. Location FIRST - Filter wrong cities before anything else
        city = get_location_trust(soup, full_text, city_ctx)
        if not city:
            # Be more lenient - log warning but try to extract
            # If we have context from search, use it even if extraction fails
            if city_ctx and any(c in city_ctx for c in TARGET_CITIES_JP):
                city = normalize_city(city_ctx)
            if not city:
                # Extract city name from title for debug
                title_preview = title if len(title) < 40 else title[:37] + "..."
                print(f"  [LOCATION FILTERED] Not in target area: {title_preview}")
                STATS["skipped_loc"] += 1
                return

        # 2. Sold?
        if is_contracted(title, full_text):
            STATS["skipped_sold"] += 1
            return

        # 3. Mansion? - Check for specific patterns, not just the word
        # Look for "マンション情報" (mansion information) not just "マンション" (which might be a property name)
        is_mansion = False
        if "マンション情報" in title or "マンション" in title and "情報" in title:
            is_mansion = True
        elif "mansion" in title.lower() and ("information" in title.lower() or "listing" in title.lower()):
            is_mansion = True
        elif "condo" in title.lower():
            is_mansion = True

        if is_mansion:
            print(f"  [MANSION FILTERED] {city} - {title[:60]}")
            STATS["skipped_mansion"] += 1
            return

        # 4. Sea View Scoring (More nuanced)
        sea_score = 0
        if any(k in full_text for k in SEA_KEYWORDS):
            sea_score = 4
        if "海は見えません" in full_text or "海眺望なし" in full_text:
            sea_score = 0
        # Boost score if "Walk to Sea" - be more lenient
        if sea_score == 0:
            has_sea_mention = any(k in full_text for k in ["海", "ビーチ", "Beach", "Ocean"])
            has_proximity = any(k in full_text for k in ["徒歩", "歩", "近", "分", "m"])
            if has_sea_mention and has_proximity:
                sea_score = 2

        # Extract price from multiple possible locations
        price = 0
        # Try table rows first
        for tr in soup.find_all("tr"):
            tr_text = tr.get_text()
            if any(k in tr_text for k in ["価格", "販売価格", "売買価格", "Price"]):
                price = extract_price(tr_text)
                if price > 0: break

        # If not found, try full text
        if price == 0:
            price = extract_price(full_text)

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
        # Try multiple pages and pagination
        base_urls = [
            "https://www.maple-h.co.jp/estate_db/house/",
            "https://www.maple-h.co.jp/estate_db/house/page/2/",
            "https://www.maple-h.co.jp/estate_db/house/page/3/",
            "https://www.maple-h.co.jp/estate_db/estate/",
            "https://www.maple-h.co.jp/estate_db/estate/page/2/",
            "https://www.maple-h.co.jp/estate_db/estate/page/3/"
        ]
        candidates = set()

        for u in base_urls:
            soup = self.fetch(u)
            if not soup: continue

            # Look for article entries (WordPress structure)
            for article in soup.find_all("article"):
                # Find links within article blocks
                for a in article.find_all("a", href=True):
                    href = a.get("href", "")
                    full = urljoin(u, href)

                    if "maple-h.co.jp/estate_db/" in full:
                        # Exclude pagination, feed, and meta pages
                        if not any(x in full for x in ["page/", "feed", "category/", "tag/", "author/", "#"]):
                            # Must have a property slug after category
                            path_parts = [p for p in urlparse(full).path.split('/') if p]
                            # Valid: ['estate_db', 'house', 'property-slug'] = 3 parts
                            if len(path_parts) >= 3 and path_parts[0] == "estate_db":
                                candidates.add(full)

        print(f"  > Processing {len(candidates)} candidates...")
        # Process all candidates
        for link in list(candidates):
            self.parse_detail(link)
            sleep_jitter()

    def parse_detail(self, url):
        STATS["scanned"] += 1
        soup = self.fetch(url)
        if not soup: return

        # Title extraction - try multiple selectors
        title = ""
        for selector in ["h1.entry-title", "h1", ".property-title", "title"]:
            elem = soup.select_one(selector)
            if elem:
                title = clean_text(elem.get_text())
                if "|" in title: title = title.split("|")[0]
                if "–" in title: title = title.split("–")[0]
                if title: break

        if not title: title = "Maple Property"

        full_text = clean_text(soup.get_text())

        if is_contracted(title, full_text):
            STATS["skipped_sold"] += 1
            return

        if any(k in title for k in MANSION_KEYWORDS):
            STATS["skipped_mansion"] += 1
            return

        city = get_location_trust(soup, full_text)
        if not city:
            # Be more lenient - if we can't find city, log but don't skip immediately
            print(f"  [WARNING] Could not determine city for: {url}")
            # Check if it mentions any target areas in full text
            if not any(c in full_text for c in TARGET_CITIES_JP):
                STATS["skipped_loc"] += 1
                return
            # Default to first found city or skip
            city = normalize_city(full_text)
            if not city:
                STATS["skipped_loc"] += 1
                return

        sea_score = 4 if any(k in full_text for k in SEA_KEYWORDS) else 0
        # Soft boost for proximity mentions
        if sea_score == 0 and any(k in full_text for k in ["海", "ビーチ", "Beach"]):
            if any(k in full_text for k in ["徒歩", "歩", "近", "分"]):
                sea_score = 2

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
        # We'll be more lenient here and filter by city after fetching
        # The area codes might not be in the listing page URLs

        # Try multiple pages
        urls = [
            "https://www.aoba-resort.com/house/",
            "https://www.aoba-resort.com/land/"
        ]
        candidates = set()

        # Exclude these known wrong area codes
        exclude_codes = ["ao22208", "ao22222", "ao22205"]  # Ito, Atami, etc.

        for u in urls:
            soup = self.fetch(u)
            if not soup: continue

            # Find all property links
            for a in soup.find_all("a", href=True):
                href = a['href']
                full = urljoin("https://www.aoba-resort.com", href)

                # Look for room pages
                if "room" in full and full.endswith(".html"):
                    # Exclude known wrong areas
                    has_exclude = any(code in full for code in exclude_codes)

                    if not has_exclude:
                        candidates.add(full)

        print(f"  > Processing {len(candidates)} candidates (will filter by city)...")
        # Process all candidates - parse_detail will filter by city
        for link in list(candidates)[:60]:  # Cap at 60 to avoid timeout
            self.parse_detail(link)
            sleep_jitter()

    def parse_detail(self, url):
        STATS["scanned"] += 1
        soup = self.fetch(url)
        if not soup: return

        # Extract city from URL as context (Aoba uses area codes in URLs)
        url_city_map = {
            "ao22219": "下田",
            "ao22301": "河津",
            "ao22302": "東伊豆",
            "ao22304": "南伊豆"
        }
        url_city = None
        for code, city_name in url_city_map.items():
            if code in url:
                url_city = city_name
                break

        # Try multiple title selectors
        title = ""
        for selector in ["h2", "h1", ".property-title", ".entry-title"]:
            if isinstance(selector, str) and selector.startswith("."):
                elem = soup.select_one(selector)
            else:
                elem = soup.find(selector)
            if elem:
                title = clean_text(elem.get_text())
                if title: break

        if not title:
            title_tag = soup.find("title")
            title = clean_text(title_tag.get_text()).split("|")[0] if title_tag else "Aoba Property"

        full_text = clean_text(soup.get_text())

        if is_contracted(title, full_text):
            STATS["skipped_sold"] += 1
            return

        if any(k in title for k in MANSION_KEYWORDS):
            STATS["skipped_mansion"] += 1
            return

        # Use URL city as context if available
        city = get_location_trust(soup, full_text, url_city)
        if not city:
            # If URL had area code, trust it
            if url_city:
                city = url_city
            else:
                print(f"  [WARNING] Could not determine city for: {url}")
                STATS["skipped_loc"] += 1
                return

        sea_score = 4 if any(k in full_text for k in SEA_KEYWORDS) else 0
        # Soft boost for proximity mentions
        if sea_score == 0 and any(k in full_text for k in ["海", "ビーチ", "Beach"]):
            if any(k in full_text for k in ["徒歩", "歩", "近", "分"]):
                sea_score = 2

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

    print("\n" + "="*50)
    print(" SCAN SUMMARY")
    print("="*50)
    print(f" Total Scanned:        {STATS['scanned']}")
    print(f" ✓ SAVED:              {STATS['saved']}")
    print(f" ✗ Skipped (Location): {STATS['skipped_loc']}")
    print(f" ✗ Skipped (Sold):     {STATS['skipped_sold']}")
    print(f" ✗ Skipped (Mansion):  {STATS['skipped_mansion']}")
    print(f" ✗ Errors:             {STATS['error']}")
    print("="*50)
    print(" Breakdown by Source:")
    for source, count in sorted(counts.items()):
        print(f"   {source}: {count}")
    print("="*50)

    if STATS['saved'] == 0:
        print("\n⚠️  WARNING: No listings saved!")
        print("   This may indicate:")
        print("   - Website structure has changed")
        print("   - Network connectivity issues")
        print("   - Filters are too restrictive")
    elif STATS['saved'] < 10:
        print("\n⚠️  WARNING: Very few listings saved ({})".format(STATS['saved']))
        print("   Expected: 50+ listings")
        print("   Check the website structure and filters")

if __name__ == "__main__":
    main()