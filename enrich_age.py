#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
enrich_age.py — One-shot enrichment script.

Reads listings.json, fetches each Izu Taiyo detail page that is missing
`yearBuilt`, extracts the construction year (築年月), and writes the result
back to listings.json.

Run once from the repo root:
    python3 enrich_age.py

Requires: requests, beautifulsoup4  (same deps as generate_listings.py)
"""

import json
import re
import time
import random
import sys

sys.path.insert(0, '/usr/local/lib/python3.11/site-packages')

import requests
from bs4 import BeautifulSoup
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

LISTINGS_FILE = "listings.json"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ja,en;q=0.9",
}

# Japanese era → Gregorian year offsets
ERA_OFFSET = {"昭和": 1925, "平成": 1988, "令和": 2018}

ERA_PATTERN = re.compile(
    r'築[年月\s:：]*(?:([昭平令]和|令和)(\d{1,2})年|(\d{4})年)'
)


def extract_year_built(soup, full_text):
    """Return integer year built, or None."""
    # 1. Table rows labelled 築年月 / 建築年
    for row in soup.find_all("tr"):
        cells = row.find_all(["th", "td"])
        if len(cells) >= 2:
            label = cells[0].get_text(strip=True)
            if "築年" in label or "建築年" in label:
                val = cells[1].get_text(strip=True)
                m = re.search(r'([昭平令]和|令和)(\d{1,2})年', val)
                if m:
                    era_key = m.group(1)[:2]
                    if era_key in ERA_OFFSET:
                        return ERA_OFFSET[era_key] + int(m.group(2))
                m2 = re.search(r'(\d{4})年', val)
                if m2:
                    y = int(m2.group(1))
                    if 1950 <= y <= 2026:
                        return y
    # 2. Full-text scan
    for m in ERA_PATTERN.finditer(full_text):
        era, era_yr, western_yr = m.group(1), m.group(2), m.group(3)
        if western_yr:
            y = int(western_yr)
            if 1950 <= y <= 2026:
                return y
        if era and era_yr:
            era_key = era[:2]
            if era_key in ERA_OFFSET:
                return ERA_OFFSET[era_key] + int(era_yr)
    return None


def fetch_page(session, url, retries=3):
    for attempt in range(retries):
        try:
            r = session.get(url, timeout=15, verify=False)
            if r.status_code == 200:
                return BeautifulSoup(r.content, "html.parser")
            print(f"  HTTP {r.status_code} for {url}")
            return None
        except Exception as e:
            wait = 2 ** attempt + random.uniform(0, 1)
            print(f"  Error ({e}), retrying in {wait:.1f}s...")
            time.sleep(wait)
    return None


def main():
    with open(LISTINGS_FILE, encoding="utf-8") as f:
        data = json.load(f)

    listings = data["listings"]
    missing = [l for l in listings if "yearBuilt" not in l and "izutaiyo" in l.get("id", "")]
    print(f"Listings missing yearBuilt: {len(missing)} / {len(listings)}")

    if not missing:
        print("All listings already have yearBuilt — nothing to do.")
        return

    session = requests.Session()
    session.headers.update(HEADERS)

    # Warm up: hit the homepage first
    try:
        session.get("https://www.izutaiyo.co.jp/", timeout=10, verify=False)
        time.sleep(1)
    except Exception:
        pass

    updated = 0
    for i, listing in enumerate(missing):
        url = listing.get("sourceUrl", "")
        if not url:
            continue
        print(f"[{i+1}/{len(missing)}] {url}")
        soup = fetch_page(session, url)
        if not soup:
            print(f"  Could not fetch — skipping")
            time.sleep(2)
            continue

        full_text = soup.get_text(" ", strip=True)
        year = extract_year_built(soup, full_text)
        if year:
            print(f"  yearBuilt = {year}")
            listing["yearBuilt"] = year
            updated += 1
        else:
            print(f"  yearBuilt not found on page")

        # Polite delay
        time.sleep(random.uniform(1.5, 3.0))

    print(f"\nDone. Updated {updated} listings with yearBuilt.")
    with open(LISTINGS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"Saved to {LISTINGS_FILE}")


if __name__ == "__main__":
    main()
