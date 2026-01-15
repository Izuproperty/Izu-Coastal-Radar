# Izu Taiyo Search Parameters to Test

## Current Implementation (What We Know Works)
```
Shimoda Houses: https://www.izutaiyo.co.jp/tokusen.php?hpcity[]=22219&hpkind=1
Shimoda Land:   https://www.izutaiyo.co.jp/tokusen.php?hpcity[]=22219&hpkind=2
```

## Property IDs
- **Working:** SMB392H (appears in search, successfully scraped)
- **Missing:** SMB240H, SMB225H, SMB368H, SMB195H (direct access works, but not in search)

## Test Matrix: Search Variations to Try

### 1. Different hpkind Values
```bash
# Test hpkind values 0-9
curl "https://www.izutaiyo.co.jp/tokusen.php?hpcity[]=22219&hpkind=0"  # Mansions (we skip these)
curl "https://www.izutaiyo.co.jp/tokusen.php?hpcity[]=22219&hpkind=1"  # Houses (CURRENT)
curl "https://www.izutaiyo.co.jp/tokusen.php?hpcity[]=22219&hpkind=2"  # Land (CURRENT)
curl "https://www.izutaiyo.co.jp/tokusen.php?hpcity[]=22219&hpkind=3"  # ??? (TEST THIS)
curl "https://www.izutaiyo.co.jp/tokusen.php?hpcity[]=22219&hpkind=4"  # ??? (TEST THIS)
curl "https://www.izutaiyo.co.jp/tokusen.php?hpcity[]=22219&hpkind=5"  # ??? (TEST THIS)
```

Expected: Find which hpkind contains the missing properties

### 2. No hpkind Filter
```bash
# Remove hpkind entirely - get ALL property types
curl "https://www.izutaiyo.co.jp/tokusen.php?hpcity[]=22219"
```

Expected: Should return all properties regardless of type

### 3. Keyword/Text Search
```bash
# The user says they search for "下田市[sm]"
curl "https://www.izutaiyo.co.jp/tokusen.php?keyword=下田市+sm"
curl "https://www.izutaiyo.co.jp/tokusen.php?search=下田市+sm"
curl "https://www.izutaiyo.co.jp/tokusen.php?q=下田市+sm"
curl "https://www.izutaiyo.co.jp/tokusen.php?hpcity[]=22219&keyword=sm"
curl "https://www.izutaiyo.co.jp/tokusen.php?hpcity[]=22219&code=sm"
```

Expected: Mimics the manual search that finds all properties

### 4. Status/Availability Parameters
```bash
# Include properties with different statuses
curl "https://www.izutaiyo.co.jp/tokusen.php?hpcity[]=22219&hpkind=1&status=all"
curl "https://www.izutaiyo.co.jp/tokusen.php?hpcity[]=22219&hpkind=1&show=all"
curl "https://www.izutaiyo.co.jp/tokusen.php?hpcity[]=22219&hpkind=1&include_reserved=1"
```

Expected: Include reserved/pending properties

### 5. Sorting/Limiting Parameters
```bash
# Maybe properties are hidden by default sort/limit
curl "https://www.izutaiyo.co.jp/tokusen.php?hpcity[]=22219&hpkind=1&limit=999"
curl "https://www.izutaiyo.co.jp/tokusen.php?hpcity[]=22219&hpkind=1&sort=new"
curl "https://www.izutaiyo.co.jp/tokusen.php?hpcity[]=22219&hpkind=1&sort=old"
```

Expected: Ensure we're not missing properties due to result limits

### 6. Alternative Listing Pages
```bash
# Different listing endpoints
curl "https://www.izutaiyo.co.jp/shinchaku.php?hpcity[]=22219"  # New listings
curl "https://www.izutaiyo.co.jp/bukken.php?hpcity[]=22219"     # All properties
curl "https://www.izutaiyo.co.jp/list.php?hpcity[]=22219"       # Property list
curl "https://www.izutaiyo.co.jp/search.php?city=22219"         # Search page
```

Expected: Find alternative endpoints that might list these properties

### 7. Direct Property Number Range Check
```bash
# Test if there's a pattern to SM property numbers
for i in {190..450}; do
    curl -I "https://www.izutaiyo.co.jp/d.php?hpno=SMB${i}H" 2>/dev/null | grep "HTTP/"
done
```

Expected: Find all existing SMB###H properties, not just the ones in search

## Analysis Checklist for Each Missing Property

When you fetch each missing property page, check for:

### HTML Content to Examine
- [ ] `<title>` tag - Look for category indicators
- [ ] `<meta>` tags - Check for property_type, category, status
- [ ] `<form>` hidden inputs - Look for hpkind, status, category fields
- [ ] `<script>` variables - Check for JavaScript property data objects
- [ ] Table rows - Look for "物件種別" (property type) or "ステータス" (status)

### Text to Search For
```
Search for these patterns in the HTML:
- hpkind[=:]\s*(\d+)
- status[=:]\s*["']([^"']+)
- category[=:]\s*["']([^"']+)
- 物件種別：(.+)
- ステータス：(.+)
- 商談中|成約|予約
```

### Comparison Points
Compare each missing property against SMB392H (the working one):
- Property type classification
- Status flags
- Meta tags
- Hidden form fields
- URL structure
- Price display format
- Date listed
- Agent/office handling it

## Quick Test Script

```python
#!/usr/bin/env python3
import requests
from bs4 import BeautifulSoup

properties = ["SMB392H", "SMB240H", "SMB225H", "SMB368H", "SMB195H"]
hpkinds = [None, 0, 1, 2, 3, 4, 5]

print("Testing which hpkind values show each property...")
for hpkind in hpkinds:
    if hpkind is None:
        url = "https://www.izutaiyo.co.jp/tokusen.php?hpcity[]=22219"
        label = "No filter"
    else:
        url = f"https://www.izutaiyo.co.jp/tokusen.php?hpcity[]=22219&hpkind={hpkind}"
        label = f"hpkind={hpkind}"

    try:
        r = requests.get(url, timeout=10, verify=False)
        found = []
        for prop in properties:
            if prop in r.text:
                found.append(prop)

        print(f"{label:15} | Found: {', '.join(found) if found else 'NONE'}")
    except Exception as e:
        print(f"{label:15} | ERROR: {e}")
```

## Expected Outcomes

### Most Likely Scenario
The missing properties have `hpkind=3` or `hpkind=4`, or require no hpkind filter.

### Alternative Scenario
The properties require a keyword search parameter rather than the filtered search.

### Worst Case Scenario
The properties are dynamically loaded via JavaScript or require POST requests with specific tokens.

## Implementation Priority

1. **HIGH**: Test hpkind values 3, 4, 5, and None
2. **HIGH**: Test search without hpkind parameter
3. **MEDIUM**: Test keyword-based search
4. **MEDIUM**: Analyze property page HTML for classification clues
5. **LOW**: Test alternative listing endpoints
6. **LOW**: Implement property number range checking
