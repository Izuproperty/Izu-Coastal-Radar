#!/usr/bin/env python3
"""
Biccamera Price Monitor
Checks the price of a specific item daily and alerts when it drops below a threshold.

Item: https://www.biccamera.com/bc/item/14325899/
Alert threshold: JPY 298,000
"""

import json
import logging
import os
import re
import smtplib
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime, date
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

# ── Configuration ─────────────────────────────────────────────────────────────

ITEM_URL = "https://www.biccamera.com/bc/item/14325899/"
PRICE_THRESHOLD = 298_000  # JPY — alert if price drops BELOW this
HISTORY_FILE = Path(__file__).parent / "price_history.json"
LOG_FILE = Path(__file__).parent / "monitor.log"

# Loaded from environment variables (set these in .env or export before running)
SMTP_HOST = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", "")           # Your Gmail address
SMTP_PASS = os.getenv("SMTP_PASS", "")           # App password (not your main password)
ALERT_TO   = os.getenv("ALERT_TO", "")           # Where to send alerts
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "")       # Optional: Slack/Discord/generic webhook

# ── Logging ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger(__name__)

# ── Scraping ──────────────────────────────────────────────────────────────────

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/121.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ja,en-US;q=0.7,en;q=0.3",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Cache-Control": "no-cache",
    "Referer": "https://www.biccamera.com/",
}

_FETCH_RETRIES = 3
_FETCH_TIMEOUT = 60  # seconds per attempt


def fetch_page(url: str) -> str:
    """Download the page HTML, retrying up to _FETCH_RETRIES times with backoff."""
    last_exc: Exception | None = None
    for attempt in range(1, _FETCH_RETRIES + 1):
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=_FETCH_TIMEOUT) as resp:
                raw = resp.read()
                if resp.info().get("Content-Encoding") == "gzip":
                    import gzip
                    raw = gzip.decompress(raw)
                charset = resp.headers.get_content_charset() or "utf-8"
                return raw.decode(charset, errors="replace")
        except Exception as exc:
            last_exc = exc
            if attempt < _FETCH_RETRIES:
                wait = 2 ** attempt  # 2 s, 4 s
                log.warning("Fetch attempt %d/%d failed (%s) — retrying in %ds…",
                            attempt, _FETCH_RETRIES, exc, wait)
                time.sleep(wait)
    raise last_exc  # re-raise after all attempts exhausted


def _extract_first_number(text: str) -> int | None:
    """Pull the first run of digits (possibly with commas) from a string."""
    m = re.search(r"[\d,]+", text)
    if m:
        return int(m.group().replace(",", ""))
    return None


def parse_price(html: str) -> int | None:
    """
    Try several strategies to extract the current price from the page.
    Returns price as an integer (JPY) or None if not found.
    """

    # ── Strategy 1: JSON-LD structured data ──────────────────────────────────
    # <script type="application/ld+json">{"@type":"Product","offers":{"price":"..."}}
    for block in re.findall(
        r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        html,
        re.DOTALL | re.IGNORECASE,
    ):
        try:
            data = json.loads(block)
            offers = data.get("offers") or {}
            if isinstance(offers, list):
                offers = offers[0]
            if "price" in offers:
                return int(float(str(offers["price"]).replace(",", "")))
        except (json.JSONDecodeError, ValueError, KeyError):
            pass

    # ── Strategy 2: Open Graph / meta tags ───────────────────────────────────
    # <meta property="product:price:amount" content="298000">
    # <meta property="og:price:amount" content="298000">
    for pattern in [
        r'<meta[^>]+property=["\']product:price:amount["\'][^>]+content=["\']([^"\']+)["\']',
        r'<meta[^>]+property=["\']og:price:amount["\'][^>]+content=["\']([^"\']+)["\']',
        r'<meta[^>]+name=["\']price["\'][^>]+content=["\']([^"\']+)["\']',
    ]:
        m = re.search(pattern, html, re.IGNORECASE)
        if m:
            val = _extract_first_number(m.group(1))
            if val:
                return val

    # ── Strategy 3: data-price / data-value attributes ───────────────────────
    # Common in Japanese EC sites: <span data-price="298000">
    for pattern in [
        r'data-price=["\'](\d[\d,]*)["\']',
        r'data-item-price=["\'](\d[\d,]*)["\']',
        r'data-normal-price=["\'](\d[\d,]*)["\']',
    ]:
        m = re.search(pattern, html, re.IGNORECASE)
        if m:
            return int(m.group(1).replace(",", ""))

    # ── Strategy 4: Known biccamera CSS class patterns ────────────────────────
    # Biccamera typically wraps the price in one of these structures.
    # We look for the class, then grab the first number that follows.
    class_patterns = [
        r'class="[^"]*item_normal_price[^"]*"[^>]*>([^<]+)',
        r'class="[^"]*itemPrice[^"]*"[^>]*>([^<]+)',
        r'class="[^"]*js-item_normal_price[^"]*"[^>]*>([^<]+)',
        r'class="[^"]*price_txt[^"]*"[^>]*>([^<]+)',
        r'class="[^"]*p-item-price[^"]*"[^>]*>([^<]+)',
        r'class="[^"]*sale_price[^"]*"[^>]*>([^<]+)',
        r'id="[^"]*item_normal_price[^"]*"[^>]*>([^<]+)',
    ]
    for pattern in class_patterns:
        m = re.search(pattern, html, re.IGNORECASE)
        if m:
            val = _extract_first_number(m.group(1))
            if val and val > 1_000:   # sanity: skip tiny numbers
                return val

    # ── Strategy 5: Broad price regex near yen symbols ───────────────────────
    # Look for patterns like "298,000円" or "¥298,000"
    for pattern in [
        r"¥\s*([\d,]+)",
        r"([\d,]+)\s*円",
        r"([\d,]+)\s*JPY",
    ]:
        for m in re.finditer(pattern, html):
            val = int(m.group(1).replace(",", ""))
            # Plausible product price: between 10,000 and 2,000,000 JPY
            if 10_000 <= val <= 2_000_000:
                return val

    return None


