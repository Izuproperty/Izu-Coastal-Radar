#!/usr/bin/env python3
"""
Test different search variations to find missing Shimoda properties.
This script tests all possible hpkind values and search parameters.
"""

import requests
import urllib3
from bs4 import BeautifulSoup
import re
import time

# Disable SSL warnings
urllib3.disable_warnings()

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
}

# Properties we're looking for
WORKING_PROPERTY = "SMB392H"  # This one appears in search
MISSING_PROPERTIES = ["SMB240H", "SMB225H", "SMB368H", "SMB195H"]
ALL_TEST_PROPERTIES = [WORKING_PROPERTY] + MISSING_PROPERTIES

def test_search_variation(url, description):
    """Test a search URL and see which properties it returns"""
    print(f"\n{'='*80}")
    print(f"Testing: {description}")
    print(f"URL: {url}")
    print(f"{'='*80}")

    try:
        r = requests.get(url, headers=HEADERS, timeout=15, verify=False)

        if r.status_code != 200:
            print(f"  ❌ HTTP {r.status_code}")
            return None

        r.encoding = r.apparent_encoding
        content = r.text

        # Check which properties are found
        found_properties = []
        missing_properties = []

        for prop in ALL_TEST_PROPERTIES:
            if prop in content:
                found_properties.append(prop)
            else:
                missing_properties.append(prop)

        # Count total property links
        onclick_count = len(re.findall(r'd\.php\?hpno=\w+', content))
        link_count = content.count('d.php?hpno=')

        print(f"\n  📊 Results:")
        print(f"     Total property links: {link_count}")
        print(f"     Onclick handlers: {onclick_count}")

        if found_properties:
            print(f"\n  ✅ FOUND ({len(found_properties)}):")
            for prop in found_properties:
                status = "⭐ WORKING PROPERTY" if prop == WORKING_PROPERTY else "🎯 TARGET"
                print(f"     - {prop} {status}")
        else:
            print(f"\n  ✅ FOUND: None")

        if missing_properties:
            print(f"\n  ❌ NOT FOUND ({len(missing_properties)}):")
            for prop in missing_properties:
                status = "⚠️  SHOULD BE IN SEARCH" if prop == WORKING_PROPERTY else "❓ MISSING TARGET"
                print(f"     - {prop} {status}")

        # Extract a few property IDs to see what's actually there
        property_ids = re.findall(r'd\.php\?hpno=(\w+)', content)
        if property_ids:
            unique_ids = list(dict.fromkeys(property_ids))[:10]  # First 10 unique
            print(f"\n  🔍 Sample property IDs found:")
            for pid in unique_ids:
                print(f"     - {pid}")

        return {
            'found': found_properties,
            'missing': missing_properties,
            'total_links': link_count,
            'property_ids': list(dict.fromkeys(property_ids))
        }

    except Exception as e:
        print(f"  ❌ ERROR: {e}")
        return None

def test_direct_property_access():
    """Test if we can access each property directly"""
    print(f"\n{'='*80}")
    print(f"TESTING DIRECT PROPERTY ACCESS")
    print(f"{'='*80}")

    for prop_id in ALL_TEST_PROPERTIES:
        url = f"https://www.izutaiyo.co.jp/d.php?hpno={prop_id}"
        status = "⭐ WORKING" if prop_id == WORKING_PROPERTY else "❓ MISSING"

        try:
            r = requests.get(url, headers=HEADERS, timeout=10, verify=False)

            if r.status_code == 200:
                r.encoding = r.apparent_encoding
                soup = BeautifulSoup(r.text, 'html.parser')

                # Get title
                h1 = soup.find('h1')
                title = h1.get_text().strip() if h1 else "No title"

                # Check for status keywords
                page_text = soup.get_text()
                is_sold = any(k in page_text for k in ["成約", "商談中"])

                # Look for hpkind in HTML
                hpkind_match = re.search(r'hpkind["\'\s:=]+(\d+)', r.text)
                hpkind = hpkind_match.group(1) if hpkind_match else "Not found"

                print(f"\n  {prop_id} ({status}):")
                print(f"    ✅ Accessible")
                print(f"    📄 Title: {title[:80]}")
                print(f"    🏷️  hpkind: {hpkind}")
                print(f"    📊 Status: {'SOLD/Reserved' if is_sold else 'Available'}")

            else:
                print(f"\n  {prop_id} ({status}):")
                print(f"    ❌ HTTP {r.status_code}")

        except Exception as e:
            print(f"\n  {prop_id} ({status}):")
            print(f"    ❌ ERROR: {e}")

        time.sleep(0.5)  # Be nice to the server

