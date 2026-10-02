"""Turn a normalised product into Shopify format: CSV import rows and Admin API uploads."""

from __future__ import annotations

import csv
import html
import io
import os
import re
import time
from typing import List, Optional

import requests

API_VERSION = os.getenv("SHOPIFY_API_VERSION", "2026-07")

# Column order of Shopify's product import template.
CSV_COLUMNS = [
    "Handle", "Title", "Body (HTML)", "Vendor", "Product Category", "Type", "Tags", "Published",
    "Option1 Name", "Option1 Value", "Variant SKU", "Variant Grams", "Variant Inventory Tracker",
    "Variant Inventory Qty", "Variant Inventory Policy", "Variant Fulfillment Service",
    "Variant Price", "Variant Compare At Price", "Variant Requires Shipping", "Variant Taxable",
    "Image Src", "Image Position", "Image Alt Text", "Gift Card", "SEO Title", "SEO Description",
    "Variant Weight Unit", "Status",
]

PRODUCT_SET = """
mutation ImportProduct($input: ProductSetInput!, $identifier: ProductSetIdentifiers) {
  productSet(synchronous: true, input: $input, identifier: $identifier) {
    product { id handle title status onlineStoreUrl }
    userErrors { field message code }
  }
}
"""

EXISTING_LOOKUP = """
query ($sku: String!, $handle: String!) {
  productVariants(first: 1, query: $sku) { nodes { product { id handle } } }
  productByIdentifier(identifier: {handle: $handle}) { id }
}
"""


LOCATIONS = """
query { locations(first: 10, query: "active:true") { nodes { id name fulfillsOnlineOrders } } }
"""


class ShopifyError(Exception):
    pass


# ---------------------------------------------------------------- formatting

def handle_for(product: dict) -> str:
    """Readable handle from the product name: "boAt Airdopes 219, 40H Battery, ..." -> "boat-airdopes-219".

    Marketplace titles append specs after a comma, "w/", "with" or "(", so only the name before them is used.
    """
    name = re.split(r",|\(|\s(?:w/|with)\s", product["title"], maxsplit=1, flags=re.I)[0]
    words = re.findall(r"[a-z0-9]+", name.lower().replace("+", " plus "))[:8]
    return "-".join(words) or unique_handle_for(product)


def unique_handle_for(product: dict) -> str:
    # Used when the readable handle is taken by a different product (e.g. another colour of the same model).
    words = re.findall(r"[a-z0-9]+", product["title"].lower())[:8]
    return "-".join(words + [product["source_id"].lower()])


def sku_for(product: dict) -> str:
    prefix = "AMZ" if product["source"] == "amazon" else "FK"
    return f"{prefix}-{product['source_id']}"


def apply_markup(price: Optional[float]) -> Optional[float]:
    if price is None:
        return None
    markup = float(os.getenv("PRICE_MARKUP_PERCENT", "0") or 0)
    return round(price * (1 + markup / 100), 2)


def inventory_quantity() -> int:
    try:
        quantity = int(os.getenv("INVENTORY_QUANTITY", "100"))
    except ValueError:
        raise ShopifyError("INVENTORY_QUANTITY must be a whole number.")
    if quantity <= 0:
        raise ShopifyError("INVENTORY_QUANTITY must be a positive number.")
    return quantity


def money(value: Optional[float]) -> str:
    return "" if value is None else f"{value:.2f}"


def body_html(product: dict) -> str:
    parts = []
    if product["description"]:
        parts.append(f"<p>{html.escape(product['description'])}</p>")
    if product["highlights"]:
        items = "".join(f"<li>{html.escape(h)}</li>" for h in product["highlights"])
        parts.append(f"<h3>Highlights</h3><ul>{items}</ul>")
    if product["specifications"]:
        rows = "".join(
            f"<tr><th style=\"text-align:left\">{html.escape(k)}</th><td>{html.escape(v)}</td></tr>"
            for k, v in product["specifications"].items()
        )
        parts.append(f"<h3>Specifications</h3><table>{rows}</table>")
    return "\n".join(parts)


def tags_for(product: dict) -> List[str]:
    tags = [f"source:{product['source']}"]
    if product["brand"]:
        tags.append(product["brand"])
    tags.extend(product["category_path"])
    seen, result = set(), []
    for tag in tags:
        if tag and tag.lower() not in seen:
            seen.add(tag.lower())
            result.append(tag[:255])
    return result


def seo_description(product: dict) -> str:
    text = product["description"] or "; ".join(product["highlights"]) or product["title"]
    return text[:320]


