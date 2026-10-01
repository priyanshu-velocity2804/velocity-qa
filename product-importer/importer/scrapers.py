"""Fetch a product from Amazon (Rainforest API) or Flipkart and normalise it.

Both sources return the same plain dict ("normalised product"), which
importer.shopify turns into a Shopify product / CSV rows:

    source, source_id, source_url, title, brand, product_type, category_path,
    description, highlights, specifications {name: value}, price,
    compare_at_price, currency, images [url], weight_grams, in_stock
"""

from __future__ import annotations

import os
import re
from typing import Optional
from urllib.parse import urlsplit, urlunsplit

import requests

from . import flipkart_fsn
from .links import ProductLink

RAINFOREST_URL = "https://api.rainforestapi.com/request"


class ScrapeError(Exception):
    pass


def clean(text) -> str:
    if text is None:
        return ""
    # Flipkart titles sometimes carry U+FFFD where the page had a stray byte.
    return re.sub(r"\s+", " ", str(text).replace("�", " ")).strip()


def to_float(value) -> Optional[float]:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    match = re.search(r"\d[\d,]*(?:\.\d+)?", str(value))
    return float(match.group(0).replace(",", "")) if match else None


def weight_in_grams(text) -> Optional[float]:
    match = re.search(r"(\d+(?:\.\d+)?)\s*(kg|kilo|g|gram|gm|pound|lb|ounce|oz)", str(text or ""), re.I)
    if not match:
        return None
    value, unit = float(match.group(1)), match.group(2).lower()
    factor = {"kg": 1000, "kilo": 1000, "pound": 453.592, "lb": 453.592, "ounce": 28.3495, "oz": 28.3495}
    return round(value * factor.get(unit, 1), 1)


# ---------------------------------------------------------------- Amazon

def fetch_amazon(link: ProductLink) -> dict:
    api_key = os.getenv("RAINFOREST_API_KEY")
    if not api_key:
        raise ScrapeError("RAINFOREST_API_KEY is not set.")
    params = {"api_key": api_key, "type": "product", "asin": link.product_id,
              "amazon_domain": link.amazon_domain or "amazon.in"}
    try:
        response = requests.get(RAINFOREST_URL, params=params, timeout=90)
        data = response.json()
    except (requests.RequestException, ValueError) as exc:
        raise ScrapeError(f"Rainforest API request failed: {exc}") from exc
    info = data.get("request_info") or {}
    if not info.get("success") or not data.get("product"):
        raise ScrapeError(f"Rainforest API: {info.get('message') or response.text[:300]}")
    return normalise_amazon(data["product"], link)


def normalise_amazon(p: dict, link: ProductLink) -> dict:
    buybox = p.get("buybox_winner") or {}
    price = to_float((buybox.get("price") or {}).get("value"))
    rrp = to_float((buybox.get("rrp") or {}).get("value"))
    currency = (buybox.get("price") or {}).get("currency")
    images = [i.get("link") for i in p.get("images") or [] if i.get("link")]
    main = (p.get("main_image") or {}).get("link")
    if main and not images:
        images = [main]
    specs = {clean(s.get("name")): clean(s.get("value")) for s in p.get("specifications") or []
             if s.get("name") and s.get("value")}
    categories = [clean(c.get("name")) for c in p.get("categories") or [] if c.get("name")]
    availability = (buybox.get("availability") or {}).get("type")
    return {
        "source": "amazon",
        "source_id": link.product_id,
        "source_url": p.get("link") or link.url,
        "title": clean(p.get("title")),
        "brand": clean(p.get("brand") or p.get("manufacturer")),
        "product_type": categories[-1] if categories else "",
        "category_path": categories,
        "description": clean(p.get("description")),
        "highlights": [clean(b) for b in p.get("feature_bullets") or [] if clean(b)],
        "specifications": specs,
        "price": price,
        "compare_at_price": rrp if rrp and price and rrp > price else None,
        "currency": currency,
        "images": images,
        "weight_grams": weight_in_grams(p.get("weight") or specs.get("Item Weight")),
        "in_stock": availability in (None, "in_stock"),
    }


# ---------------------------------------------------------------- Flipkart

def fetch_flipkart(link: ProductLink) -> dict:
    try:
        result = flipkart_fsn.get_product(link.product_id, cookie=os.getenv("FLIPKART_COOKIE", ""))
    except flipkart_fsn.FlipkartError as exc:
        raise ScrapeError(f"Flipkart: {exc}") from exc
    if result["status"] != "product_page":
        # Search cards alone have no description/specs; better to say so than upload half a product.
        raise ScrapeError("Flipkart only returned search data (likely blocked). "
                          + " ".join(result.get("warnings", []))[:300])
    return normalise_flipkart(result["product"], link)


def normalise_flipkart(p: dict, link: ProductLink) -> dict:
    specs = {}
    for group in (p.get("specifications") or {}).values():
        for name, value in group.items():
            value = ", ".join(map(str, value)) if isinstance(value, list) else value
            if clean(value):
                specs[clean(name)] = clean(value)
    price, mrp = to_float(p.get("price")), to_float(p.get("mrp"))
    # Flipkart's meta description is SEO boilerplate ("Buy X for Rs. ... Cash On Delivery!").
    description = clean(p.get("description"))
    if description.lower().startswith("buy "):
        description = ""
    category = clean(p.get("category"))
    generic = (p.get("manufacturer_details") or {}).get("Generic Name")
    product_type = clean(generic[0] if isinstance(generic, list) and generic else generic) or category.title()
    availability = str(p.get("availability") or "")
    return {
        "source": "flipkart",
        "source_id": link.product_id,
        # Drop Flipkart's tracking parameters; the pid alone resolves the product.
        "source_url": urlunsplit(urlsplit(link.url)._replace(query="pid=" + link.product_id, fragment="")),
        "title": clean(p.get("title")),
        "brand": clean(p.get("brand")),
        "product_type": product_type,
        "category_path": [category.title()] if category else [],
        "description": description,
        "highlights": [clean(h) for h in p.get("highlights") or [] if clean(h)],
        "specifications": specs,
        "price": price,
        "compare_at_price": mrp if mrp and price and mrp > price else None,
        "currency": p.get("currency") or "INR",
        "images": list(p.get("images") or []),
        "weight_grams": weight_in_grams(specs.get("Weight") or specs.get("Net Quantity")),
        "in_stock": not availability or "instock" in availability.lower().replace("_", ""),
    }


def fetch(link: ProductLink) -> dict:
    product = fetch_amazon(link) if link.source == "amazon" else fetch_flipkart(link)
    if not product["title"]:
        raise ScrapeError("No product title found.")
    if product["price"] is None:
        raise ScrapeError("No price found (product may be unavailable).")
    return product
