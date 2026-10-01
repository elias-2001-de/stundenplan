"""Veranstaltungssuche (`searchCourseNonStaff-flow`).

Der Baum des Vorlesungsverzeichnisses enthält nur Veranstaltungen, die einem
Studiengang zugeordnet sind – rund 13 % fehlen dort. Die Suchmaske findet
dagegen alle Veranstaltungen eines Semesters. Sie liefert aber höchstens
300 Zeilen pro Seite und lässt sich ohne JavaScript nicht weiterblättern,
deshalb wird die Suche in Scheiben zerlegt (Nummernpräfix, notfalls feiner).
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from bs4 import BeautifulSoup

from .session import START_FLOW, ZeusSession

log = logging.getLogger(__name__)

SEARCH_URL = f"{START_FLOW}?_flowId=searchCourseNonStaff-flow"
_FORM_ID = "genericSearchMask"
MAX_ROWS = 300

_COUNT_RE = re.compile(r'dataScrollerResultText">\s*([\d.,]+)')


@dataclass
class Hit:
    unit_id: int
    number: str = ""
    title: str = ""


def term_value(semester_name: str) -> str:
    """"Wintersemester 2026/27" -> "eq|2|2026" (Wert der Semesterauswahl)."""
    m = re.search(r"(\d{4})", semester_name)
    if not m:
        raise ValueError(f"Kein Jahr in {semester_name!r}")
    kind = 2 if semester_name.lower().startswith("winter") else 1
    return f"eq|{kind}|{m.group(1)}"


RESULT_FORM_ID = "genSearchRes"


def _labelled_select(form, label_text: str) -> str | None:
    """Name des `<select>`-Feldes hinter einer Beschriftung der Suchmaske."""
    label = form.find("label", string=re.compile(label_text))
    if label is None or not label.get("for"):
        return None
    base = label["for"].removesuffix("_focus")
    sel = form.find("select", id=base + "_input")
    return sel.get("name") if sel is not None else None


def _form_state(soup: BeautifulSoup,
                form_id: str = _FORM_ID) -> tuple[dict[str, str], str | None]:
    form = soup.find("form", id=form_id)
    if form is None:
        return {}, None
    data: dict[str, str] = {}
    for el in form.find_all(["input", "textarea"]):
        name = el.get("name")
        if not name or el.get("type") in ("submit", "button", "image", "checkbox", "radio"):
            continue
        data[name] = el.get("value", "")
    for el in form.find_all("select"):
        name = el.get("name")
        if not name:
            continue
        opt = el.find("option", selected=True) or el.find("option")
        data[name] = opt.get("value", "") if opt else ""
    label = form.find("label", string=re.compile("Suchbegriffe"))
    query_field = None
    if label is not None and label.get("for"):
        el = form.find(id=label["for"])
        query_field = el.get("name") if el is not None else None
    return data, query_field


def _count(html: str) -> int:
    m = _COUNT_RE.search(html)
    return int(m.group(1).replace(".", "").replace(",", "")) if m else 0


def parse_hits(html: str) -> list[Hit]:
    """Ergebniszeilen lesen. Spalten werden über die Überschriften zugeordnet."""
    soup = BeautifulSoup(html, "lxml")
    hits: dict[int, Hit] = {}
    candidates = []
    for table in soup.select("table"):
        heads = [th.get_text(" ", strip=True).lower() for th in table.select("thead th")]
        if any(h.startswith("nummer") for h in heads) and \
                any(h.startswith("titel") for h in heads):
            candidates.append((len(table.select("tbody > tr")), table, heads))
    # Die Ergebnisliste ist die Tabelle mit den meisten Zeilen; HISinOne rendert
    # daneben kleinere Tabellen mit gleichnamigen Spalten (z.B. Platzvergabe).
    for _, table, heads in sorted(candidates, key=lambda c: -c[0])[:1]:
        cols = {}
        for i, h in enumerate(heads):
            if h.startswith("nummer"):
                cols[i] = "number"
            elif h.startswith("titel"):
                cols[i] = "title"
        for tr in table.select("tbody > tr"):
            link = tr.find("a", href=re.compile(r"detailView-flow.*unitId=\d+"))
            if not link:
                continue
            uid = int(re.search(r"unitId=(\d+)", link["href"]).group(1))
            tds = tr.find_all("td", recursive=False) or tr.find_all("td")
            vals = {cols[i]: td.get_text(" ", strip=True)
                    for i, td in enumerate(tds) if i in cols}
            hits.setdefault(uid, Hit(uid, vals.get("number", ""), vals.get("title", "")))
    return list(hits.values())


def course_types(sess: ZeusSession) -> list[tuple[str, str]]:
    """Alle Werte der Auswahl "Veranstaltungsart", inkl. "(nicht gefüllt)".

    Jede Veranstaltung hat genau eine Art, deshalb zerlegt diese Liste die
    Treffermenge lückenlos – anders als eine Suche nach Nummernpräfixen.
    """
    g = sess.get_response(SEARCH_URL)
    form = BeautifulSoup(g.text, "lxml").find("form", id=_FORM_ID)
    name = _labelled_select(form, "Veranstaltungsart")
    sel = form.find("select", attrs={"name": name}) if name else None
    if sel is None:
        return []
    return [(o.get("value", ""), o.get_text(strip=True))
            for o in sel.select("option") if o.get("value")]


def search(sess: ZeusSession, term: str, query: str = "",
           course_type: str | None = None,
           rows: int = MAX_ROWS) -> tuple[int, list[Hit]]:
    """Eine Suchanfrage stellen. Rückgabe: (Trefferzahl laut Server, Zeilen)."""
    g = sess.get_response(SEARCH_URL)
    soup = BeautifulSoup(g.text, "lxml")
    data, query_field = _form_state(soup)
    if not data:
        raise RuntimeError("Suchmaske nicht gefunden")
    term_fields = [n for n in data if n.endswith("termSelect_input")]
    if term_fields:
        data[term_fields[0]] = term
    if query and query_field:
        data[query_field] = query
    if course_type is not None:
        field = _labelled_select(soup.find("form", id=_FORM_ID), "Veranstaltungsart")
        if field:
            data[field] = course_type
    data[f"{_FORM_ID}:search"] = "Suchen"
    r = sess.post(g.url, data, referer=g.url)
    total = _count(r.text)

    if total > 10 and rows > 10:
        soup = BeautifulSoup(r.text, "lxml")
        btn = soup.select_one('[name$="NumRowsRefresh"]')
        if btn is not None:
            data2, _ = _form_state(soup, RESULT_FORM_ID)
            for name in list(data2):
                if name.endswith("NumRowsInput"):
                    data2[name] = str(min(rows, MAX_ROWS))
            data2[btn["name"]] = btn.get("value", "")
            r2 = sess.post(r.url, data2, referer=r.url)
            if r2.ok and _count(r2.text) == total:
                r = r2
    return total, parse_hits(r.text)


def enumerate_units(sess: ZeusSession, term: str, prefixes: list[str],
                    progress=None) -> dict[int, Hit]:
    """Alle Veranstaltungen eines Semesters über Nummernpräfixe einsammeln.

    Scheiben mit mehr als `MAX_ROWS` Treffern werden durch Anhängen von
    `-0` … `-9` weiter zerlegt, bis sie auf eine Seite passen.
    """
    found: dict[int, Hit] = {}
    queue = list(prefixes)
    while queue:
        q = queue.pop(0)
        try:
            total, hits = search(sess, term, q)
        except Exception as e:
            log.warning("Präfix %r übersprungen: %s", q, e)
            continue
        if total > MAX_ROWS and len(q) < 12:
            queue.extend(f"{q}{d}" for d in "0123456789")
            log.info("%r: %s Treffer – wird feiner zerlegt", q, total)
            continue
        for h in hits:
            found.setdefault(h.unit_id, h)
        if progress:
            progress(q, total, len(hits), len(found))
    return found


def enumerate_by_type(sess: ZeusSession, term: str, progress=None) -> dict[int, Hit]:
    """Alle Veranstaltungen eines Semesters über die Veranstaltungsart einsammeln.

    Das ist eine echte Partition der Treffermenge. Scheiben über `MAX_ROWS`
    werden zusätzlich nach Nummernpräfix (A–Z, 0–9) zerlegt.
    """
    found: dict[int, Hit] = {}
    for value, label in course_types(sess):
        try:
            total, hits = search(sess, term, course_type=value)
        except Exception as e:      # eine Scheibe darf den Lauf nicht stoppen
            log.warning("Veranstaltungsart %r übersprungen: %s", label, e)
            continue
        if total > MAX_ROWS:
            log.info("Veranstaltungsart %r: %s Treffer – wird zerlegt", label, total)
            sub: dict[int, Hit] = {}
            for ch in "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789":
                try:
                    _, part = search(sess, term, ch, course_type=value)
                except Exception as e:
                    log.warning("Scheibe %r/%r übersprungen: %s", label, ch, e)
                    continue
                for h in part:
                    sub.setdefault(h.unit_id, h)
            hits = list(sub.values())
        for h in hits:
            found.setdefault(h.unit_id, h)
        if progress:
            progress(label, total, len(hits), len(found))
    return found
