# Amazon Live Review Accessibility Probe

This is a deliberately small, non-evasive feasibility probe.

## Install

```bash
python -m venv .venv
source .venv/bin/activate        # Linux/macOS
# .venv\Scripts\activate         # Windows PowerShell

pip install -r requirements.txt
```

## Run

Only after company/client authorization:

### Linux/macOS

```bash
export I_HAVE_PERMISSION=yes
python amazon_accessibility_probe.py \
  --asins B0XXXXXXXX \
  --pages 1 \
  --delay 20 \
  --contact your.work.email@example.com
```

### Windows PowerShell

```powershell
$env:I_HAVE_PERMISSION="yes"
python .\amazon_accessibility_probe.py `
  --asins B0XXXXXXXX `
  --pages 1 `
  --delay 20 `
  --contact your.work.email@example.com
```

Replace `B0XXXXXXXX` with a real 10-character ASIN from an Amazon product URL.

## Outputs

- `access_log.csv`: request status, redirects, blocking result, latency, response hash.
- `reviews_sample.jsonl`: small parsed sample, when available.
- `summary.json`: success rate, block rate, duplicates, and field completeness.

## Interpretation

- `captcha_or_bot_block`, `rate_limited`, `access_denied`, or `login_required`:
  stop the test and report the restriction.
- `reachable_but_no_reviews_parsed`:
  the page was reachable, but the HTML schema may have changed, the content may
  require JavaScript/login/region state, or the selected ASIN may not expose reviews.
- Successful parsing does not itself authorize production scraping.
