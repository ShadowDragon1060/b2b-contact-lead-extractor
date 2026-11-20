"""
Automated B2B Contact & Lead Intelligence Extractor
Author: Waseequr Rahman
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import re
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import List, Set
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("b2b_lead_extractor")

EMAIL_REGEX = re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}")
PHONE_REGEX = re.compile(
    r"(?:\+?\d{1,3}[-.\s]?)?\(?\d{2,5}\)?[-.\s]?\d{2,5}[-.\s]?\d{2,5}(?:[-.\s]?\d{1,5})?"
)
CONTACT_HINTS = ("contact", "kontakt", "impressum", "imprint", "about")
IGNORED_EMAIL_SUFFIXES = (".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".css", ".js")


@dataclass
class CompanyContactRecord:
    domain: str
    pages_scanned: int
    emails: List[str]
    phone_numbers: List[str]
    status: str = "ok"


def build_session() -> requests.Session:
    session = requests.Session()
    retries = Retry(total=3, backoff_factor=1.0, status_forcelist=[429, 500, 502, 503, 504])
    adapter = HTTPAdapter(max_retries=retries)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    session.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            )
        }
    )
    return session


def extract_emails(text: str, soup: BeautifulSoup) -> Set[str]:
    found = set(EMAIL_REGEX.findall(text))
    for a_tag in soup.select("a[href^='mailto:']"):
        href = a_tag.get("href", "")
        email = href.replace("mailto:", "").split("?")[0].strip()
        if EMAIL_REGEX.fullmatch(email):
            found.add(email)
    return {
        e.lower()
        for e in found
        if not e.lower().endswith(IGNORED_EMAIL_SUFFIXES)
    }


def extract_phones(text: str) -> Set[str]:
    phones: Set[str] = set()
    for match in PHONE_REGEX.finditer(text):
        raw = match.group(0).strip()
        digits = re.sub(r"\D", "", raw)
        if 8 <= len(digits) <= 15:
            phones.add(raw)
    return phones


def discover_contact_links(base_url: str, soup: BeautifulSoup, max_links: int = 2) -> List[str]:
    base_host = urlparse(base_url).netloc
    candidates: List[str] = []
    for a_tag in soup.find_all("a", href=True):
        href = a_tag["href"].strip()
        full = urljoin(base_url, href)
        parsed = urlparse(full)
        if parsed.netloc == base_host and any(h in parsed.path.lower() for h in CONTACT_HINTS):
            if full not in candidates and full != base_url:
                candidates.append(full)
        if len(candidates) >= max_links:
            break
    return candidates


def scrape_domain(session: requests.Session, url: str, delay: float = 1.5) -> CompanyContactRecord:
    emails: Set[str] = set()
    phones: Set[str] = set()
    scanned = 0

    try:
        resp = session.get(url, timeout=15)
        resp.raise_for_status()
        scanned += 1
        soup = BeautifulSoup(resp.text, "html.parser")
        for tag in soup(["script", "style", "noscript"]):
            tag.decompose()
        visible_text = soup.get_text(separator=" ", strip=True)
        emails |= extract_emails(visible_text, soup)
        phones |= extract_phones(visible_text)

        for sub_url in discover_contact_links(url, soup):
            time.sleep(delay)
            try:
                sub_resp = session.get(sub_url, timeout=12)
                sub_resp.raise_for_status()
                scanned += 1
                sub_soup = BeautifulSoup(sub_resp.text, "html.parser")
                for tag in sub_soup(["script", "style", "noscript"]):
                    tag.decompose()
                sub_text = sub_soup.get_text(separator=" ", strip=True)
                emails |= extract_emails(sub_text, sub_soup)
                phones |= extract_phones(sub_text)
            except requests.RequestException:
                continue

        return CompanyContactRecord(
            domain=url,
            pages_scanned=scanned,
            emails=sorted(emails),
            phone_numbers=sorted(phones)[:10],
            status="ok",
        )
    except requests.RequestException as exc:
        logger.warning("Failed to crawl %s: %s", url, exc)
        return CompanyContactRecord(
            domain=url, pages_scanned=scanned, emails=[], phone_numbers=[], status=str(exc)
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="Automated B2B Contact & Lead Extractor")
    parser.add_argument("--input", help="Path to text file with one URL per line", default=None)
    parser.add_argument("--csv-out", help="Output CSV path", default="b2b_contacts.csv")
    parser.add_argument("--json-out", help="Output JSON path", default="b2b_contacts.json")
    args = parser.parse_args()

    default_urls = [
        "https://www.sellon24.de/",
        "https://www.floordirekt.de/",
        "https://bmsc24.de/",
        "https://www.windhager.eu/",
        "https://schellenberg-shop.de/",
    ]
    urls = (
        [u.strip() for u in Path(args.input).read_text(encoding="utf-8").splitlines() if u.strip()]
        if args.input and Path(args.input).exists()
        else default_urls
    )

    session = build_session()
    results: List[CompanyContactRecord] = []
    for u in urls:
        logger.info("Scanning %s ...", u)
        results.append(scrape_domain(session, u))

    with open(args.csv_out, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f, fieldnames=["domain", "pages_scanned", "emails", "phone_numbers", "status"]
        )
        writer.writeheader()
        for r in results:
            writer.writerow(
                {
                    "domain": r.domain,
                    "pages_scanned": r.pages_scanned,
                    "emails": "; ".join(r.emails),
                    "phone_numbers": "; ".join(r.phone_numbers),
                    "status": r.status,
                }
            )

    Path(args.json_out).write_text(
        json.dumps([asdict(r) for r in results], indent=2), encoding="utf-8"
    )
    logger.info("Saved %d records to %s and %s.", len(results), args.csv_out, args.json_out)


if __name__ == "__main__":
    main()