def product_status(override: Optional[str] = None) -> str:
    status = (override or os.getenv("SHOPIFY_PRODUCT_STATUS") or "DRAFT").upper()
    return status if status in ("ACTIVE", "DRAFT", "ARCHIVED") else "DRAFT"


def to_csv_rows(product: dict, status: Optional[str] = None) -> List[dict]:
    status = product_status(status)
    first = {
        "Handle": handle_for(product),
        "Title": product["title"],
        "Body (HTML)": body_html(product),
        "Vendor": product["brand"],
        "Type": product["product_type"],
        "Tags": ", ".join(tags_for(product)),
        "Published": "TRUE" if status == "ACTIVE" else "FALSE",
        "Option1 Name": "Title",
        "Option1 Value": "Default Title",
        "Variant SKU": sku_for(product),
        "Variant Grams": int(product["weight_grams"]) if product["weight_grams"] else "",
        "Variant Inventory Tracker": "shopify",
        "Variant Inventory Qty": inventory_quantity(),
        "Variant Inventory Policy": "deny",  # stop selling at 0 stock
        "Variant Fulfillment Service": "manual",
        "Variant Price": money(apply_markup(product["price"])),
        "Variant Compare At Price": money(apply_markup(product["compare_at_price"])),
        "Variant Requires Shipping": "TRUE",
        "Variant Taxable": "TRUE",
        "Gift Card": "FALSE",
        "SEO Title": product["title"][:70],
        "SEO Description": seo_description(product),
        "Variant Weight Unit": "g",
        "Status": status.lower(),
    }
    rows = []
    for position, image in enumerate(product["images"] or [None], start=1):
        row = first if position == 1 else {"Handle": first["Handle"]}
        if image:
            row.update({"Image Src": image, "Image Position": position, "Image Alt Text": product["title"][:512]})
        rows.append(row)
    return rows


def to_csv(products: List[dict], status: Optional[str] = None) -> str:
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=CSV_COLUMNS, extrasaction="ignore")
    writer.writeheader()
    used = set()
    for product in products:
        rows = to_csv_rows(product, status)
        # Shopify merges CSV rows sharing a handle into one product, so handles must be unique per file.
        if rows[0]["Handle"] in used:
            for row in rows:
                row["Handle"] = unique_handle_for(product)
        used.add(rows[0]["Handle"])
        writer.writerows(rows)
    return buffer.getvalue()


def to_product_set_input(product: dict, status: Optional[str] = None) -> dict:
    inventory_item = {"tracked": True, "requiresShipping": True}
    if product["weight_grams"]:
        inventory_item["measurement"] = {"weight": {"value": product["weight_grams"], "unit": "GRAMS"}}
    variant = {
        "optionValues": [{"optionName": "Title", "name": "Default Title"}],
        "price": money(apply_markup(product["price"])),
        "sku": sku_for(product),
        "inventoryPolicy": "DENY",
        "inventoryItem": inventory_item,
    }
    if product["compare_at_price"]:
        variant["compareAtPrice"] = money(apply_markup(product["compare_at_price"]))
    return {
        "title": product["title"][:255],
        "handle": handle_for(product),
        "descriptionHtml": body_html(product),
        "vendor": product["brand"][:255],
        "productType": product["product_type"][:255],
        "tags": tags_for(product),
        "status": product_status(status),
        "productOptions": [{"name": "Title", "values": [{"name": "Default Title"}]}],
        "variants": [variant],
        "files": [{"originalSource": url, "contentType": "IMAGE", "alt": product["title"][:512]}
                  for url in product["images"][:20]],
        "seo": {"title": product["title"][:70], "description": seo_description(product)},
        "metafields": [
            {"namespace": "importer", "key": "source_url", "type": "url", "value": product["source_url"]},
            {"namespace": "importer", "key": "source_id", "type": "single_line_text_field",
             "value": product["source_id"]},
        ],
    }


# ---------------------------------------------------------------- Admin API

