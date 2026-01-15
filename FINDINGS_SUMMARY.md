# Missing Shimoda Properties - Investigation Summary

## Quick Answer

**Why these properties don't appear in `hpcity[]=22219` searches:**

The most likely reason is that these properties have a **different `hpkind` (property type) value** than what we're currently searching for, or they require **searching without the `hpkind` parameter**.

Our scraper currently only searches:
- `hpkind=1` (Houses / 戸建)
- `hpkind=2` (Land / 売地)

But Izu Taiyo may have additional property types like:
- `hpkind=3` (possibly Resort properties, Vacation homes, etc.)
- `hpkind=4` (possibly another category)
- Or properties that don't fit into the standard hpkind categories

## The Evidence

### What Works
- **SMB392H** appears in `hpcity[]=22219&hpkind=1` search ✅
- It's successfully scraped and appears in listings.json ✅
- URL: https://www.izutaiyo.co.jp/d.php?hpno=SMB392H

### What Doesn't Work
These properties can be accessed directly but don't appear in search results:
- **SMB240H** ❌ https://www.izutaiyo.co.jp/d.php?hpno=SMB240H
- **SMB225H** ❌ https://www.izutaiyo.co.jp/d.php?hpno=SMB225H
- **SMB368H** ❌ https://www.izutaiyo.co.jp/d.php?hpno=SMB368H
- **SMB195H** ❌ https://www.izutaiyo.co.jp/d.php?hpno=SMB195H

### What You Told Us
- You can find these properties by searching "下田市[sm]" on the Izu Taiyo website
- This suggests a **keyword/text search** rather than the filtered search our scraper uses

## Current Scraper Implementation

### Search Method (Lines 480-591 in generate_listings.py)

```python
# Current city code
city_code = "22219"  # Shimoda

# Current property types searched
hpkind = 1  # Houses
hpkind = 2  # Land

# Search URL format
url = f"https://www.izutaiyo.co.jp/tokusen.php?hpcity[]={city_code}&hpkind={hpkind}"
```

### Link Extraction
The scraper finds properties by:
1. Looking for `onclick` attributes with `d.php?hpno=` patterns
2. Looking for direct `<a>` links to `d.php?hpno=` URLs

### Filtering Applied After Finding
Each property is then filtered by:
1. Location (must be in target cities)
2. Status (excludes sold/contracted)
3. Type (excludes mansions)
4. Sea view score (must be ≥2)
5. Price (must be valid and >0)

**Important:** The filtering happens AFTER finding the property in search results. If a property doesn't appear in the search results at all, it never gets to the filtering stage.

## What Makes SMB392H Different?

We need to determine why SMB392H appears in search but the others don't. Possibilities:

1. **Different property category** - SMB392H might be cross-listed in hpkind=1, but others are in hpkind=3 or 4
2. **Different listing status** - SMB392H might have a status that includes it in standard search
3. **Date/time factors** - Recently updated properties might appear in different searches
4. **Agent/office differences** - Different offices might use different listing categories

## Recommended Solution

### Option 1: Test All hpkind Values (RECOMMENDED - Quick Fix)

**Fastest solution** - Modify the scraper to test all possible hpkind values:

```python
# In generate_listings.py, line 474, change:
property_types = [1, 2]  # Current

# To:
property_types = [0, 1, 2, 3, 4, 5, None]  # Test all types

# And handle None case:
if hpkind is None:
    search_url = f"https://www.izutaiyo.co.jp/tokusen.php?hpcity[]={code}"
else:
    search_url = f"https://www.izutaiyo.co.jp/tokusen.php?hpcity[]={code}&hpkind={hpkind}"
```

**Pros:**
- Simple one-line code change
- Will find properties regardless of their hpkind classification
- No need to understand Izu Taiyo's internal categorization

**Cons:**
- May return duplicate properties
- Slower (more search requests)
- May include unwanted property types

### Option 2: Remove hpkind Filter Entirely (SIMPLER)

Even simpler - just search without the hpkind parameter:

```python
# Just search without hpkind:
search_url = f"https://www.izutaiyo.co.jp/tokusen.php?hpcity[]={code}"
```

