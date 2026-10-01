"""Parst die öffentliche Detailansicht einer Veranstaltung (detailView-flow)."""
from __future__ import annotations

import copy
import re
import urllib.parse
from dataclasses import dataclass, field

from bs4 import BeautifulSoup, Tag

from .session import START_FLOW, ZeusSession

DETAIL_FLOW = "detailView-flow"
DETAIL_FORM = "detailViewData"
# Die Registerkarte "Inhalte" wird nicht mitgeliefert, sondern per Formular-POST
# nachgeladen (JSF/myfaces: Buttonname als Parameter im Formular "detailViewData").
CONTENTS_TAB = "detailViewData:tabContainer:term-planning-container:tabs:contentsTab"
# Fieldsets, die auf jeder Registerkarte stehen und keine Inhaltsabschnitte sind.
_FRAME_LEGENDS = {"Semesterauswahl", "Semesterplanung", "Grunddaten", "Termine"}

_TIME_RE = re.compile(r"(\d{1,2}:\d{2})\s*[-–]\s*(\d{1,2}:\d{2})")
_DATE_RE = re.compile(r"(\d{2}\.\d{2}\.\d{4})")

# Spaltenüberschriften der Termintabelle -> Feldname. Wir matchen auf Präfix,
# weil die Überschriften Zusätze wie "[Sortierbare Spalte]" tragen.
_COLS = {
    "rhythmus": "rhythm",
    "wochentag": "weekday",
    "von - bis": "time",
    "ausfalltermin": "cancelled",
    "startdatum - enddatum": "dates",
    "erw. tn.": "expected_participants",
    "bemerkung": "note",
    "durchführende*r": "lecturers",
    "raum": "room",
    "gebäude": "building",
    "max. tn.": "max_participants",
}

# Prüfungstermin-Tabellen (Container "examinationPeriod_*") haben eigene Spalten.
_EXAM_COLS = {
    "wochentag": "weekday",
    "von - bis": "time",
    "prüfungsdatum": "dates",
    "beginn der anmeldefrist": "registration_start",
    "ende der anmeldefrist": "registration_end",
    "prüfer*in": "lecturers",
    "raum": "room",
    "bemerkung": "note",
}


@dataclass
class Appointment:
    """Eine Zeile der Termintabelle einer Parallelgruppe (Terminmuster)."""

    rhythm: str = ""
    weekday: str = ""
    start_time: str | None = None
    end_time: str | None = None
    first_date: str | None = None   # ISO
    last_date: str | None = None    # ISO
    cancelled: list[str] = field(default_factory=list)   # ISO-Daten
    room: str = ""
    lecturers: list[str] = field(default_factory=list)
    note: str = ""
    expected_participants: str = ""
    raw: dict[str, str] = field(default_factory=dict)


@dataclass
class ParallelGroup:
    name: str = ""
    index: int = 0
    responsible: list[str] = field(default_factory=list)
    appointments: list[Appointment] = field(default_factory=list)
    fields: dict[str, str] = field(default_factory=dict)
    is_exam: bool = False           # Prüfungstermine statt Lehrveranstaltungsterminen


@dataclass
class Course:
    unit_id: int
    period_id: int
    title: str = ""
    number: str = ""
    element_type: str = ""         # "Veranstaltung", "Modul", "Prüfung", …
    course_type: str = ""          # Veranstaltungsart
    org_units: list[str] = field(default_factory=list)
    frequency: str = ""            # Angebotshäufigkeit
    fields: dict[str, str] = field(default_factory=dict)
    groups: list[ParallelGroup] = field(default_factory=list)
    exam_groups: list[ParallelGroup] = field(default_factory=list)
    # Registerkarte "Inhalte": None = nicht abgerufen, {} = abgerufen und leer.
    contents: dict[str, str] | None = None
    requirements: list[dict[str, str]] | None = None

    @property
    def url(self) -> str:
        return detail_url(self.unit_id, self.period_id)


def detail_url(unit_id: int, period_id: int) -> str:
    return f"{START_FLOW}?" + urllib.parse.urlencode(
        {"_flowId": DETAIL_FLOW, "unitId": str(unit_id), "periodId": str(period_id)}
    )


# -- Hilfsfunktionen ----------------------------------------------------
def _iso(d: str) -> str:
    dd, mm, yy = d.split(".")
    return f"{yy}-{mm}-{dd}"


