#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
EPG scraper for zuragt.mn (ID-normalized + QC)

What's new vs previous build:
- Channel ID normalization: spaces in ID replaced with underscores (display-name stays Cyrillic)
- Post-generation QC: detect overlaps, gaps, invalid intervals and log a compact summary
- Windows-friendly timezone fallback (+08:00) when tzdata is missing
"""
from __future__ import annotations

import logging
import os
import re
import time
import xml.dom.minidom
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from typing import Optional, List, Dict
from urllib.parse import quote

import requests
from bs4 import BeautifulSoup
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# ------------------ CONFIG ------------------
BASE_URL = "https://www.zuragt.mn/"

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; EPGScraper/1.2)"}
OUTPUT_FILE = "weekly_epg_updated.xml"
DAYS_TO_FETCH = 7
REQUEST_TIMEOUT = 12  # seconds
REQUEST_RETRIES = 3
REQUEST_BACKOFF = 1.5  # seconds
INCLUDE_TZ_OFFSET = True  # XMLTV times like YYYYmmddHHMMSS +0800 if True
USE_PROXY = os.environ.get("USE_PROXY", "true").strip().lower() not in ("0", "false", "no", "off")

# corsproxy.io fetches zuragt.mn server-side and hands back the HTML, which
# sidesteps zuragt.mn's own Mongolia-only geo-block without needing an actual
# Mongolia proxy. Tried first; Mongolia proxies / direct connection are the fallback.
USE_CORSPROXY = os.environ.get("USE_CORSPROXY", "true").strip().lower() not in ("0", "false", "no", "off")
CORSPROXY_ENDPOINT = "https://corsproxy.io/?key=webdemo1&url="
CORSPROXY_HEADERS = {
    **HEADERS,
    "Accept": "*/*",
    "Origin": "https://console.corsproxy.io",
    "Referer": "https://console.corsproxy.io/",
}


def build_corsproxy_url(target_url: str) -> str:
    return CORSPROXY_ENDPOINT + quote(target_url, safe="")

# Local timezone with Windows fallback (+08:00)
try:
    LOCAL_TZ = ZoneInfo("Asia/Ulaanbaatar")
except (Exception, ZoneInfoNotFoundError):
    LOCAL_TZ = timezone(timedelta(hours=8))

# Non-channel headers to skip
NON_CHANNEL_DISPLAY_NAMES = {"Сувгууд", "Суваг", "Channels", "Сувгуудын жагсаалт"}

# Logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

TIME_RE = re.compile(r"^\s*(\d{1,2})\s*:\s*(\d{2})\s*$")

# ------------------ PROXIES ------------------
# zuragt.mn is only reachable from Mongolia. proxy_finder.py scrapes and
# tests Mongolia proxies and writes the fastest ones here (ip:port per line).
PROXY_FILE = os.environ.get("PROXY_FILE", "working_proxies.txt")
COUNTRY_CHECK_URL = "https://api.country.is/"


def load_proxies() -> List[str]:
    if not os.path.exists(PROXY_FILE):
        return []
    with open(PROXY_FILE, "r", encoding="utf-8") as f:
        return [line.strip() for line in f if line.strip() and not line.startswith("#")]


def is_already_in_mongolia() -> bool:
    """Check our own public IP's country; skip proxies entirely if we're already in MN."""
    try:
        resp = requests.get(COUNTRY_CHECK_URL, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        country = resp.json().get("country")
        logging.info("Own IP country check (%s): %s", COUNTRY_CHECK_URL, country)
        return country == "MN"
    except (requests.RequestException, ValueError) as e:
        logging.warning("Could not determine own IP country: %s", e)
        return False


if not USE_PROXY:
    logging.info("USE_PROXY is disabled, connecting directly.")
    PROXIES: List[str] = []
elif is_already_in_mongolia():
    logging.info("Already connecting from Mongolia, skipping proxies.")
    PROXIES = []
else:
    PROXIES = load_proxies()
    if PROXIES:
        logging.info("Loaded %d proxies from %s: %s", len(PROXIES), PROXY_FILE, ", ".join(PROXIES))
    else:
        logging.info("No proxy file found at %s, will connect directly.", PROXY_FILE)

def to_proxy_url(proxy: str) -> str:
    """working_proxies.txt entries are full URLs like 'socks5://ip:port'; plain
    'ip:port' (no scheme) is treated as a plain HTTP proxy for backward compatibility."""
    return proxy if "://" in proxy else f"http://{proxy}"

# ------------------ HTTP ------------------
def http_get(url: str) -> Optional[str]:
    """GET with retries/backoff, trying corsproxy.io first (if enabled), then each
    configured Mongolia proxy in turn (http, https, socks4, or socks5), then a
    direct connection as the last resort."""
    attempts = []  # (label, request_url, headers, proxies)
    if USE_CORSPROXY:
        attempts.append(("corsproxy.io", build_corsproxy_url(url), CORSPROXY_HEADERS, None))
    for proxy in PROXIES:
        proxy_url = to_proxy_url(proxy)
        attempts.append((proxy, url, HEADERS, {"http": proxy_url, "https": proxy_url}))
    attempts.append(("direct", url, HEADERS, None))

    for label, request_url, headers, proxies in attempts:
        for attempt in range(1, REQUEST_RETRIES + 1):
            try:
                resp = requests.get(request_url, headers=headers, timeout=REQUEST_TIMEOUT, proxies=proxies)
                resp.raise_for_status()
                return resp.text
            except requests.RequestException as e:
                logging.warning(
                    "Fetch failed via %s (%s/%s): %s -> %s",
                    label, attempt, REQUEST_RETRIES, url, e,
                )
                if attempt < REQUEST_RETRIES:
                    time.sleep(REQUEST_BACKOFF * attempt)
        logging.warning("%s exhausted retries, trying next...", label)
    logging.error("Giving up fetching (all methods failed): %s", url)
    return None

# ------------------ TIME FORMAT ------------------
def fmt_xmltv_time(dt: datetime) -> str:
    """Return XMLTV time string. If INCLUDE_TZ_OFFSET is True, append local offset."""
    if INCLUDE_TZ_OFFSET:
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=LOCAL_TZ)
        return dt.strftime("%Y%m%d%H%M%S %z")
    return dt.strftime("%Y%m%d%H%M%S")

# ------------------ XML ROOT ------------------
current_date = datetime.now(LOCAL_TZ).strftime("%Y%m%d%H%M%S")
tv = ET.Element(
    "tv",
    {
        "date": current_date,
        "generator-info-name": "Tugldr",
        "generator-info-url": "https://epg.pw",
        "source-info-name": "FREE EPG",
        "source-info-url": BASE_URL,
    },
)

# Collected across all fetched days per channel before stop times are computed,
# so a programme that airs past midnight (e.g. listed as "01:50" at the bottom
# of a day's schedule) can be re-dated onto the following calendar day and its
# stop time can be taken from whatever programme actually starts next.
channel_programs: Dict[str, List[Dict]] = {}

# Tracks which dates' pages were successfully fetched vs. not, for a summary log.
fetched_dates: List[str] = []
failed_dates: List[str] = []

# ------------------ HELPERS ------------------
def normalize_channel_id(name: str) -> str:
    """Normalize channel id: replace spaces with underscores; keep other chars as-is (Cyrillic allowed)."""
    # Collapse multiple spaces to one underscore
    return re.sub(r"\s+", "_", name.strip())

# ------------------ PARSE DAY ------------------
def parse_day_program(day_local: datetime) -> None:
    """Parse all channels for the given local day and append to XML tree."""
    if day_local.tzinfo is None:
        day_local = day_local.replace(tzinfo=LOCAL_TZ)
    day_local = day_local.astimezone(LOCAL_TZ)

    date_str = day_local.strftime("%Y-%m-%d")
    today_str = datetime.now(LOCAL_TZ).date().isoformat()
    url = BASE_URL if date_str == today_str else f"{BASE_URL}?date={date_str}"
    logging.info("Өгөгдөл татаж байна: %s", url)

    html = http_get(url)
    if not html:
        failed_dates.append(date_str)
        logging.error("Уг өдрийг татаж чадсангүй: %s", date_str)
        return

    fetched_dates.append(date_str)

    soup = BeautifulSoup(html, "html.parser")
    tv_boxes = soup.find_all("div", class_="tv-box")
    if not tv_boxes:
        logging.warning("Тухайн өдөрт tv-box элемент олдсонгүй: %s", date_str)

    for tv_box in tv_boxes:
        header = tv_box.find("div", class_="tv-header")
        h1 = header.find("h1") if header else None
        channel_name = (h1.get_text(strip=True) if h1 else "").strip()

        if not channel_name or channel_name in NON_CHANNEL_DISPLAY_NAMES:
            continue

        # Channel element with normalized ID, display-name remains Cyrillic
        channel_id = normalize_channel_id(channel_name)
        channel_elem = tv.find(f"./channel[@id='{channel_id}']")
        if channel_elem is None:
            channel_elem = ET.SubElement(tv, "channel", id=channel_id)
            ET.SubElement(channel_elem, "display-name", lang="mn").text = channel_name

        # Collect programme rows (li that contains 'addBookmark' in any class)
        li_items: List = []
        for li in tv_box.find_all("li"):
            classes = li.get("class") or []
            if any("addBookmark" in cls for cls in classes):
                li_items.append(li)
        if not li_items:
            logging.debug("Хөтөлбөр хоосон: %s", channel_name)
            continue

        # Parse each programme. Times are listed in order for the page's declared
        # day, but late-night items (e.g. "01:50" after "23:05") actually belong
        # to the following calendar day -- any drop in hour signals that rollover.
        invalid_times = 0
        current_date = day_local.date()
        prev_hour = None
        programs = channel_programs.setdefault(channel_id, [])

        for li in li_items:
            time_tag = li.find("div", class_="time")
            title_tag = li.find("div", class_="program")
            if not time_tag or not title_tag:
                continue

            ttxt = time_tag.get_text(strip=True)
            m = TIME_RE.match(ttxt)
            if not m:
                invalid_times += 1
                continue

            hour = int(m.group(1))
            minute = int(m.group(2))
            if hour > 23 or minute > 59:
                invalid_times += 1
                continue

            if prev_hour is not None and hour < prev_hour:
                current_date += timedelta(days=1)
            prev_hour = hour

            start_dt = datetime(
                current_date.year, current_date.month, current_date.day,
                hour, minute, tzinfo=LOCAL_TZ,
            )
            title = title_tag.get_text(strip=True)
            programs.append({"dt": start_dt, "title": title})

        if invalid_times:
            logging.warning("Цагийн формат танигдаагүй %d мөр (канал: %s)", invalid_times, channel_name)

# ------------------ EMIT PROGRAMMES ------------------
def emit_programmes() -> None:
    """Dedupe/sort each channel's collected programmes and write them to the XML
    tree, using the next programme's start as this one's stop (so a programme
    re-dated onto the next day via midnight rollover ends exactly when that
    day's next programme begins). The very last known programme for a channel
    has no successor, so it falls back to ending at 23:59:59 on its own date."""
    for channel_id, programs in channel_programs.items():
        seen_dts = set()
        deduped = []
        for item in programs:
            if item["dt"] in seen_dts:
                continue
            seen_dts.add(item["dt"])
            deduped.append(item)
        deduped.sort(key=lambda p: p["dt"])

        for i, prog in enumerate(deduped):
            start_xml = fmt_xmltv_time(prog["dt"])
            if i + 1 < len(deduped):
                stop_xml = fmt_xmltv_time(deduped[i + 1]["dt"])
            else:
                last_stop = prog["dt"].replace(hour=23, minute=59, second=59, microsecond=0)
                stop_xml = fmt_xmltv_time(last_stop)

            programme_elem = ET.SubElement(
                tv,
                "programme",
                start=start_xml,
                stop=stop_xml,
                channel=channel_id,
            )
            ET.SubElement(programme_elem, "title", lang="mn").text = prog["title"]

# ------------------ QC CHECK ------------------
def _parse_ts(ts: str) -> datetime:
    ts = ts.strip()
    if " " in ts:
        ts = ts.split()[0]
    return datetime.strptime(ts, "%Y%m%d%H%M%S")

def run_qc(xml_path: str) -> None:
    tree = ET.parse(xml_path)
    root = tree.getroot()

    # Build channel map
    channels = {}
    for ch in root.findall("channel"):
        ch_id = ch.get("id", "")
        dn = ch.findtext("display-name") or ""
        channels[ch_id] = dn

    # Gather programmes by channel
    prog_map: Dict[str, List[Dict]] = {}
    for pr in root.findAll("programme") if hasattr(root, "findAll") else root.findall("programme"):
        ch = pr.get("channel", "")
        start = _parse_ts(pr.get("start"))
        stop = _parse_ts(pr.get("stop"))
        title = pr.findtext("title") or ""
        prog_map.setdefault(ch, []).append({"start": start, "stop": stop, "title": title})

    # Check each channel
    total_overlaps = total_gaps = total_invalid = 0
    for ch_id, plist in prog_map.items():
        plist.sort(key=lambda x: x["start"])
        overlaps = gaps = invalid = 0
        last_stop = None
        last_day = None
        for p in plist:
            # invalid only if strictly negative duration
            if p["stop"] < p["start"]:
                invalid += 1

            # if day changed, reset continuity (don't count day-boundary as gap/overlap)
            day = p["start"].date()
            if last_day is not None and day != last_day:
                last_stop = None

            if last_stop is not None:
                if p["start"] < last_stop:
                    overlaps += 1
                elif p["start"] > last_stop:
                    gaps += 1

            last_stop = p["stop"]
            last_day = day

        total_overlaps += overlaps
        total_gaps += gaps
        total_invalid += invalid
        if overlaps or gaps or invalid:
            logging.warning("QC %s (%s): overlaps=%d gaps=%d invalid=%d",
                            ch_id, channels.get(ch_id, ""), overlaps, gaps, invalid)

    if total_overlaps == total_gaps == total_invalid == 0:
        logging.info("QC: No overlaps, gaps, or invalid intervals detected.")
    else:
        logging.info("QC summary: overlaps=%d gaps=%d invalid=%d",
                     total_overlaps, total_gaps, total_invalid)

def main() -> None:

    start_of_today = datetime.now(LOCAL_TZ).replace(hour=0, minute=0, second=0, microsecond=0)
    start_of_week = start_of_today - timedelta(days=start_of_today.weekday())  # Monday
    for day_offset in range(DAYS_TO_FETCH):
        day = start_of_week + timedelta(days=day_offset)
        parse_day_program(day)

    logging.info(
        "Татаж чадсан өдрүүд (%d): %s", len(fetched_dates), ", ".join(fetched_dates) or "-"
    )
    if failed_dates:
        logging.error(
            "Татаж чадаагүй өдрүүд (%d): %s", len(failed_dates), ", ".join(failed_dates)
        )
    else:
        logging.info("Татаж чадаагүй өдөр алга.")

    emit_programmes()

    # Pretty XML string with prolog and UTF-8 encoding
    xml_bytes = ET.tostring(tv, encoding="utf-8")
    parsed = xml.dom.minidom.parseString(xml_bytes)
    formatted_bytes = parsed.toprettyxml(indent="  ", encoding="UTF-8")  # returns bytes

    with open(OUTPUT_FILE, "wb") as f:
        f.write(formatted_bytes)

    logging.info("%s амжилттай үүсгэгдлээ!", OUTPUT_FILE)

    # Run quick QC and log findings
    run_qc(OUTPUT_FILE)

if __name__ == "__main__":
    main()
    