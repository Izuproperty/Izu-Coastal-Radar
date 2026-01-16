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
    "白浜", "吉佐美", "入田", "多々戸", "相模湾", "太平洋", "オーシャン", "Ocean",
    "城ヶ崎海岸"  # Jogasaki Coast
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

# --- FOREX RATE ---

def get_usd_jpy_rate():
    """
    Fetch current USD/JPY exchange rate from free API.
    Falls back to 155 if API fails.
    """
    try:
        # Using frankfurter.app - free, no API key required
        response = requests.get("https://api.frankfurter.app/latest?from=USD&to=JPY", timeout=5)
        if response.status_code == 200:
            data = response.json()
            rate = data.get("rates", {}).get("JPY")
            if rate:
                print(f"  [FOREX] Fetched USD/JPY rate: ¥{rate:.2f}/$1")
                return round(rate, 2)
    except Exception as e:
        print(f"  [FOREX] Failed to fetch rate: {e}")

    # Fallback
    print(f"  [FOREX] Using fallback rate: ¥155/$1")
    return 155

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

    # Limit search to first 3000 chars to avoid concatenating page-wide digits
    t = t[:3000]

    try:
        # Pattern: 1億 2800万
        if "億" in t:
            parts = t.split("億")
            # Only look at first 20 chars of each part to avoid digit concatenation
            oku_text = parts[0][-20:] if len(parts[0]) > 20 else parts[0]
            oku = safe_int(oku_text) * 100000000
            man = 0
            if len(parts) > 1:
                man_text = parts[1][:20]
                man = safe_int(man_text) * 10000
            price = oku + man
            # Sanity check: max 10億円 (1 billion yen, ~$7M USD)
            if price > 0 and price <= 1000000000:
                return price
            return 0

        # Pattern: 3500万円
        m = re.search(r"([\d,]+)万", t)
        if m:
            price = safe_int(m.group(1)) * 10000
            # Sanity check: max 10億円
            if price > 0 and price <= 1000000000:
                return price

        # Pattern: 12000000円
        m = re.search(r"([\d,]+)円", t)
        if m:
            price = safe_int(m.group(1))
            # Sanity check: max 10億円
            if price > 0 and price <= 1000000000:
                return price
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
    # 1. Meta Tag (Best quality usually) - but skip if it's a logo
    og = soup.find("meta", attrs={"property": "og:image"})
    if og and og.get("content"):
        og_url = og.get("content")
        og_lower = og_url.lower()
        # Skip if it's obviously a logo
        if not any(skip in og_lower for skip in ["logo", "rogo", "icon", "og.png", "og.jpg"]):
            return urljoin(url, og_url)

    # 2. Known ID/Classes
    selectors = ["#main_img", ".main_img", ".wp-post-image", ".item_img img", ".swiper-slide img"]
    for sel in selectors:
        el = soup.select_one(sel)
        if el and el.get("src"):
            src = el.get("src")
            src_lower = src.lower()
            # Skip logos and banners
            if not any(skip in src_lower for skip in ["logo", "rogo", "icon", "bnr", "banner", "tel.gif"]):
                return urljoin(url, src)

    # 3. Fallback: First image that looks like a photo (jpg/png) and not a logo
    for img in soup.find_all("img"):
        src = img.get("src", "")
        if not src: continue
        lower = src.lower()
        if any(skip in lower for skip in ["logo", "rogo", "icon", "map", "banner", "bnr", "nav", "/title/", "tel.gif"]): continue
        if ".jpg" in lower or ".jpeg" in lower or ".png" in lower:
            return urljoin(url, src)

    return ""

