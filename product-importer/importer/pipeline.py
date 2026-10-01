"""Links in -> scraped products -> Shopify. Shared by the CLI and the Slack bot."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import List, Optional

from . import scrapers, shopify
from .links import ProductLink, parse_message

MAX_LINKS_PER_REQUEST = 25


@dataclass
class ImportResult:
    link: ProductLink
    product: Optional[dict] = None
    shopify: Optional[dict] = None
    error: str = ""


@dataclass
class ImportRun:
    results: List[ImportResult] = field(default_factory=list)
    link_errors: List[str] = field(default_factory=list)

    @property
    def products(self) -> List[dict]:
        return [r.product for r in self.results if r.product]


def run_import(text: str, *, upload: bool = True, status: Optional[str] = None) -> ImportRun:
    links, link_errors = parse_message(text)
    run = ImportRun(link_errors=link_errors)
    if len(links) > MAX_LINKS_PER_REQUEST:
        run.link_errors.append(f"Only the first {MAX_LINKS_PER_REQUEST} of {len(links)} links were processed.")
        links = links[:MAX_LINKS_PER_REQUEST]
    client = shopify.ShopifyClient() if upload and links else None

    def process(link: ProductLink) -> ImportResult:
        result = ImportResult(link)
        try:
            result.product = scrapers.fetch(link)
            if client:
                result.shopify = client.upsert_product(result.product, status)
        except (scrapers.ScrapeError, shopify.ShopifyError) as exc:
            result.error = str(exc)
        except Exception as exc:  # one bad link shouldn't sink the rest
            result.error = f"Unexpected error: {exc!r}"
        return result

    # Small pool: Flipkart rate-limits aggressively and Shopify throttles mutations.
    with ThreadPoolExecutor(max_workers=3) as pool:
        run.results = list(pool.map(process, links))
    return run
