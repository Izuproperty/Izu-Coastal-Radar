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
# Tiered scoring for more accuracy
# HIGH CONFIDENCE: Explicit sea view language (score 4)
HIGH_SEA_KEYWORDS = [
    "海一望", "海を望", "海望", "海が見え", "海見え", "オーシャンビュー",
    "海の見え", "海眺望", "海を一望", "海が一望", "オーシャンフロント",
    "シービュー", "ベイビュー", "ウォーターフロント", "海 ：一望", "海： 一望", "海 ： 一望"
]

# MEDIUM CONFIDENCE: Beach names, ocean names (score 3)
MEDIUM_SEA_KEYWORDS = [
    "白浜", "吉佐美", "入田", "多々戸", "相模湾", "太平洋", "オーシャン", "Ocean"
]

# For proximity scoring - used with "海" mention
PROXIMITY_KEYWORDS = ["徒歩", "歩", "近", "分", "m", "メートル"]

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

def get_izutaiyo_image(soup, url, property_id):
    """Izu Taiyo-specific image finder based on property ID"""
    # First, look for images on the page that contain the property ID
    for img in soup.find_all("img"):
        src = img.get("src", "")
        # Check if image contains property ID or looks like a property photo
        if property_id.lower() in src.lower():
            return urljoin(url, src)
        # Also check for numbered photo patterns (common for properties)
        if any(pattern in src.lower() for pattern in ["photo", "p01", "_1.", "_01.", "img01"]):
            # Skip logos and icons
            if not any(skip in src.lower() for skip in ["logo", "icon", "map", "button"]):
                full_url = urljoin(url, src)
                # Make sure it's a real image file
                if any(ext in full_url.lower() for ext in [".jpg", ".jpeg", ".png"]):
                    return full_url

    # Try to find the main property image div/section
    main_img_selectors = ["#mainimage img", ".mainimage img", "#photo img", ".photo img", ".bukken-image img"]
    for selector in main_img_selectors:
        img = soup.select_one(selector)
        if img and img.get("src"):
            src = img.get("src")
            if "logo" not in src.lower():
                return urljoin(url, src)

    # Last resort: find first large image that's not a logo
    for img in soup.find_all("img"):
        src = img.get("src", "")
        if not src:
            continue
        # Skip small images (likely logos/icons)
        width = img.get("width", "")
        if width and width.isdigit() and int(width) < 100:
            continue
        # Skip logos and navigation
        if any(skip in src.lower() for skip in ["logo", "icon", "nav", "button", "arrow", "banner"]):
            continue
        # Must be a valid image
        if any(ext in src.lower() for ext in [".jpg", ".jpeg", ".png"]):
            return urljoin(url, src)

    # Fallback to generic image finder
    return get_best_image(soup, url)

def extract_actual_city_from_title(title):
    """
    Extract the actual city name from Izu Taiyo title format.
    Titles are formatted as: CITY_NAME + PROPERTY + PRICE + の家情報/のマンション情報

    Returns the city name if found, otherwise None.
    Only returns if it's one of our target cities.
    """
    if not title: return None

    # Common Japanese city/town suffixes
    city_patterns = [
        r'^([^「（]+?[市町村])',  # City at start followed by city/town/village suffix
        r'^([^「（]+?[郡])',      # District
    ]

    for pattern in city_patterns:
        match = re.search(pattern, title)
        if match:
            potential_city = match.group(1).strip()
            # Check if this is one of our target cities
            for target_city in TARGET_CITIES_JP:
                if target_city in potential_city:
                    return target_city
            # If not a target city, return None (we'll filter it out)
            return None

    return None

