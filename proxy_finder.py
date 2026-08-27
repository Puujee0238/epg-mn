#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Find working Mongolia proxies for scraping zuragt.mn (Mongolia-only site).

Scrapes candidate ip:port:scheme entries from a few public Mongolia proxy
lists (http/https/socks4/socks5), tests each candidate against the real
target site concurrently, and writes the top N fastest working proxies
(as full proxy URLs) to a file that epg.py reads.
"""
from __future__ import annotations

import logging
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List, Tuple

import requests
from bs4 import BeautifulSoup

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
FETCH_TIMEOUT = 20
TEST_URL = "https://www.zuragt.mn/"
TEST_TIMEOUT = 15
TEST_RETRIES = 2
TEST_WORKERS = 20
TOP_N = 10
PER_SOURCE_LIMIT = 10
PROXY5_API_URL = "https://proxy5.net/api/free-proxies.php?v=993249"
PROXY5_API_LIMIT = 20
OUTPUT_FILE = "working_proxies.txt"
DEFAULT_SCHEME = "http"
VALID_SCHEMES = ("socks5", "socks4", "https", "http")  # preference order when multiple apply

IP_PORT_RE = re.compile(r"\b((?:\d{1,3}\.){3}\d{1,3})\b[:\s]*?(\d{2,5})\b")
LAST_CHECKED_RE = re.compile(r"(\d+)\s*(min|h|hr|hour|d|day)", re.I)


def normalize_scheme(text: str) -> str | None:
    """Map free-text proxy type labels (e.g. 'SOCKS5', 'https') to a canonical scheme."""
    t = (text or "").strip().lower()
    for scheme in VALID_SCHEMES:
        if scheme in t:
            return scheme
    return None


def best_scheme(types: List[str]) -> str:
    """Pick one scheme to test a proxy with, preferring the most versatile."""
    found = {normalize_scheme(t) for t in types}
    found.discard(None)
    for scheme in VALID_SCHEMES:
        if scheme in found:
            return scheme
    return DEFAULT_SCHEME


def last_checked_minutes(text: str) -> float:
    """Parse strings like '8 min', '2 h', '1 d' into minutes ago (lower = more recent)."""
    m = LAST_CHECKED_RE.match((text or "").strip())
    if not m:
        return float("inf")
    value, unit = int(m.group(1)), m.group(2).lower()
    if unit.startswith("min"):
        return value
    if unit.startswith("h"):
        return value * 60
    if unit.startswith("d"):
        return value * 1440
    return float("inf")


def fetch(url: str) -> str | None:
    try:
        resp = requests.get(url, headers=HEADERS, timeout=FETCH_TIMEOUT)
        resp.raise_for_status()
        return resp.text
    except requests.RequestException as e:
        logging.warning("Could not fetch source %s: %s", url, e)
        return None


# ------------------ SOURCE SCRAPERS ------------------
# Each scraper returns (ip, port, scheme) tuples, scheme in {"http","https","socks4","socks5"}.
def scrape_freeproxy_world() -> List[Tuple[str, str, str]]:
    """https://www.freeproxy.world/?country=MN -- plain HTML table."""
    url = "https://www.freeproxy.world/?country=MN"
    html = fetch(url)
    if not html:
        return []
    soup = BeautifulSoup(html, "html.parser")
    out = []
    for row in soup.select("table.table tbody tr"):
        cells = row.find_all("td")
        if len(cells) < 6:
            continue
        ip = cells[0].get_text(strip=True)
        port_link = cells[1].find("a")
        port = port_link.get_text(strip=True) if port_link else cells[1].get_text(strip=True)
        scheme = best_scheme([a.get_text(strip=True) for a in cells[5].find_all("a")])
        if re.match(r"^(\d{1,3}\.){3}\d{1,3}$", ip) and port.isdigit():
            out.append((ip, port, scheme))
    out = out[:PER_SOURCE_LIMIT]
    logging.info("freeproxy.world: found %d candidates", len(out))
    return out


