#!/usr/bin/env python3
"""
Verify that the search endpoint fix is working correctly.
Tests the new s.php search URLs to see if missing properties appear.
"""
import sys
sys.path.insert(0, '/usr/local/lib/python3.11/site-packages')

import requests
import urllib3
import re

urllib3.disable_warnings()

HEADERS = {"User-Agent": "Mozilla/5.0"}
TARGET_PROPERTIES = ["SMB240H", "SMB225H", "SMB368H", "SMB195H", "SMB392H"]

def test_search_url(url, description):
    """Test a search URL and check for target properties"""
    print(f"\n{'='*70}")
    print(f"Testing: {description}")
    print(f"URL: {url}")
    print(f"{'='*70}")

    try:
        r = requests.get(url, headers=HEADERS, timeout=15, verify=False)

        if r.status_code != 200:
            print(f"  ❌ HTTP {r.status_code}")
            return None

        r.encoding = r.apparent_encoding
        content = r.text

        # Extract all property IDs
        property_ids = re.findall(r'd\.php\?hpno=(\w+)', content)
        unique_ids = list(dict.fromkeys(property_ids))

        # Check for target properties
        found_targets = [pid for pid in unique_ids if pid in TARGET_PROPERTIES]
        missing_targets = [pid for pid in TARGET_PROPERTIES if pid not in unique_ids]

        print(f"\n  📊 Results:")
        print(f"     Total property listings: {len(unique_ids)}")

        if found_targets:
            print(f"\n  ✅ FOUND TARGET PROPERTIES ({len(found_targets)}):")
            for prop in found_targets:
                print(f"     - {prop}")

        if missing_targets:
            print(f"\n  ❌ MISSING TARGET PROPERTIES ({len(missing_targets)}):")
            for prop in missing_targets:
                print(f"     - {prop}")

        # Show sample of property IDs found
        print(f"\n  🔍 Sample property IDs (first 15):")
        for pid in unique_ids[:15]:
            indicator = "🎯" if pid in TARGET_PROPERTIES else "  "
            print(f"     {indicator} {pid}")

        if len(unique_ids) > 15:
            print(f"     ... and {len(unique_ids) - 15} more")

        return {
            'total': len(unique_ids),
            'found_targets': found_targets,
            'missing_targets': missing_targets,
            'all_ids': unique_ids
        }

    except Exception as e:
        print(f"  ❌ ERROR: {e}")
        return None

def main():
    print("\n" + "="*70)
    print("VERIFYING SEARCH ENDPOINT FIX")
    print("="*70)
    print(f"\nLooking for these properties:")
    for prop in TARGET_PROPERTIES:
        print(f"  - {prop}")

    results = {}

    # Test Shimoda Houses with sea view (should return ~10 properties)
    print("\n" + "="*70)
    print("TEST 1: Shimoda Houses with Sea View")
    print("="*70)
    results['shimoda_houses'] = test_search_url(
        "https://www.izutaiyo.co.jp/s.php?ar[]=sm&mk[]=海が見える&mk[]=海へ歩いて行ける&kd=家",
        "Shimoda Houses (家) with sea conditions"
    )

    # Test Shimoda Land with sea view (should return ~6 properties)
    print("\n" + "="*70)
    print("TEST 2: Shimoda Land with Sea View")
    print("="*70)
    results['shimoda_land'] = test_search_url(
        "https://www.izutaiyo.co.jp/s.php?ar[]=sm&mk[]=海が見える&mk[]=海へ歩いて行ける&kd=土地",
        "Shimoda Land (土地) with sea conditions"
    )

    # Summary
    print("\n" + "="*70)
    print("SUMMARY")
    print("="*70)

    total_found = 0
    total_missing = 0

    for test_name, result in results.items():
        if result:
            found = len(result['found_targets'])
            missing = len(result['missing_targets'])
            total_found += found
            total_missing += missing
            print(f"\n{test_name}:")
            print(f"  Properties: {result['total']}")
            print(f"  Targets found: {found}")
            print(f"  Targets missing: {missing}")

    print("\n" + "="*70)
    print("EXPECTED RESULTS:")
    print("  Shimoda Houses: ~10 properties")
    print("  Shimoda Land: ~6 properties")
    print("  All 5 target properties should be found")
    print("="*70)

    # Determine success
    if total_found == len(TARGET_PROPERTIES):
        print("\n✅ SUCCESS! All target properties found!")
    elif total_found > 0:
        print(f"\n⚠️  PARTIAL: Found {total_found}/{len(TARGET_PROPERTIES)} target properties")
    else:
        print("\n❌ FAILED: No target properties found")

    print()

if __name__ == "__main__":
    main()
