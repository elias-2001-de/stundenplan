"""iCalendar-Bausteine (RFC 5545) – reine Standardbibliothek.

Gemeinsam genutzt von `zeus.cli export --format ics` und dem Stundenplaner
(`app.py`), damit beide Exporte dieselben Regeln fuer Zeitzone, Escaping und
Zeilenfaltung verwenden.
"""
from __future__ import annotations

import datetime as dt

# Minimale Zeitzonendefinition, damit die Datei auch in strengen Clients
# (Thunderbird, Outlook) ohne Nachfrage importiert.
VTIMEZONE = """BEGIN:VTIMEZONE
TZID:Europe/Berlin
BEGIN:DAYLIGHT
TZOFFSETFROM:+0100
TZOFFSETTO:+0200
TZNAME:CEST
DTSTART:19700329T020000
RRULE:FREQ=YEARLY;BYMONTH=3;BYDAY=-1SU
END:DAYLIGHT
BEGIN:STANDARD
TZOFFSETFROM:+0200
TZOFFSETTO:+0100
TZNAME:CET
DTSTART:19701025T030000
RRULE:FREQ=YEARLY;BYMONTH=10;BYDAY=-1SU
END:STANDARD
END:VTIMEZONE""".split("\n")


def esc(s: str) -> str:
    return ((s or "").replace("\\", "\\\\").replace(";", r"\;")
            .replace(",", r"\,").replace("\n", r"\n"))


def fold(line: str) -> list[str]:
    """Zeilen auf 75 Oktett falten (RFC 5545); Folgezeilen beginnen mit Space."""
    raw = line.encode("utf-8")
    if len(raw) <= 75:
        return [line]
    out, cur = [], b""
    for ch in line:
        b = ch.encode("utf-8")
        limit = 75 if not out else 74
        if len(cur) + len(b) > limit:
            out.append(cur.decode("utf-8"))
            cur = b""
        cur += b
    if cur:
        out.append(cur.decode("utf-8"))
    return [out[0]] + [" " + p for p in out[1:]]


def stamp() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _d(iso: str) -> str:
    return iso.replace("-", "")


def event(uid, summary, *, date, start=None, end=None, end_date=None,
          location="", description="", url="", rrule="", transp="",
          now=None) -> list[str]:
    """Ein VEVENT. Ohne `start` wird es ein ganztaegiger Eintrag.

    `end_date` ist bei ganztaegigen Eintraegen **inklusiv** (RFC 5545 will
    exklusiv, das rechnen wir hier um)."""
    lines = ["BEGIN:VEVENT", f"UID:{uid}", f"DTSTAMP:{now or stamp()}"]
    if start:
        lines += [
            f"DTSTART;TZID=Europe/Berlin:{_d(date)}T{start.replace(':', '')}00",
            f"DTEND;TZID=Europe/Berlin:{_d(end_date or date)}"
            f"T{(end or start).replace(':', '')}00",
        ]
    else:
        last = dt.date.fromisoformat(end_date or date) + dt.timedelta(days=1)
        lines += [f"DTSTART;VALUE=DATE:{_d(date)}",
                  f"DTEND;VALUE=DATE:{_d(last.isoformat())}"]
    if rrule:
        lines.append(f"RRULE:{rrule}")
    lines.append(f"SUMMARY:{esc(summary)}")
    if location:
        lines.append(f"LOCATION:{esc(location)}")
    if description:
        lines.append(f"DESCRIPTION:{esc(description)}")
    if url:
        lines.append(f"URL:{url}")
    if transp:
        lines.append(f"TRANSP:{transp}")
    lines.append("END:VEVENT")
    return lines


def calendar(body: list[str], *, name: str = "", prodid: str = "-//stundenplan//zeus//DE") -> str:
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", f"PRODID:{prodid}",
             "CALSCALE:GREGORIAN", "METHOD:PUBLISH"]
    if name:
        lines += [f"X-WR-CALNAME:{esc(name)}", f"NAME:{esc(name)}"]
    lines += [*VTIMEZONE, *body, "END:VCALENDAR"]
    return "\r\n".join(f for line in lines for f in fold(line)) + "\r\n"
