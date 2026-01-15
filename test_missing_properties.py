#!/usr/bin/env python3
"""
Test script to investigate missing Shimoda properties.
This will fetch properties directly and show their characteristics.
"""

import requests
from bs4 import BeautifulSoup
import re
import json

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
}

# Properties to test
PROPERTIES = {
    "SMB392H": "FOUND in search",
    "SMB240H": "MISSING from search",
    "SMB225H": "MISSING from search",
    "SMB368H": "MISSING from search",
    "SMB195H": "MISSING from search"
}

def fetch_property(prop_id):
    """Fetch a property page and extract key info"""
    url = f"https://www.izutaiyo.co.jp/d.php?hpno={prop_id}"
    print(f"\n{'='*70}")
    print(f"Testing: {prop_id} - {PROPERTIES[prop_id]}")
    print(f"URL: {url}")
    print(f"{'='*70}")

    try:
        r = requests.get(url, headers=HEADERS, timeout=15, verify=False)
        if r.status_code != 200:
            print(f"ERROR: HTTP {r.status_code}")
            return None

        r.encoding = r.apparent_encoding
        soup = BeautifulSoup(r.text, 'html.parser')

        # Extract key information
        info = {}

        # Title
        h1 = soup.find('h1')
        info['title'] = h1.get_text().strip() if h1 else "No title"

        # Look for property type indicators in the page
        page_text = soup.get_text()

        # Check for status keywords
        status_keywords = ["成約", "商談中", "予約", "Sold", "Contracted", "Reserved", "済"]
        info['status_keywords_found'] = [k for k in status_keywords if k in page_text]

        # Check for property type
        info['has_のマンション情報'] = 'のマンション情報' in info['title']
        info['has_の家情報'] = 'の家情報' in info['title']
        info['has_売地'] = '売地' in page_text or '土地' in page_text

        # Look for hpkind value in the HTML
        hpkind_match = re.search(r'hpkind["\']?\s*[:=]\s*["\']?(\d+)', r.text)
        if hpkind_match:
            info['hpkind'] = hpkind_match.group(1)
        else:
            info['hpkind'] = "Not found in HTML"

        # Look for any form data or hidden fields
        hidden_inputs = soup.find_all('input', type='hidden')
        info['hidden_fields'] = {}
        for inp in hidden_inputs:
            name = inp.get('name', '')
            value = inp.get('value', '')
            if name:
                info['hidden_fields'][name] = value

        # Check if it's listed in search results by fetching the search page
        print(f"\n--- Property Details ---")
        print(f"Title: {info['title'][:100]}")
        print(f"Status keywords found: {info['status_keywords_found']}")
        print(f"Has 'の家情報': {info['has_の家情報']}")
        print(f"Has 'のマンション情報': {info['has_のマンション情報']}")
        print(f"Has '売地/土地': {info['has_売地']}")
        print(f"hpkind value: {info['hpkind']}")
        if info['hidden_fields']:
            print(f"Hidden fields: {json.dumps(info['hidden_fields'], ensure_ascii=False)}")

        # Try to find the property in search results
        print(f"\n--- Checking Search Results ---")
        for hpkind in [1, 2]:
            search_url = f"https://www.izutaiyo.co.jp/tokusen.php?hpcity[]=22219&hpkind={hpkind}"
            print(f"Checking: {search_url}")

            try:
                r_search = requests.get(search_url, headers=HEADERS, timeout=15, verify=False)
                if r_search.status_code == 200:
                    r_search.encoding = r_search.apparent_encoding
                    if prop_id in r_search.text:
                        print(f"  ✓ FOUND in hpkind={hpkind} ({'House' if hpkind == 1 else 'Land'})")
                    else:
                        print(f"  ✗ NOT FOUND in hpkind={hpkind} ({'House' if hpkind == 1 else 'Land'})")
                else:
                    print(f"  ERROR: HTTP {r_search.status_code}")
            except Exception as e:
                print(f"  ERROR: {e}")

        return info

    except Exception as e:
        print(f"ERROR: {e}")
        import traceback
        traceback.print_exc()
        return None

def main():
    print("\n" + "="*70)
    print("INVESTIGATING MISSING SHIMODA PROPERTIES")
    print("="*70)

    results = {}
    for prop_id in PROPERTIES.keys():
        results[prop_id] = fetch_property(prop_id)

    # Summary
    print("\n" + "="*70)
    print("SUMMARY")
    print("="*70)

    for prop_id, info in results.items():
        if info:
            print(f"\n{prop_id} ({PROPERTIES[prop_id]}):")
            print(f"  - Status keywords: {info['status_keywords_found']}")
            print(f"  - Type: {'House' if info['has_の家情報'] else 'Land' if info['has_売地'] else 'Unknown'}")
            print(f"  - hpkind: {info['hpkind']}")

if __name__ == "__main__":
    # Disable SSL warnings
    import urllib3
    urllib3.disable_warnings()

    main()