def _label_values(scope: Tag) -> dict[str, str]:
    """Alle `Label: Wert`-Paare eines Abschnitts (div.labelItemLine)."""
    out: dict[str, str] = {}
    for line in scope.select("div.labelItemLine"):
        label = line.find("label")
        answer = line.select_one("div.answer")
        if not label or not answer:
            continue
        key = label.get_text(" ", strip=True).rstrip(":").strip()
        items = [li.get_text(" ", strip=True) for li in answer.select("li")]
        val = "; ".join(items) if items else answer.get_text(" ", strip=True)
        if key and key not in out:
            out[key] = val
    return out


def _col_map(table: Tag, mapping: dict[str, str]) -> dict[int, str]:
    cols: dict[int, str] = {}
    for i, th in enumerate(table.select("thead th")):
        head = th.get_text(" ", strip=True).replace("\xa0", " ")
        head = re.sub(r"\[.*?\]", "", head).strip().lower()
        for prefix, name in mapping.items():
            if head.startswith(prefix):
                cols[i] = name
                break
    return cols


def _cell_list(td: Tag) -> list[str]:
    """Zellinhalt als Liste: <li>-Elemente einzeln, sonst an Komma getrennt."""
    items = [li.get_text(" ", strip=True) for li in td.select("li")]
    if items:
        return [i for i in items if i]
    txt = td.get_text(" ", strip=True).replace("\xa0", " ").strip()
    return [p.strip() for p in txt.split(",") if p.strip()] if txt else []


def _parse_appointments(container: Tag, *, is_exam: bool = False) -> list[Appointment]:
    """Termintabelle einer Parallelgruppe lesen (Spalten über Überschriften)."""
    tables = [t for t in container.select("table") if t.select("thead th")]
    if not tables:
        return []
    # innerste Tabelle nehmen: HISinOne schachtelt Wrapper-Tabellen
    table = min(tables, key=lambda t: len(t.select("table")))
    cols = _col_map(table, _EXAM_COLS if is_exam else _COLS)
    if not cols:
        return []
    out: list[Appointment] = []
    for tr in table.select("tbody > tr"):
        tds = tr.find_all("td", recursive=False) or tr.find_all("td")
        if not tds:
            continue
        raw = {}
        for i, td in enumerate(tds):
            name = cols.get(i)
            if name:
                raw[name] = td.get_text(" ", strip=True).replace("\xa0", " ").strip()
        if not raw or not any(raw.values()):
            continue
        ap = Appointment(rhythm=raw.get("rhythm", ""), weekday=raw.get("weekday", ""),
                         room=raw.get("room", ""), note=raw.get("note", ""),
                         expected_participants=raw.get("expected_participants", ""),
                         raw=raw)
        if m := _TIME_RE.search(raw.get("time", "")):
            ap.start_time, ap.end_time = m.group(1), m.group(2)
        dates = _DATE_RE.findall(raw.get("dates", ""))
        if dates:
            ap.first_date = _iso(dates[0])
            ap.last_date = _iso(dates[-1])
        ap.cancelled = [_iso(d) for d in _DATE_RE.findall(raw.get("cancelled", ""))]
        for i, td in enumerate(tds):
            if cols.get(i) == "lecturers":
                ap.lecturers = _cell_list(td)
        if is_exam:
            ap.rhythm = ap.rhythm or "Prüfungstermin"
        out.append(ap)
    return out


def _rich_text(tag: Tag) -> str:
    """Fliesstext eines Abschnitts mit Zeilenumbruechen.

    get_text() wuerde die <br>-getrennte Wochentabelle unter "Inhalte" und die
    Aufzaehlungen unter "Lernziele" zu einer einzigen Zeile verkleben.
    """
    tag = copy.copy(tag)
    for junk in tag.select('[id$="helpPlaceholder"], legend'):
        junk.decompose()
    # Umbrueche im Quelltext sind in HTML nur Leerraum – erst platt machen,
    # sonst wird aus "<br>\n" spaeter eine Leerzeile.
    for node in tag.find_all(string=True):
        flat = re.sub(r"\s+", " ", str(node))
        if flat != str(node):
            node.replace_with(flat)
    for br in tag.find_all("br"):
        br.replace_with("\n")
    for li in tag.find_all("li"):
        li.insert_before("\n• ")
        li.unwrap()
    for blk in tag.find_all(["p", "div", "tr", "h1", "h2", "h3", "h4", "h5"]):
        blk.insert_before("\n")
        blk.insert_after("\n")
    text = tag.get_text("", strip=False).replace("\xa0", " ")
    lines = [re.sub(r"[ \t]+", " ", ln).strip() for ln in text.split("\n")]
    out: list[str] = []
    for ln in lines:                       # Leerzeilen nicht haeufen
        if ln or (out and out[-1]):
            out.append(ln)
    return "\n".join(out).strip()


