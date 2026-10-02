# Product Importer: Amazon/Flipkart to Shopify, from Slack

Tag the bot in Slack with one or more product links. It scrapes each product,
converts it to Shopify format and creates the products in your store. It replies
in the thread with links to the products and a Shopify import CSV.

```
@Product Importer https://www.amazon.in/dp/B073JYC4XM https://www.flipkart.com/...?pid=ACCHE5DZFMEP4PYG
```

| Add to the message | Effect |
|---|---|
| *(nothing)* | Create or update the products with `SHOPIFY_PRODUCT_STATUS` (default **draft**) |
| `publish` | Create them as **active** |
| `csv` | Only scrape and return the CSV. The store isn't touched |
| `help` | Show usage |

- **Amazon:** Rainforest API (`type=product`). Works with any Amazon domain (`amazon.in`, `.com`, …) and short links (`amzn.in`, `amzn.to`, `a.co`).
- **Flipkart:** `importer/flipkart_fsn.py` (the supplied scraper, unchanged). Reads the `pid=` FSN from the link and also follows `dl.flipkart.com` / `fkrt.it` short links.
- **Shopify:** Admin GraphQL `productSet`. Each import includes the title, HTML description (highlights and a specs table), brand as vendor, type, tags, price, MRP as compare-at price, SKU, weight, up to 20 images, SEO fields, and the source URL as a metafield.
- **No duplicates:** the handle is stable (`amz-<asin>` / `fk-<fsn>`), so sending the same link again updates the existing product.

## Setup

### 1. Install

```bash
cd product-importer
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env   # then fill it in
```

### 2. Shopify credentials

Shopify no longer allows new custom apps created in the store admin. Use the Dev Dashboard instead:

1. Go to https://dev.shopify.com, create an app and set the Admin API scopes `write_products, read_products, write_inventory, read_inventory, read_locations`.
2. Install it on your store.
3. Copy the **Client ID** and **Client secret** into `.env` as `SHOPIFY_CLIENT_ID` / `SHOPIFY_CLIENT_SECRET`, and set `SHOPIFY_STORE=your-store.myshopify.com`.

The importer fetches and refreshes its 24-hour token automatically. If you already have an
admin-created custom app, set `SHOPIFY_ACCESS_TOKEN=shpat_...` instead.

### 3. Slack app

1. Go to https://api.slack.com/apps, then **Create New App**, choose **From a manifest**, and paste `slack_manifest.json`.
2. **Basic Information → App-Level Tokens:** create a token with the `connections:write` scope. Put it in `.env` as `SLACK_APP_TOKEN` (`xapp-…`).
3. **Install App:** install it to the workspace and copy the **Bot User OAuth Token** into `.env` as `SLACK_BOT_TOKEN` (`xoxb-…`).
4. In Slack, invite the bot to the channels where people will use it: `/invite @Product Importer`.

The bot uses Socket Mode, so it needs no public URL. It only has to be running somewhere.

**Triggering on *your* name instead of the bot:** set `TRIGGER_USER_ID` to your Slack member ID
(Profile → ⋯ → Copy member ID). Any message in a channel the bot is in that tags you *and*
contains a link is then imported as well. Messages that tag you without a link are ignored.

**Restricting who can use it:** set `ALLOWED_CHANNEL_IDS` and/or `ALLOWED_USER_IDS` (comma-separated).

### 4. Run

```bash
.venv/bin/python slack_bot.py
```

Keep it running with `tmux`, `launchd`, or any small VM, Railway or Render worker. It's a single long-running process.

## Command line

```bash
.venv/bin/python cli.py <links...>                               # import into Shopify
.venv/bin/python cli.py --no-upload --csv out.csv <links...>     # CSV only (Shopify → Products → Import)
.venv/bin/python cli.py --status active --json data.json <links...>
```

## Options (`.env`)

| Variable | Default | |
|---|---|---|
| `SHOPIFY_PRODUCT_STATUS` | `DRAFT` | `DRAFT` lets someone review before going live |
| `INVENTORY_QUANTITY` | `100` | Starting stock for new products (tracked by Shopify, selling stops at 0). Re-imports don't reset it. CLI: `--inventory 50` |
| `SHOPIFY_LOCATION_ID` | first active location | Which location holds that stock |
| `PRICE_MARKUP_PERCENT` | `0` | Applied to both price and compare-at price |
| `FLIPKART_COOKIE` | | Only needed if Flipkart starts returning 403/429. See the cookie notes in `importer/flipkart_fsn.py` |
| `SHOPIFY_API_VERSION` | `2026-07` | |

## Limitations

- Each source product becomes **one Shopify variant**. Other sizes and colours of a listing are not imported as variants.
- Prices are imported in the source currency without conversion. This assumes your store uses the same currency (INR for `.in` / Flipkart).
- Each Amazon link uses 1 Rainforest credit.
- Flipkart may block datacenter IPs. Running from a home or office network works best.

## Tests

```bash
.venv/bin/python -m unittest discover -s tests -t .
```