def get_izutaiyo_image(soup, url, property_id):
    """Izu Taiyo-specific image finder - constructs image URLs from property ID"""
    # Izu Taiyo images follow a predictable pattern:
    # Property ID: SMB410H -> Images at: bb/sm/smb410ha.jpg, bb/sm/smb410hb.jpg, etc.

    if property_id:
        # Convert property ID to lowercase for image path
        prop_lower = property_id.lower()
        # Get first 2 characters for directory
        dir_name = prop_lower[:2]

        # Try constructing image URLs (a, b, c variants)
        for letter in ['a', 'b', 'c']:
            img_path = f"bb/{dir_name}/{prop_lower}{letter}.jpg"
            img_url = urljoin(url, img_path)
            print(f"  [DEBUG] Constructed Izu Taiyo image URL: {img_url}")
            # Return the first one (they can check others on the site)
            return img_url

    # Fallback to searching the page
    print(f"  [DEBUG] No property ID available for image construction")

    # Strategy 1: Look for image URLs in noscript or commented sections
    # Check for images in pattern bb/{dir}/{id}{letter}.jpg
    import re
    page_text = str(soup)
    img_pattern = r'bb/\w+/\w+[a-z]\.jpg'
    img_matches = re.findall(img_pattern, page_text)

    if img_matches:
        # Use the first match
        img_url = urljoin(url, img_matches[0])
        print(f"  [DEBUG] Found Izu Taiyo image in HTML: {img_url}")
        return img_url
    # Strategy 2: Fall back to img tags as last resort
    all_imgs = soup.find_all("img")
    candidates = []

    print(f"  [DEBUG] Falling back to img tag search, found {len(all_imgs)} img tags")

    for img in all_imgs:
        src = img.get("src", "")
        if not src:
            continue

        # Get full URL
        full_url = urljoin(url, src)
        lower_src = src.lower()

        # Debug: show what we're examining
        print(f"  [DEBUG]   Examining: {src[:60]}")

        # Skip obvious non-property images (EXPLICIT rogo.jpg check)
        skip_keywords = ["logo", "rogo", "icon", "banner", "bnr", "nav", "button", "arrow", "spacer", "bg_", "/title/", "tel.gif"]
        should_skip = any(skip in lower_src for skip in skip_keywords)
        if should_skip:
            print(f"  [DEBUG]     -> SKIP (contains excluded keyword)")
            continue

        # Must be an image file
        if not any(ext in lower_src for ext in [".jpg", ".jpeg", ".png", ".gif"]):
            print(f"  [DEBUG]     -> SKIP (not image extension)")
            continue

        # Prefer images that look like property photos
        priority = 10  # Base priority for any valid image

        if property_id and property_id.lower() in lower_src:
            priority = 100  # Highest priority
            print(f"  [DEBUG]     -> MATCH property ID! Priority: {priority}")
        elif any(pattern in lower_src for pattern in ["photo", "img", "pic", "image", "_1", "_01", "p01"]):
            priority = 50
            print(f"  [DEBUG]     -> Looks like photo. Priority: {priority}")
        else:
            print(f"  [DEBUG]     -> Generic image. Priority: {priority}")

        # Check size if available
        width = img.get("width", "")
        height = img.get("height", "")
        if width and width.isdigit():
            w = int(width)
            if w >= 200:  # Prefer larger images
                priority += 20
                print(f"  [DEBUG]     -> Large image ({w}px), bonus +20")
            elif w < 100:  # Penalize tiny images
                priority -= 30
                print(f"  [DEBUG]     -> Tiny image ({w}px), penalty -30")

        if priority > 0:  # Only add if priority is positive
            candidates.append((priority, full_url))
            print(f"  [DEBUG]     -> Added as candidate with priority {priority}")

    # Sort by priority and return best match
    if candidates:
        candidates.sort(reverse=True, key=lambda x: x[0])
        best_priority, best_url = candidates[0]
        print(f"  [DEBUG] ✓ Selected image for {property_id}: priority={best_priority}")
        print(f"  [DEBUG]   URL: {best_url}")
        return best_url

    # Fallback - but log it
    print(f"  [DEBUG] ✗ No valid image candidates for {property_id} (found {len(all_imgs)} total images)")
    print(f"  [DEBUG]   Trying generic fallback...")
    fallback_img = get_best_image(soup, url)
    if fallback_img:
        print(f"  [DEBUG]   Fallback found: {fallback_img}")
    else:
        print(f"  [DEBUG]   No fallback image found either!")
    return fallback_img

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
        # Location codes for the search endpoint (s.php)
        # These are used in the search form with format: 下田市[sm]
        location_codes = {
            "sm": "下田",    # Shimoda
            "kw": "河津",    # Kawazu
            "hi": "東伊豆",  # Higashi-Izu
            "mi": "南伊豆"   # Minami-Izu
        }

        found_links = {} # url -> city_context

        # Track specific properties we're looking for
        target_properties = ["SMB240H", "SMB225H", "SMB368H", "SMB195H", "SMB392H"]
        target_found = {prop: False for prop in target_properties}

        # Strategy: Use the regular search endpoint (s.php) instead of featured (tokusen.php)
        # The search form allows filtering by location and sea view conditions
        # Property types: 家 (House) and 土地 (Land) - must be searched separately

        property_types = [
            ("家", "House"),
            ("土地", "Land")
        ]

        for loc_code, city_name in location_codes.items():
            for prop_type, type_name in property_types:
                # Pagination: Loop through pages until no more results
                page = 1
                max_pages = 10  # Safety limit

                while page <= max_pages:
                    # Build search URL with sea view filters
                    # The s.php endpoint uses location codes like [sm], [kw], etc.
                    # and includes sea view conditions
                    search_url = f"https://www.izutaiyo.co.jp/s.php?ar[]={loc_code}&mk[]=海が見える&mk[]=海へ歩いて行ける&kd={prop_type}"

                    if page > 1:
                        search_url += f"&page={page}"

                    print(f"  Fetching {city_name} {type_name} (page {page})...")
                    soup = self.fetch(search_url)
                    if not soup:
                        print(f"  [WARNING] Failed to fetch {city_name} {type_name} page {page}")
                        break

                    # Track if we found any properties on this page
                    page_found_count = 0

                    # DEBUG: For Shimoda searches, capture ALL property IDs found
                    if city_name == "下田":
                        debug_property_ids = []

                    # Look for onclick handlers with property IDs
                    onclick_tags = soup.find_all(True, onclick=True)
                    if city_name == "下田":
                        print(f"    [DEBUG] Found {len(onclick_tags)} tags with onclick handlers")

                    for tag in onclick_tags:
                        onclick = tag.get("onclick", "")
                        # Extract hpno from onclick like: location.href='d.php?hpno=12345' or 'hpno=SMB240H'
                        match = re.search(r"d\.php\?hpno=(\w+)", onclick)
                        if match:
                            prop_id = match.group(1)
                            d_link = f"https://www.izutaiyo.co.jp/d.php?hpno={prop_id}"

                            # DEBUG: Track all Shimoda property IDs
                            if city_name == "下田":
                                debug_property_ids.append(prop_id)
                                # Check if this is one of our target properties
                                if prop_id in target_properties:
                                    print(f"    [DEBUG] *** FOUND TARGET PROPERTY: {prop_id} ***")
                                    target_found[prop_id] = True

                            if d_link not in found_links:
                                found_links[d_link] = city_name
                                page_found_count += 1

                        # Also check for hpbunno
                        match = re.search(r"d\.php\?hpbunno=([^'\"&]+)", onclick)
                        if match:
                            prop_id = match.group(1).strip()
                            d_link = f"https://www.izutaiyo.co.jp/d.php?hpbunno={prop_id}"

                            # DEBUG: Track Shimoda properties with hpbunno
                            if city_name == "下田":
                                debug_property_ids.append(f"{prop_id}(bunno)")
                                if prop_id in target_properties:
                                    print(f"    [DEBUG] *** FOUND TARGET PROPERTY (bunno): {prop_id} ***")
                                    target_found[prop_id] = True

                            if d_link not in found_links:
                                found_links[d_link] = city_name
                                page_found_count += 1

                    # Also try direct links
                    direct_links = soup.find_all("a", href=True)
                    d_php_links = [a for a in direct_links if "d.php" in a.get("href", "")]
                    if city_name == "下田":
                        print(f"    [DEBUG] Found {len(d_php_links)} direct d.php links")

                    for a in direct_links:
                        href = a['href']
                        if "d.php" in href and ("hpno=" in href or "hpbunno=" in href):
                            full = urljoin("https://www.izutaiyo.co.jp", href)

                            # DEBUG: Extract property ID from direct link
                            if city_name == "下田":
                                if "hpno=" in href:
                                    prop_id_match = re.search(r"hpno=(\w+)", href)
                                    if prop_id_match:
                                        link_prop_id = prop_id_match.group(1)
                                        if link_prop_id not in debug_property_ids:
                                            debug_property_ids.append(link_prop_id + "(direct)")
                                        if link_prop_id in target_properties:
                                            print(f"    [DEBUG] *** FOUND TARGET PROPERTY (direct link): {link_prop_id} ***")
                                            target_found[link_prop_id] = True

                            if full not in found_links:
                                # Extract the city context
                                found_links[full] = city_name
                                page_found_count += 1

                    # DEBUG: Show all property IDs found on this page for Shimoda
                    if city_name == "下田" and debug_property_ids:
                        print(f"    [DEBUG] Property IDs on this page ({len(debug_property_ids)}): {', '.join(debug_property_ids[:20])}")
                        if len(debug_property_ids) > 20:
                            print(f"    [DEBUG] ... and {len(debug_property_ids) - 20} more")

                    # If no properties found on this page, stop pagination for this search
                    if page_found_count == 0:
                        print(f"    No new properties on page {page}, ending pagination")
                        break
                    else:
                        print(f"    Found {page_found_count} new properties on page {page}")

                    page += 1

        print(f"  > Processing {len(found_links)} unique listings...")

        # DEBUG: Report on target properties
        print("\n" + "="*60)
        print("TARGET PROPERTY SEARCH RESULTS:")
        print("="*60)
        for prop, found in target_found.items():
            status = "✓ FOUND" if found else "✗ NOT FOUND"
            print(f"  {prop}: {status}")
        print("="*60 + "\n")

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

        # Special debug logging for specific properties
        is_special = property_id in ["KW2002H", "SMB240H", "SMB225H", "SMB368H", "SMB195H"]
        if is_special:
            print(f"\n{'='*60}")
            print(f"SPECIAL PROPERTY DEBUG: {property_id}")
            print(f"URL: {url}")
            print(f"{'='*60}")

        # Remove footer and nav (can contain misleading location info)
        for tag in soup.find_all(["footer", "nav", ".footer", ".navigation"]):
            tag.decompose()

        title = clean_text(soup.find("h1").get_text()) if soup.find("h1") else "Izu Taiyo Property"
        full_text = clean_text(soup.get_text())

        # 1. Location FIRST - Filter wrong cities before anything else
        city = get_location_trust(soup, full_text, city_ctx)
        if is_special:
            print(f"  City detected: {city}")
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
            if is_special:
                print(f"  >>> SPECIAL PROPERTY {property_id} REJECTED: Location check failed")
            STATS["skipped_loc"] += 1
            return

        # 2. Sold?
        if is_contracted(title, full_text):
            if is_special:
                print(f"  >>> SPECIAL PROPERTY {property_id} REJECTED: Property is sold/contracted")
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
            if is_special:
                print(f"  >>> SPECIAL PROPERTY {property_id} REJECTED: Mansion/condo")
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
        # LOW: Walking distance to sea (stricter check to avoid false positives)
        elif any(k in full_text for k in ["海", "ビーチ", "Beach"]):
            # Require explicit distance/time measurements to avoid false positives
            # Must have numbers: "海まで徒歩5分", "海から100m", etc.
            proximity_patterns = [
                r"海まで徒歩[0-9０-９]",          # 海まで徒歩5分
                r"海まで.*[0-9０-９]+.*分",       # 海まで約5分
                r"海まで.*[0-9０-９]+.*[mｍメートル]",  # 海まで100m
                r"海から[0-9０-９]+.*[mｍメートル]",    # 海から100m
                r"徒歩[0-9０-９]+.*分.*海",       # 徒歩5分で海
                r"ビーチまで.*[0-9０-９]+",      # ビーチまで5分
                r"海.*徒歩圏",                    # 海が徒歩圏内
            ]
            if any(re.search(pattern, full_text) for pattern in proximity_patterns):
                sea_score = 2
            # Just generic "海" mention without distance/time = score 0

        # 5. Filter by sea view score - only include properties with clear sea connection
        # Minimum score of 2 required (explicit proximity or better)
        MIN_SEA_SCORE = 2
        if is_special:
            print(f"  Sea view score: {sea_score} (minimum required: {MIN_SEA_SCORE})")
            # Show which keywords matched
            if sea_score == 4:
                matched = [k for k in HIGH_SEA_KEYWORDS if k in full_text]
                print(f"    Matched HIGH keywords: {matched[:3]}")
            elif sea_score == 3:
                matched = [k for k in MEDIUM_SEA_KEYWORDS if k in full_text]
                print(f"    Matched MEDIUM keywords: {matched[:3]}")
            elif sea_score == 2:
                print(f"    Matched proximity patterns")
        if sea_score < MIN_SEA_SCORE:
            title_preview = title if len(title) < 40 else title[:37] + "..."
            print(f"  [SEA VIEW FILTERED] Insufficient sea connection (score={sea_score}): {title_preview}")
            if is_special:
                print(f"  >>> SPECIAL PROPERTY {property_id} REJECTED: Sea view score too low ({sea_score} < {MIN_SEA_SCORE})")
            STATS["skipped_loc"] += 1  # Count as location filter
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
        if is_special:
            print(f"  Price extracted: {price} JPY")
        if not price or price <= 0:
            title_preview = title if len(title) < 40 else title[:37] + "..."
            print(f"  [PRICE FILTERED] No valid price found: {title_preview} (price={price})")
            if is_special:
                print(f"  >>> SPECIAL PROPERTY {property_id} REJECTED: No valid price")
            STATS["skipped_sold"] += 1
            return

        # Get image - use Izu Taiyo-specific method if we have property_id
        if property_id:
            img = get_izutaiyo_image(soup, url, property_id)
        else:
            img = get_best_image(soup, url)

        ptype = determine_type(title, full_text)

        if is_special:
            print(f"  >>> SPECIAL PROPERTY {property_id} PASSED ALL FILTERS")
            print(f"      City: {city}, Price: {price}, Sea Score: {sea_score}")
            print(f"{'='*60}\n")

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
                    # ALSO exclude if URL ends with a category page (with trailing slash)
                    if full.rstrip('/').endswith(tuple(f'/estate_db/{cat}' for cat in category_pages)):
                        continue
                    # Also exclude if it contains these keywords anywhere
                    if any(cat in full.lower() for cat in ["/office", "/lease", "/mansion"]):
                        continue
                    candidates.add(full)

        print(f"  > Processing {len(candidates)} candidates...")
        # Process all candidates
        for link in list(candidates)[:50]:  # Cap at 50
            self.parse_detail(link)
            sleep_jitter()

    def parse_detail(self, url):
        # IMMEDIATE REJECTION: Category pages (bulletproof check)
        url_lower = url.lower().rstrip('/')
        if url_lower.endswith('estate_db/office') or url_lower.endswith('estate_db/lease') or \
           url_lower.endswith('estate_db/mansion') or url_lower.endswith('estate_db/house') or \
           url_lower.endswith('estate_db/estate') or url_lower.endswith('estate_db/land'):
            print(f"  [CATEGORY PAGE FILTERED] {url}")
            return

        # Special debug logging for specific properties (false positives)
        is_special = "6780" in url or "6831" in url

        STATS["scanned"] += 1
        if is_special:
            print(f"\n{'='*60}")
            print(f"SPECIAL MAPLE PROPERTY DEBUG")
            print(f"URL: {url}")
            print(f"{'='*60}")

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
        is_generic = not title or "メープルハウジング" in title or "伊豆の不動産" in title
        if is_generic:
            try:
                from urllib.parse import unquote
                # URL like: .../6763%ef%bc%9a%e8%87%aa%e7%84%b6...
                # Extract the encoded part after estate_db/
                url_parts = url.split('/estate_db/')
                if len(url_parts) > 1:
                    # Get everything between estate_db/ and the next / or end
                    path_after_db = url_parts[1].strip('/')
                    # Get first path segment (the property slug)
                    encoded_title = path_after_db.split('/')[0] if '/' in path_after_db else path_after_db

                    print(f"  [DEBUG] Decoding title from URL segment: {encoded_title[:50]}...")
                    decoded = unquote(encoded_title)
                    print(f"  [DEBUG] Decoded to: {decoded[:80]}")

                    # Extract property description after the ID and colon
                    if '：' in decoded:
                        title = decoded.split('：', 1)[1].strip()
                        print(f"  [DEBUG] Extracted after ：: {title[:80]}")
                    elif ':' in decoded:
                        title = decoded.split(':', 1)[1].strip()
                        print(f"  [DEBUG] Extracted after :: {title[:80]}")
                    else:
                        # No colon, check if it starts with digits (property ID)
                        # and try to extract meaningful part
                        if decoded[:4].isdigit():
                            title = decoded[4:].strip()  # Skip property ID
                            print(f"  [DEBUG] Removed ID prefix: {title[:80]}")
                        else:
                            title = decoded
                            print(f"  [DEBUG] Using full decoded: {title[:80]}")

                    # If title is still generic or empty, keep original
                    if not title or len(title) < 3 or "メープルハウジング" in title:
                        print(f"  [DEBUG] Decoded title still generic or empty, keeping fallback")
                        title = None
            except Exception as e:
                print(f"  [DEBUG] Title decode failed for {url}: {e}")
                import traceback
                traceback.print_exc()
                title = None

        if not title:
            title = "Maple Property"

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
            if is_special:
                print(f"  Sea view score: 0 - Explicit 'no sea view' found")
        elif any(k in full_text for k in HIGH_SEA_KEYWORDS):
            sea_score = 4
            if is_special:
                matched = [k for k in HIGH_SEA_KEYWORDS if k in full_text]
                print(f"  Sea view score: 4 - HIGH confidence")
                print(f"    Matched keywords: {matched}")
        elif any(k in full_text for k in MEDIUM_SEA_KEYWORDS):
            sea_score = 3
            if is_special:
                matched = [k for k in MEDIUM_SEA_KEYWORDS if k in full_text]
                print(f"  Sea view score: 3 - MEDIUM confidence (beach/coast names)")
                print(f"    Matched keywords: {matched}")
        elif any(k in full_text for k in ["海", "ビーチ", "Beach"]):
            # Require explicit distance/time measurements to avoid false positives
            proximity_patterns = [
                r"海まで徒歩[0-9０-９]",          # 海まで徒歩5分
                r"海まで.*[0-9０-９]+.*分",       # 海まで約5分
                r"海まで.*[0-9０-９]+.*[mｍメートル]",  # 海まで100m
                r"海から[0-9０-９]+.*[mｍメートル]",    # 海から100m
                r"徒歩[0-9０-９]+.*分.*海",       # 徒歩5分で海
                r"ビーチまで.*[0-9０-９]+",      # ビーチまで5分
                r"海.*徒歩圏",                    # 海が徒歩圏内
            ]
            matched_patterns = [p for p in proximity_patterns if re.search(p, full_text)]
            if matched_patterns:
                sea_score = 2
                if is_special:
                    print(f"  Sea view score: 2 - Proximity detected")
                    print(f"    Matched patterns: {matched_patterns}")
                    for pattern in matched_patterns[:2]:
                        match = re.search(f".{{0,20}}{pattern}.{{0,20}}", full_text)
                        if match:
                            print(f"    Context: ...{match.group()}...")
            else:
                if is_special:
                    print(f"  Sea view score: 0 - Generic sea mention without clear proximity")

        # Filter by sea view score - only include properties with clear sea connection
        MIN_SEA_SCORE = 2
        if is_special:
            print(f"  Minimum sea score required: {MIN_SEA_SCORE}")
        if sea_score < MIN_SEA_SCORE:
            print(f"  [SEA VIEW FILTERED] Maple - Insufficient sea connection (score={sea_score}): {url[:60]}")
            if is_special:
                print(f"  >>> SPECIAL MAPLE PROPERTY REJECTED: Sea view score too low ({sea_score} < {MIN_SEA_SCORE})")
            STATS["skipped_loc"] += 1
            return

        price = extract_price(full_text)
        if is_special:
            print(f"  Price extracted: {price} JPY")
        else:
            print(f"  [DEBUG] Maple - Extracted price for {url[:60]}: {price}")

        # Price validation - Exclude properties with no price (likely sold/unavailable)
        if not price or price <= 0:
            print(f"  [PRICE FILTERED] No valid price found: {url} (price={price})")
            STATS["skipped_sold"] += 1
            return

        img = get_best_image(soup, url)
        ptype = determine_type(title, full_text)

        if is_special:
            print(f"  >>> SPECIAL MAPLE PROPERTY PASSED ALL FILTERS")
            print(f"      City: {city}, Price: {price}, Sea Score: {sea_score}")
            print(f"{'='*60}\n")

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
        # Target area codes (Shimoda, Kawazu, Higashi-Izu, Minami-Izu)
        target_codes = {
            "ao22219": "下田",      # Shimoda
            "ao22301": "河津",      # Kawazu
            "ao22302": "東伊豆",    # Higashi-Izu
            "ao22304": "南伊豆"     # Minami-Izu
        }
        # Exclude these known wrong area codes (Ito, Atami, Izu city)
        exclude_codes = ["ao22208", "ao22222", "ao22205"]

        # Scan both general listing pages AND area-specific pages
        urls = [
            "https://www.aoba-resort.com/house/",
            "https://www.aoba-resort.com/house/page/2/",
            "https://www.aoba-resort.com/land/",
            "https://www.aoba-resort.com/land/page/2/",
        ]

        # Add area-specific pages for each target city
        for code in target_codes.keys():
            urls.append(f"https://www.aoba-resort.com/area-b2/bknarea-{code}/")

        candidates = set()

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

        print(f"  > Found {len(candidates)} property pages total")

        if len(candidates) == 0:
            print(f"  [WARNING] Aoba: No property links found on any pages!")
            print(f"  [WARNING] This could indicate:")
            print(f"             - Website structure has changed")
            print(f"             - No properties currently listed")
            print(f"             - Link detection logic needs updating")
            return

        print(f"  > Processing {len(candidates)} candidates (will filter by city later)...")

        # Process all candidates - parse_detail will filter by city
        aoba_before = len(self.items)
        for link in list(candidates)[:60]:  # Cap at 60 to avoid timeout
            self.parse_detail(link)
            sleep_jitter()

        aoba_after = len(self.items)
        aoba_saved = aoba_after - aoba_before
        print(f"  > Aoba: Saved {aoba_saved} out of {len(candidates)} candidates")
        if aoba_saved == 0 and len(candidates) > 0:
            print(f"  [WARNING] All Aoba properties were filtered out!")
            print(f"  [WARNING] Check: sea view requirements, location matching, price validation")

    def parse_detail(self, url):
        STATS["scanned"] += 1

        # Special debug logging for specific properties
        is_special = "room94930761" in url or "room98586218" in url or "room95327115" in url or "room82946986" in url or "room95106919" in url

        if is_special:
            print(f"\n{'='*60}")
            print(f"SPECIAL AOBA PROPERTY DEBUG")
            print(f"URL: {url}")
            print(f"{'='*60}")
        else:
            print(f"  [DEBUG] Aoba - Parsing: {url[:80]}")

        soup = self.fetch(url)
        if not soup:
            print(f"  [DEBUG] Aoba - Failed to fetch")
            return

        # Extract city from URL as context (Aoba uses area codes in URLs)
        url_city_map = {
            "ao22219": "下田",      # Shimoda
            "ao22301": "河津",      # Kawazu
            "ao22302": "東伊豆",    # Higashi-Izu
            "ao22304": "南伊豆"     # Minami-Izu
        }
        url_city = None
        for code, city_name in url_city_map.items():
            if code in url:
                url_city = city_name
                if is_special:
                    print(f"  Area code detected: {code} ({city_name})")
                else:
                    print(f"  [DEBUG] Aoba - Detected area code {code} ({city_name}) in URL")
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
        if is_special:
            print(f"  City detected: {city} (context: {url_city})")
        if city == "WRONG_CITY":
            # Property is explicitly from wrong city
            print(f"  [LOCATION FILTERED] Wrong city detected: {url}")
            if is_special:
                print(f"  >>> SPECIAL AOBA PROPERTY REJECTED: Wrong city")
            STATS["skipped_loc"] += 1
            return
        if not city:
            # If URL had area code, trust it
            if url_city:
                city = url_city
                if is_special:
                    print(f"  Using URL city as fallback: {city}")
            else:
                print(f"  [WARNING] Could not determine city for: {url}")
                if is_special:
                    print(f"  >>> SPECIAL AOBA PROPERTY REJECTED: Could not determine city")
                STATS["skipped_loc"] += 1
                return

        # Sea View Scoring (Tiered for accuracy)
        sea_score = 0
        if "海は見えません" in full_text or "海眺望なし" in full_text or "海見えず" in full_text:
            sea_score = 0
            print(f"  [DEBUG] Aoba - Explicit 'no sea view' found")
        elif any(k in full_text for k in HIGH_SEA_KEYWORDS):
            sea_score = 4
            if is_special:
                matched = [k for k in HIGH_SEA_KEYWORDS if k in full_text]
                print(f"  Sea view score: 4 - HIGH confidence")
                print(f"    Matched keywords: {matched}")
            else:
                print(f"  [DEBUG] Aoba - High confidence sea view detected")
        elif any(k in full_text for k in MEDIUM_SEA_KEYWORDS):
            sea_score = 3
            if is_special:
                matched = [k for k in MEDIUM_SEA_KEYWORDS if k in full_text]
                print(f"  Sea view score: 3 - MEDIUM confidence (beach names)")
                print(f"    Matched keywords: {matched}")
            else:
                print(f"  [DEBUG] Aoba - Medium confidence (beach name) detected")
        elif any(k in full_text for k in ["海", "ビーチ", "Beach"]):
            # Require explicit distance/time measurements to avoid false positives
            # Must have numbers: "海まで徒歩5分", "海から100m", etc.
            proximity_patterns = [
                r"海まで徒歩[0-9０-９]",          # 海まで徒歩5分
                r"海まで.*[0-9０-９]+.*分",       # 海まで約5分
                r"海まで.*[0-9０-９]+.*[mｍメートル]",  # 海まで100m
                r"海から[0-9０-９]+.*[mｍメートル]",    # 海から100m
                r"徒歩[0-9０-９]+.*分.*海",       # 徒歩5分で海
                r"ビーチまで.*[0-9０-９]+",      # ビーチまで5分
                r"海.*徒歩圏",                    # 海が徒歩圏内
            ]
            matched_patterns = [p for p in proximity_patterns if re.search(p, full_text)]
            if matched_patterns:
                sea_score = 2
                if is_special:
                    print(f"  Sea view score: 2 - Proximity detected")
                    print(f"    Matched patterns: {matched_patterns}")
                    # Show actual matched text snippets
                    for pattern in matched_patterns[:2]:  # Show first 2 matches
                        match = re.search(f".{{0,20}}{pattern}.{{0,20}}", full_text)
                        if match:
                            print(f"    Context: ...{match.group()}...")
                else:
                    print(f"  [DEBUG] Aoba - Sea proximity detected")
            else:
                if is_special:
                    print(f"  Sea view score: 0 - Generic sea mention without clear proximity")
                    print(f"    Contains '海' but no proximity patterns matched")
                else:
                    print(f"  [DEBUG] Aoba - Generic sea mention without clear proximity")
        else:
            if is_special:
                print(f"  Sea view score: 0 - No sea-related keywords found")
            else:
                print(f"  [DEBUG] Aoba - No sea-related keywords found")

        # Filter by sea view score - only include properties with clear sea connection
        MIN_SEA_SCORE = 2
        if is_special:
            print(f"  Minimum sea score required: {MIN_SEA_SCORE}")
        if sea_score < MIN_SEA_SCORE:
            print(f"  [SEA VIEW FILTERED] Aoba - Insufficient sea connection (score={sea_score}): {url[:60]}")
            if is_special:
                print(f"  >>> SPECIAL AOBA PROPERTY REJECTED: Sea view score too low ({sea_score} < {MIN_SEA_SCORE})")
            STATS["skipped_loc"] += 1
            return

        price = extract_price(full_text)
        if is_special:
            print(f"  Price extracted: {price} JPY")
        else:
            print(f"  [DEBUG] Aoba - Extracted price for {url[:60]}: {price}")

        # Price validation - Exclude properties with no price (likely sold/unavailable)
        if not price or price <= 0:
            print(f"  [PRICE FILTERED] No valid price found: {url} (price={price})")
            if is_special:
                print(f"  >>> SPECIAL AOBA PROPERTY REJECTED: No valid price")
            STATS["skipped_sold"] += 1
            return

        img = get_best_image(soup, url)
        ptype = determine_type(title, full_text)

        if is_special:
            print(f"  >>> SPECIAL AOBA PROPERTY PASSED ALL FILTERS")
            print(f"      City: {city}, Price: {price}, Sea Score: {sea_score}")
            print(f"{'='*60}\n")

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
    # Fetch current forex rate
    print("\n" + "="*50)
    print(" FETCHING FOREX RATE")
    print("="*50)
    forex_rate = get_usd_jpy_rate()

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
        json.dump({
            "counts": counts,
            "generatedAt": out["generatedAt"],
            "forexRate": forex_rate
        }, f)

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