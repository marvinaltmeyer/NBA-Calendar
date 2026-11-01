#!/usr/bin/env python3
"""
Gefilterter NBA-Kalender.

Holt den offiziellen NBA-Spielplan (aktuelle Saison), behaelt nur Spiele,
deren Anwurf in deutscher Zeit (Europe/Berlin) im gewuenschten Zeitfenster
liegt, und schreibt eine abonnierbare .ics-Datei.

Aufruf:
    python nba_kalender.py                      # Spielplan von nba.com laden
    python nba_kalender.py --input plan.json    # lokale JSON-Datei (Test)
"""

import argparse
import json
import sys
import urllib.request
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

# ---------------------------------------------------------------------------
# EINSTELLUNGEN – hier kannst du alles anpassen
# ---------------------------------------------------------------------------
ZEITZONE = "Europe/Berlin"
FENSTER_START = "07:00"   # fruehester Anwurf (deutsche Zeit)
FENSTER_ENDE = "00:30"    # spaetester Anwurf (deutsche Zeit, inklusive)
NUR_TEAMS = []            # leer = alle Teams; sonst z.B. ["LAL", "BOS", "GSW"]
SPIELDAUER_MIN = 150      # Laenge des Kalendereintrags in Minuten
KALENDERNAME = "NBA (Anwurf 7–0:30 Uhr)"
OUTPUT = "nba.ics"
# ---------------------------------------------------------------------------

SCHEDULE_URL = "https://cdn.nba.com/static/json/staticData/scheduleLeagueV2.json"
TZ = ZoneInfo(ZEITZONE)


def lade_spielplan(pfad=None):
    if pfad:
        with open(pfad, encoding="utf-8") as f:
            return json.load(f)
    req = urllib.request.Request(
        SCHEDULE_URL,
        headers={
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
            "Accept": "application/json",
            "Referer": "https://www.nba.com/",
        },
    )
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)


def parse_hhmm(s):
    h, m = s.split(":")
    return time(int(h), int(m))


def im_fenster(t, start, ende):
    """Prueft, ob Uhrzeit t im Fenster liegt (auch ueber Mitternacht hinweg)."""
    if start <= ende:
        return start <= t <= ende
    return t >= start or t <= ende


def teamname(team):
    if not team:
        return "TBD"
    stadt = (team.get("teamCity") or "").strip()
    name = (team.get("teamName") or "").strip()
    voll = f"{stadt} {name}".strip()
    return voll or team.get("teamTricode") or "TBD"


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
    raw = line.encode("utf-8")
    if len(raw) <= 75:
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


def tv_sender(spiel):
    try:
        nat = spiel.get("broadcasters", {}).get("nationalBroadcasters", []) or []
        namen = [b.get("broadcasterDisplay") for b in nat if b.get("broadcasterDisplay")]
        return ", ".join(dict.fromkeys(namen))
    except Exception:
        return ""


def baue_events(daten):
    start, ende = parse_hhmm(FENSTER_START), parse_hhmm(FENSTER_ENDE)
    teams_filter = {t.upper() for t in NUR_TEAMS}
    jetzt = fmt_utc(datetime.now(timezone.utc))
    events, gesamt, behalten = [], 0, 0

    for tag in daten["leagueSchedule"]["gameDates"]:
        for spiel in tag.get("games", []):
            gesamt += 1
            status_text = (spiel.get("gameStatusText") or "").upper()
            if "TBD" in status_text:
                continue  # Anwurfzeit steht noch nicht fest
            if str(spiel.get("postponedStatus") or "").upper() in {"Y", "P", "PPD"}:
                continue
            utc_str = spiel.get("gameDateTimeUTC")
            if not utc_str:
                continue
            anwurf = datetime.fromisoformat(utc_str.replace("Z", "+00:00"))
            lokal = anwurf.astimezone(TZ)
            if not im_fenster(lokal.time().replace(second=0, microsecond=0), start, ende):
                continue

            heim, gast = spiel.get("homeTeam") or {}, spiel.get("awayTeam") or {}
            if teams_filter and not (
                (heim.get("teamTricode") or "").upper() in teams_filter
                or (gast.get("teamTricode") or "").upper() in teams_filter
            ):
                continue

            behalten += 1
            summary = f"🏀 {teamname(gast)} @ {teamname(heim)}"
            beschreibung = []
            for feld in ("gameLabel", "gameSubLabel", "seriesText"):
                if spiel.get(feld):
                    beschreibung.append(spiel[feld])
            tv = tv_sender(spiel)
            if tv:
                beschreibung.append(f"TV (USA): {tv}")
            beschreibung.append(f"Anwurf: {lokal:%d.%m.%Y, %H:%M} Uhr (deutsche Zeit)")
            ort = ", ".join(
                x for x in (spiel.get("arenaName"), spiel.get("arenaCity")) if x
            )

            zeilen = [
                "BEGIN:VEVENT",
                f"UID:{spiel.get('gameId', utc_str)}@nba-kalender",
                f"DTSTAMP:{jetzt}",
                f"DTSTART:{fmt_utc(anwurf)}",
                f"DTEND:{fmt_utc(anwurf + timedelta(minutes=SPIELDAUER_MIN))}",
                f"SUMMARY:{ics_escape(summary)}",
                f"DESCRIPTION:{ics_escape(chr(10).join(beschreibung))}",
            ]
            if ort:
                zeilen.append(f"LOCATION:{ics_escape(ort)}")
            zeilen.append("END:VEVENT")
            events.append(zeilen)

    return events, gesamt, behalten


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
    ap.add_argument("--input", help="lokale Spielplan-JSON statt Download")
    ap.add_argument("--output", default=OUTPUT)
    args = ap.parse_args()

    daten = lade_spielplan(args.input)
    events, gesamt, behalten = baue_events(daten)
    if gesamt == 0:
        sys.exit("Fehler: Spielplan enthaelt keine Spiele – nichts geschrieben.")
    schreibe_ics(events, args.output)
    saison = daten.get("leagueSchedule", {}).get("seasonYear", "?")
    print(f"Saison {saison}: {behalten} von {gesamt} Spielen im Fenster "
          f"{FENSTER_START}–{FENSTER_ENDE} Uhr -> {args.output}")


if __name__ == "__main__":
    main()
