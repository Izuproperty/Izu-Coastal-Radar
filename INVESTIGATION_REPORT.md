# Investigation Report: Missing Shimoda Properties

## Problem Statement
The following Shimoda properties can be found manually but don't appear in automated search results:
- SMB240H: https://www.izutaiyo.co.jp/d.php?hpno=SMB240H
- SMB225H: https://www.izutaiyo.co.jp/d.php?hpno=SMB225H
- SMB368H: https://www.izutaiyo.co.jp/d.php?hpno=SMB368H
- SMB195H: https://www.izutaiyo.co.jp/d.php?hpno=SMB195H

Meanwhile, SMB392H DOES appear in search results and is successfully scraped.

## Current Search Implementation

### Search URLs Used
The scraper currently uses these search endpoints:
```
https://www.izutaiyo.co.jp/tokusen.php?hpcity[]=22219&hpkind=1  (Houses)
https://www.izutaiyo.co.jp/tokusen.php?hpcity[]=22219&hpkind=2  (Land)
```

Where:
- `hpcity[]=22219` = Shimoda city code
- `hpkind=1` = Houses (戸建)
- `hpkind=2` = Land (売地)
- `hpkind=0` = Mansions (マンション) - explicitly excluded

### Link Extraction Method
The scraper extracts property links by:
1. **Finding onclick handlers** (lines 511-531)
   - Pattern: `d.php?hpno=(\w+)` or `d.php?hpbunno=([^'"&]+)`
2. **Finding direct <a> links** (lines 555-576)
   - Links containing `d.php` with `hpno=` or `hpbunno=` parameters

### Property Filtering
After fetching a property page, it applies these filters (in order):
1. **Location check** - Must be in target cities (下田, 河津, 東伊豆, 南伊豆)
2. **Sold status** - Rejects if contains: 成約, 商談中, 予約, Sold, Contracted, Reserved, 済
3. **Mansion filter** - Rejects if title contains "のマンション情報"
4. **Sea view score** - Must score ≥2 (proximity or explicit sea view mention)
5. **Price validation** - Must have valid price > 0

## Likely Reasons for Missing Properties

### Hypothesis 1: Different Property Type (hpkind)
**Most Likely**

The missing properties may have a different `hpkind` value that we're not searching for:
- `hpkind=3` - Could be a special category (resort properties, vacation homes, etc.)
- `hpkind=4` - Could be another category
- No hpkind filter - Properties might require searching without the hpkind parameter

**Evidence:**
- All missing properties have "SMB" prefix (Shimoda Beach?)
- SMB392H (which DOES appear) might be an exception that's cross-listed
- The user can find them by searching "下田市[sm]" which may use a different search mechanism

**Test Required:**
```python
# Try these search variations:
tokusen.php?hpcity[]=22219&hpkind=3
tokusen.php?hpcity[]=22219&hpkind=4
tokusen.php?hpcity[]=22219  # No hpkind filter
tokusen.php?search=下田市&keyword=sm  # Keyword search
```

### Hypothesis 2: Properties in "Featured" or "Special" Category
The properties might be in a different listing category:
- Featured properties (特選)
- New listings (新着)
- Price reduced
- Resort-specific category

**Search endpoints to test:**
```
/shinchaku.php  (new listings)
/tokusen.php with different parameters
/bknarea-{code}/  (area-specific pages)
```

### Hypothesis 3: Property Status Flag
The properties might have a status that excludes them from general search but not from direct access:
- Reserved but not yet contracted
- Pending
- Pre-listing
- Under negotiation

**Check for:**
- Hidden status fields in the HTML
- Status codes in URL parameters
- Special badges or markers on the page

### Hypothesis 4: Search Requires Additional Parameters
The search might require additional parameters to show all properties:
```
?hpcity[]=22219&hpkind=1&status=all
?hpcity[]=22219&hpkind=1&include_reserved=1
?hpcity[]=22219&show=all
```

### Hypothesis 5: Pagination or Limit Issues
- Properties might be on page 2+ but pagination is failing
- Results might be limited by price range
- Date filters might be excluding older/newer listings

## Comparison with SMB392H (Working Property)

SMB392H successfully appears in search results. Key differences to investigate:
- Is it cross-listed in multiple categories?
- Does it have different metadata?
- Is it newer/older than the missing properties?
- Does it have a different status flag?