def main():
    print("\n" + "="*80)
    print("IZU TAIYO SEARCH INVESTIGATION")
    print("="*80)
    print(f"\nLooking for these properties:")
    print(f"  ⭐ {WORKING_PROPERTY} - WORKING (appears in current search)")
    for prop in MISSING_PROPERTIES:
        print(f"  ❓ {prop} - MISSING (needs to be found)")

    results = {}

    # Test 1: Current implementation (baseline)
    print("\n" + "="*80)
    print("PHASE 1: BASELINE - Current Search Implementation")
    print("="*80)

    results['house'] = test_search_variation(
        "https://www.izutaiyo.co.jp/tokusen.php?hpcity[]=22219&hpkind=1",
        "Current: Houses (hpkind=1)"
    )
    time.sleep(1)

    results['land'] = test_search_variation(
        "https://www.izutaiyo.co.jp/tokusen.php?hpcity[]=22219&hpkind=2",
        "Current: Land (hpkind=2)"
    )
    time.sleep(1)

    # Test 2: Other hpkind values
    print("\n" + "="*80)
    print("PHASE 2: HPKIND EXPLORATION - Testing Other Values")
    print("="*80)

    for hpkind in [0, 3, 4, 5]:
        results[f'hpkind{hpkind}'] = test_search_variation(
            f"https://www.izutaiyo.co.jp/tokusen.php?hpcity[]=22219&hpkind={hpkind}",
            f"Testing: hpkind={hpkind}"
        )
        time.sleep(1)

    # Test 3: No hpkind filter
    print("\n" + "="*80)
    print("PHASE 3: NO FILTER - All Property Types")
    print("="*80)

    results['no_filter'] = test_search_variation(
        "https://www.izutaiyo.co.jp/tokusen.php?hpcity[]=22219",
        "No hpkind filter (all types)"
    )
    time.sleep(1)

    # Test 4: Direct property access
    print("\n" + "="*80)
    print("PHASE 4: DIRECT ACCESS - Can We Reach Each Property?")
    print("="*80)

    test_direct_property_access()

    # Summary
    print("\n" + "="*80)
    print("FINAL SUMMARY")
    print("="*80)

    print("\n📋 Search Variations Tested:")
    for key, result in results.items():
        if result:
            found_count = len(result['found'])
            missing_count = len(result['missing'])
            total = result['total_links']
            print(f"\n  {key}:")
            print(f"    Target properties found: {found_count}/{len(ALL_TEST_PROPERTIES)}")
            print(f"    Total properties in search: {total}")
            if result['found']:
                print(f"    Found: {', '.join(result['found'])}")

    # Determine best solution
    print("\n" + "="*80)
    print("RECOMMENDATIONS")
    print("="*80)

    # Find which search variation found the most missing properties
    best_variation = None
    best_count = 0

    for key, result in results.items():
        if result:
            missing_found = len([p for p in MISSING_PROPERTIES if p in result['found']])
            if missing_found > best_count:
                best_count = missing_found
                best_variation = key

    if best_count == len(MISSING_PROPERTIES):
        print(f"\n✅ SUCCESS! Found all missing properties in: {best_variation}")
        print(f"\n💡 Recommendation: Update scraper to use this search variation.")
    elif best_count > 0:
        print(f"\n⚠️  PARTIAL SUCCESS: Found {best_count}/{len(MISSING_PROPERTIES)} missing properties in: {best_variation}")
        print(f"\n💡 Recommendation: This is an improvement but may need additional search methods.")
    else:
        print(f"\n❌ NO SOLUTION FOUND: None of the tested search variations found the missing properties.")
        print(f"\n💡 Next steps:")
        print(f"   1. Check if properties are accessible directly (Phase 4 results)")
        print(f"   2. Investigate keyword/text search functionality")
        print(f"   3. Check if properties require POST requests or authentication")
        print(f"   4. Look for alternative listing pages or sitemaps")

    print("\n" + "="*80)

if __name__ == "__main__":
    main()