# ── Price history ─────────────────────────────────────────────────────────────

def load_history() -> list[dict]:
    if HISTORY_FILE.exists():
        return json.loads(HISTORY_FILE.read_text())
    return []


def save_history(history: list[dict]) -> None:
    HISTORY_FILE.write_text(json.dumps(history, indent=2, ensure_ascii=False))


def record_price(price: int | None) -> None:
    history = load_history()
    history.append({
        "date": date.today().isoformat(),
        "timestamp": datetime.now().isoformat(),
        "price": price,
        "url": ITEM_URL,
    })
    save_history(history)


# ── Notifications ─────────────────────────────────────────────────────────────

def send_email(subject: str, body: str) -> None:
    if not (SMTP_USER and SMTP_PASS and ALERT_TO):
        log.warning("Email not configured — skipping email alert.")
        return

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = SMTP_USER
    msg["To"] = ALERT_TO

    html_body = f"""<html><body>
    <p>{body.replace(chr(10), '<br>')}</p>
    <p><a href="{ITEM_URL}">{ITEM_URL}</a></p>
    </body></html>"""

    msg.attach(MIMEText(body, "plain"))
    msg.attach(MIMEText(html_body, "html"))

    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
            server.ehlo()
            server.starttls()
            server.login(SMTP_USER, SMTP_PASS)
            server.sendmail(SMTP_USER, ALERT_TO, msg.as_string())
        log.info("Email alert sent to %s", ALERT_TO)
    except Exception as exc:
        log.error("Failed to send email: %s", exc)


def send_webhook(payload: dict) -> None:
    if not WEBHOOK_URL:
        return
    import json as _json
    data = _json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        WEBHOOK_URL,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            log.info("Webhook delivered (status %s)", resp.status)
    except Exception as exc:
        log.error("Webhook failed: %s", exc)


def notify(price: int) -> None:
    price_fmt = f"¥{price:,}"
    threshold_fmt = f"¥{PRICE_THRESHOLD:,}"
    subject = f"[Price Alert] Biccamera item dropped to {price_fmt}!"
    body = (
        f"Good news! The item you're watching has dropped below {threshold_fmt}.\n\n"
        f"Current price : {price_fmt}\n"
        f"Your threshold: {threshold_fmt}\n"
        f"Item URL      : {ITEM_URL}\n\n"
        f"Checked at    : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
    )
    log.info("ALERT: %s", subject)
    send_email(subject, body)
    send_webhook({
        "text": subject,
        "price": price,
        "threshold": PRICE_THRESHOLD,
        "url": ITEM_URL,
    })


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    log.info("Checking price for %s", ITEM_URL)

    try:
        html = fetch_page(ITEM_URL)
    except urllib.error.HTTPError as exc:
        log.error("HTTP error fetching page: %s %s", exc.code, exc.reason)
        record_price(None)
        sys.exit(1)
    except Exception as exc:
        log.error("Failed to fetch page: %s", exc)
        record_price(None)
        sys.exit(1)

    price = parse_price(html)

    if price is None:
        log.warning("Could not parse price from page — page structure may have changed.")
        log.warning("Saving a snippet for debugging…")
        snippet_path = Path(__file__).parent / "last_page_snippet.html"
        # Save first 8 KB to aid debugging
        snippet_path.write_text(html[:8192])
        log.warning("Snippet saved to %s", snippet_path)
        record_price(None)
        sys.exit(1)

    log.info("Current price: ¥%s", f"{price:,}")
    record_price(price)

    if price < PRICE_THRESHOLD:
        log.info("Price is BELOW threshold (¥%s) — sending alert!", f"{PRICE_THRESHOLD:,}")
        notify(price)
    else:
        log.info(
            "Price ¥%s is still above threshold ¥%s — no alert.",
            f"{price:,}",
            f"{PRICE_THRESHOLD:,}",
        )


if __name__ == "__main__":
    main()
