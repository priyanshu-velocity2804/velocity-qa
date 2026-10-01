"""Find Amazon / Flipkart product links in free text and identify the product."""

from __future__ import annotations

from dataclasses import dataclass
import html
import re
from typing import List, Optional
from urllib.parse import parse_qs, urlsplit

import requests

# Short-link hosts we are willing to follow. Anything else is never fetched,
# so a Slack message can't make the bot request arbitrary URLs.
SHORT_LINK_HOSTS = {"amzn.in", "amzn.to", "amzn.eu", "a.co", "dl.flipkart.com", "fkrt.it", "fkrt.cc"}
URL_PATTERN = re.compile(r"https?://[^\s<>|\"']+")
ASIN_PATTERN = re.compile(r"/(?:dp|gp/product|gp/aw/d|product)/([A-Z0-9]{10})(?:[/?#]|$)", re.I)
FSN_PATTERN = re.compile(r"^[A-Z0-9]{16}$")


class LinkError(Exception):
    pass


@dataclass
class ProductLink:
    source: str          # "amazon" | "flipkart"
    product_id: str      # ASIN or FSN
    url: str             # the link as given (after short-link resolution)
    amazon_domain: str = ""  # e.g. "amazon.in"


def find_urls(text: str) -> List[str]:
    # Slack formats links as <https://url|label> or <https://url>.
    urls = []
    for url in URL_PATTERN.findall(text or ""):
        url = html.unescape(url).rstrip(".,);>")  # Slack sends & as &amp;
        if url not in urls:
            urls.append(url)
    return urls


def _host(url: str) -> str:
    host = (urlsplit(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def resolve_short_link(url: str, timeout: float = 15) -> str:
    if _host(url) not in SHORT_LINK_HOSTS:
        return url
    try:
        response = requests.get(
            url, allow_redirects=True, timeout=timeout, stream=True,
            headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"},
        )
        response.close()
        return response.url
    except requests.RequestException as exc:
        raise LinkError(f"Could not open short link {url}: {exc}") from exc


def identify(url: str) -> Optional[ProductLink]:
    """Return the product a URL points to, or None if it isn't an Amazon/Flipkart product URL."""
    url = resolve_short_link(url)
    host = _host(url)
    if re.fullmatch(r"(?:[a-z]+\.)?amazon\.[a-z.]+", host):
        match = ASIN_PATTERN.search(urlsplit(url).path)
        if not match:
            raise LinkError(f"Couldn't find an ASIN in {url}")
        domain = host.split("amazon.", 1)[1]
        return ProductLink("amazon", match.group(1).upper(), url, "amazon." + domain)
    if host.endswith("flipkart.com"):
        pid = (parse_qs(urlsplit(url).query).get("pid") or [""])[0].upper()
        if not FSN_PATTERN.match(pid):
            raise LinkError(f"Couldn't find a Flipkart product id (pid=...) in {url}")
        return ProductLink("flipkart", pid, url)
    return None


def parse_message(text: str):
    """Return (links, errors) for every Amazon/Flipkart URL in the message, de-duplicated."""
    links, errors, seen = [], [], set()
    for url in find_urls(text):
        try:
            link = identify(url)
        except LinkError as exc:
            errors.append(str(exc))
            continue
        if link and (link.source, link.product_id) not in seen:
            seen.add((link.source, link.product_id))
            links.append(link)
    return links, errors
