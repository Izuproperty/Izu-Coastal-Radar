# Biccamera Price Monitor

Checks the price of a Biccamera item once per day and emails you (or sends a webhook) if it drops below ¥298,000.

**Item being monitored:** <https://www.biccamera.com/bc/item/14325899/>
**Alert threshold:** ¥298,000

---

## Quick start

### 1. Configure credentials

```bash
cd price-monitor
cp .env.example .env
# Edit .env with your Gmail address, App Password, and destination email
```

For Gmail you need an **App Password** (not your normal password):
1. Enable 2-Step Verification on your Google account
2. Visit <https://myaccount.google.com/apppasswords>
3. Create an app password — paste the 16-character code into `SMTP_PASS`

### 2. Test a single run

```bash
# Load the env vars and run once
set -a; source .env; set +a
python3 monitor.py
```

If it can't parse the price, it saves the first 8 KB of the page to `last_page_snippet.html` so you can inspect the HTML and adjust `parse_price()` in `monitor.py`.

### 3. Schedule daily checks (Linux / macOS)

```bash
bash setup_cron.sh
```

This installs a cron job that fires at **00:00 UTC = 09:00 JST** every day.

To remove it later:

```bash
crontab -e   # delete the line containing monitor.py
```

### 4. (Windows) Scheduled Task

Open **Task Scheduler → Create Basic Task** and set the action to:

```
Program : python
Arguments: C:\path\to\price-monitor\monitor.py
Start in : C:\path\to\price-monitor
```

Set environment variables via System Properties → Advanced → Environment Variables.

---

## Optional: Slack / Discord alerts

Set `WEBHOOK_URL` in `.env` to your Slack incoming webhook or Discord webhook URL.
The script will POST a JSON payload to it on every alert.

---

## How it works

`monitor.py` tries several strategies to extract the price from the page, in order:

1. **JSON-LD** structured data (`@type: Product → offers.price`)
2. **Open Graph** meta tags (`product:price:amount`)
3. **`data-price` attributes** (common in Japanese EC sites)
4. **CSS class patterns** matching known Biccamera class names
5. **Yen-symbol regex** (`¥298,000` / `298,000円`)

Results are appended to `price_history.json` every run.

---

## Files

| File | Purpose |
|------|---------|
| `monitor.py` | Main script |
| `.env.example` | Config template — copy to `.env` |
| `setup_cron.sh` | One-shot cron installer |
| `price_history.json` | Auto-created — daily price log |
| `monitor.log` | Auto-created — run log |
| `last_page_snippet.html` | Auto-created on parse failure — for debugging |
