#!/usr/bin/env python3
"""
Quick test to check if missing Shimoda properties appear without hpkind filter
"""
import sys
sys.path.insert(0, '/usr/local/lib/python3.11/site-packages')

import requests
import urllib3

urllib3.disable_warnings()

HEADERS = {"User-Agent": "Mozilla/5.0"}
MISSING_PROPS = ["SMB240H", "SMB225H", "SMB368H", "SMB195H"]
WORKING_PROP = "SMB392H"

def test_search(url, name):
    print(f"\n{'='*60}")
    print(f"Testing: {name}")
    print(f"URL: {url}")
    print(f"{'='*60}")

    try:
        r = requests.get(url, headers=HEADERS, timeout=15, verify=False)
        if r.status_code != 200:
            print(f"  ❌ HTTP {r.status_code}")
            return

        content = r.text

        # Check for each property
        print(f"\n  Results:")
        print(f"    SMB392H (working):  {'✅ FOUND' if WORKING_PROP in content else '❌ NOT FOUND'}")

        for prop in MISSING_PROPS:
            found = "✅ FOUND" if prop in content else "❌ NOT FOUND"
            print(f"    {prop}: {found}")

        # Count total properties
        count = content.count('d.php?hpno=')
        print(f"\n  Total property links: {count}")

    except Exception as e:
        print(f"  ❌ ERROR: {e}")

# Test current searches
print("\nCURRENT SEARCHES (Houses & Land only):")
test_search("https://www.izutaiyo.co.jp/tokusen.php?hpcity[]=22219&hpkind=1",
            "Shimoda Houses (hpkind=1)")

test_search("https://www.izutaiyo.co.jp/tokusen.php?hpcity[]=22219&hpkind=2",
            "Shimoda Land (hpkind=2)")

# Test without filter
print("\n\nNEW SEARCH (All property types):")
test_search("https://www.izutaiyo.co.jp/tokusen.php?hpcity[]=22219",
            "Shimoda All Types (no hpkind filter)")

print("\n" + "="*60)
print("If missing properties appear in 'All Types', the fix works!")
print("="*60 + "\n")
