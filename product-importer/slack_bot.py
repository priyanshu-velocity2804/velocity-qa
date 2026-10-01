#!/usr/bin/env python3
"""Slack bot: mention it with Amazon/Flipkart links and it imports them into Shopify.

    @Product Importer https://www.amazon.in/dp/B073JYC4XM https://www.flipkart.com/...?pid=...

Words in the message change behaviour:
    "csv" / "no upload"   scrape and reply with a Shopify CSV, don't touch the store
    "publish" / "active"  create products as Active instead of the default status
    "help"                show usage

Runs in Socket Mode, so no public URL or server is needed — just keep this process running.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
import re

from dotenv import load_dotenv

load_dotenv(Path(__file__).with_name(".env"))

from slack_bolt import App  # noqa: E402
from slack_bolt.adapter.socket_mode import SocketModeHandler  # noqa: E402

from importer import shopify  # noqa: E402
from importer.pipeline import run_import  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("product-importer")

app = App(token=os.environ["SLACK_BOT_TOKEN"])

# Optional: also react when someone tags this person (e.g. you) instead of the bot.
TRIGGER_USER_ID = os.getenv("TRIGGER_USER_ID", "").strip()
ALLOWED_CHANNELS = {c.strip() for c in os.getenv("ALLOWED_CHANNEL_IDS", "").split(",") if c.strip()}
ALLOWED_USERS = {u.strip() for u in os.getenv("ALLOWED_USER_IDS", "").split(",") if u.strip()}

HELP = (
    "*Send me Amazon or Flipkart product links and I'll add them to Shopify.*\n"
    "• `@me <link> <link> …` — scrape and create/update the products (as "
    f"*{shopify.product_status().lower()}*)\n"
    "• add `publish` to create them as *active*\n"
    "• add `csv` to only get a Shopify import CSV, without touching the store\n"
    "Re-sending a link updates the existing product instead of creating a duplicate."
)


def allowed(event: dict) -> bool:
    if ALLOWED_CHANNELS and event.get("channel") not in ALLOWED_CHANNELS:
        return False
    if ALLOWED_USERS and event.get("user") not in ALLOWED_USERS:
        return False
    return True


def react(client, event, name):
    try:
        client.reactions_add(channel=event["channel"], timestamp=event["ts"], name=name)
    except Exception:  # reactions are cosmetic; e.g. already_reacted
        pass


def summary_line(result) -> str:
    link = result.link
    label = f"{'Amazon' if link.source == 'amazon' else 'Flipkart'} `{link.product_id}`"
    if result.error:
        return f":x: {label} — {result.error[:400]}"
    p = result.product
    price = f"{p['currency'] or ''} {p['price']:,.0f}".strip()
    line = f":white_check_mark: *{p['title'][:90]}* — {price}, {len(p['images'])} images"
    if result.shopify:
        verb = "updated" if result.shopify["updated_existing"] else "created"
        line += f"\n      <{result.shopify['admin_url']}|Open in Shopify> ({verb}, {result.shopify['status'].lower()})"
    return line


def handle_request(event: dict, client, bot_user_id: str):
    if not allowed(event):
        return
    text = event.get("text", "")
    channel, thread_ts = event["channel"], event.get("thread_ts") or event["ts"]
    lowered = re.sub(r"<[^>]+>", " ", text).lower()  # ignore words inside links/mentions

    if re.search(r"\bhelp\b", lowered) and "http" not in text:
        client.chat_postMessage(channel=channel, thread_ts=thread_ts, text=HELP)
        return
    csv_only = bool(re.search(r"\bcsv\b|no[ -]?upload", lowered))
    status = "ACTIVE" if re.search(r"\b(publish|active|live)\b", lowered) else None

    react(client, event, "eyes")
    try:
        run = run_import(text, upload=not csv_only, status=status)
    except shopify.ShopifyError as exc:
        client.chat_postMessage(channel=channel, thread_ts=thread_ts, text=f":x: Shopify isn't configured: {exc}")
        react(client, event, "x")
        return

    if not run.results:
        msg = "I didn't find any Amazon or Flipkart product links in that message."
        if run.link_errors:
            msg += "\n" + "\n".join(f"• {e}" for e in run.link_errors)
        client.chat_postMessage(channel=channel, thread_ts=thread_ts, text=msg + "\n\n" + HELP)
        react(client, event, "question")
        return

    ok = [r for r in run.results if not r.error]
    heading = (f"Scraped {len(ok)}/{len(run.results)} products"
               + ("" if csv_only else " and sent them to Shopify") + ":")
    lines = [heading] + [summary_line(r) for r in run.results]
    lines += [f":warning: {e}" for e in run.link_errors]
    client.chat_postMessage(channel=channel, thread_ts=thread_ts, text="\n".join(lines),
                            unfurl_links=False, unfurl_media=False)

    if run.products:
        try:
            client.files_upload_v2(
                channel=channel, thread_ts=thread_ts, filename="shopify_products.csv",
                content=shopify.to_csv(run.products, status), title="Shopify import CSV",
            )
        except Exception as exc:
            log.warning("CSV upload failed: %s", exc)
    react(client, event, "white_check_mark" if len(ok) == len(run.results) else "warning")


@app.event("app_mention")
def on_mention(event, client, context):
    handle_request(event, client, context.bot_user_id)


@app.event("message")
def on_message(event, client, context):
    # Only used when TRIGGER_USER_ID is set: treat "@that person <links>" like a bot mention.
    if not TRIGGER_USER_ID or event.get("subtype") or event.get("bot_id"):
        return
    text = event.get("text", "")
    if f"<@{TRIGGER_USER_ID}>" not in text or f"<@{context.bot_user_id}>" in text:
        return  # not for us, or app_mention already handles it
    if "http" not in text:
        return  # a normal message to that person, not an import request
    handle_request(event, client, context.bot_user_id)


if __name__ == "__main__":
    log.info("Starting Slack product importer (Socket Mode)…")
    SocketModeHandler(app, os.environ["SLACK_APP_TOKEN"]).start()
