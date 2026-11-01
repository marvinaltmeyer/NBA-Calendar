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
import os
import re
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
MAX_FEHLTAGE_ANTEIL = 0.3  # scheitern mehr Tage als dieser Anteil, wird abgebrochen
# ---------------------------------------------------------------------------

ESPN_SERVER = [
    "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard",
    "https://site.web.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard",
]
TZ = ZoneInfo(ZEITZONE)
TZ_US = ZoneInfo("America/New_York")  # ESPN-Tage sind US-Ostkuesten-Tage
HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept": "application/json",
}


# ---------------------------------------------------------------------------
# Laden
# ---------------------------------------------------------------------------
def hole_tag(tag, versuche=4):
    """Laedt die Spiele eines US-Tages. Wechselt bei Problemen den Server."""
    letzter_fehler = None
    for i in range(versuche):
        url = f"{ESPN_SERVER[i % len(ESPN_SERVER)]}?dates={tag:%Y%m%d}"
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=60) as r:
                roh = r.read()
            try:
                return json.loads(roh).get("events", []) or []
            except ValueError:
                vorschau = roh[:120].decode("utf-8", "replace").replace("\n", " ")
                raise ValueError(f"keine JSON-Antwort ({len(roh)} Byte): {vorschau!r}")
        except Exception as e:
            letzter_fehler = e
            if i < versuche - 1:
                warte = [5, 15, 30, 60][min(i, 3)]
                print(f"  {tag}: Versuch {i + 1} fehlgeschlagen ({e}) – neuer Versuch in {warte}s")
                zeit.sleep(warte)
    raise RuntimeError(f"{tag}: endgueltig fehlgeschlagen ({letzter_fehler})")


def lade_events():
    """Laedt alle Spiele im Zeitraum Tag fuer Tag. Gibt (events, fehltage) zurueck."""
    heute = date.today()
    tag = heute - timedelta(days=TAGE_ZURUECK)
    ende = heute + timedelta(days=TAGE_VORAUS)
    gesamt_tage = (ende - tag).days + 1
    max_fehltage = int(gesamt_tage * MAX_FEHLTAGE_ANTEIL)
    events, fehltage, anzahl_tage, in_folge = {}, set(), 0, 0
    while tag <= ende:
        anzahl_tage += 1
        try:
            for ev in hole_tag(tag):
                events[ev.get("id")] = ev
            in_folge = 0
        except RuntimeError as e:
            print(f"WARNUNG: {e}")
            fehltage.add(tag)
            in_folge += 1
            if in_folge >= 5 or len(fehltage) > max_fehltage:
                sys.exit(f"Fehler: ESPN liefert gerade keine Daten ({len(fehltage)} Tage "
                         "fehlgeschlagen) – Abbruch, alter Kalender bleibt bestehen.")
        tag += timedelta(days=1)
        zeit.sleep(0.3)
    print(f"{anzahl_tage - len(fehltage)} von {anzahl_tage} Tagen geladen.")
    return list(events.values()), fehltage


def alte_events_fuer_tage(pfad, tage):
    """Holt aus dem bisherigen Kalender die Eintraege fuer die angegebenen US-Tage."""
    if not tage or not os.path.exists(pfad):
        return []
    with open(pfad, encoding="utf-8") as f:
        text = f.read().replace("\r\n ", "").replace("\n ", "")  # Zeilen entfalten
    ergebnis = []
    for block in re.findall(r"BEGIN:VEVENT\r?\n(.*?)END:VEVENT", text, re.S):
        zeilen = [z for z in re.split(r"\r?\n", block) if z]
        dtstart = next((z for z in zeilen if z.startswith("DTSTART:")), None)
        if not dtstart:
            continue
        anwurf = datetime.strptime(dtstart[8:], "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
        if anwurf.astimezone(TZ_US).date() in tage:
            ergebnis.append((anwurf, ["BEGIN:VEVENT"] + zeilen + ["END:VEVENT"]))
    return ergebnis


# ---------------------------------------------------------------------------
# Kalender bauen
# ---------------------------------------------------------------------------
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
    """Gibt eine Liste von (anwurf, zeilen) fuer alle Spiele im Fenster zurueck."""
    start, ende = parse_hhmm(FENSTER_START), parse_hhmm(FENSTER_ENDE)
    teams_filter = {t.upper() for t in NUR_TEAMS}
    jetzt = fmt_utc(datetime.now(timezone.utc))
    ergebnis = []

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

    return ergebnis


def schreibe_ics(eintraege, pfad):
    # doppelte UIDs entfernen, nach Anwurf sortieren
    gesehen, eindeutig = set(), []
    for anwurf, zeilen in sorted(eintraege, key=lambda x: x[0]):
        uid = next((z for z in zeilen if z.startswith("UID:")), None)
        if uid in gesehen:
            continue
        gesehen.add(uid)
        eindeutig.append(zeilen)

    kopf = [
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
    zeilen = kopf + [z for ev in eindeutig for z in ev] + ["END:VCALENDAR"]
    with open(pfad, "w", encoding="utf-8", newline="") as f:
        f.write("\r\n".join(fold(z) for z in zeilen) + "\r\n")
    return len(eindeutig)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", help="lokale JSON-Datei mit ESPN-Events (Test)")
    ap.add_argument("--output", default=OUTPUT)
    args = ap.parse_args()

    if args.input:
        with open(args.input, encoding="utf-8") as f:
            daten = json.load(f)
        events, fehltage = daten["events"], {date.fromisoformat(t) for t in daten.get("fehltage", [])}
    else:
        events, fehltage = lade_events()

    if not events:
        sys.exit("Fehler: keine Spiele geladen – alter Kalender bleibt bestehen.")

    eintraege = baue_ics_events(events)
    alte = alte_events_fuer_tage(args.output, fehltage)
    if alte:
        print(f"{len(alte)} Eintraege fuer nicht ladbare Tage aus altem Kalender uebernommen.")
    anzahl = schreibe_ics(eintraege + alte, args.output)
    print(f"{anzahl} Spiele im Fenster {FENSTER_START}–{FENSTER_ENDE} Uhr "
          f"(von {len(events)} geladenen) -> {args.output}")


if __name__ == "__main__":
    main()