Then filter by property type AFTER scraping based on our existing logic.

**Pros:**
- Simplest solution
- Guaranteed to get all properties
- Fewer search requests

**Cons:**
- Will get mansions too (but we already filter those out)

### Option 3: Add Keyword Search (COMPREHENSIVE)

Implement a keyword-based search that mimics your manual search:

```python
def search_shimoda_sm_properties(self):
    """Special search for SM-series Shimoda properties"""
    # Try keyword search
    search_url = "https://www.izutaiyo.co.jp/tokusen.php?keyword=下田市+sm"
    # Or: search_url = "https://www.izutaiyo.co.jp/tokusen.php?hpcity[]=22219&keyword=sm"

    soup = self.fetch(search_url)
    # Extract property links as usual
```

**Pros:**
- Most similar to your manual search method
- May find other SM properties we're missing

**Cons:**
- Need to reverse-engineer the keyword search parameters
- May require different parsing logic

## Testing Script

I've created `test_search_variations.py` which will:
1. Test hpkind values 0-5 and None
2. Test searches with and without hpkind parameter
3. Check direct access to each missing property
4. Show which search variation finds the most properties

**To run:**
```bash
cd /home/user/Izu-Coastal-Radar
pip3 install requests beautifulsoup4 urllib3
python3 test_search_variations.py
```

This will show you exactly which hpkind value (if any) contains the missing properties.

## What to Check Next

### 1. Run the Test Script (PRIORITY 1)
```bash
python3 test_search_variations.py > test_results.txt 2>&1
```

This will tell you definitively which hpkind value contains the missing properties.

### 2. Check Property Pages Directly (PRIORITY 2)
```bash
python3 test_missing_properties.py > property_analysis.txt 2>&1
```

This will analyze each missing property page to see:
- What their actual property type is
- If they have any status flags
- What makes them different from SMB392H

### 3. Monitor Manual Search (PRIORITY 3)
Use browser developer tools when you search "下田市[sm]" manually:
1. Open browser DevTools (F12)
2. Go to Network tab
3. Search for "下田市[sm]" on the Izu Taiyo website
4. Look at the actual HTTP request made
5. Check the URL and parameters used

This will show the EXACT search endpoint and parameters needed.

## Expected Timeline

- **Immediate (5 minutes):** Run test_search_variations.py to identify the hpkind value
- **Quick Fix (15 minutes):** Update scraper to include additional hpkind values
- **Testing (30 minutes):** Run scraper and verify all properties are found
- **Deployment:** Commit and push changes, wait for next daily scrape

## Success Criteria

✅ All 4 missing properties appear in next scrape
✅ No false positives (wrong city, sold properties, etc.)
✅ No regression (SMB392H still appears)
✅ Solution is maintainable (no hardcoded property IDs)

## Files Created for Investigation

1. **INVESTIGATION_REPORT.md** - Detailed analysis of the problem
2. **SEARCH_PARAMETERS_TO_TEST.md** - Comprehensive test matrix
3. **test_search_variations.py** - Automated test script for search endpoints
4. **test_missing_properties.py** - Script to analyze individual property pages
5. **FINDINGS_SUMMARY.md** - This file - executive summary

## Questions Still to Answer

1. ❓ What hpkind value contains the missing properties?
2. ❓ Are there other SM-series properties we're missing?
3. ❓ Why is SMB392H in hpkind=1 but others aren't?
4. ❓ What does the manual search "下田市[sm]" actually query?

**Run the test scripts to answer these questions!**

## Contact Points in Code

If you need to modify the scraper:

- **Search URL generation:** Line 489 in generate_listings.py
- **Property types array:** Line 474 in generate_listings.py
- **Link extraction:** Lines 506-576 in generate_listings.py
- **Property filtering:** Lines 632-738 in generate_listings.py

## Bottom Line

🔧 **Quick Fix:** Change line 474 to `property_types = [1, 2, 3, 4, None]`
🧪 **Proper Fix:** Run test_search_variations.py first to identify the exact hpkind value
📊 **Root Cause:** Properties have a different hpkind classification than we're searching for
