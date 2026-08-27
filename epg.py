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
import re
import time
import xml.dom.minidom
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from typing import Optional, List, Dict

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
INCLUDE_TZ_OFFSET = False  # XMLTV times like YYYYmmddHHMMSS +0800 if True

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

# ------------------ HTTP ------------------
def http_get(url: str) -> Optional[str]:
    """GET with basic retries/backoff."""
    for attempt in range(1, REQUEST_RETRIES + 1):
        try:
            resp = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
            return resp.text
        except requests.RequestException as e:
            logging.warning("Fetch failed (%s/%s): %s -> %s", attempt, REQUEST_RETRIES, url, e)
            if attempt < REQUEST_RETRIES:
                time.sleep(REQUEST_BACKOFF * attempt)
    logging.error("Giving up fetching: %s", url)
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
        return

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

        # Parse each programme
        programme_list: List[Dict[str, str]] = []
        invalid_times = 0

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

            start_dt = day_local.replace(hour=hour, minute=minute, second=0, microsecond=0)
            start_naive = start_dt.replace(tzinfo=None)
            start_xml = fmt_xmltv_time(start_naive)

            title = title_tag.get_text(strip=True)
            programme_list.append({"start": start_xml, "title": title, "channel_id": channel_id})

        if invalid_times:
            logging.warning("Цагийн формат танигдаагүй %d мөр (канал: %s)", invalid_times, channel_name)

        # Deduplicate by start timestamp (keep first occurrence)
        seen_starts = set()
        deduped = []
        for item in programme_list:
            key = item["start"]
            if key in seen_starts:
                continue
            seen_starts.add(key)
            deduped.append(item)
        programme_list = deduped

        # Sort by start
        def to_int(s: str) -> int:
            return int(s.split()[0])  # drop tz part if present
        programme_list.sort(key=lambda p: to_int(p["start"]))

        # Emit XML with computed stop
        for i, prog in enumerate(programme_list):
            if i + 1 < len(programme_list):
                stop_xml = programme_list[i + 1]["start"]  # allow zero-duration; QC treats only stop<start as invalid
            else:
                last_stop = day_local.replace(hour=23, minute=59, second=59, microsecond=0).replace(tzinfo=None)
                stop_xml = fmt_xmltv_time(last_stop)

            programme_elem = ET.SubElement(
                tv,
                "programme",
                start=prog["start"],
                stop=stop_xml,
                channel=prog["channel_id"],
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
    for day_offset in range(DAYS_TO_FETCH):
        day = start_of_today + timedelta(days=day_offset)
        parse_day_program(day)

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
    