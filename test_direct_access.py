#!/usr/bin/env python3
"""Test if missing properties are accessible directly"""
import sys
sys.path.insert(0, '/usr/local/lib/python3.11/site-packages')

import requests
import urllib3
from bs4 import BeautifulSoup

urllib3.disable_warnings()

HEADERS = {"User-Agent": "Mozilla/5.0"}
MISSING = ["SMB240H", "SMB225H", "SMB368H", "SMB195H"]
WORKING = "SMB392H"

print("\n" + "="*70)
print("TESTING DIRECT ACCESS TO MISSING PROPERTIES")
print("="*70)

for prop_id in [WORKING] + MISSING:
    url = f"https://www.izutaiyo.co.jp/d.php?hpno={prop_id}"
    status_icon = "⭐" if prop_id == WORKING else "❓"

    try:
        r = requests.get(url, headers=HEADERS, timeout=10, verify=False)

        print(f"\n{status_icon} {prop_id}:")
        print(f"   URL: {url}")
        print(f"   HTTP Status: {r.status_code}")

        if r.status_code == 200:
            soup = BeautifulSoup(r.text, 'html.parser')

            # Get title
            h1 = soup.find('h1')
            title = h1.get_text().strip()[:80] if h1 else "No title"
            print(f"   ✅ ACCESSIBLE")
            print(f"   Title: {title}")

            # Check for sold/contracted status
            page_text = r.text
            is_sold = any(k in page_text for k in ["成約", "商談中", "予約"])
            sold_status = "SOLD/CONTRACTED" if is_sold else "Available"
            print(f"   Status: {sold_status}")

            # Try to find if there's any search exclusion flag
            if "非公開" in page_text:
                print(f"   ⚠️  Contains '非公開' (unlisted/private)")

        elif r.status_code == 404:
            print(f"   ❌ NOT FOUND (404)")
        else:
            print(f"   ⚠️  Unexpected status")

    except Exception as e:
        print(f"\n{status_icon} {prop_id}:")
        print(f"   ❌ ERROR: {e}")

print("\n" + "="*70)
print("CONCLUSION:")
print("="*70)
print("If missing properties are accessible (200 OK):")
print("  → They exist but aren't in search results")
print("  → May need keyword/text search instead of city code search")
print("\nIf missing properties return 404:")
print("  → They've been removed/deleted from the site")
print("="*70 + "\n")