## Manual Search Method Analysis

The user reports finding properties by searching "下田市[sm]":
- This appears to be using a **keyword/text search**, not the filtered search we're using
- The search might be hitting a different endpoint like:
  - `/search.php?q=下田市+sm`
  - `/tokusen.php?keyword=下田市&code=sm`
  - A site-wide search function rather than the filtered property browser

## Recommended Actions

### 1. Immediate Test (High Priority)
Test different hpkind values and search parameters:
```python
# Add to the scraper:
for hpkind in [1, 2, 3, 4, None]:
    if hpkind is None:
        url = f"tokusen.php?hpcity[]={code}"
    else:
        url = f"tokusen.php?hpcity[]={code}&hpkind={hpkind}"
    # Check if missing properties appear
```

### 2. Test Direct Search Endpoint (High Priority)
Implement a keyword-based search that mimics the manual search:
```python
# Try these:
search_url = "https://www.izutaiyo.co.jp/tokusen.php?keyword=下田市+sm"
search_url = "https://www.izutaiyo.co.jp/search/?q=SMB"
search_url = "https://www.izutaiyo.co.jp/tokusen.php?hpcity[]=22219&code=sm"
```

### 3. Analyze Property Pages (Medium Priority)
Fetch each missing property page and look for:
- Hidden form fields indicating category/type
- Meta tags with property classification
- JavaScript variables with property data
- Any status indicators not visible in normal text

### 4. Check for Additional Categories (Medium Priority)
Look for other listing pages on the site:
- Browse site navigation
- Check robots.txt for additional endpoints
- Look for sitemaps
- Check for mobile-specific URLs

### 5. Network Traffic Analysis (Low Priority)
If possible, monitor network traffic when doing manual search on the website to see:
- Actual search endpoint URLs
- POST vs GET parameters
- Any API calls being made
- Session/cookie requirements

## Code Changes Needed

### Option A: Expand hpkind Search (Quick Fix)
```python
# In IzuTaiyo.run() method, line 474:
property_types = [1, 2, 3, 4, None]  # Test all possible types

# And handle None case:
if hpkind is None:
    search_url = f"https://www.izutaiyo.co.jp/tokusen.php?hpcity[]={code}"
else:
    search_url = f"https://www.izutaiyo.co.jp/tokusen.php?hpcity[]={code}&hpkind={hpkind}"
```

### Option B: Add Keyword Search (Comprehensive Fix)
```python
# Add a new search method:
def search_by_keyword(self, city_code, keyword):
    """Search using keyword instead of filtered search"""
    search_url = f"https://www.izutaiyo.co.jp/tokusen.php?hpcity[]={city_code}&keyword={keyword}"
    # Or try site-wide search:
    # search_url = f"https://www.izutaiyo.co.jp/?s={keyword}+{city_name}"

# For Shimoda specifically:
if city_code == "22219":  # Shimoda
    # Try keyword search for SM properties
    self.search_by_keyword(city_code, "sm")
```

### Option C: Direct Property List (Fallback)
If search endpoints don't work, maintain a list of known property IDs and check them directly:
```python
# Check known "SM" series properties
for i in range(195, 500):  # SMB195H to SMB500H
    prop_id = f"SMB{i}H"
    url = f"https://www.izutaiyo.co.jp/d.php?hpno={prop_id}"
    # Try to fetch and see if it exists
```

## Success Criteria

The fix will be successful when:
1. All 4 missing properties appear in automated scraping
2. We understand the classification system used by Izu Taiyo
3. No other "SM" series properties are being missed
4. The solution is maintainable and doesn't require hardcoding property IDs

## Questions for Testing

1. What hpkind values exist besides 0, 1, 2?
2. What does the manual search "下田市[sm]" actually send to the server?
3. Are there other SMB### properties we're missing?
4. Do the missing properties have a common status/category?
5. Is there a sitemap or property index page we can use?

## Next Steps

1. Run test_missing_properties.py script to fetch and analyze each property
2. Try fetching search pages with different hpkind values
3. Use browser dev tools to capture the exact search request when searching "下田市[sm]"
4. Check if there's a difference in the HTML structure of missing vs found properties
5. Look for any JavaScript-based property loading that we might be missing
