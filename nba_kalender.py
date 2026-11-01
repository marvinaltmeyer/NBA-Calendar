#!/usr/bin/env python3
"""
Gefilterter NBA-Kalender.

Holt den NBA-Spielplan (Datenquelle: ESPN), behaelt nur Spiele, deren
Anwurf in deutscher Zeit (Europe/Berlin) im gewuenschten Zeitfenster liegt,
und schreibt eine abonnierbare .ics-Datei.

Aufruf:
    python nba_kalender.py                      # Spielplan von ESPN laden
    python nba_kalender.py --input events.json  # lokale Testdaten
"""

import argparse
import json
import sys
import time as zeit
import urllib.error
import urllib.request
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

# ---------------------------------------------------------------------------
# EINSTELLUNGEN – hier kannst du alles anpassen
# ---------------------------------------------------------------------------
ZEITZONE = "Europe/Berlin"
FENSTER_START = "07:00"   # fruehester Anwurf (deutsche Zeit)
FENSTER_ENDE = "23:59"    # spaetester Anwurf (deutsche Zeit, inklusive)
NUR_TEAMS = []            # leer = alle Teams; sonst ESPN-Kuerzel, z.B. ["LAL", "BOS", "GS"]
SPIELDAUER_MIN = 150      # Laenge des Kalendereintrags in Minuten
AM_TAGESENDE_KAPPEN = True  # Eintraege enden spaetestens um 23:59 am Anwurftag
TAGE_ZURUECK = 14         # vergangene Spiele, die im Kalender bleiben
TAGE_VORAUS = 270         # wie weit in die Zukunft geladen wird (deckt Playoffs ab)
KALENDERNAME = "NBA (Anwurf 7–23:59 Uhr)"
OUTPUT = "nba.ics"
# ---------------------------------------------------------------------------

ESPN_BASIS = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard"
TZ = ZoneInfo(ZEITZONE)


def hole_json(url, versuche=3):
    """Laedt JSON. 4xx-Fehler werden sofort weitergereicht, 5xx/Netzfehler wiederholt."""
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
        "Accept": "application/json",
    })
    for i in range(versuche):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if 400 <= e.code < 500 or i == versuche - 1:
                raise
            print(f"Versuch {i + 1} fehlgeschlagen ({e}), neuer Versuch ...")
        except Exception as e:
            if i == versuche - 1:
                raise
            print(f"Versuch {i + 1} fehlgeschlagen ({e}), neuer Versuch ...")
        zeit.sleep(5 * (i + 1))


def lade_bloecke(start, ende):
    """Schneller Weg: ganze Monate auf einmal abfragen."""
    events = {}
    block_start = start
    while block_start <= ende:
        block_ende = min(block_start + timedelta(days=30), ende)
        url = (f"{ESPN_BASIS}?limit=500&dates="
               f"{block_start:%Y%m%d}-{block_ende:%Y%m%d}")
        block = hole_json(url).get("events", []) or []
        if len(block) >= 500:
            raise ValueError("Monatsblock hat das Limit erreicht")
        for ev in block:
            events[ev.get("id")] = ev
        block_start = block_ende + timedelta(days=1)
    return events


def lade_tageweise(start, ende):
    """Sicherer Weg: jeden Tag einzeln abfragen."""
    events = {}
    tag = start
    while tag <= ende:
        url = f"{ESPN_BASIS}?dates={tag:%Y%m%d}"
        for ev in hole_json(url).get("events", []) or []:
            events[ev.get("id")] = ev
        tag += timedelta(days=1)
        zeit.sleep(0.2)
    return events


def lade_events():
    heute = date.today()
    start = heute - timedelta(days=TAGE_ZURUECK)
    ende = heute + timedelta(days=TAGE_VORAUS)
    try:
        events = lade_bloecke(start, ende)
        print("Spielplan in Monatsbloecken geladen.")
    except Exception as e:
        print(f"Monatsabfrage nicht moeglich ({e}) – lade tageweise ...")
        events = lade_tageweise(start, ende)
        print("Spielplan tageweise geladen.")
    return list(events.values())


def parse_hhmm(s):
    h, m = s.split(":")
    return time(int(h), int(m))


def im_fenster(t, start, ende):
    """Prueft, ob Uhrzeit t im Fenster liegt (auch ueber Mitternacht hinweg)."""
    if start <= ende:
        return start <= t <= ende
    return t >= start or t <= ende


def ics_escape(text):
    return (
        str(text)
        .replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\r\n", "\\n")
        .replace("\n", "\\n")
    )


def fold(line):
    """Zeilen nach RFC 5545 auf max. 75 Byte umbrechen (UTF-8-sicher)."""
    if len(line.encode("utf-8")) <= 75:
        return line
    teile, aktuell, limit = [], b"", 75
    for ch in line:
        b = ch.encode("utf-8")
        if len(aktuell) + len(b) > limit:
            teile.append(aktuell.decode("utf-8"))
            aktuell, limit = b"", 74  # Folgezeilen beginnen mit Leerzeichen
        aktuell += b
    teile.append(aktuell.decode("utf-8"))
    return "\r\n ".join(teile)


