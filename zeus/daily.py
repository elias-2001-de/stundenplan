"""Tagesaktuelle Veranstaltungen: alle Termine eines Kalendertags in einer Tabelle.

Dient als Gegenprobe zum Baum-Crawl (Vollständigkeit) und als schneller Weg,
für einen Zeitraum alle tatsächlich stattfindenden Termine einzusammeln.
Die Seite ist ein JSF-Formular, deshalb POST statt GET.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from bs4 import BeautifulSoup

from .session import BASE, ZeusSession

DAILY_URL = (f"{BASE}/pages/cm/exa/timetable/currentLectures.xhtml"
             "?_flowId=showEventsAndExaminationsOnDate-flow")
_FORM_ID = "showEventsAndExaminationsOnDateForm"
_DATE_FIELD = f"{_FORM_ID}:tabContainer:date-selection-container:date"
_SEARCH_BTN = f"{_FORM_ID}:searchButtonId"

_COLS = {
    "titel": "title", "beginn": "start_time", "ende": "end_time", "nummer": "number",
    "parallelgruppe": "group", "veranstaltungsart": "course_type",
    "dozent*in (verantwortlich)": "responsible", "dozent*in (durchführend)": "lecturers",
    "räume": "room", "semester": "semester", "bemerkung": "note",
}


@dataclass
class DailyEvent:
    date: str
    unit_id: int | None = None
    title: str = ""
    number: str = ""
    group: str = ""
    course_type: str = ""
    start_time: str = ""
    end_time: str = ""
    room: str = ""
    lecturers: str = ""
    semester: str = ""
    note: str = ""


def _form_fields(soup: BeautifulSoup) -> dict[str, str]:
    form = soup.find("form", id=_FORM_ID) or soup
    out: dict[str, str] = {}
    for inp in form.find_all("input"):
        typ = (inp.get("type") or "text").lower()
        name = inp.get("name")
        if not name or typ in ("submit", "button", "image", "checkbox", "radio"):
            continue
        out[name] = inp.get("value", "")
    return out


MAX_ROWS = 300   # Serverseitiges Limit "Zeilen pro Seite (max.: 300)"
_FILTER = f"{_FORM_ID}:tabContainer:filter-container:selectCheckbox"


def fetch_day(sess: ZeusSession, day: str, *, page_size: int = MAX_ROWS) -> list[DailyEvent]:
    """`day` im ISO-Format (YYYY-MM-DD). Nicht gecacht (POST).

    Die Seite zeigt nur das laufende und kommende Semester; für vergangene
    Termine liefert sie nichts.
    """
    y, m, d = day.split("-")
    german = f"{d}.{m}.{y}"
    # Genau ein GET: die Flow-Execution aus dieser Antwort muss zum POST passen.
    g = sess.s.get(DAILY_URL, timeout=sess.timeout)
    g.raise_for_status()
    fields = _form_fields(BeautifulSoup(g.text, "lxml"))
    fields[_DATE_FIELD] = german
    fields[_SEARCH_BTN] = "Suchen"
    # Filter explizit auf "Alle Termine" setzen (Checkbox wird von _form_fields ausgelassen)
    fields[_FILTER] = "selectAllCourses"
    fields.setdefault(f"{_FORM_ID}_SUBMIT", "1")
    r = sess.s.post(g.url, data=fields, timeout=sess.timeout,
                    headers={"Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                             "Referer": g.url})
    r.raise_for_status()
    events = parse_day(r.text, day)
    # Default sind 100 Zeilen pro Seite. Statt zu blättern erhöhen wir die
    # Zeilenzahl ("Zeilenanzahl aktualisieren") auf das Maximum und laden neu.
    # Mehr als MAX_ROWS Termine pro Tag liefert die Seite nicht auf einmal;
    # vollständig ist nur der Baum-Crawl (catalog.py).
    if len(events) >= 100 and page_size > 100:
        soup2 = BeautifulSoup(r.text, "lxml")
        btn = soup2.select_one('[name$="NumRowsRefresh"]')
        if btn is not None:
            fields2 = _form_fields(soup2)
            fields2[_FILTER] = "selectAllCourses"
            for name in list(fields2):
                if name.endswith("NumRowsInput"):
                    fields2[name] = str(min(page_size, MAX_ROWS))
            fields2[btn["name"]] = btn.get("value", "")
            r2 = sess.s.post(r.url, data=fields2, timeout=sess.timeout,
                             headers={"Referer": r.url})
            if r2.ok:
                bigger = parse_day(r2.text, day)
                if len(bigger) > len(events):
                    events = bigger
    return events


def parse_day(html: str, day: str) -> list[DailyEvent]:
    soup = BeautifulSoup(html, "lxml")
    best: list[DailyEvent] = []
    for table in soup.select("table"):
        heads = [re.sub(r"\[.*?\]", "", th.get_text(" ", strip=True)).strip().lower()
                 for th in table.select("thead th")]
        if not heads or not any(h.startswith("titel") for h in heads):
            continue
        cols: dict[int, str] = {}
        for i, h in enumerate(heads):
            for prefix, name in _COLS.items():
                if h.startswith(prefix):
                    cols[i] = name
                    break
        rows: list[DailyEvent] = []
        for tr in table.select("tbody > tr"):
            tds = tr.find_all("td", recursive=False) or tr.find_all("td")
            if not tds:
                continue
            ev = DailyEvent(date=day)
            for i, td in enumerate(tds):
                name = cols.get(i)
                if name:
                    setattr(ev, name, td.get_text(" ", strip=True).replace("\xa0", " "))
            link = tr.find("a", href=re.compile(r"unitId=\d+"))
            if link:
                ev.unit_id = int(re.search(r"unitId=(\d+)", link["href"]).group(1))
            if ev.title:
                rows.append(ev)
        if len(rows) > len(best):
            best = rows
    return best
