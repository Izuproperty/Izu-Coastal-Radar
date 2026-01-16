#!/usr/bin/env python3
"""
Diagnostic script to see what Izu Taiyo search is actually returning
"""
import sys
sys.path.insert(0, '/usr/local/lib/python3.11/site-packages')

import requests
from bs4 import BeautifulSoup
import urllib3
import re

urllib3.disable_warnings()

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
}

# Test the search with proper params
# IMPORTANT: sa.php is the search action (results), s.php is just the form
url = "https://www.izutaiyo.co.jp/sa.php"
params = {
    'ar[]': 'sm',
    'mk[]': ['海が見える', '海へ歩いて行ける'],
    'kd': '家'
}

print("="*80)
print("IZU TAIYO SEARCH DIAGNOSTIC")
print("="*80)
print(f"\nBase URL: {url}")
print(f"Parameters: {params}")

try:
    session = requests.Session()
    session.headers.update(HEADERS)

    r = session.get(url, params=params, timeout=15, verify=False)

    print(f"\nHTTP Status: {r.status_code}")
    print(f"Final URL: {r.url}")
    print(f"Content Length: {len(r.text)} bytes")

    # Save the HTML
    with open('izutaiyo_search_response.html', 'w', encoding='utf-8') as f:
        f.write(r.text)
    print("\n✓ HTML saved to: izutaiyo_search_response.html")

    # Parse and analyze
    r.encoding = r.apparent_encoding
    soup = BeautifulSoup(r.text, 'html.parser')

    print("\n" + "="*80)
    print("RESPONSE ANALYSIS")
    print("="*80)

    # Check page title
    title = soup.find('title')
    if title:
        print(f"\nPage Title: {title.text}")

    # Check for common messages
    page_text = soup.get_text()

    checks = [
        ('検索結果', 'Search Results'),
        ('見つかりませんでした', 'Not Found'),
        ('該当する物件', 'Matching Properties'),
        ('該当する物件がありません', 'No Matching Properties'),
        ('物件', 'Property (generic)'),
        ('検索', 'Search (generic)'),
    ]

    print("\nText Pattern Checks:")
    for japanese, english in checks:
        if japanese in page_text:
            print(f"  ✓ Found: {japanese} ({english})")
        else:
            print(f"  ✗ NOT found: {japanese} ({english})")

    # Look for property IDs
    property_ids_hpno = re.findall(r'd\.php\?hpno=(\w+)', r.text)
    property_ids_bunno = re.findall(r'd\.php\?hpbunno=(\w+)', r.text)

    print(f"\nProperty IDs (hpno): {len(property_ids_hpno)}")
    if property_ids_hpno:
        print(f"  Sample: {property_ids_hpno[:5]}")

    print(f"Property IDs (hpbunno): {len(property_ids_bunno)}")
    if property_ids_bunno:
        print(f"  Sample: {property_ids_bunno[:5]}")

    # Look for onclick handlers
    onclick_tags = soup.find_all(True, onclick=True)
    print(f"\nTags with onclick attribute: {len(onclick_tags)}")
    if onclick_tags:
        print("  Sample onclick values:")
        for tag in onclick_tags[:3]:
            print(f"    - {tag.get('onclick')[:100]}")

    # Look for d.php links
    all_links = soup.find_all('a', href=True)
    d_php_links = [a for a in all_links if 'd.php' in a.get('href', '')]
    print(f"\nLinks containing 'd.php': {len(d_php_links)}")
    if d_php_links:
        print("  Sample links:")
        for link in d_php_links[:3]:
            print(f"    - {link.get('href')}")

    # Check for form elements (maybe search needs POST?)
    forms = soup.find_all('form')
    print(f"\nForms found: {len(forms)}")
    if forms:
        for i, form in enumerate(forms[:2]):
            method = form.get('method', 'GET').upper()
            action = form.get('action', '(no action)')
            print(f"  Form {i+1}: method={method}, action={action}")

    print("\n" + "="*80)
    print("RECOMMENDATION")
    print("="*80)
    print("\nPlease check 'izutaiyo_search_response.html' to see what the page")
    print("actually contains. This will help us understand if:")
    print("  1. The search returned 0 results (no properties match)")
    print("  2. The search form needs different parameters")
    print("  3. The website requires POST instead of GET")
    print("  4. There's some bot detection or session requirement")
    print()

except Exception as e:
    print(f"\n❌ ERROR: {e}")
    import traceback
    traceback.print_exc()