def scrape_ditatompel() -> List[Tuple[str, str, str]]:
    """https://www.ditatompel.com/proxy/country/mn -- server-rendered table, 'IP:PORT' in <strong>."""
    url = "https://www.ditatompel.com/proxy/country/mn"
    html = fetch(url)
    if not html:
        return []
    soup = BeautifulSoup(html, "html.parser")
    out = []
    for row in soup.select("table tbody tr"):
        cells = row.find_all("td")
        if not cells:
            continue
        strong = cells[0].find("strong")
        text = strong.get_text(strip=True) if strong else cells[0].get_text(strip=True)
        m = IP_PORT_RE.search(text)
        if not m:
            continue
        type_cell_text = cells[1].get_text(" ", strip=True) if len(cells) > 1 else ""
        scheme = best_scheme([type_cell_text])
        out.append((m.group(1), m.group(2), scheme))
    out = out[:PER_SOURCE_LIMIT]
    logging.info("ditatompel.com: found %d candidates", len(out))
    return out


def scrape_proxy5_api() -> List[Tuple[str, str, str]]:
    """https://proxy5.net/api/free-proxies.php -- JSON API behind the free-proxy page.
    Filtered to Mongolia, ranked by most-recently-checked, then uptime, then latency."""
    try:
        resp = requests.get(PROXY5_API_URL, headers=HEADERS, timeout=FETCH_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
    except (requests.RequestException, ValueError) as e:
        logging.warning("proxy5.net API fetch failed: %s", e)
        return []

    mongolia = [r for r in data.get("results", []) if r.get("country_code") == "MN"]
    mongolia.sort(
        key=lambda r: (
            last_checked_minutes(r.get("last_checked", "")),
            -(r.get("uptime") or 0),
            r.get("latency") if r.get("latency") is not None else float("inf"),
        )
    )
    out = [
        (str(r["ip_address"]), str(r["port"]), best_scheme(r.get("protocols") or []))
        for r in mongolia[:PROXY5_API_LIMIT]
    ]
    logging.info(
        "proxy5.net API: %d Mongolia candidates found, top %d selected", len(mongolia), len(out)
    )
    return out


SOURCES = [scrape_freeproxy_world, scrape_ditatompel, scrape_proxy5_api]


# ------------------ TESTING ------------------
def test_proxy(ip: str, port: str, scheme: str) -> Tuple[str, str, str, float] | None:
    """Try a proxy a few times before giving up -- free proxies often fail a
    single connection attempt but succeed on a retry."""
    proxy_url = f"{scheme}://{ip}:{port}"
    proxies = {"http": proxy_url, "https": proxy_url}
    for attempt in range(1, TEST_RETRIES + 1):
        try:
            resp = requests.get(
                TEST_URL, headers=HEADERS, proxies=proxies, timeout=TEST_TIMEOUT
            )
            if resp.status_code == 200 and len(resp.text) > 500:
                elapsed = resp.elapsed.total_seconds()
                logging.info("OK   %s (%.2fs, attempt %d)", proxy_url, elapsed, attempt)
                return (ip, port, scheme, elapsed)
        except requests.RequestException:
            pass
    return None


def find_working_proxies() -> List[Tuple[str, str, str, float]]:
    candidates = set()
    for scrape in SOURCES:
        try:
            candidates.update(scrape())
        except Exception as e:
            logging.warning("Source %s failed: %s", scrape.__name__, e)

    if not candidates:
        logging.error("No proxy candidates scraped from any source.")
        return []

    logging.info("Testing %d unique candidates against %s ...", len(candidates), TEST_URL)
    working = []
    with ThreadPoolExecutor(max_workers=TEST_WORKERS) as pool:
        futures = [pool.submit(test_proxy, ip, port, scheme) for ip, port, scheme in candidates]
        for fut in as_completed(futures):
            result = fut.result()
            if result:
                working.append(result)

    working.sort(key=lambda x: x[3])  # fastest first
    logging.info("%d/%d candidates passed the live test", len(working), len(candidates))
    return working


def main() -> None:
    working = find_working_proxies()
    top = working[:TOP_N]

    if not top:
        logging.error("No working Mongolia proxies found.")
        # Leave no proxy file behind so epg.py falls back to a direct connection.
        sys.exit(1)

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        for ip, port, scheme, elapsed in top:
            f.write(f"{scheme}://{ip}:{port}\n")

    logging.info("Top %d proxies written to %s:", len(top), OUTPUT_FILE)
    for ip, port, scheme, elapsed in top:
        logging.info("  %s://%s:%s (%.2fs)", scheme, ip, port, elapsed)


if __name__ == "__main__":
    main()