def _requirements(scope: Tag) -> list[dict[str, str]]:
    """Tabelle "Zu erbringende Prüfungsleistung/Studienleistung" (ECTS, Form, …)."""
    table = scope.find("table", id="rudi")
    if table is None:
        return []
    heads = [th.get_text(" ", strip=True).replace("\xa0", " ")
             for th in table.select("thead th")]
    # "ECTS", "Art (Type of event)" -> nur der deutsche Teil vor der Klammer
    heads = [re.sub(r"\s*\(.*?\)\s*$", "", h).strip() for h in heads]
    rows = []
    for tr in table.select("tbody > tr"):
        cells = [td.get_text(" ", strip=True).replace("\xa0", " ").strip()
                 for td in tr.find_all("td")]
        if not any(cells):                 # HISinOne haengt leere Zeilen an
            continue
        rows.append({heads[i] if i < len(heads) else str(i): v
                     for i, v in enumerate(cells) if v})
    return rows


def parse_contents(html: str) -> tuple[dict[str, str], list[dict[str, str]]]:
    """Registerkarte "Inhalte": Abschnitt (legend) -> Text, plus Leistungstabelle.

    Jeder Abschnitt ist ein <fieldset> mit <legend>. Rahmen-Fieldsets (die
    Registerkarten-Huelle) erkennt man daran, dass sie weitere beschriftete
    Fieldsets enthalten; der Rest steht in `_FRAME_LEGENDS`. Ueber die id laesst
    sich das nicht machen: JSF vergibt sie je View neu.
    """
    soup = BeautifulSoup(html, "lxml")
    scope = soup.select_one('[id$="term-planning-container"]') or soup
    sections: dict[str, str] = {}
    for fs in scope.select("fieldset"):
        legend = fs.find("legend")
        if legend is None:
            continue
        if any(inner.find("legend") for inner in fs.select("fieldset")):
            continue                        # Huelle, kein Abschnitt
        name = legend.get_text(" ", strip=True)
        if not name or name in _FRAME_LEGENDS or name in sections:
            continue
        if fs.find("table", id="rudi") is not None:
            continue                        # steht strukturiert in requirements
        text = _rich_text(fs)
        if text:
            sections[name] = text
    return sections, _requirements(scope)


def _header(soup: BeautifulSoup) -> tuple[str, str, str]:
    """Kopfzeile "Titel | Nummer | Elementtyp" der Detailansicht zerlegen."""
    head = soup.select_one(".dialogHeaderDataBox") or soup.select_one("#dialogHeader")
    if head is None:
        return "", "", ""
    text = head.get_text(" ", strip=True)
    for stop in (" Zurück", " Permalink:"):
        if stop in text:
            text = text.split(stop)[0]
    parts = [p.strip() for p in text.split("|")]
    title = parts[0] if parts else ""
    number = parts[1] if len(parts) > 2 else ""
    etype = parts[-1] if len(parts) > 1 else ""
    return title, number, etype


def parse(html: str, unit_id: int, period_id: int) -> Course:
    soup = BeautifulSoup(html, "lxml")
    course = Course(unit_id=unit_id, period_id=period_id)

    h_title, h_number, course.element_type = _header(soup)

    # Veranstaltungen nutzen "basicDataTabfieldsetId", Module nur "basicData".
    basic = (soup.select_one('[id*="basicDataTabfieldsetId"]')
             or soup.select_one('[id$=":basicData"]')
             or soup.select_one('[id*=":basicData:"]'))
    if basic is not None:
        course.fields = _label_values(basic)
    course.title = course.fields.get("Titel", "")
    course.number = course.fields.get("Nummer", "")
    course.course_type = course.fields.get("Veranstaltungsart", "")
    course.frequency = course.fields.get("Angebotshäufigkeit", "")
    org = course.fields.get("Organisationseinheit", "")
    course.org_units = [o.strip() for o in org.split(";") if o.strip()]

    course.title = course.title or h_title
    course.number = course.number or h_number

    # Parallelgruppen: Container-IDs ...parallelGroupSchedule_<n>.
    # Liegt "examinationPeriod_" im Pfad, sind es Prüfungs-, keine Lehrtermine.
    # Pruefungstermine sind nach Pruefungsperiode gruppiert und fangen in jeder
    # Periode wieder bei parallelGroupSchedule_1 an. Der Index ist aber Teil des
    # Primaerschluessels in der DB, also zaehlen wir sie durchgehend weiter.
    seen: set[str] = set()
    exam_n = 0
    for div in soup.select('[id*="parallelGroupSchedule_"]'):
        gid = div.get("id", "")
        m = re.search(r"parallelGroupSchedule_(\d+)$", gid)
        if not m or gid in seen:
            continue
        seen.add(gid)
        is_exam = "examinationPeriod_" in gid
        if is_exam:
            exam_n += 1
        group = ParallelGroup(index=exam_n if is_exam else int(m.group(1)),
                              is_exam=is_exam)
        legend = div.find("legend")
        group.name = legend.get_text(" ", strip=True) if legend else ""
        basics = div.select_one('[id*="basicDataFieldset"], [id*="basicData"]')
        group.fields = _label_values(basics if basics is not None else div)
        resp = group.fields.get("Verantwortliche*r") or group.fields.get("Verantwortlicher", "")
        group.responsible = [p.strip() for p in resp.split(";") if p.strip()] if resp else []
        appt = div.select_one('[id*="appointmentsFieldset"]')
        group.appointments = _parse_appointments(appt if appt is not None else div,
                                                 is_exam=is_exam)
        (course.exam_groups if is_exam else course.groups).append(group)

    course.groups.sort(key=lambda g: g.index)
    course.exam_groups.sort(key=lambda g: g.index)
    return course


