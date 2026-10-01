"""Rechnet Terminmuster ("wöchentlich, Mo, 20.10.–02.02.") in konkrete Termine um."""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, timedelta

from .detail import Appointment

WEEKDAYS = {"mo": 0, "di": 1, "mi": 2, "do": 3, "fr": 4, "sa": 5, "so": 6,
            "mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}

# Rhythmus -> Schrittweite in Tagen. None = kein regelmäßiger Takt.
_RHYTHM_STEP = {
    "einzeltermin": 0,
    "wöchentlich": 7,
    "woechentlich": 7,
    "weekly": 7,
    "14-täglich": 14,
    "14-taeglich": 14,
    "zweiwöchentlich": 14,
    "vierzehntägig": 14,
    "dreiwöchentlich": 21,
    "vierwöchentlich": 28,
    "monatlich": 28,
}

EXACT, SPAN, UNKNOWN = "exact", "span", "unknown"


@dataclass
class Occurrence:
    """Ein konkreter Termin an einem konkreten Tag."""

    date: str          # ISO
    start_time: str | None
    end_time: str | None
    room: str = ""
    cancelled: bool = False


def _d(iso: str) -> date:
    y, m, dd = iso.split("-")
    return date(int(y), int(m), int(dd))


def rhythm_step(rhythm: str) -> int | None:
    r = (rhythm or "").strip().lower()
    for key, step in _RHYTHM_STEP.items():
        if key in r:
            return step
    return None


def expand(ap: Appointment, *, limit: int = 400) -> tuple[list[Occurrence], str]:
    """Termine eines Musters aufzählen.

    Rückgabe: (Termine, Status). Status `exact` = vollständig aufgelöst,
    `span` = Blockveranstaltung o.ä., nur Zeitraum bekannt (ein Eintrag mit
    Start- und Enddatum), `unknown` = Muster nicht interpretierbar.
    """
    if not ap.first_date:
        return [], UNKNOWN
    start, end = _d(ap.first_date), _d(ap.last_date or ap.first_date)
    if end < start:
        end = start
    cancelled = set(ap.cancelled)
    step = rhythm_step(ap.rhythm)

    if step == 0 or start == end:
        occ = Occurrence(ap.first_date, ap.start_time, ap.end_time, ap.room,
                         ap.first_date in cancelled)
        return [occ], EXACT

    if step is None:
        # Blockveranstaltung / unbekannter Rhythmus: nur den Zeitraum melden.
        occ = Occurrence(ap.first_date, ap.start_time, ap.end_time, ap.room,
                         ap.first_date in cancelled)
        return [occ], SPAN if ap.rhythm else UNKNOWN

    wd = WEEKDAYS.get((ap.weekday or "").strip().lower()[:3].rstrip("."))
    if wd is None:
        wd = WEEKDAYS.get((ap.weekday or "").strip().lower()[:2])
    cur = start
    if wd is not None and cur.weekday() != wd:
        cur += timedelta(days=(wd - cur.weekday()) % 7)

    out: list[Occurrence] = []
    while cur <= end and len(out) < limit:
        iso = cur.isoformat()
        out.append(Occurrence(iso, ap.start_time, ap.end_time, ap.room, iso in cancelled))
        cur += timedelta(days=step)
    return out, EXACT