class ShopifyClient:
    """Admin GraphQL client. Auth: SHOPIFY_ACCESS_TOKEN, or a Dev Dashboard app's
    SHOPIFY_CLIENT_ID + SHOPIFY_CLIENT_SECRET (client credentials grant, 24h tokens)."""

    def __init__(self):
        store = (os.getenv("SHOPIFY_STORE") or "").strip().replace("https://", "").strip("/")
        if not store:
            raise ShopifyError("SHOPIFY_STORE is not set (e.g. your-store.myshopify.com).")
        self.store = store if "." in store else f"{store}.myshopify.com"
        self.static_token = os.getenv("SHOPIFY_ACCESS_TOKEN", "").strip()
        self.client_id = os.getenv("SHOPIFY_CLIENT_ID", "").strip()
        self.client_secret = os.getenv("SHOPIFY_CLIENT_SECRET", "").strip()
        if not self.static_token and not (self.client_id and self.client_secret):
            raise ShopifyError("Set SHOPIFY_ACCESS_TOKEN, or SHOPIFY_CLIENT_ID and SHOPIFY_CLIENT_SECRET.")
        self._token, self._token_expiry = "", 0.0
        self._location_id = os.getenv("SHOPIFY_LOCATION_ID", "").strip()
        if self._location_id.isdigit():
            self._location_id = f"gid://shopify/Location/{self._location_id}"

    def token(self) -> str:
        if self.static_token:
            return self.static_token
        if self._token and time.time() < self._token_expiry - 300:
            return self._token
        response = requests.post(
            f"https://{self.store}/admin/oauth/access_token", timeout=30,
            data={"grant_type": "client_credentials", "client_id": self.client_id,
                  "client_secret": self.client_secret},
        )
        if response.status_code != 200:
            raise ShopifyError(f"Shopify token request failed ({response.status_code}): {response.text[:300]}")
        data = response.json()
        self._token = data["access_token"]
        self._token_expiry = time.time() + float(data.get("expires_in", 86399))
        return self._token

    def graphql(self, query: str, variables: Optional[dict] = None) -> dict:
        for attempt in range(4):
            response = requests.post(
                f"https://{self.store}/admin/api/{API_VERSION}/graphql.json", timeout=120,
                json={"query": query, "variables": variables or {}},
                headers={"X-Shopify-Access-Token": self.token(), "Content-Type": "application/json"},
            )
            if response.status_code == 429 or response.status_code >= 500:
                time.sleep(2 ** attempt)
                continue
            if response.status_code != 200:
                raise ShopifyError(f"Shopify API HTTP {response.status_code}: {response.text[:300]}")
            payload = response.json()
            errors = payload.get("errors")
            if errors and any((e.get("extensions") or {}).get("code") == "THROTTLED" for e in errors):
                time.sleep(2 ** attempt)
                continue
            if errors:
                raise ShopifyError("; ".join(e.get("message", str(e)) for e in errors))
            return payload["data"]
        raise ShopifyError("Shopify API kept throttling; try again shortly.")

    def location_id(self) -> str:
        """SHOPIFY_LOCATION_ID, else the store's first active location that fulfils online orders."""
        if not self._location_id:
            locations = self.graphql(LOCATIONS)["locations"]["nodes"]
            preferred = [l for l in locations if l.get("fulfillsOnlineOrders")] or locations
            if not preferred:
                raise ShopifyError("No active Shopify location found to put inventory in.")
            self._location_id = preferred[0]["id"]
        return self._location_id

    def admin_url(self, product_gid: str) -> str:
        return f"https://{self.store}/admin/products/{product_gid.rsplit('/', 1)[-1]}"

    def upsert_product(self, product: dict, status: Optional[str] = None) -> dict:
        """Create the product, or update it if this source product was imported before (matched by SKU)."""
        product_input = to_product_set_input(product, status)
        found = self.graphql(EXISTING_LOOKUP, {"sku": f'sku:"{sku_for(product)}"', "handle": product_input["handle"]})
        matches = found["productVariants"]["nodes"]
        existing = matches[0]["product"] if matches else None
        variables = {"input": product_input}
        if existing:
            variables["identifier"] = {"id": existing["id"]}
            del product_input["handle"]  # keep the live product's URL stable
        else:
            if found.get("productByIdentifier"):
                product_input["handle"] = unique_handle_for(product)
            # Starting stock only on create; updates must not overwrite real stock levels after sales.
            product_input["variants"][0]["inventoryQuantities"] = [
                {"locationId": self.location_id(), "name": "available", "quantity": inventory_quantity()}]
        result = self.graphql(PRODUCT_SET, variables)["productSet"]
        if result["userErrors"]:
            raise ShopifyError("; ".join(f"{'.'.join(e.get('field') or [])} {e['message']}".strip()
                                         for e in result["userErrors"]))
        created = result["product"]
        created["admin_url"] = self.admin_url(created["id"])
        created["updated_existing"] = bool(existing)
        return created