def fetch(sess: ZeusSession, unit_id: int, period_id: int) -> Course:
    return parse(sess.get(detail_url(unit_id, period_id)), unit_id, period_id)


def _form_state(soup: BeautifulSoup) -> dict[str, str]:
    """Versteckte Felder des Detail-Formulars (ViewState, Flow-Key, …)."""
    form = soup.find("form", id=DETAIL_FORM)
    if form is None:
        return {}
    data: dict[str, str] = {}
    for el in form.find_all(["input", "textarea"]):
        name = el.get("name")
        if name and el.get("type") not in ("submit", "button", "image",
                                           "checkbox", "radio"):
            data[name] = el.get("value", "")
    for el in form.find_all("select"):
        name = el.get("name")
        if not name:
            continue
        opt = el.find("option", selected=True) or el.find("option")
        data[name] = opt.get("value", "") if opt else ""
    return data


def _contents_once(sess: ZeusSession, unit_id: int, period_id: int):
    """Ein Versuch: Detailseite holen, Registerkarte posten, Antwort prüfen.

    Liefert (sections, requirements, umgeschaltet). `umgeschaltet` ist False,
    wenn ZEuS weiter die Termin-Registerkarte zurückgibt – dann wäre ein leeres
    Ergebnis nur ein Artefakt des Flow-States, kein echtes "keine Inhalte".
    """
    r = sess.get_response(detail_url(unit_id, period_id))
    soup = BeautifulSoup(r.text, "lxml")
    button = soup.find(id=CONTENTS_TAB)
    form = soup.find("form", id=DETAIL_FORM)
    if button is None or form is None:
        return {}, [], True          # Registerkarte gibt es hier schlicht nicht
    data = _form_state(soup)
    if not data:
        return {}, [], True
    data[CONTENTS_TAB] = button.get("value", "Inhalte")
    data["DISABLE_VALIDATION"] = "true"
    action = urllib.parse.urljoin(r.url, form.get("action"))
    html = sess.post(action, data, referer=r.url).text
    after = BeautifulSoup(html, "lxml").find(id=CONTENTS_TAB)
    active = after is not None and "active" in (after.get("class") or [])
    sections, reqs = parse_contents(html)
    return sections, reqs, active


def fetch_contents(sess: ZeusSession, unit_id: int,
                   period_id: int) -> tuple[dict[str, str], list[dict[str, str]]]:
    """Registerkarte "Inhalte" nachladen (Beschreibung, Lernziele, Literatur …).

    Zwei Requests: die Detailseite **ungecacht** holen (der Flow-Key einer alten
    Seite ist ungültig), dann das Formular mit dem Registerkarten-Button posten.
    Schaltet ZEuS die Registerkarte nicht um, wird es mit frischer Session noch
    einmal versucht – sonst landen stillschweigend leere Inhalte in der DB.
    """
    sections, reqs, active = _contents_once(sess, unit_id, period_id)
    if not active:
        sess._new_session()
        sections, reqs, active = _contents_once(sess, unit_id, period_id)
        if not active:
            # Weiter die Termin-Registerkarte: lieber nichts als das, was
            # zufaellig von der falschen Seite haengenbleibt.
            return {}, []
    return sections, reqs
