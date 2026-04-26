# -*- coding: utf-8 -*-
"""
update_timmy.py
---------------
Roulette-basierter Scraper. Pro Lauf wird genau EINE Quelle abgefragt:
diejenige mit dem hoechsten Score = (jetzt - last_checked) * weight.

Dadurch werden einzelne Quellen hoeflich (in grossen Abstaenden) angefragt,
die Site-Aktualisierung passiert aber im engen Cron-Takt (z.B. alle 10 min).

Zitatrechtliche Leitlinie: Pro Eintrag nur Ueberschrift + Quell-URL.
Kein Summary/Teaser — vermeidet Konflikt mit Presse-Leistungsschutzrecht (§ 87f UrhG).
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
from typing import Callable

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent.parent
OUTPUT = ROOT / "updates.json"
DATA_DIR = ROOT / "data"
DATA_DIR.mkdir(exist_ok=True)
STATE_FILE = DATA_DIR / "sources_state.json"
LOG_DIR = ROOT / "logs"
LOG_DIR.mkdir(exist_ok=True)
LOG_FILE = LOG_DIR / "scraper.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler(LOG_FILE, encoding="utf-8"), logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("timmy")

USER_AGENT = "Mozilla/5.0 (compatible; WalTimmyInfoBot/1.0; +https://timmy-wal.de)"
HEADERS = {"User-Agent": USER_AGENT, "Accept-Language": "de-DE,de;q=0.9"}
TIMEOUT = 20
MAX_ENTRIES = 30
MAX_PER_SOURCE = 3   # bunte Mischung: pro Quelle nur die N neuesten Eintraege im Ticker
# Pflicht: ein Wal-Keyword muss vorkommen. Ortsnamen alleine (Wismar, Poel, Ostsee) reichen nicht —
# sonst landen Stichwahl/Stadtfest/Verkehr-Meldungen im Ticker.
# Matched: Timmy, Hope, Buckelwal, Wal (Wortgrenze), Wal- (Bindestrich), Walrettung/Walretter, Walfang.
# NICHT matched: Walter, Wahl, Stichwahl, Wismar, Walnuss, Wall.
WHALE_RE = re.compile(r"(timmy|hope|buckelwal|\bwal(\b|-|rett|retter|fang|gesang|forsch))", re.I)
KEYWORD_RE = WHALE_RE  # Backwards-Compat-Alias


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


def make_id(source: str, title: str, url: str) -> str:
    return hashlib.sha1(f"{source}|{title}|{url}".encode("utf-8")).hexdigest()[:12]


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# URL -> ISO-Timestamp Cache. Wird in main() aus prev befuellt, damit fuer bekannte
# Artikel kein zweiter HTTP-Request anfaellt.
ARTICLE_DATE_CACHE: dict[str, str] = {}


def _parse_article_date(html: str) -> str | None:
    """Sucht in der Artikelseite nach dem Veroeffentlichungsdatum.
    Reihenfolge: meta-Tags, JSON-LD, erstes <time datetime>."""
    soup = BeautifulSoup(html, "html.parser")
    for sel in (
        'meta[property="article:published_time"]',
        'meta[name="article:published_time"]',
        'meta[itemprop="datePublished"]',
        'meta[property="og:article:published_time"]',
        'meta[name="date"]',
        'meta[name="DC.date.issued"]',
        'meta[name="pubdate"]',
        'meta[name="last-modified"]',
    ):
        el = soup.select_one(sel)
        if el and el.get("content"):
            return el["content"].strip()
    for s in soup.find_all("script", attrs={"type": "application/ld+json"}):
        raw = s.string or s.get_text() or ""
        try:
            data = json.loads(raw)
        except Exception:
            continue
        candidates = data if isinstance(data, list) else [data]
        for c in candidates:
            if isinstance(c, dict):
                d = c.get("datePublished") or c.get("dateCreated")
                if d:
                    return d
    t = soup.find("time", attrs={"datetime": True})
    if t and t.get("datetime"):
        return t["datetime"]
    return None


def resolve_article_date(url: str) -> str:
    """Liefert ISO-Timestamp fuer Artikel-URL. Cache-first, sonst Artikelseite holen.
    Faellt auf jetzt zurueck, wenn nichts Verwertbares gefunden wird."""
    if url in ARTICLE_DATE_CACHE:
        return ARTICLE_DATE_CACHE[url]
    iso = now_iso()
    html = fetch(url)
    if html:
        raw = _parse_article_date(html)
        if raw:
            iso = to_iso(raw)
    ARTICLE_DATE_CACHE[url] = iso
    return iso


def to_iso(ts: str | None) -> str:
    """Parst beliebige Timestamp-Formate (RFC 822, ISO 8601) in ISO-UTC.
    Faellt auf jetzt zurueck, wenn Parsing fehlschlaegt."""
    if not ts:
        return now_iso()
    ts = ts.strip()
    # ISO 8601 (incl. trailing Z)
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).isoformat(timespec="seconds")
    except Exception:
        pass
    # RFC 822 / RSS pubDate (e.g. "Sat, 26 Apr 2026 08:24:00 GMT")
    try:
        from email.utils import parsedate_to_datetime
        dt = parsedate_to_datetime(ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).isoformat(timespec="seconds")
    except Exception:
        pass
    return now_iso()


# ---------- Scraper-Funktionen ----------

def _generic_headline_scrape(url: str, source: str, tier: str, selectors: list[str], min_len: int = 15) -> list[dict]:
    """Suche nach Schlagzeilen via CSS-Selektoren mit Keyword-Filter."""
    html = fetch(url)
    if not html:
        return []
    soup = BeautifulSoup(html, "html.parser")
    entries: list[dict] = []
    seen: set[str] = set()
    for sel in selectors:
        for el in soup.select(sel):
            title = clean(el.get_text())
            if not title or len(title) < min_len or title in seen:
                continue
            if not KEYWORD_RE.search(title):
                continue
            seen.add(title)
            href = el.get("href") if el.name == "a" else None
            if not href:
                a = el.find("a", href=True)
                href = a["href"] if a else None
            link = href if (href and href.startswith("http")) else (
                f"{url.rstrip('/')}/{href.lstrip('/')}" if href else url
            )
            entries.append({"source": source, "tier": tier, "title": title, "url": link, "timestamp": resolve_article_date(link)})
            if len(entries) >= 10:
                return entries
    return entries


def scrape_zdf_liveblog() -> list[dict]:
    url = "https://www.zdfheute.de/panorama/wal-timmy-ostsee-liveblog-100.html"
    html = fetch(url)
    if not html:
        return []
    soup = BeautifulSoup(html, "html.parser")
    entries: list[dict] = []
    seen: set[str] = set()
    candidates = soup.select("article, [data-entry-id], .liveblog-entry, .t-article-card")
    for node in candidates[:MAX_ENTRIES * 2]:
        h = node.find(["h2", "h3", "h4"])
        if not h:
            continue
        title = clean(h.get_text())
        if not title or title in seen or not KEYWORD_RE.search(title):
            continue
        seen.add(title)
        time_el = node.find("time")
        ts = to_iso(time_el["datetime"]) if (time_el and time_el.has_attr("datetime")) else now_iso()
        entries.append({"source": "ZDFheute Liveblog", "tier": "primary", "title": title, "url": url, "timestamp": ts})
        if len(entries) >= MAX_ENTRIES:
            break
    log.info("ZDF liveblog: %d entries", len(entries))
    return entries


def _scrape_google_news_rss(query: str, source: str, tier: str, max_items: int = 8) -> list[dict]:
    """Holt Treffer via Google-News-RSS. Robust gegen JS-Rendering der Zielsite."""
    url = f"https://news.google.com/rss/search?q={query}&hl=de&gl=DE&ceid=DE:de"
    html = fetch(url)
    if not html:
        return []
    soup = BeautifulSoup(html, "xml")
    entries: list[dict] = []
    for item in soup.find_all("item")[:max_items]:
        title = clean(item.title.text if item.title else "")
        link = clean(item.link.text if item.link else "")
        pubdate = clean(item.pubDate.text if item.pubDate else "")
        if not title or not link or not KEYWORD_RE.search(title):
            continue
        # Google-News-Titel hat Format: "Titel - Quelle"; trenne auf
        if " - " in title:
            title = title.rsplit(" - ", 1)[0]
        entries.append({"source": source, "tier": tier, "title": title, "url": link, "timestamp": to_iso(pubdate)})
    log.info("%s (Google News): %d entries", source, len(entries))
    return entries


def scrape_meeresmuseum() -> list[dict]:
    url = "https://www.deutsches-meeresmuseum.de/wissenschaft/sichtungen/buckelwal-in-der-ostsee"
    html = fetch(url)
    if not html:
        return []
    soup = BeautifulSoup(html, "html.parser")
    entries: list[dict] = []
    # Nur Inhalts-Container, nicht Cookie-Banner / Newsletter / Footer
    content = soup.select_one("main, article, .content, #content") or soup
    for h in content.find_all(["h2", "h3"], limit=30):
        title = clean(h.get_text())
        if not title or len(title) < 15:
            continue
        if not KEYWORD_RE.search(title):
            continue
        # Cookie/Datenschutz-Headlines aussortieren
        if re.search(r"(daten|cookie|newsletter|abschicken|entscheidung)", title, re.I):
            continue
        entries.append({"source": "Dt. Meeresmuseum", "tier": "primary", "title": title, "url": url, "timestamp": resolve_article_date(url)})
        if len(entries) >= 10:
            break
    log.info("Meeresmuseum: %d entries", len(entries))
    return entries


def scrape_nordkurier() -> list[dict]:
    return _scrape_google_news_rss("Buckelwal+Timmy+site:nordkurier.de", "Nordkurier", "regional")


def scrape_ndr() -> list[dict]:
    """NDR via Mecklenburg-Vorpommern-Index plus URL/Headline-Keyword-Filter."""
    url = "https://www.ndr.de/nachrichten/mecklenburg-vorpommern/index.html"
    html = fetch(url)
    if not html:
        return []
    soup = BeautifulSoup(html, "html.parser")
    entries: list[dict] = []
    seen: set[str] = set()
    for a in soup.find_all("a", href=True):
        href = a["href"]
        title = clean(a.get_text())
        if not title or len(title) < 20 or title in seen:
            continue
        # Treffer wenn URL ODER Titel das Keyword enthaelt
        # Treffer wenn URL ODER Titel ein Wal-Keyword enthaelt.
        # Ortsnamen alleine (poel, wismar, kirchsee) sind nicht ausreichend — sonst landen Wismarer Stichwahl-Meldungen im Ticker.
        if not WHALE_RE.search(title):
            continue
        seen.add(title)
        full = href if href.startswith("http") else f"https://www.ndr.de{href}"
        entries.append({"source": "NDR", "tier": "primary", "title": title, "url": full, "timestamp": resolve_article_date(full)})
        if len(entries) >= 8:
            break
    log.info("NDR: %d entries", len(entries))
    return entries


def scrape_tagesschau() -> list[dict]:
    url = "https://www.tagesschau.de/api2u/search/?searchText=buckelwal+timmy&resultPage=0"
    html = fetch(url)
    if not html:
        return []
    try:
        data = json.loads(html)
    except Exception:
        return []
    entries: list[dict] = []
    for item in (data.get("searchResults") or [])[:10]:
        title = clean(item.get("title", ""))
        link = item.get("shareURL") or item.get("detailsweb") or "https://www.tagesschau.de/"
        if not title or not KEYWORD_RE.search(title):
            continue
        entries.append({"source": "Tagesschau", "tier": "primary", "title": title, "url": link, "timestamp": to_iso(item.get("date"))})
    log.info("Tagesschau: %d entries", len(entries))
    return entries


def scrape_tonline() -> list[dict]:
    url = "https://www.t-online.de/nachrichten/panorama/tiere/"
    return _generic_headline_scrape(
        url, "t-online", "quality",
        selectors=["a[href*='/id_']", "h3 a", "article a"],
        min_len=25,
    )


_BILD_LIVESTREAM_RE = re.compile(r"(livestream|liveticker|liveblog|tag\s*\d+\s*in\s*der\s*bucht)", re.I)


def scrape_bild() -> list[dict]:
    """BILD via Google News RSS (BILD-Suche selbst ist JS-rendered, daher nicht direkt scrapebar).

    Filter: Livestream-/Liveticker-Beitraege werden ausgeschlossen, weil deren URL auf eine
    sich permanent aendernde Seite zeigt — der Schlagzeilen-Kontext stimmt dann nicht mehr.
    """
    entries = _scrape_google_news_rss("Buckelwal+Timmy+site:bild.de", "BILD", "boulevard")
    filtered = [e for e in entries if not _BILD_LIVESTREAM_RE.search(e["title"])]
    dropped = len(entries) - len(filtered)
    if dropped:
        log.info("BILD: %d Livestream-/Liveticker-Eintraege gefiltert", dropped)
    return filtered


def scrape_ifaw() -> list[dict]:
    url = "https://www.ifaw.org/de/aktuelles/buckelwal-ostsee-2026"
    html = fetch(url)
    if not html:
        return []
    soup = BeautifulSoup(html, "html.parser")
    entries: list[dict] = []
    for h in soup.find_all(["h1", "h2", "h3"], limit=20):
        title = clean(h.get_text())
        if not title or not KEYWORD_RE.search(title):
            continue
        entries.append({"source": "IFAW", "tier": "primary", "title": title, "url": url, "timestamp": resolve_article_date(url)})
        if len(entries) >= 5:
            break
    log.info("IFAW: %d entries", len(entries))
    return entries


# ---------- Source-Registry ----------

SOURCES: list[dict] = [
    {"name": "ZDFheute Liveblog", "weight": 2.0, "fn": scrape_zdf_liveblog},
    {"name": "NDR",               "weight": 2.0, "fn": scrape_ndr},
    {"name": "t-online",          "weight": 2.0, "fn": scrape_tonline},
    {"name": "BILD",              "weight": 2.0, "fn": scrape_bild},
    {"name": "Tagesschau",        "weight": 1.0, "fn": scrape_tagesschau},
    {"name": "Nordkurier",        "weight": 1.0, "fn": scrape_nordkurier},
    {"name": "Dt. Meeresmuseum",  "weight": 0.5, "fn": scrape_meeresmuseum},
    {"name": "IFAW",              "weight": 0.5, "fn": scrape_ifaw},
]


# ---------- State / Roulette ----------

def load_state() -> dict:
    if not STATE_FILE.exists():
        return {}
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def pick_source(state: dict) -> dict:
    """Quelle mit hoechstem Score = age_minutes * weight."""
    now = datetime.now(timezone.utc)
    best = None
    best_score = -1.0
    for src in SOURCES:
        last = state.get(src["name"], {}).get("last_checked")
        if last:
            try:
                last_dt = datetime.fromisoformat(last.replace("Z", "+00:00"))
            except Exception:
                last_dt = now
            age_min = max(0.0, (now - last_dt).total_seconds() / 60.0)
        else:
            age_min = 9999.0
        score = age_min * src["weight"]
        if score > best_score:
            best_score = score
            best = src
    return best


# ---------- Merge / Output ----------

def load_previous() -> dict:
    if not OUTPUT.exists():
        return {"generated_at": None, "entries": []}
    try:
        return json.loads(OUTPUT.read_text(encoding="utf-8"))
    except Exception:
        return {"generated_at": None, "entries": []}


def merge_and_dedupe(new_entries: list[dict], prev: dict, picked_source: str) -> list[dict]:
    # ID -> Timestamp aus prev. Bei Re-Scrape behalten wir den AELTEREN Timestamp,
    # damit ein erneuter Lauf die einmal gefundene Veroeffentlichungszeit nicht
    # mit der aktuellen Run-Zeit ueberschreibt (Artikel-Pubdate ist stabil).
    prev_ts: dict[str, str] = {e.get("id"): e.get("timestamp") for e in prev.get("entries", []) if e.get("id") and e.get("timestamp")}
    merged: list[dict] = []
    new_ids: set[str] = set()
    for entry in new_entries:
        entry["id"] = make_id(entry["source"], entry["title"], entry["url"])
        old_ts = prev_ts.get(entry["id"])
        if old_ts and (entry.get("timestamp") or "") > old_ts:
            entry["timestamp"] = old_ts
        new_ids.add(entry["id"])
        merged.append(entry)
    # Alte Eintraege gegen aktuellen Wal-Filter pruefen — entfernt False-Positives, die unter laxerem Filter reinkamen.
    prev_entries = [e for e in prev.get("entries", []) if WHALE_RE.search(e.get("title", ""))]
    for old in prev_entries:
        oid = old.get("id")
        # Bei der heute gepickten Quelle: alte Eintraege dieser Quelle nur uebernehmen, wenn sie auch jetzt wieder
        # auftauchen (sonst koennten geloeschte/aelter geworden Posts ewig stehen bleiben).
        # Andere Quellen: alte Eintraege beibehalten.
        if old.get("source") == picked_source:
            if oid in new_ids:
                continue  # neue Version uebernimmt timestamp aus new_entries
            # alte Eintraege der gepickten Quelle, die NICHT im neuen Scrape sind: behalten,
            # damit nichts verschwindet wenn Quelle die Seitenstruktur aendert.
            merged.append(old)
        else:
            if oid not in new_ids:
                merged.append(old)
    # Alle Timestamps in ISO-UTC normalisieren (Altdaten konnten RFC 822 enthalten)
    for e in merged:
        e["timestamp"] = to_iso(e.get("timestamp"))
    # Pro Quelle nur die N neuesten — bunte Mischung statt Quellen-Dominanz
    by_source: dict[str, list[dict]] = {}
    for e in sorted(merged, key=lambda x: x.get("timestamp") or "", reverse=True):
        by_source.setdefault(e.get("source", ""), []).append(e)
    capped = [e for src_entries in by_source.values() for e in src_entries[:MAX_PER_SOURCE]]
    capped.sort(key=lambda e: e.get("timestamp") or "", reverse=True)
    capped = capped[:MAX_ENTRIES]
    # Strikt zeitlich absteigend (Variante 2). Anschliessend MINIMALER Swap, um zwei aufeinanderfolgende
    # Eintraege derselben Quelle aufzubrechen — DESC bleibt bevorzugt, Reorder nur wo Konflikt entsteht.
    out = list(capped)
    i = 1
    while i < len(out):
        if out[i].get("source") == out[i - 1].get("source"):
            for j in range(i + 1, len(out)):
                if out[j].get("source") != out[i - 1].get("source") and out[j].get("source") != out[i].get("source"):
                    out[i], out[j] = out[j], out[i]
                    break
            else:
                # Fallback: Quelle != vorherige reicht (Cluster aufbrechen, auch wenn naechster gleicher Quelle ist)
                for j in range(i + 1, len(out)):
                    if out[j].get("source") != out[i - 1].get("source"):
                        out[i], out[j] = out[j], out[i]
                        break
        i += 1
    return out


def main() -> int:
    log.info("=== Timmy Update Run ===")
    state = load_state()
    # Cache aus vorherigem Lauf vorbefuellen: bekannte URLs nicht erneut nachladen.
    prev_pre = load_previous()
    for e in prev_pre.get("entries", []):
        u, ts = e.get("url"), e.get("timestamp")
        if u and ts:
            ARTICLE_DATE_CACHE[u] = ts
    src = pick_source(state)
    log.info("Picked source: %s (weight=%.1f)", src["name"], src["weight"])

    try:
        new_entries = src["fn"]()
    except Exception as e:
        log.exception("Scraper %s failed: %s", src["name"], e)
        new_entries = []

    state[src["name"]] = {"last_checked": now_iso(), "entries_found": len(new_entries)}
    save_state(state)

    prev = prev_pre
    merged = merge_and_dedupe(new_entries, prev, src["name"])

    prev_ids = {e.get("id") for e in prev.get("entries", [])}
    new_id_set = {e["id"] for e in merged}
    if prev_ids == new_id_set and prev.get("entries"):
        log.info("No changes — skipping write")
        return 0

    output = {
        "generated_at": now_iso(),
        "last_picked_source": src["name"],
        "sources": [s["name"] for s in SOURCES],
        "entries": merged,
    }
    OUTPUT.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    log.info("Wrote %d entries (picked %s)", len(merged), src["name"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