def fmt_utc(dt):
    return dt.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def baue_ics_events(events):
    start, ende = parse_hhmm(FENSTER_START), parse_hhmm(FENSTER_ENDE)
    teams_filter = {t.upper() for t in NUR_TEAMS}
    jetzt = fmt_utc(datetime.now(timezone.utc))
    ergebnis, behalten = [], 0

    for ev in events:
        comp = (ev.get("competitions") or [{}])[0]
        if comp.get("timeValid") is False:
            continue  # Anwurfzeit steht noch nicht fest
        status = (((comp.get("status") or ev.get("status") or {})
                   .get("type") or {}).get("name") or "").upper()
        if "POSTPONED" in status or "CANCELED" in status or "CANCELLED" in status:
            continue
        datum = ev.get("date")
        if not datum:
            continue
        anwurf = datetime.fromisoformat(datum.replace("Z", "+00:00"))
        lokal = anwurf.astimezone(TZ)
        if not im_fenster(lokal.time().replace(second=0, microsecond=0), start, ende):
            continue

        heim = gast = {}
        for c in comp.get("competitors", []) or []:
            if c.get("homeAway") == "home":
                heim = c.get("team") or {}
            elif c.get("homeAway") == "away":
                gast = c.get("team") or {}
        kuerzel = {(heim.get("abbreviation") or "").upper(),
                   (gast.get("abbreviation") or "").upper()}
        if teams_filter and not (kuerzel & teams_filter):
            continue

        behalten += 1
        name_gast = gast.get("displayName") or "TBD"
        name_heim = heim.get("displayName") or "TBD"
        summary = f"🏀 {name_gast} @ {name_heim}"

        beschreibung = []
        if (ev.get("season") or {}).get("type") == 3:
            beschreibung.append("Playoffs")
        for n in comp.get("notes", []) or []:
            if n.get("headline"):
                beschreibung.append(n["headline"])
        tv = []
        for b in comp.get("broadcasts", []) or []:
            if b.get("market") == "national":
                tv.extend(b.get("names") or [])
        if tv:
            beschreibung.append("TV (USA): " + ", ".join(dict.fromkeys(tv)))
        beschreibung.append(f"Anwurf: {lokal:%d.%m.%Y, %H:%M} Uhr (deutsche Zeit)")

        venue = comp.get("venue") or {}
        ort = ", ".join(x for x in (venue.get("fullName"),
                                    (venue.get("address") or {}).get("city")) if x)

        spielende = anwurf + timedelta(minutes=SPIELDAUER_MIN)
        if AM_TAGESENDE_KAPPEN:
            tagesende = lokal.replace(hour=23, minute=59, second=0, microsecond=0)
            spielende = min(spielende, tagesende)

        zeilen = [
            "BEGIN:VEVENT",
            f"UID:{ev.get('id', datum)}@nba-kalender",
            f"DTSTAMP:{jetzt}",
            f"DTSTART:{fmt_utc(anwurf)}",
            f"DTEND:{fmt_utc(spielende)}",
            f"SUMMARY:{ics_escape(summary)}",
            f"DESCRIPTION:{ics_escape(chr(10).join(beschreibung))}",
        ]
        if ort:
            zeilen.append(f"LOCATION:{ics_escape(ort)}")
        zeilen.append("END:VEVENT")
        ergebnis.append((anwurf, zeilen))

    ergebnis.sort(key=lambda x: x[0])
    return [z for _, z in ergebnis], behalten


def schreibe_ics(events, pfad):
    zeilen = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//nba-kalender//gefiltert//DE",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        f"X-WR-CALNAME:{ics_escape(KALENDERNAME)}",
        f"X-WR-TIMEZONE:{ZEITZONE}",
        "REFRESH-INTERVAL;VALUE=DURATION:PT6H",
        "X-PUBLISHED-TTL:PT6H",
    ]
    for ev in events:
        zeilen.extend(ev)
    zeilen.append("END:VCALENDAR")
    with open(pfad, "w", encoding="utf-8", newline="") as f:
        f.write("\r\n".join(fold(z) for z in zeilen) + "\r\n")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", help="lokale JSON-Datei mit ESPN-Events (Test)")
    ap.add_argument("--output", default=OUTPUT)
    args = ap.parse_args()

    if args.input:
        with open(args.input, encoding="utf-8") as f:
            events = json.load(f)["events"]
    else:
        events = lade_events()

    if not events:
        sys.exit("Fehler: keine Spiele geladen – alter Kalender bleibt bestehen.")
    ics_events, behalten = baue_ics_events(events)
    schreibe_ics(ics_events, args.output)
    print(f"{behalten} von {len(events)} Spielen im Fenster "
          f"{FENSTER_START}–{FENSTER_ENDE} Uhr -> {args.output}")


if __name__ == "__main__":
    main()
