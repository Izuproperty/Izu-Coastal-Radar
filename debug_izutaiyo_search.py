#!/usr/bin/env python3
"""
Debug script to test Izu Taiyo search endpoint
"""
import sys
sys.path.insert(0, '/usr/local/lib/python3.11/site-packages')

import requests
import urllib3
from bs4 import BeautifulSoup
from urllib.parse import urlencode
import re

urllib3.disable_warnings()

HEADERS = {"User-Agent": "Mozilla/5.0"}

def test_search_method_1():
    """Test with manually constructed URL string"""
    print("\n" + "="*70)
    print("METHOD 1: Manually constructed URL string (CURRENT METHOD)")
    print("="*70)

    url = "https://www.izutaiyo.co.jp/s.php?ar[]=sm&mk[]=海が見える&mk[]=海へ歩いて行ける&kd=家"
    print(f"URL: {url}\n")

    r = requests.get(url, headers=HEADERS, timeout=15, verify=False)
    print(f"Status Code: {r.status_code}")
    print(f"Final URL: {r.url}")
    print(f"Content Length: {len(r.text)} bytes")

    r.encoding = r.apparent_encoding
    soup = BeautifulSoup(r.text, "html.parser")

    # Look for onclick handlers
    onclick_tags = soup.find_all(True, onclick=True)
    print(f"Tags with onclick: {len(onclick_tags)}")

    # Look for d.php links
    property_ids = re.findall(r'd\.php\?hpno=(\w+)', r.text)
    print(f"Property IDs found: {len(property_ids)}")
    if property_ids:
        print(f"Sample IDs: {property_ids[:10]}")

    # Save HTML for inspection
    with open('/tmp/izutaiyo_method1.html', 'w', encoding='utf-8') as f:
        f.write(r.text)
    print("HTML saved to: /tmp/izutaiyo_method1.html")

    return r.text

def test_search_method_2():
    """Test with params dictionary (RECOMMENDED)"""
    print("\n" + "="*70)
    print("METHOD 2: Using params dictionary (RECOMMENDED)")
    print("="*70)

    url = "https://www.izutaiyo.co.jp/s.php"
    params = {
        'ar[]': 'sm',
        'mk[]': ['海が見える', '海へ歩いて行ける'],
        'kd': '家'
    }

    print(f"URL: {url}")
    print(f"Params: {params}\n")

    r = requests.get(url, params=params, headers=HEADERS, timeout=15, verify=False)
    print(f"Status Code: {r.status_code}")
    print(f"Final URL: {r.url}")
    print(f"Content Length: {len(r.text)} bytes")

    r.encoding = r.apparent_encoding
    soup = BeautifulSoup(r.text, "html.parser")

    # Look for onclick handlers
    onclick_tags = soup.find_all(True, onclick=True)
    print(f"Tags with onclick: {len(onclick_tags)}")

    # Look for d.php links
    property_ids = re.findall(r'd\.php\?hpno=(\w+)', r.text)
    print(f"Property IDs found: {len(property_ids)}")
    if property_ids:
        print(f"Sample IDs: {property_ids[:10]}")

    # Save HTML for inspection
    with open('/tmp/izutaiyo_method2.html', 'w', encoding='utf-8') as f:
        f.write(r.text)
    print("HTML saved to: /tmp/izutaiyo_method2.html")

    return r.text

def test_search_method_3():
    """Test with URL-encoded query string"""
    print("\n" + "="*70)
    print("METHOD 3: Pre-encoded query string")
    print("="*70)

    # Manually URL-encode the parameters
    base_url = "https://www.izutaiyo.co.jp/s.php"
    query_params = {
        'ar[]': 'sm',
        'mk[]': ['海が見える', '海へ歩いて行ける'],
        'kd': '家'
    }

    # Build query string manually
    query_parts = []
    query_parts.append(f"ar[]={query_params['ar[]']}")
    for mk_value in query_params['mk[]']:
        from urllib.parse import quote
        query_parts.append(f"mk[]={quote(mk_value)}")
    query_parts.append(f"kd={quote(query_params['kd'])}")

    query_string = "&".join(query_parts)
    url = f"{base_url}?{query_string}"

    print(f"URL: {url}\n")

    r = requests.get(url, headers=HEADERS, timeout=15, verify=False)
    print(f"Status Code: {r.status_code}")
    print(f"Final URL: {r.url}")
    print(f"Content Length: {len(r.text)} bytes")

    r.encoding = r.apparent_encoding
    soup = BeautifulSoup(r.text, "html.parser")

    # Look for onclick handlers
    onclick_tags = soup.find_all(True, onclick=True)
    print(f"Tags with onclick: {len(onclick_tags)}")

    # Look for d.php links
    property_ids = re.findall(r'd\.php\?hpno=(\w+)', r.text)
    print(f"Property IDs found: {len(property_ids)}")
    if property_ids:
        print(f"Sample IDs: {property_ids[:10]}")

    # Save HTML for inspection
    with open('/tmp/izutaiyo_method3.html', 'w', encoding='utf-8') as f:
        f.write(r.text)
    print("HTML saved to: /tmp/izutaiyo_method3.html")

    return r.text

def analyze_html_structure(html_content):
    """Analyze the HTML to understand its structure"""
    print("\n" + "="*70)
    print("HTML STRUCTURE ANALYSIS")
    print("="*70)

    soup = BeautifulSoup(html_content, "html.parser")

    # Look for common property listing patterns
    print("\nSearching for common patterns:")

    # Check for property cards/items
    for selector in ['.property', '.item', '.listing', '[class*="prop"]', '[class*="estate"]']:
        elements = soup.select(selector)
        if elements:
            print(f"  Found {len(elements)} elements matching: {selector}")

    # Check for links containing certain keywords
    all_links = soup.find_all('a', href=True)
    print(f"\nTotal <a> tags: {len(all_links)}")

    d_php_links = [a for a in all_links if 'd.php' in a.get('href', '')]
    print(f"Links containing 'd.php': {len(d_php_links)}")
    if d_php_links:
        print(f"Sample d.php links:")
        for link in d_php_links[:5]:
            print(f"  - {link.get('href')}")

    # Check for specific text patterns
    if '検索結果' in html_content:
        print("\n✓ Found '検索結果' (search results) text")
    if '該当する物件' in html_content:
        print("✓ Found '該当する物件' (matching properties) text")
    if '見つかりませんでした' in html_content:
        print("⚠️  Found '見つかりませんでした' (not found) text - NO RESULTS!")

    # Check page title
    title_tag = soup.find('title')
    if title_tag:
        print(f"\nPage title: {title_tag.text}")

def main():
    print("\n" + "="*70)
    print("IZU TAIYO SEARCH ENDPOINT DEBUG")
    print("="*70)
    print("\nTesting different methods to construct the search URL...")
    print("Looking for Shimoda (sm) Houses (家) with sea views")
    print("Expected: Should find properties like SMB240H, SMB225H, etc.")

    # Test all three methods
    html1 = test_search_method_1()
    html2 = test_search_method_2()
    html3 = test_search_method_3()

    # Analyze the structure of the first result
    analyze_html_structure(html1)

    print("\n" + "="*70)
    print("RECOMMENDATION")
    print("="*70)
    print("Check the HTML files saved in /tmp/ to see what's being returned.")
    print("Compare the results from different methods to find which one works.")
    print()

if __name__ == "__main__":
    main()