def get_location_trust(soup, full_text, context_city=None):
    """
    Determines city.
    1. Extract from title using proper parsing (best for Izu Taiyo)
    2. Search Address Table.
    3. Search Title with normalize_city.
    4. Search Body.
    5. If nothing found, use context_city as fallback.

    CRITICAL: If title explicitly shows a city that's NOT in our target list,
    we return "WRONG_CITY" to signal rejection. Never fall back to context
    when we've detected the property is in the wrong location.
    """
    # NOTE: We check the page content FIRST because search results often
    # return properties from neighboring cities even when filtering by city code

    # 1. Try to extract city from title using proper parsing (Izu Taiyo format)
    h1 = soup.find("h1")
    if h1:
        city = extract_actual_city_from_title(h1.get_text())
        if city: return city
        # If extract found a city but it's not in target list, it returned None
        # In that case, we know this property is in wrong area, so REJECT
        title_text = h1.get_text()
        # Check if title starts with a city name that's not ours
        if re.match(r'^[^「（]+?[市町村郡]', title_text):
            # Title has a city name, but it's not in our target list
            # Return special marker to indicate this should be rejected
            return "WRONG_CITY"

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

    # 3. Title with normalize_city
    if h1:
        city = normalize_city(h1.get_text())
        if city: return city

    # Also check h2 tags
    h2 = soup.find("h2")
    if h2:
        city = normalize_city(h2.get_text())
        if city: return city

    # 4. Full Text scan - DO NOT use full text scan as it picks up keywords
    # city = normalize_city(full_text[:1000])
    # if city: return city

    # 5. Last resort: use search context if provided
    # BUT NEVER if we already determined the property is in wrong city
    if context_city and context_city in TARGET_CITIES_JP:
        return context_city

    # If we couldn't determine location, return None (will be filtered out)
    return None

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

        # Extract property ID from URL for image lookup
        property_id = None
        if "hpno=" in url:
            property_id = url.split("hpno=")[1].split("&")[0]
        elif "hpbunno=" in url:
            property_id = url.split("hpbunno=")[1].split("&")[0]

        # Remove footer and nav (can contain misleading location info)
        for tag in soup.find_all(["footer", "nav", ".footer", ".navigation"]):
            tag.decompose()

        title = clean_text(soup.find("h1").get_text()) if soup.find("h1") else "Izu Taiyo Property"
        full_text = clean_text(soup.get_text())

        # 1. Location FIRST - Filter wrong cities before anything else
        city = get_location_trust(soup, full_text, city_ctx)
        if city == "WRONG_CITY" or not city:
            # Extract city name from title for debug
            title_preview = title if len(title) < 40 else title[:37] + "..."
            if city == "WRONG_CITY":
                # Extract the actual wrong city name for better logging
                h1 = soup.find("h1")
                if h1:
                    match = re.match(r'^([^「（]+?[市町村郡])', h1.get_text())
                    if match:
                        wrong_city = match.group(1).strip()
                        print(f"  [LOCATION FILTERED] Wrong city {wrong_city}: {title_preview}")
                    else:
                        print(f"  [LOCATION FILTERED] Not in target area: {title_preview}")
                else:
                    print(f"  [LOCATION FILTERED] Not in target area: {title_preview}")
            else:
                print(f"  [LOCATION FILTERED] Could not determine city: {title_preview}")
            STATS["skipped_loc"] += 1
            return

        # 2. Sold?
        if is_contracted(title, full_text):
            STATS["skipped_sold"] += 1
            return

        # 3. Mansion? - Check for specific type indicators
        # "のマンション情報" = mansion listing, "の家情報" = house listing
        # Brokers often add "マンション" as a keyword tag, so we need to be specific
        is_mansion = False

        # Positive indicators it's a mansion
        if "のマンション情報" in title or "のマンション" in title:
            is_mansion = True
        elif "condo" in title.lower():
            is_mansion = True

        # Negative indicators it's NOT a mansion (override)
        if "の家情報" in title or "戸建" in title:
            is_mansion = False

        if is_mansion:
            print(f"  [MANSION FILTERED] {city} - {title[:60]}")
            STATS["skipped_mansion"] += 1
            return

        # 4. Sea View Scoring (Tiered for accuracy)
        sea_score = 0

        # Check for explicit "no sea view" statements first
        if "海は見えません" in full_text or "海眺望なし" in full_text or "海見えず" in full_text:
            sea_score = 0
        # HIGH: Explicit sea view language
        elif any(k in full_text for k in HIGH_SEA_KEYWORDS):
            sea_score = 4
        # MEDIUM: Famous beach names or ocean names
        elif any(k in full_text for k in MEDIUM_SEA_KEYWORDS):
            sea_score = 3
        # LOW: Walking distance to sea (proximity + sea mention)
        elif any(k in full_text for k in ["海", "ビーチ", "Beach"]):
            if any(k in full_text for k in PROXIMITY_KEYWORDS):
                sea_score = 2
            # Just generic "海" mention without view or proximity = score 0

        # 5. Sea View/Proximity Required - Filter out properties with no sea connection
        if sea_score == 0:
            title_preview = title if len(title) < 40 else title[:37] + "..."
            print(f"  [SEA VIEW FILTERED] No sea view or proximity: {title_preview}")
            STATS["skipped_loc"] += 1  # Using skipped_loc for now, could add new stat
            return

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

        # 6. Price validation - Exclude properties with no price (likely sold/unavailable)
        if price == 0:
            title_preview = title if len(title) < 40 else title[:37] + "..."
            print(f"  [PRICE FILTERED] No valid price found: {title_preview}")
            STATS["skipped_sold"] += 1
            return

        # Get image - use Izu Taiyo-specific method if we have property_id
        if property_id:
            img = get_izutaiyo_image(soup, url, property_id)
        else:
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
            "https://www.maple-h.co.jp/estate_db/estate/",
            "https://www.maple-h.co.jp/estate_db/estate/page/2/"
        ]
        candidates = set()

        for u in base_urls:
            soup = self.fetch(u)
            if not soup:
                print(f"  [DEBUG] Failed to fetch {u}")
                continue

            # Debug: Show what we found
            articles = soup.find_all("article")
            all_links = soup.find_all("a", href=True)
            estate_links = [a for a in all_links if "estate_db" in a.get("href", "")]

            print(f"  [DEBUG] {u}")
            print(f"    Found {len(articles)} article blocks")
            print(f"    Found {len(all_links)} total links")
            print(f"    Found {len(estate_links)} estate_db links")

            # Show more sample links to see actual property pages
            if len(estate_links) > 0:
                print(f"    Sample links (first 10):")
                for a in estate_links[:10]:
                    href = a.get("href", "")
                    full = urljoin(u, href)
                    print(f"      - {full}")

            # Extract property links - ignore article blocks since they don't exist
            # Just look for estate_db links that aren't navigation
            for a in soup.find_all("a", href=True):
                href = a.get("href", "")
                full = urljoin(u, href)

                # Must contain estate_db
                if "maple-h.co.jp/estate_db/" not in full:
                    continue

                # Skip base category URLs
                if full.rstrip('/') in [x.rstrip('/') for x in base_urls]:
                    continue

                # Skip navigation/meta pages
                if any(x in full for x in ["page/", "feed", "category/", "tag/", "author/", "/estate_db/#", "/estate_db$", "/house/$", "/estate/$"]):
                    continue

                # Must be longer than just the category (has property slug)
                # Looking for: /estate_db/house/PROPERTY-NAME/ or /estate_db/estate/PROPERTY-NAME/
                path = urlparse(full).path.rstrip('/')
                parts = [p for p in path.split('/') if p]

                # Valid property: ['estate_db', 'property-name'] (2+ parts)
                # Exclude category pages like /estate_db/house/ or /estate_db/estate/
                if len(parts) >= 2 and parts[0] == "estate_db":
                    # Exclude if second part is just a category (not a property)
                    category_pages = ["house", "estate", "office", "lease", "mansion", "land"]
                    if len(parts) == 2 and parts[1] in category_pages:
                        continue
                    # Also exclude if it contains these keywords anywhere
                    if any(cat in full for cat in ["/office/", "/lease/", "/mansion/"]):
                        continue
                    candidates.add(full)

        print(f"  > Processing {len(candidates)} candidates...")
        # Process all candidates
        for link in list(candidates)[:50]:  # Cap at 50
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
                # Check if it's the generic site title
                if "メープルハウジング" not in title and title:
                    break

        # If we got generic title, try to decode from URL
        if not title or "メープルハウジング" in title:
            try:
                from urllib.parse import unquote
                # URL like: .../6763%ef%bc%9a%e8%87%aa%e7%84%b6...
                # Extract the encoded part after estate_db/
                url_parts = url.split('/estate_db/')
                if len(url_parts) > 1:
                    encoded_title = url_parts[1].rstrip('/')
                    decoded = unquote(encoded_title)
                    # Extract property description after the ID and colon
                    if ':' in decoded or '：' in decoded:
                        # Split on either : or ：
                        for sep in ['：', ':']:
                            if sep in decoded:
                                title = decoded.split(sep, 1)[1].strip()
                                break
                    else:
                        title = decoded
            except:
                pass

        if not title: title = "Maple Property"

        full_text = clean_text(soup.get_text())

        if is_contracted(title, full_text):
            STATS["skipped_sold"] += 1
            return

        if any(k in title for k in MANSION_KEYWORDS):
            STATS["skipped_mansion"] += 1
            return

        city = get_location_trust(soup, full_text)
        if city == "WRONG_CITY" or not city:
            # Log and skip if wrong city or can't determine
            print(f"  [WARNING] Could not determine city for: {url}")
            STATS["skipped_loc"] += 1
            return

        # Sea View Scoring (Tiered for accuracy)
        sea_score = 0
        if "海は見えません" in full_text or "海眺望なし" in full_text or "海見えず" in full_text:
            sea_score = 0
        elif any(k in full_text for k in HIGH_SEA_KEYWORDS):
            sea_score = 4
        elif any(k in full_text for k in MEDIUM_SEA_KEYWORDS):
            sea_score = 3
        elif any(k in full_text for k in ["海", "ビーチ", "Beach"]):
            if any(k in full_text for k in PROXIMITY_KEYWORDS):
                sea_score = 2

        # Filter out properties with no sea connection
        if sea_score == 0:
            print(f"  [SEA VIEW FILTERED] No sea view or proximity: {url}")
            STATS["skipped_loc"] += 1
            return

        price = extract_price(full_text)

        # Price validation - Exclude properties with no price (likely sold/unavailable)
        if price == 0:
            print(f"  [PRICE FILTERED] No valid price found: {url}")
            STATS["skipped_sold"] += 1
            return

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
        # Scan general listing pages, but filter by area code in URLs
        urls = [
            "https://www.aoba-resort.com/house/",
            "https://www.aoba-resort.com/house/page/2/",
            "https://www.aoba-resort.com/land/",
            "https://www.aoba-resort.com/land/page/2/",
        ]
        candidates = set()

        # Target area codes (Shimoda, Kawazu, Higashi-Izu, Minami-Izu)
        target_codes = ["ao22219", "ao22301", "ao22302", "ao22304"]
        # Exclude these known wrong area codes (Ito, Atami, Izu city)
        exclude_codes = ["ao22208", "ao22222", "ao22205"]

        for u in urls:
            soup = self.fetch(u)
            if not soup:
                print(f"  [DEBUG] Failed to fetch {u}")
                continue

            # Debug: Show what we found
            all_links = soup.find_all("a", href=True)
            html_links = [a for a in all_links if a.get("href", "").endswith(".html")]
            room_links = [a for a in all_links if "room" in a.get("href", "")]

            print(f"  [DEBUG] {u}")
            print(f"    Found {len(all_links)} total links")
            print(f"    Found {len(html_links)} .html links")
            print(f"    Found {len(room_links)} 'room' links")

            # Find all property links - be more permissive initially
            # We'll filter by actual location in parse_detail()
            for a in soup.find_all("a", href=True):
                href = a['href']
                full = urljoin("https://www.aoba-resort.com", href)

                # Look for property pages (room + .html or /house/ or /land/)
                is_property = False
                if "room" in full and full.endswith(".html"):
                    is_property = True
                elif "/house/" in full and full.endswith(".html"):
                    # Exclude the main category page itself
                    if full.rstrip('/') not in [u.rstrip('/') for u in urls]:
                        is_property = True
                elif "/land/" in full and full.endswith(".html"):
                    # Exclude the main category page itself
                    if full.rstrip('/') not in [u.rstrip('/') for u in urls]:
                        is_property = True

                if is_property:
                    # Exclude known wrong areas if area code is in URL
                    has_exclude = any(code in full for code in exclude_codes)
                    if not has_exclude and full not in urls:
                        candidates.add(full)

            print(f"    Found {len(candidates)} candidate property links (before location filtering)")

        print(f"  > Found {len(candidates)} property pages")
        print(f"  > Processing {len(candidates)} candidates (will filter by city later)...")

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
        if city == "WRONG_CITY":
            # Property is explicitly from wrong city
            print(f"  [LOCATION FILTERED] Wrong city detected: {url}")
            STATS["skipped_loc"] += 1
            return
        if not city:
            # If URL had area code, trust it
            if url_city:
                city = url_city
            else:
                print(f"  [WARNING] Could not determine city for: {url}")
                STATS["skipped_loc"] += 1
                return

        # Sea View Scoring (Tiered for accuracy)
        sea_score = 0
        if "海は見えません" in full_text or "海眺望なし" in full_text or "海見えず" in full_text:
            sea_score = 0
        elif any(k in full_text for k in HIGH_SEA_KEYWORDS):
            sea_score = 4
        elif any(k in full_text for k in MEDIUM_SEA_KEYWORDS):
            sea_score = 3
        elif any(k in full_text for k in ["海", "ビーチ", "Beach"]):
            if any(k in full_text for k in PROXIMITY_KEYWORDS):
                sea_score = 2

        # Filter out properties with no sea connection
        if sea_score == 0:
            print(f"  [SEA VIEW FILTERED] No sea view or proximity: {url}")
            STATS["skipped_loc"] += 1
            return

        price = extract_price(full_text)

        # Price validation - Exclude properties with no price (likely sold/unavailable)
        if price == 0:
            print(f"  [PRICE FILTERED] No valid price found: {url}")
            STATS["skipped_sold"] += 1
            return

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