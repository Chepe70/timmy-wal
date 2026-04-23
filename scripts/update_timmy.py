# -*- coding: utf-8 -*-
"""
update_timmy.py
---------------
Scraped verifizierte Primaerquellen zum Fall Timmy und schreibt updates.json.
Laeuft stuendlich via GitHub Actions. Schreibt nur bei Aenderungen.

Quellen:
- ZDFheute Liveblog
- Deutsches Meeresmuseum Fachseite
- Nordkurier / NDR (falls zugaenglich)

Zitatrechtliche Leitlinie: Pro Eintrag nur Ueberschrift + 1 Zeile + Quell-URL.
"""
from __future__ import annotations
import hashlib
import json
import logging
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent.parent
OUTPUT = ROOT / "updates.json"
LOG_DIR = ROOT / "logs"
LOG_DIR.mkdir(exist_ok=True)
LOG_FILE = LOG_DIR / "scraper.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler(LOG_FILE, encoding="utf-8"), logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("timmy")

USER_AGENT = "Mozilla/5.0 (compatible; WalTimmyInfoBot/1.0; +https://wal-timmy.de)"
HEADERS = {"User-Agent": USER_AGENT, "Accept-Language": "de-DE,de;q=0.9"}
TIMEOUT = 20

MAX_ENTRIES = 30
SUMMARY_MAX = 220


def fetch(url: str) -> str | None:
    try:
        r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
        r.raise_for_status()
        return r.text
    except Exception as e:
        log.warning("Fetch failed %s: %s", url, e)
        return None


def clean(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "")).strip()


def shorten(text: str, limit: int = SUMMARY_MAX) -> str:
    text = clean(text)
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0]
    return cut + " …"


def make_id(source: str, title: str, url: str) -> str:
    return hashlib.sha1(f"{source}|{title}|{url}".encode("utf-8")).hexdigest()[:12]


def scrape_zdf_liveblog() -> list[dict]:
    """ZDFheute Liveblog. Struktur kann sich aendern; defensive Parsing."""
    url = "https://www.zdfheute.de/panorama/wal-timmy-ostsee-liveblog-100.html"
    html = fetch(url)
    if not html:
        return []

    soup = BeautifulSoup(html, "html.parser")
    entries: list[dict] = []

    candidates = soup.select("article, [data-entry-id], .liveblog-entry, .t-article-card")
    seen_titles: set[str] = set()

    for node in candidates[:MAX_ENTRIES * 2]:
        headline_el = node.find(["h2", "h3", "h4"])
        if not headline_el:
            continue
        title = clean(headline_el.get_text())
        if not title or title in seen_titles:
            continue
        if not re.search(r"(timmy|wal|buckelwal|ostsee|rettung|kirchsee|poel)", title, re.I):
            continue
        seen_titles.add(title)

        summary = ""
        for p in node.find_all("p", limit=3):
            s = clean(p.get_text())
            if s and s != title and len(s) > 40:
                summary = shorten(s)
                break

        time_el = node.find("time")
        ts = None
        if time_el and time_el.has_attr("datetime"):
            ts = time_el["datetime"]

        entries.append({
            "source": "ZDFheute Liveblog",
            "title": title,
            "summary": summary,
            "url": url,
            "timestamp": ts or datetime.now(timezone.utc).isoformat(timespec="seconds"),
        })
        if len(entries) >= MAX_ENTRIES:
            break

    log.info("ZDF liveblog: %d entries", len(entries))
    return entries


def scrape_meeresmuseum() -> list[dict]:
    url = "https://www.deutsches-meeresmuseum.de/wissenschaft/sichtungen/buckelwal-in-der-ostsee"
    html = fetch(url)
    if not html:
        return []

    soup = BeautifulSoup(html, "html.parser")
    entries: list[dict] = []

    main = soup.find("main") or soup
    headlines = main.find_all(["h2", "h3"], limit=15)

    for h in headlines:
        title = clean(h.get_text())
        if not title or not re.search(r"(timmy|wal|buckelwal|ostsee|update)", title, re.I):
            continue
        summary = ""
        nxt = h.find_next("p")
        if nxt:
            summary = shorten(clean(nxt.get_text()))
        entries.append({
            "source": "Dt. Meeresmuseum",
            "title": title,
            "summary": summary,
            "url": url,
            "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        })
        if len(entries) >= 10:
            break

    log.info("Meeresmuseum: %d entries", len(entries))
    return entries


def scrape_nordkurier() -> list[dict]:
    """Nordkurier Suchergebnisse fuer Timmy. Opportunistisch."""
    url = "https://www.nordkurier.de/suche?q=timmy+wal"
    html = fetch(url)
    if not html:
        return []

    soup = BeautifulSoup(html, "html.parser")
    entries: list[dict] = []

    for a in soup.select("a[href*='/regional/']")[:20]:
        title = clean(a.get_text())
        href = a.get("href", "")
        if not title or len(title) < 20:
            continue
        if not re.search(r"(timmy|wal|buckelwal)", title, re.I):
            continue
        full = href if href.startswith("http") else f"https://www.nordkurier.de{href}"
        entries.append({
            "source": "Nordkurier",
            "title": title,
            "summary": "",
            "url": full,
            "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        })
        if len(entries) >= 5:
            break

    log.info("Nordkurier: %d entries", len(entries))
    return entries


def load_previous() -> dict:
    if not OUTPUT.exists():
        return {"generated_at": None, "entries": []}
    try:
        return json.loads(OUTPUT.read_text(encoding="utf-8"))
    except Exception as e:
        log.warning("Could not read previous updates.json: %s", e)
        return {"generated_at": None, "entries": []}


def merge_and_dedupe(new_entries: list[dict], prev: dict) -> list[dict]:
    existing = {e.get("id"): e for e in prev.get("entries", []) if e.get("id")}
    merged: list[dict] = []

    for entry in new_entries:
        eid = make_id(entry["source"], entry["title"], entry["url"])
        entry["id"] = eid
        if eid in existing:
            entry["timestamp"] = existing[eid].get("timestamp", entry["timestamp"])
        merged.append(entry)

    for old in prev.get("entries", []):
        if old.get("id") and old["id"] not in {e["id"] for e in merged}:
            merged.append(old)

    def sort_key(e):
        return e.get("timestamp") or ""
    merged.sort(key=sort_key, reverse=True)
    return merged[:MAX_ENTRIES]


def main() -> int:
    log.info("=== Timmy Update Run ===")

    all_entries: list[dict] = []
    for scraper in (scrape_zdf_liveblog, scrape_meeresmuseum, scrape_nordkurier):
        try:
            all_entries.extend(scraper())
        except Exception as e:
            log.exception("Scraper %s failed: %s", scraper.__name__, e)

    prev = load_previous()
    merged = merge_and_dedupe(all_entries, prev)

    prev_ids = {e.get("id") for e in prev.get("entries", [])}
    new_ids = {e["id"] for e in merged}

    if prev_ids == new_ids and prev.get("entries"):
        log.info("No changes detected — skipping write")
        return 0

    output = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source_check": [
            "https://www.zdfheute.de/panorama/wal-timmy-ostsee-liveblog-100.html",
            "https://www.deutsches-meeresmuseum.de/wissenschaft/sichtungen/buckelwal-in-der-ostsee",
            "https://www.nordkurier.de/suche?q=timmy+wal",
        ],
        "entries": merged,
    }

    OUTPUT.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    log.info("Wrote %d entries to %s", len(merged), OUTPUT)
    return 0


if __name__ == "__main__":
    sys.exit(main())
