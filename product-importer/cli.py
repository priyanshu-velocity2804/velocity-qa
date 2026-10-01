#!/usr/bin/env python3
"""Import Amazon / Flipkart product links into Shopify from the command line.

    python3 cli.py "https://www.amazon.in/dp/B073JYC4XM" "https://www.flipkart.com/x/p/itm?pid=ACCHE5DZFMEP4PYG"
    python3 cli.py --no-upload --csv products.csv <links...>     # CSV only, no Shopify writes
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from dotenv import load_dotenv

load_dotenv(Path(__file__).with_name(".env"))

from importer import shopify  # noqa: E402
from importer.pipeline import run_import  # noqa: E402


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("links", nargs="+", help="Amazon/Flipkart product URLs")
    parser.add_argument("--no-upload", action="store_true", help="Scrape and write CSV only")
    parser.add_argument("--csv", type=Path, help="Write Shopify import CSV to this path")
    parser.add_argument("--json", type=Path, help="Write normalised product data to this path")
    parser.add_argument("--status", choices=["draft", "active"], help="Shopify product status (default from .env)")
    args = parser.parse_args(argv)

    try:
        run = run_import(" ".join(args.links), upload=not args.no_upload, status=args.status)
    except shopify.ShopifyError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    for error in run.link_errors:
        print(f"! {error}", file=sys.stderr)
    for result in run.results:
        name = f"{result.link.source}:{result.link.product_id}"
        if result.error:
            print(f"✗ {name}  {result.error}")
            continue
        p = result.product
        line = f"✓ {name}  {p['title'][:70]}  {p['currency'] or ''} {p['price']}  ({len(p['images'])} images)"
        if result.shopify:
            verb = "updated" if result.shopify["updated_existing"] else "created"
            line += f"\n    Shopify {verb}: {result.shopify['admin_url']}"
        print(line)
    if args.csv and run.products:
        args.csv.write_text(shopify.to_csv(run.products, args.status), encoding="utf-8")
        print(f"CSV: {args.csv.resolve()}")
    if args.json and run.products:
        args.json.write_text(json.dumps(run.products, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"JSON: {args.json.resolve()}")
    if not run.results:
        print("No Amazon or Flipkart product links found.", file=sys.stderr)
        return 1
    return 0 if all(not r.error for r in run.results) else 2


if __name__ == "__main__":
    raise SystemExit(main())
