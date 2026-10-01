#!/usr/bin/env python3
"""Fetch a Flipkart FSN and extract its embedded JSON. Python 3.10+ and curl."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit


BASE_URL = "https://www.flipkart.com"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/153.0.0.0 Safari/537.36"
)
HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-GB,en-US;q=0.9,en;q=0.8",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
    "User-Agent": USER_AGENT,
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
}
STATE_ASSIGNMENT = re.compile(
    r'(?:window\s*\.\s*)?__INITIAL_STATE__\s*=\s*'
)
PRESENTATION_METADATA = {"tracking", "action", "properties", "stateRule"}


class FlipkartError(Exception):
    """An actionable fetch, extraction, or product matching error."""


def validate_fsn(fsn: str) -> str:
    fsn = fsn.strip().upper()
    if not re.fullmatch(r"[A-Z0-9]{16}", fsn):
        raise FlipkartError("FSN must contain exactly 16 letters/digits, e.g. ACCHE5DZFMEP4PYG.")
    return fsn


def walk_dicts(value, skip=()):
    if isinstance(value, dict):
        yield value
        for key, child in value.items():
            if key not in skip:
                yield from walk_dicts(child, skip)
    elif isinstance(value, list):
        for child in value:
            yield from walk_dicts(child, skip)


def at(value, *keys, default=None):
    for key in keys:
        if not isinstance(value, dict) or key not in value:
            return default
        value = value[key]
    return value


def unique(items):
    result, seen = [], set()
    for item in items:
        marker = json.dumps(item, sort_keys=True, ensure_ascii=False)
        if marker not in seen:
            seen.add(marker)
            result.append(item)
    return result


def flipkart_url(url: str) -> str:
    absolute = urljoin(BASE_URL, url)
    parsed = urlsplit(absolute)
    if (parsed.scheme != "https" or parsed.hostname != "www.flipkart.com"
            or parsed.username or parsed.password or parsed.port not in (None, 443)):
        raise FlipkartError("Refusing a URL outside https://www.flipkart.com.")
    return absolute


def url_matches_fsn(url: str, fsn: str) -> bool:
    try:
        parsed = urlsplit(flipkart_url(url))
    except (FlipkartError, ValueError):
        return False
    return "/p/" in parsed.path and parse_qs(parsed.query).get("pid") == [fsn]


def normalize_cookie(cookie: str) -> str:
    # A raw Cookie header, or just the contents of curl's -b '...' argument.
    cookie = cookie.strip()
    if cookie.lower().startswith("cookie:"):
        cookie = cookie[7:].strip()
    # Undo the Markdown escapes present in copied chat messages.
    cookie = cookie.replace(r"\_", "_").replace(r"\.", ".")
    if any(ord(c) < 32 or ord(c) == 127 for c in cookie):
        raise FlipkartError("The cookie must be a single-line Cookie header.")
    if cookie and "=" not in cookie:
        raise FlipkartError("Expected cookies in name=value; name2=value2 format.")
    return cookie


def curl_quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def fetch_html(url: str, *, cookie: str = "", timeout: float = 35) -> tuple[str, str]:
    """Use the same HTTP client as the supplied curl request; never execute a shell."""
    if not shutil.which("curl"):
        raise FlipkartError("curl is required. Install it and ensure it is on PATH.")
    url = flipkart_url(url)
    headers = dict(HEADERS)
    if cookie:
        headers["Cookie"] = normalize_cookie(cookie)
    # Pass credentials on stdin, rather than expose them in process arguments.
    config = "\n".join("header = " + curl_quote(f"{k}: {v}") for k, v in headers.items())
    with tempfile.TemporaryDirectory(prefix="flipkart-fsn-") as folder:
        body_path = Path(folder) / "body.html"
        headers_path = Path(folder) / "headers.txt"
        for _ in range(6):
            command = [
                "curl", "-q", "--config", "-", "--silent", "--show-error", "--compressed",
                "--proto", "=https", "--connect-timeout", str(min(15, timeout)),
                "--max-time", str(timeout), "--output", str(body_path),
                "--dump-header", str(headers_path), "--write-out", "%{http_code}",
                "--url", url,
            ]
            try:
                response = subprocess.run(
                    command, input=config, text=True, capture_output=True, timeout=timeout + 5,
                    encoding="utf-8", errors="replace", check=False,
                )
            except subprocess.TimeoutExpired as exc:
                raise FlipkartError(f"Request timed out after {timeout:g} seconds.") from exc
            if response.returncode:
                raise FlipkartError(f"curl failed (code {response.returncode}): {response.stderr.strip()}")
            try:
                status = int(response.stdout)
            except ValueError as exc:
                raise FlipkartError("curl did not return an HTTP status.") from exc
            if status in (301, 302, 303, 307, 308):
                locations = re.findall(
                    r"(?im)^location:\s*([^\r\n]+)", headers_path.read_text(errors="replace")
                )
                if not locations:
                    raise FlipkartError(f"HTTP {status} without a redirect location.")
                # Validate every redirect before sending session cookies again.
                url = flipkart_url(urljoin(url, locations[-1].strip()))
                continue
            if status in (401, 403, 429):
                raise FlipkartError(
                    f"Flipkart returned HTTP {status}. Try a fresh --cookie-file or parse "
                    "a page saved by your working curl request with --html."
                )
            if status != 200:
                raise FlipkartError(f"Flipkart returned HTTP {status} for {url}.")
            return body_path.read_text(encoding="utf-8", errors="replace"), url
    raise FlipkartError("Too many redirects.")


class PageParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.scripts = []
        self.links = []
        self.canonical = None
        self.current = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "script":
            self.current = (attrs, [])
        elif tag == "a" and attrs.get("href"):
            self.links.append(attrs["href"])
        elif tag == "link" and "canonical" in (attrs.get("rel") or "").split():
            self.canonical = attrs.get("href")

    def handle_data(self, data):
        if self.current is not None:
            self.current[1].append(data)

    def handle_endtag(self, tag):
        if tag == "script" and self.current is not None:
            attrs, chunks = self.current
            self.scripts.append((attrs, "".join(chunks)))
            self.current = None


def extract_page(html: str, url: str = "") -> dict:
    parser = PageParser()
    parser.feed(html)
    parser.close()
    states, json_ld, json_scripts, warnings = [], [], [], []
    decoder = json.JSONDecoder()
    for attrs, source in parser.scripts:
        for match in STATE_ASSIGNMENT.finditer(source):
            text = source[match.end():].lstrip()
            try:
                if text.startswith("JSON.parse("):
                    encoded, _ = decoder.raw_decode(text[len("JSON.parse("):].lstrip())
                    state = json.loads(encoded)
                else:
                    # raw_decode handles nested braces, escaped quotes and trailing JS.
                    state, _ = decoder.raw_decode(text)
                if isinstance(state, dict) and state:
                    states.append(state)
            except (ValueError, TypeError):
                warnings.append("An __INITIAL_STATE__ assignment was not valid JSON.")
        script_type = (attrs.get("type") or "").split(";", 1)[0].strip().lower()
        if script_type in ("application/ld+json", "application/json"):
            try:
                data = json.loads(source)
                if script_type == "application/ld+json":
                    json_ld.append(data)
                else:
                    json_scripts.append({"id": attrs.get("id"), "data": data})
            except ValueError:
                warnings.append(f"Could not parse JSON script {attrs.get('id') or script_type}.")
    if not states and not json_ld and not json_scripts:
        raise FlipkartError(
            "No embedded JSON found. The response may be a challenge/login page. "
            "Use a fresh --cookie-file or --html with a saved working response."
        )
    return {
        "url": url, "canonical_url": parser.canonical,
        "initial_states": states, "json_ld": json_ld, "json_scripts": json_scripts,
        "product_links": unique(link for link in parser.links if "/p/" in link),
        "warnings": warnings,
    }


def search_products(page: dict, fsn: str) -> list[dict]:
    # Match the product's own identifier, never the search query in its URL.
    return unique(
        node for node in walk_dicts(page["initial_states"])
        if node.get("id") == fsn and any(k in node for k in ("titles", "pricing", "media"))
    )


def schema_products(page: dict) -> list[dict]:
    return [
        node for node in walk_dicts(page["json_ld"])
        if "Product" in ([node.get("@type")] if isinstance(node.get("@type"), str)
                         else node.get("@type", []) or [])
    ]


def schema_matches(product: dict, fsn: str) -> bool:
    ids = [product[k] for k in ("sku", "productID", "mpn") if product.get(k)]
    return fsn in ids or url_matches_fsn(product.get("url", ""), fsn)


def is_product_page(page: dict, fsn: str) -> bool:
    products = schema_products(page)
    if any(schema_matches(p, fsn) for p in products):
        return True
    if any(p.get("sku") or p.get("productID") for p in products):
        return False
    for state in page["initial_states"]:
        product_id = at(state, "multiWidgetState", "pageDataResponse", "pageContext", "productId")
        legacy_id = at(state, "pageDataV4", "productPageMetadata", "productId")
        if fsn in (product_id, legacy_id):
            return True
    return False


def page_widgets(page: dict | None) -> list[dict]:
    if page is None:
        return []
    widgets = []
    for state in page["initial_states"]:
        slots = at(state, "multiWidgetState", "widgetsData", "slots", default=[])
        widgets.extend(w for slot in slots if (w := at(slot, "slotData", "widget")))
    return widgets


def widget_type(widget: dict) -> str:
    return str(at(widget, "tracking", "widgetType") or widget.get("widgetType") or widget.get("type", "")).lower()


def components(data, prefix: str):
    for node in walk_dicts(data, PRESENTATION_METADATA):
        for key, value in node.items():
            if key.startswith(prefix) and isinstance(value, dict):
                yield value.get("value", {})


def label(node, index: int):
    return at(node, f"label_{index}", "value", "text")


def label_pairs(data) -> dict:
    pairs = {}
    for node in walk_dicts(data, PRESENTATION_METADATA):
        for index in (0, 2, 4, 6, 8):
            key, value = label(node, index), label(node, index + 1)
            if isinstance(key, str) and value is not None:
                pairs[key] = value
    return pairs


def display_texts(data) -> list[str]:
    texts = []
    for node in walk_dicts(data, PRESENTATION_METADATA):
        value = node.get("text")
        if isinstance(value, str) and value.strip():
            texts.append(value.strip())
        elif isinstance(value, list):
            texts.extend(t.strip() for t in value if isinstance(t, str) and t.strip())
    return unique(texts)


def image_url(url: str) -> str:
    return (url.replace("{@width}", "1500").replace("{@height}", "1500")
            .replace("{@quality}", "90"))


def normalize_product(fsn: str, search: list[dict], page: dict | None) -> dict:
    card = search[0] if search else {}
    schema = next((p for p in schema_products(page) if schema_matches(p, fsn)), {}) if page else {}
    offers = schema.get("offers") or {}
    if isinstance(offers, list):
        offers = offers[0] if offers else {}
    brand = schema.get("brand")
    if isinstance(brand, dict):
        brand = brand.get("name")
    pricing = card.get("pricing") or {}
    prices = pricing.get("prices") or []
    rating = schema.get("aggregateRating") or {}
    images = schema.get("image") or []
    if isinstance(images, str):
        images = [images]
    if not images:
        images = [i["url"] for i in at(card, "media", "images", default=[]) if i.get("url")]
    product = {
        "fsn": fsn,
        "url": page.get("canonical_url") or page.get("url") if page else card.get("baseUrl"),
        "title": schema.get("name") or at(card, "titles", "title"),
        "subtitle": at(card, "titles", "subtitle"),
        "brand": brand or at(card, "titles", "superTitle"),
        "category": schema.get("category") or card.get("vertical"),
        "color": schema.get("color"),
        "price": offers.get("price", next((p.get("value") for p in prices if p.get("strikeOff") is False), None)),
        "mrp": next((p.get("value") for p in prices if p.get("strikeOff") is True), None),
        "discount_percent": pricing.get("totalDiscount"),
        "currency": offers.get("priceCurrency"),
        "availability": offers.get("availability") or at(card, "availability", "displayState"),
        "rating": rating.get("ratingValue", at(card, "rating", "average")),
        "rating_count": rating.get("ratingCount", at(card, "rating", "count")),
        "review_count": rating.get("reviewCount", at(card, "rating", "reviewCount")),
        "images": unique(image_url(i) for i in images if isinstance(i, str)),
        "description": schema.get("description"),
        "specifications": {}, "highlights": [], "warranty": {}, "manufacturer_details": {},
        "seller": {}, "offers": [], "variants": [],
        "reviews": schema.get("review", []),
        "shipping": offers.get("shippingDetails"),
        "return_policy": offers.get("hasMerchantReturnPolicy"),
    }
    if product["url"]:
        product["url"] = urljoin(BASE_URL, product["url"])
    for widget in page_widgets(page):
        kind, data = widget_type(widget), widget.get("data", {})
        if kind == "atlas_product_title":
            for item in components(data, "customEllipsisData_"):
                if item.get("prependingText"):
                    product["title"] = item["prependingText"]
        if "rich_product_details" in kind or "specifications" in kind:
            for component in components(data, "rpd_specifications_"):
                for group in walk_dicts(component, PRESENTATION_METADATA):
                    grids = [v.get("value", {}) for k, v in group.items()
                             if k.startswith("rpd_grid_") and isinstance(v, dict)]
                    if grids and isinstance(label(group, 0), str):
                        product["specifications"].setdefault(label(group, 0), {}).update(label_pairs(grids))
            for component in components(data, "rpd_warranty_"):
                product["warranty"].update(label_pairs(component))
            for component in components(data, "rpd_manufacture_"):
                product["manufacturer_details"].update(label_pairs(component))
        if "product_highlights" in kind:
            for component in components(data, "product-highlight-list_"):
                product["highlights"].extend(display_texts(component))
        if "delivery" in kind:
            for component in components(data, "default_fk_pp_delivery_widget_seller_"):
                name = label(component, 0)
                if isinstance(name, str):
                    product["seller"] = {"name": re.sub(r"^Seller:\s*", "", name), "rating": label(component, 1)}
        if kind == "atlas_nep_v2":
            product["offers"].extend(data.get("offers") or [])
            try:
                context = json.loads(data.get("nepContext") or "{}")
                product["mrp"] = context.get("mrp", product["mrp"])
            except (ValueError, TypeError, AttributeError):
                pass
        if "product_pricing" in kind:
            for component in components(data, "price_description_"):
                mrp = label(component, 1)
                if isinstance(mrp, str) and re.fullmatch(r"[\d,]+(?:\.\d+)?", mrp):
                    product["mrp"] = float(mrp.replace(",", ""))
                # Prefer the displayed discount over computing a different rounding.
                for text in display_texts(component.get("label_0", {})):
                    if re.fullmatch(r"\d+(?:\.\d+)?%", text):
                        product["discount_percent"] = float(text[:-1])
        if "swatch" in kind:
            for node in walk_dicts(data):
                tracking = node.get("tracking", {})
                if tracking.get("contentType") == "InStock:Variant" and tracking.get("productId"):
                    product["variants"].append({"fsn": tracking["productId"], "name": tracking.get("contentTitle")})
    product["highlights"] = unique(product["highlights"])
    product["variants"] = unique(product["variants"])
    return product


def assemble_result(fsn: str, pages: dict, warnings: list[str], *, search_only=False) -> dict:
    cards = unique(card for page in pages.values() for card in search_products(page, fsn))
    product_page = next((p for p in pages.values() if is_product_page(p, fsn)), None)
    if not cards and product_page is None:
        detail = " ".join(warnings)
        raise FlipkartError(
            f"No exact product match for {fsn}. Flipkart may return unrelated search results. "
            "Try a fresh --cookie-file or --html with your working saved response. " + detail
        )
    for page in pages.values():
        warnings.extend(page.get("warnings", []))
    if product_page is None and not search_only:
        warnings.append("Only search-card data is available; product-page details could not be retrieved.")
    return {
        "fsn": fsn,
        "extracted_at": datetime.now(timezone.utc).isoformat(),
        "status": "product_page" if product_page else "search_only" if search_only else "partial",
        "source_urls": [p["url"] for p in pages.values() if p.get("url")],
        "product": normalize_product(fsn, cards, product_page),
        "matched_search_products": cards,
        "warnings": unique(warnings),
        "raw": pages,
    }


def get_product(fsn: str, *, cookie: str = "", timeout: float = 35, search_only=False) -> dict:
    """Return normalized details plus all embedded page payloads in result['raw']."""
    fsn, cookie = validate_fsn(fsn), normalize_cookie(cookie)
    if timeout <= 0:
        raise FlipkartError("Timeout must be greater than zero.")
    pages, warnings = {}, []
    search_url = BASE_URL + "/search?" + urlencode({"q": fsn})
    try:
        html, url = fetch_html(search_url, cookie=cookie, timeout=timeout)
        pages["search"] = extract_page(html, url)
    except FlipkartError as exc:
        warnings.append(f"Search: {exc}")
    if not search_only:
        search_page = pages.get("search", {})
        links = [p.get("baseUrl", "") for p in search_products(search_page, fsn)] if search_page else []
        links += search_page.get("product_links", [])
        product_url = next((flipkart_url(u) for u in links if url_matches_fsn(u, fsn)), None)
        # This route resolves an FSN even when the search page only contains recommendations.
        product_url = product_url or BASE_URL + "/product/p/itme?" + urlencode({"pid": fsn})
        try:
            html, url = fetch_html(product_url, cookie=cookie, timeout=timeout)
            pages["product"] = extract_page(html, url)
            if not is_product_page(pages["product"], fsn):
                warnings.append("The product-page response could not be verified against the requested FSN.")
        except FlipkartError as exc:
            warnings.append(f"Product page: {exc}")
    return assemble_result(fsn, pages, warnings, search_only=search_only)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("fsn", help="Flipkart product FSN, e.g. ACCHE5DZFMEP4PYG")
    parser.add_argument("--cookie-file", type=Path, help="Text file containing the raw Cookie header")
    parser.add_argument("--html", type=Path, help="Parse saved search/product HTML without making requests")
    parser.add_argument("--output-dir", type=Path, default=Path("output"), help="Output folder (default: output)")
    parser.add_argument("--timeout", type=float, default=35, help="Timeout per HTTP request in seconds")
    parser.add_argument("--search-only", action="store_true", help="Only fetch the search page")
    args = parser.parse_args(argv)
    try:
        fsn = validate_fsn(args.fsn)
        if args.html:
            page = extract_page(args.html.read_text(encoding="utf-8"))
            result = assemble_result(fsn, {"saved_html": page}, [], search_only=args.search_only)
            result["source_file"] = str(args.html.resolve())
        else:
            cookie = args.cookie_file.read_text(encoding="utf-8") if args.cookie_file else os.getenv("FLIPKART_COOKIE", "")
            result = get_product(fsn, cookie=cookie, timeout=args.timeout, search_only=args.search_only)
        raw = result.pop("raw")
        args.output_dir.mkdir(parents=True, exist_ok=True)
        details_path = args.output_dir / f"{fsn}.json"
        raw_path = args.output_dir / f"{fsn}.raw.json"
        result["raw_file"] = raw_path.name
        details_path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        raw_path.write_text(json.dumps(raw, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        product = result["product"]
        print(product.get("title") or fsn)
        if product.get("price") is not None:
            print(f"Price: {product.get('currency') or ''} {product['price']}")
        print(f"Details: {details_path.resolve()}")
        print(f"Full embedded JSON: {raw_path.resolve()}")
        for warning in result["warnings"]:
            print(f"Warning: {warning}", file=sys.stderr)
        return 0 if result["status"] != "partial" else 2
    except (FlipkartError, OSError, UnicodeError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
