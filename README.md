# wal-timmy.de

Informations-Seite zum in der Ostsee gestrandeten Buckelwal „Timmy".

## Was ist das?

Eine seriöse, laufend aktualisierte Informationsseite: Chronologie, Rettungsplan, Akteure, Quellen. Der Live-Ticker wird stündlich automatisch aktualisiert.

## Struktur

```
.
├── index.html                 Hauptseite
├── impressum.html             Impressum (Platzhalter)
├── datenschutz.html           Datenschutz (Platzhalter)
├── updates.json               Ticker-Daten (vom Workflow überschrieben)
├── CNAME                      wal-timmy.de (GitHub Pages)
├── scripts/
│   └── update_timmy.py        Scraper: ZDFheute + Meeresmuseum + Nordkurier
├── requirements.txt           Python-Abhängigkeiten
└── .github/workflows/
    └── update.yml             Stündlicher Cron
```

## Lokale Entwicklung

```bash
pip install -r requirements.txt
python scripts/update_timmy.py   # schreibt updates.json
python -m http.server 8000       # index.html unter http://localhost:8000
```

## Deployment

- **Hosting:** GitHub Pages (HTTPS automatisch via Let's Encrypt)
- **Domain:** `wal-timmy.de` (CNAME-Datei im Repo)
- **Updates:** GitHub Actions, stündlich

## TODO vor Live-Gang

- [ ] Domain `wal-timmy.de` registrieren und DNS auf GitHub Pages zeigen lassen
  - Apex (A-Records): `185.199.108.153`, `185.199.109.153`, `185.199.110.153`, `185.199.111.153`
  - www-CNAME: `chepe70.github.io`
- [ ] Impressum-Service-Adresse bestellen, in `impressum.html` eintragen
- [ ] Datenschutzerklärung mit Generator final prüfen und anpassen
- [ ] Google AdSense-Publisher-ID eintragen (HTML-Kommentar `<script async src=...>`)
- [ ] Amazon-Associates-Tag eintragen (Platzhalter `AMAZON_TAG_PLACEHOLDER-21`)
- [ ] Ko-fi- und PayPal-Username in `index.html` eintragen
- [ ] OG-Image erstellen und `/og-image.jpg` hochladen
- [ ] Google Publisher Center für News-Aufnahme beantragen

## Lizenz

Texte: CC BY 4.0 der Redaktion. Zitate folgen dem Urheberrecht der jeweiligen Medien.
