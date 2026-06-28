# Shopee Crawler

Small Playwright utility for logging in to Shopee with Chromium and saving the
authenticated browser session under `output/`.

## Setup

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
python -m playwright install chromium
```

## Login

### QR login

```bash
python login_shopee.py --qr
```

The script runs Chromium headless by default, prints the QR in your terminal,
saves the QR image to `output/shopee-qr.png`, and waits up to 2 minutes for
you to scan and approve the login in the Shopee mobile app.

After login succeeds it saves:

- `output/shopee-state.json` for Playwright `storage_state`
- `output/shopee-cookies.json` for raw browser cookies

Useful variants:

```bash
python login_shopee.py --qr --login-timeout 300000
python login_shopee.py --qr --qr-path /tmp/shopee-qr.png
python login_shopee.py --qr --cookies /tmp/shopee-cookies.json
python login_shopee.py --qr --no-cli-qr
python login_shopee.py --qr --cli-qr-width 64
python login_shopee.py --qr --print-qr-url --no-cli-qr
python login_shopee.py --qr --print-login-url --print-qr-url --no-cli-qr
python login_shopee.py --qr --headful
```

### Logout

Log out the saved Shopee session and remove local state/cookie files:

```bash
python login_shopee.py --logout
```

To attempt the remote logout but keep local files:

```bash
python login_shopee.py --logout --keep-files
```

### Email, phone, or username login

```bash
export SHOPEE_IDENTIFIER="email@example.com"
export SHOPEE_PASSWORD="your-password"
python login_shopee.py
```

If Shopee asks for OTP, set `SHOPEE_OTP` before running or enter the code when
prompted:

```bash
SHOPEE_OTP="123456" python login_shopee.py
```

If Shopee shows a CAPTCHA or other manual security check, run a visible browser:

```bash
python login_shopee.py --headful
```

Use the saved session in another Playwright script:

```python
context = await browser.new_context(storage_state="output/shopee-state.json")
```

## Check Orders

After login creates `output/shopee-state.json`, check orders with:

```bash
python check_orders.py --allow-http2
```

This opens Shopee using the saved session, captures order-related `fetch`/`xhr`
JSON API responses, prints API summaries, saves parsed output to
`output/shopee-orders.json`, saves matched raw API payloads to
`output/shopee-order-api.json`, saves a network log to
`output/shopee-network-log.json`, saves simplified order data to
`output/shopee-orders-simple.json`, saves a human-readable active-order report
to `output/shopee-orders-report.md`, saves WhatsApp-ready text to
`output/shopee-orders-message.txt`, and saves a screenshot to
`output/shopee-orders.png`.

The simplified JSON contains product names, prices, order status, and tracking
details when those fields are available in Shopee's browser API responses.

To print only the human-readable report in the terminal:

```bash
python check_orders.py --print-report
```

If `output/shopee-orders-simple.json` or `output/shopee-orders-report.md`
already exists, this prints local output without opening Chromium or calling
Shopee. To force fresh API data:

```bash
python check_orders.py --allow-http2 --print-report --refresh
```

To print WhatsApp-ready message text for BOWAS:

```bash
python check_orders.py --print-message
```

Example:

```bash
MESSAGE=$(python check_orders.py --print-message)
curl -X POST http://localhost:3000/send \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d "$(node -e 'const [to,msg]=process.argv.slice(1); console.log(JSON.stringify({to,message:msg}))' 6281234567890 "$MESSAGE")"
```

To fetch all orders and apply a local date filter:

```bash
python check_orders.py --allow-http2 --all-orders --date-from 2026-06-01 --date-to 2026-06-28 --refresh
```

Use `--max-pages` and `--page-limit` if you need to control how many captured
Shopee order-list request pages are replayed.

If Shopee requires a visible browser:

```bash
python check_orders.py --headful
```
