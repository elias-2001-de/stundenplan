#!/usr/bin/env python3
"""Stundenplaner — lokale Web-App auf der ZEuS-SQLite.

Kein Netzwerkzugriff auf ZEuS: liest ausschliesslich stundenplan.sqlite3
(read-only) und schreibt den eigenen Plan nach plan.sqlite3 + plan.json.

    python3 app.py            # http://127.0.0.1:8765
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import sqlite3
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from zeus import ics

ROOT = os.path.dirname(os.path.abspath(__file__))
SRC_DB = os.path.join(ROOT, "stundenplan.sqlite3")
PLAN_DB = os.path.join(ROOT, "plan.sqlite3")
PLAN_JSON = os.path.join(ROOT, "plan.json")
WEB = os.path.join(ROOT, "web")

WD = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"]
# Blockveranstaltungen: ZEuS nennt nur den Zeitraum, nicht die echten Tage.
# Ein langer Zeitraum wuerde als Belegung den halben Katalog auf
# "evtl. Ueberschneidung" setzen und den Filter wertlos machen. Solche Bloecke
# belegen deshalb nichts automatisch; die App bietet stattdessen an, daraus
# einen echten Blocker zu machen.
SPAN_MAX_DAYS = 7
_write_lock = threading.Lock()


# ---------------------------------------------------------------- Datenbank

def src() -> sqlite3.Connection:
    """Read-only-Verbindung zur Crawler-DB (eine pro Request, WAL ist an)."""
    c = sqlite3.connect(f"file:{SRC_DB}?mode=ro", uri=True, timeout=10)
    c.row_factory = sqlite3.Row
    return c


def plan() -> sqlite3.Connection:
    c = sqlite3.connect(PLAN_DB, timeout=10)
    c.row_factory = sqlite3.Row
    return c


PLAN_SCHEMA = """
CREATE TABLE IF NOT EXISTS entries (
    period_id INTEGER NOT NULL,
    unit_id   INTEGER NOT NULL,
    idx       INTEGER NOT NULL,
    state     TEXT NOT NULL,            -- 'selected' | 'favorite'
    note      TEXT,
    added_at  TEXT,
    PRIMARY KEY (period_id, unit_id, idx)
);
CREATE TABLE IF NOT EXISTS blockers (
    id         INTEGER PRIMARY KEY,
    period_id  INTEGER NOT NULL,
    title      TEXT NOT NULL,
    kind       TEXT NOT NULL,           -- 'weekly' | 'span'
    weekday    INTEGER,                 -- 0=Mo .. 6=So (nur weekly)
    start_time TEXT NOT NULL,
    end_time   TEXT NOT NULL,
    first_date TEXT,
    last_date  TEXT
);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
"""


def init_plan() -> None:
    with plan() as p:
        p.executescript(PLAN_SCHEMA)


# ---------------------------------------------------------------- Helfer

def jloads(s, default):
    try:
        v = json.loads(s) if s else default
        return v if v is not None else default
    except Exception:
        return default


def overlaps(a1, a2, b1, b2) -> bool:
    """Halboffene Intervalle: 10:00-11:30 und 11:30-13:00 kollidieren nicht."""
    return a1 < b2 and b1 < a2


def norm_date(v):
    """Akzeptiert TT.MM.JJJJ und JJJJ-MM-TT, liefert ISO oder None."""
    v = (v or "").strip()
    if not v:
        return None
    m = re.match(r"^(\d{1,2})[./-](\d{1,2})[./-](\d{2,4})\.?$", v)
    if m:
        d, mo, y = m.groups()
        if len(y) == 2:
            y = ("19" if int(y) > 70 else "20") + y
        v = f"{y}-{int(mo):02d}-{int(d):02d}"
    try:
        return dt.date.fromisoformat(v).isoformat()
    except ValueError:
        raise ValueError(f"Datum nicht verstanden: {v!r} (erwartet TT.MM.JJJJ)")


def norm_time(v):
    """24-Stunden-Zeit, z.B. 18:45. Akzeptiert auch 1845 oder 18.45."""
    m = re.match(r"^(\d{1,2})[:. ]?(\d{2})$", (v or "").strip())
    if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
        raise ValueError(f"Uhrzeit nicht verstanden: {v!r} (erwartet 24h, z.B. 18:45)")
    return f"{int(m.group(1)):02d}:{m.group(2)}"


def daterange(first: str, last: str):
    d = dt.date.fromisoformat(first)
    end = dt.date.fromisoformat(last)
    while d <= end:
        yield d
        d += dt.timedelta(days=1)


def semester_core(con, period: int):
    """Kernzeitraum des Semesters: Tage mit nennenswert vielen Terminen.

    Die Rohdaten enthalten einzelne Ausreisser (Juli, April) – als Startwoche
    des Rasters waere das eine leere Woche."""
    r = con.execute(
        "SELECT min(d) a, max(d) b FROM (SELECT date d, count(*) n FROM occurrences"
        "  WHERE period_id=? GROUP BY date HAVING n >= 20)", (period,)).fetchone()
    return (r["a"], r["b"]) if r and r["a"] else semester_window(con, period)


def semester_window(con, period: int):
    r = con.execute(
        "SELECT min(date) a, max(date) b FROM occurrences WHERE period_id=?",
        (period,),
    ).fetchone()
    return (r["a"], r["b"]) if r and r["a"] else (None, None)


# ---------------------------------------------------------------- Kursdaten

def load_groups(con, period: int, unit_ids):
    """Parallelgruppen + Termine + Einzeltermine fuer die Veranstaltungen."""
    if not unit_ids:
        return {}
    q = ",".join("?" * len(unit_ids))
    args = [period, *unit_ids]
    groups, by_gid = {}, {}
    for g in con.execute(
        f"SELECT * FROM groups WHERE period_id=? AND is_exam=0 AND unit_id IN ({q})"
        " ORDER BY unit_id, idx", args):
        rec = {
            "gid": g["id"], "unit_id": g["unit_id"], "idx": g["idx"],
            "name": g["name"] or "", "responsible": jloads(g["responsible"], []),
            "appointments": [], "dates": [], "spans": [], "unknown": 0,
        }
        groups.setdefault(g["unit_id"], []).append(rec)
        by_gid[g["id"]] = rec

    if by_gid:
        gq = ",".join("?" * len(by_gid))
        gids = list(by_gid)
        for a in con.execute(
            f"SELECT * FROM appointments WHERE group_id IN ({gq}) ORDER BY id", gids):
            rec = by_gid[a["group_id"]]
            rec["appointments"].append({
                "rhythm": a["rhythm"] or "", "start": a["start_time"] or "",
                "end": a["end_time"] or "", "first": a["first_date"] or "",
                "last": a["last_date"] or "", "room": a["room"] or "",
                "lecturers": jloads(a["lecturers"], []), "status": a["status"],
                "note": a["note"] or "", "weekday": a["weekday"] or "",
                "cancelled": jloads(a["cancelled"], []),
            })
            if a["status"] == "span" and a["first_date"] and a["start_time"]:
                rec["spans"].append((a["first_date"], a["last_date"] or a["first_date"],
                                     a["start_time"], a["end_time"], a["room"] or ""))
            elif a["status"] == "unknown":
                rec["unknown"] += 1
        # Einzeltermine nur aus exakt aufgeloesten Terminmustern, ohne Ausfaelle
        for o in con.execute(
            f"SELECT o.group_id, o.date, o.start_time, o.end_time, o.room"
            f"  FROM occurrences o JOIN appointments a ON a.id=o.appointment_id"
            f" WHERE o.group_id IN ({gq}) AND o.cancelled=0 AND a.status='exact'"
            f" ORDER BY o.date, o.start_time", gids):
            by_gid[o["group_id"]]["dates"].append(
                (o["date"], o["start_time"], o["end_time"], o["room"] or ""))
    return groups


def course_rows(con, period, unit_ids):
    if not unit_ids:
        return {}
    q = ",".join("?" * len(unit_ids))
    out = {}
    for c in con.execute(
        f"SELECT unit_id,title,number,course_type,org_units,fields FROM courses"
        f" WHERE period_id=? AND unit_id IN ({q})", [period, *unit_ids]):
        f = jloads(c["fields"], {})
        out[c["unit_id"]] = {
            "unit_id": c["unit_id"], "title": c["title"] or "",
            "number": c["number"] or "", "course_type": c["course_type"] or "",
            "org": jloads(c["org_units"], []), "sws": f.get("Semesterwochenstunden", ""),
        }
    return out


# ---------------------------------------------------------------- Belegung

def busy_set(con, period: int):
    """Alle Zeiten, die schon vergeben sind: gewaehlte Gruppen + Blocker."""
    points: dict[str, list] = {}      # datum -> [(start, end, label)]
    spans: list = []                  # (first, last, start, end, label)
    sources = []

    with plan() as p:
        ents = p.execute(
            "SELECT unit_id, idx FROM entries WHERE period_id=? AND state='selected'",
            (period,)).fetchall()
        blockers = p.execute(
            "SELECT * FROM blockers WHERE period_id=? ORDER BY id", (period,)).fetchall()

    sel = {(e["unit_id"], e["idx"]) for e in ents}
    if sel:
        units = sorted({u for u, _ in sel})
        groups = load_groups(con, period, units)
        courses = course_rows(con, period, units)
        for uid, gs in groups.items():
            for g in gs:
                if (uid, g["idx"]) not in sel:
                    continue
                c = courses.get(uid, {})
                label = c.get("title") or c.get("number") or str(uid)
                if len(gs) > 1:
                    label += f" (PG {g['idx']})"
                key = f"u{uid}:{g['idx']}"
                sources.append({"kind": "course", "label": label,
                                "unit_id": uid, "idx": g["idx"]})
                for d, s, e, _room in g["dates"]:
                    points.setdefault(d, []).append((s, e, label, key))
                for f, l, s, e, _room in g["spans"]:
                    days = (dt.date.fromisoformat(l) - dt.date.fromisoformat(f)).days
                    if days <= SPAN_MAX_DAYS:
                        spans.append((f, l, s, e, label + " [Block]", key))

    win = semester_core(con, period)
    for b in blockers:
        label = b["title"]
        first = b["first_date"] or win[0]
        last = b["last_date"] or win[1]
        if not (first and last):
            continue
        key = f"b{b['id']}"
        sources.append({"kind": "blocker", "label": label, "id": b["id"]})
        for d in daterange(first, last):
            if b["kind"] == "weekly" and d.weekday() != b["weekday"]:
                continue
            points.setdefault(d.isoformat(), []).append(
                (b["start_time"], b["end_time"], label, key))
    return points, spans, sources


def classify(group, points, spans, skip_key=None):
    """Konflikte einer Parallelgruppe gegen die Belegung bestimmen."""
    hard, soft = {}, {}

    def add(bucket, other, date, s, e):
        k = other
        b = bucket.setdefault(k, {"with": other, "count": 0, "first": date,
                                  "time": f"{s}-{e}"})
        b["count"] += 1

    for d, s, e, _room in group["dates"]:
        for bs, be, label, key in points.get(d, ()):
            if key != skip_key and overlaps(s, e, bs, be):
                add(hard, label, d, s, e)
        for f, l, ss, se, label, key in spans:
            if key != skip_key and f <= d <= l and overlaps(s, e, ss, se):
                add(soft, label, d, s, e)

    for f, l, s, e, _room in group["spans"]:
        for d in daterange(f, l):
            ds = d.isoformat()
            for bs, be, label, key in points.get(ds, ()):
                if key != skip_key and overlaps(s, e, bs, be):
                    add(soft, label, ds, s, e)
        for bf, bl, bs, be, label, key in spans:
            if key != skip_key and bf <= l and f <= bl and overlaps(s, e, bs, be):
                add(soft, label, bf, s, e)

    if hard:
        status = "conflict"
    elif soft:
        status = "maybe"
    elif group["unknown"] or group["spans"]:
        status = "unverifiable"
    elif not group["dates"]:
        status = "notimes"
    else:
        status = "free"
    return {"status": status,
            "hard": sorted(hard.values(), key=lambda x: -x["count"]),
            "soft": sorted(soft.values(), key=lambda x: -x["count"])}


# ---------------------------------------------------------------- Export

def export_plan(period: int | None = None) -> dict:
    con = src()
    out = {"generated_at": dt.datetime.now().isoformat(timespec="seconds"),
           "semesters": []}
    with plan() as p:
        periods = [r["period_id"] for r in p.execute(
            "SELECT DISTINCT period_id FROM entries UNION"
            " SELECT DISTINCT period_id FROM blockers")]
        names = {r["period_id"]: r["name"] for r in con.execute("SELECT * FROM semesters")}
        for pid in sorted(periods, reverse=True):
            ents = p.execute("SELECT * FROM entries WHERE period_id=?", (pid,)).fetchall()
            blks = p.execute("SELECT * FROM blockers WHERE period_id=?", (pid,)).fetchall()
            units = sorted({e["unit_id"] for e in ents})
            groups = load_groups(con, pid, units)
            courses = course_rows(con, pid, units)
            items = []
            for e in ents:
                g = next((x for x in groups.get(e["unit_id"], [])
                          if x["idx"] == e["idx"]), None)
                c = courses.get(e["unit_id"], {})
                items.append({
                    "state": e["state"], "unit_id": e["unit_id"], "group_idx": e["idx"],
                    "number": c.get("number"), "title": c.get("title"),
                    "course_type": c.get("course_type"),
                    "group_name": g["name"] if g else None,
                    "lecturers": g["responsible"] if g else [],
                    "appointments": g["appointments"] if g else [],
                    "dates": [{"date": d, "start": s, "end": en, "room": r}
                              for d, s, en, r in (g["dates"] if g else [])],
                })
            out["semesters"].append({
                "period_id": pid, "name": names.get(pid),
                "entries": items,
                "blockers": [dict(b) for b in blks],
            })
    con.close()
    return out


def plan_ics(period: int, what=("selected", "favorite", "blockers")) -> tuple[str, dict]:
    """Der eigene Plan als iCalendar. Liefert Text + Zaehlerwerk fuer die UI."""
    con = src()
    with plan() as p:
        ents = p.execute("SELECT * FROM entries WHERE period_id=?", (period,)).fetchall()
        blks = p.execute("SELECT * FROM blockers WHERE period_id=? ORDER BY id",
                         (period,)).fetchall()
    ents = [e for e in ents if e["state"] in what]
    units = sorted({e["unit_id"] for e in ents})
    groups = load_groups(con, period, units)
    courses = course_rows(con, period, units)
    sem = con.execute("SELECT name FROM semesters WHERE period_id=?", (period,)).fetchone()
    win = semester_core(con, period)
    now = ics.stamp()
    body, n = [], {"termine": 0, "bloecke": 0, "blocker": 0, "ohne_termin": 0}

    for e in ents:
        g = next((x for x in groups.get(e["unit_id"], []) if x["idx"] == e["idx"]), None)
        if not g:
            continue
        c = courses.get(e["unit_id"], {})
        name = c.get("title") or c.get("number") or str(e["unit_id"])
        num = c.get("number") or ""
        mark = "" if e["state"] == "selected" else "[Favorit] "
        who = "; ".join(g["responsible"])
        # Der Gruppenname ist oft wortgleich mit dem Titel – dann weglassen.
        gname = "" if (g["name"] or "").strip() == name.strip() else g["name"]
        url = (f"https://zeus.uni-konstanz.de/hioserver/pages/startFlow.xhtml"
               f"?_flowId=detailView-flow&unitId={e['unit_id']}&periodId={period}")
        base = f"plan-{e['unit_id']}-{period}-{e['idx']}"
        # UID aus Datum+Uhrzeit, nicht aus der Position in der Liste: faellt ein
        # Termin weg (Ausfall), wuerden sonst beim erneuten Import alle
        # folgenden Eintraege im Kalender auf andere Tage rutschen.
        free = "TRANSPARENT" if e["state"] == "favorite" else ""
        for d, st_, en_, room in g["dates"]:
            n["termine"] += 1
            uid = f"{base}-{d.replace('-', '')}T{st_.replace(':', '')}@uni-konstanz.de"
            body += ics.event(uid, f"{mark}{name}",
                              date=d, start=st_, end=en_, location=room,
                              description=" / ".join(
                                  x for x in (num, gname, who) if x),
                              url=url, transp=free, now=now)
        # Blockveranstaltung: ZEuS kennt nur den Zeitraum -> ganztaegiger
        # Eintrag mit Hinweis statt eines erfundenen Einzeltermins.
        for f, l, st_, en_, room in g["spans"]:
            n["bloecke"] += 1
            body += ics.event(f"{base}-block-{f.replace('-', '')}"
                              f"-{l.replace('-', '')}@uni-konstanz.de",
                              f"[Block] {mark}{name}", date=f, end_date=l,
                              location=room,
                              description=" / ".join(
                                  x for x in (num, gname, who) if x)
                                  + f" – Blockveranstaltung {st_}–{en_} Uhr, genaue "
                                    f"Tage stehen nicht in ZEuS",
                              url=url, transp=free, now=now)
        if g["unknown"]:
            n["ohne_termin"] += g["unknown"]

    if "blockers" in what:
        for b in blks:
            first, last = b["first_date"] or win[0], b["last_date"] or win[1]
            if not (first and last):
                continue
            n["blocker"] += 1
            uid = f"plan-blocker-{b['id']}-{period}@uni-konstanz.de"
            if b["kind"] == "weekly":
                d = dt.date.fromisoformat(first)
                while d.weekday() != b["weekday"]:
                    d += dt.timedelta(days=1)
                    if d.isoformat() > last:
                        break
                if d.isoformat() > last:
                    continue
                until = dt.date.fromisoformat(last).strftime("%Y%m%dT235959")
                body += ics.event(uid, b["title"], date=d.isoformat(),
                                  start=b["start_time"], end=b["end_time"],
                                  rrule=f"FREQ=WEEKLY;UNTIL={until}",
                                  description="Blocker aus dem Stundenplaner", now=now)
            elif b["start_time"] <= "00:01" and b["end_time"] >= "23:58":
                body += ics.event(uid, b["title"], date=first, end_date=last,
                                  description="Blocker aus dem Stundenplaner", now=now)
            else:
                until = dt.date.fromisoformat(last).strftime("%Y%m%dT235959")
                body += ics.event(uid, b["title"], date=first,
                                  start=b["start_time"], end=b["end_time"],
                                  rrule=f"FREQ=DAILY;UNTIL={until}",
                                  description="Blocker aus dem Stundenplaner", now=now)
    con.close()
    title = f"Stundenplan {sem['name'] if sem else period}"
    return ics.calendar(body, name=title), n


def save_json():
    data = export_plan()
    tmp = PLAN_JSON + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, PLAN_JSON)


# ---------------------------------------------------------------- API

def api_semesters():
    con = src()
    rows = con.execute(
        "SELECT s.period_id, s.name, (SELECT count(*) FROM courses c"
        "   WHERE c.period_id=s.period_id AND c.element_type='Veranstaltung') n"
        " FROM semesters s ORDER BY s.period_id DESC").fetchall()
    out = [dict(r) for r in rows if r["n"]]
    con.close()
    return out


def api_search(qs):
    period = int(qs.get("period", [797])[0])
    text = (qs.get("q", [""])[0] or "").strip()
    ctype = (qs.get("type", [""])[0] or "").strip()
    only_free = qs.get("free", ["0"])[0] == "1"
    limit = min(int(qs.get("limit", [400])[0]), 1000)

    con = src()
    where = ["period_id=?", "element_type='Veranstaltung'"]
    args = [period]
    for tok in text.split():
        where.append("(number LIKE ? OR title LIKE ?)")
        args += [f"%{tok}%", f"%{tok}%"]
    if ctype:
        where.append("course_type=?")
        args.append(ctype)
    args.append(limit)
    rows = con.execute(
        f"SELECT unit_id FROM courses WHERE {' AND '.join(where)}"
        f" ORDER BY number, title LIMIT ?", args).fetchall()
    units = [r["unit_id"] for r in rows]

    courses = course_rows(con, period, units)
    groups = load_groups(con, period, units)
    points, spans, _ = busy_set(con, period)

    with plan() as p:
        states = {(r["unit_id"], r["idx"]): r["state"] for r in p.execute(
            "SELECT unit_id, idx, state FROM entries WHERE period_id=?", (period,))}

    result = []
    for uid in units:
        c = courses.get(uid)
        if not c:
            continue
        gl = []
        for g in groups.get(uid, []):
            state = states.get((uid, g["idx"]))
            # Gegen sich selbst nicht pruefen
            cl = classify(g, points, spans, skip_key=f"u{uid}:{g['idx']}")
            if state == "selected" and cl["status"] in ("free", "notimes"):
                cl["status"] = "selected"
            gl.append({
                "idx": g["idx"], "name": g["name"], "lecturers": g["responsible"],
                "appointments": g["appointments"], "n_dates": len(g["dates"]),
                "first": g["dates"][0][0] if g["dates"] else "",
                "last": g["dates"][-1][0] if g["dates"] else "",
                "state": state, "conflict": cl,
            })
        if only_free:
            gl = [g for g in gl if g["conflict"]["status"] != "conflict"]
            if not gl:
                continue
        c = dict(c)
        c["groups"] = gl
        result.append(c)
    con.close()
    return {"courses": result, "count": len(result)}


def api_types(qs):
    period = int(qs.get("period", [797])[0])
    con = src()
    rows = con.execute(
        "SELECT course_type, count(*) n FROM courses WHERE period_id=?"
        " AND element_type='Veranstaltung' AND course_type<>''"
        " GROUP BY 1 ORDER BY 2 DESC", (period,)).fetchall()
    con.close()
    return [{"type": r["course_type"], "n": r["n"]} for r in rows]


# Reihenfolge und Beschriftung der Grunddaten-Felder im Detail-Ausklapper.
# Was ZEuS sonst noch liefert, haengt unbeschriftet hinten dran (Rest-Logik
# unten), damit nichts stillschweigend verschwindet.
DETAIL_FIELDS = [
    ("Langtext", "Vollständiger Titel"),
    ("Kommentar", "Inhalt"),
    ("Kurzkommentar", "Hinweis"),
    ("Semesterwochenstunden", "SWS"),
    ("Angebotshäufigkeit", "Angebot"),
    ("Lehrsprache", "Lehrsprache"),
    ("Teilnahmepflicht", "Teilnahmepflicht"),
    ("Empfohlenes FS", "Empfohlenes Fachsemester"),
    ("Vorgesehenes Studiensemester", "Vorgesehenes Studiensemester"),
    ("Externe*r Veranstalter*in", "Externe*r Veranstalter*in"),
    ("Links", "Links"),
]
# Diese Felder stehen schon woanders im Detail (Kopf, Veranstalter, Fristen).
DETAIL_SKIP = {"Titel", "Kurztext", "Nummer", "Veranstaltungsart",
               "Organisationseinheit", "Einrichtungen", "Zeitraum", "Zeiträume"}


def detail_groups(con, period: int, unit_id: int, is_exam: int):
    """Parallelgruppen (oder Pruefungstermine) mit allen Einzelterminen."""
    out = []
    rows = con.execute(
        "SELECT * FROM groups WHERE period_id=? AND unit_id=? AND is_exam=?"
        " ORDER BY idx", (period, unit_id, is_exam)).fetchall()
    for g in rows:
        appts = []
        for a in con.execute(
            "SELECT * FROM appointments WHERE group_id=? ORDER BY id", (g["id"],)):
            appts.append({
                "rhythm": a["rhythm"] or "", "weekday": a["weekday"] or "",
                "start": a["start_time"] or "", "end": a["end_time"] or "",
                "first": a["first_date"] or "", "last": a["last_date"] or "",
                "room": a["room"] or "", "note": a["note"] or "",
                "lecturers": jloads(a["lecturers"], []),
                "cancelled": jloads(a["cancelled"], []), "status": a["status"],
            })
        dates = [{"date": o["date"], "start": o["start_time"] or "",
                  "end": o["end_time"] or "", "room": o["room"] or "",
                  "cancelled": bool(o["cancelled"])}
                 for o in con.execute(
                     "SELECT * FROM occurrences WHERE group_id=?"
                     " ORDER BY date, start_time", (g["id"],))]
        out.append({"idx": g["idx"], "name": g["name"] or "",
                    "responsible": jloads(g["responsible"], []),
                    "appointments": appts, "dates": dates})
    return out


def api_course(qs):
    """Alles, was die DB zu einer Veranstaltung hergibt (Detail-Ausklapper)."""
    period = int(qs.get("period", [797])[0])
    unit = int(qs.get("unit", [0])[0])
    con = src()
    try:
        c = con.execute(
            "SELECT * FROM courses WHERE period_id=? AND unit_id=?",
            (period, unit)).fetchone()
        if not c:
            # kein "error": das wuerde im Frontend eine Alert-Box ausloesen,
            # obwohl nur ein Ausklapper leer bleibt.
            return {"missing": True}
        f = jloads(c["fields"], {})
        inhalte = jloads(c["contents"], {})

        info = []
        used = set(DETAIL_SKIP)
        for key, label in DETAIL_FIELDS:
            val = (f.get(key) or "").strip()
            used.add(key)
            if not val or (key == "Langtext" and val == (c["title"] or "").strip()):
                continue
            info.append({"label": label, "value": val})
        for key, val in f.items():          # was wir nicht kennen, trotzdem zeigen
            if key not in used and (val or "").strip():
                info.append({"label": key, "value": val.strip()})

        # "Belegfrist … von … bis …; Abmeldefrist …" – am Semikolon aufteilen
        fristen = []
        for key in ("Zeitraum", "Zeiträume"):
            for part in (f.get(key) or "").split(";"):
                part = part.strip()
                if part and part not in fristen:
                    fristen.append(part)

        org = jloads(c["org_units"], [])
        for part in (f.get("Einrichtungen") or "").split(";"):
            part = part.strip()
            if part and part not in org:
                org.append(part)

        return {
            "unit_id": unit, "period_id": period,
            "title": c["title"] or "", "number": c["number"] or "",
            "course_type": c["course_type"] or "",
            "element_type": c["element_type"] or "",
            "url": c["url"] or "", "fetched_at": c["fetched_at"] or "",
            "info": info, "fristen": fristen, "org": org,
            "groups": detail_groups(con, period, unit, 0),
            "exams": detail_groups(con, period, unit, 1),
            # Registerkarte "Inhalte" (Lauf `python -m zeus.cli contents`).
            # Fehlt sie oder ist sie nur halb gefuellt, zeigt das Frontend
            # einfach das, was da ist.
            "contents": inhalte.get("sections") or {},
            "requirements": inhalte.get("requirements") or [],
            "has_contents": c["contents"] is not None,
        }
    finally:
        con.close()


def api_state(qs):
    period = int(qs.get("period", [797])[0])
    con = src()
    with plan() as p:
        ents = p.execute("SELECT * FROM entries WHERE period_id=?", (period,)).fetchall()
        blks = p.execute("SELECT * FROM blockers WHERE period_id=? ORDER BY id",
                         (period,)).fetchall()
    units = sorted({e["unit_id"] for e in ents})
    groups = load_groups(con, period, units)
    courses = course_rows(con, period, units)
    points, spans, _ = busy_set(con, period)

    items = []
    for e in ents:
        g = next((x for x in groups.get(e["unit_id"], []) if x["idx"] == e["idx"]), None)
        c = courses.get(e["unit_id"], {})
        if not g:
            cl = {"status": "gone", "hard": [], "soft": []}
        else:
            cl = classify(g, points, spans, skip_key=f"u{e['unit_id']}:{e['idx']}")
            if e["state"] == "selected" and cl["status"] == "free":
                cl["status"] = "ok"
        wide = []
        if g:
            for f, l, st_, en_, _room in g["spans"]:
                if (dt.date.fromisoformat(l) - dt.date.fromisoformat(f)).days > SPAN_MAX_DAYS:
                    wide.append({"first": f, "last": l, "start": st_, "end": en_})
        items.append({
            "wide_spans": wide,
            "state": e["state"], "unit_id": e["unit_id"], "idx": e["idx"],
            "number": c.get("number", ""), "title": c.get("title", "?"),
            "course_type": c.get("course_type", ""),
            "group_name": g["name"] if g else "(nicht mehr in der DB)",
            "lecturers": g["responsible"] if g else [],
            "appointments": g["appointments"] if g else [],
            "n_dates": len(g["dates"]) if g else 0,
            "conflict": cl,
        })
    items.sort(key=lambda x: (x["state"] != "selected", x["number"]))
    win = semester_core(con, period)
    name = con.execute("SELECT name FROM semesters WHERE period_id=?",
                       (period,)).fetchone()
    con.close()
    return {"period_id": period, "semester": name["name"] if name else str(period),
            "window": {"first": win[0], "last": win[1]},
            "entries": items, "blockers": [dict(b) for b in blks]}


def api_week(qs):
    period = int(qs.get("period", [797])[0])
    start = norm_date(qs.get("start", [""])[0]) or ""
    con = src()
    if not start:
        core = semester_core(con, period)
        today = dt.date.today().isoformat()
        start = core[0] or today
        if core[0] and core[1] and core[0] <= today <= core[1]:
            start = today            # laufendes Semester: aktuelle Woche
        else:
            pts, _sp, _src = busy_set(con, period)
            future = sorted(d for d in pts if d >= start)
            if future:
                start = future[0]    # erste Woche, in der wirklich etwas liegt
    d0 = dt.date.fromisoformat(start)
    d0 -= dt.timedelta(days=d0.weekday())
    d1 = d0 + dt.timedelta(days=6)

    with plan() as p:
        ents = p.execute(
            "SELECT unit_id, idx, state FROM entries WHERE period_id=?",
            (period,)).fetchall()
        blks = p.execute("SELECT * FROM blockers WHERE period_id=?", (period,)).fetchall()

    units = sorted({e["unit_id"] for e in ents})
    groups = load_groups(con, period, units)
    courses = course_rows(con, period, units)
    state_of = {(e["unit_id"], e["idx"]): e["state"] for e in ents}

    ev = []
    for uid, gs in groups.items():
        for g in gs:
            st = state_of.get((uid, g["idx"]))
            if not st:
                continue
            c = courses.get(uid, {})
            title = c.get("title") or c.get("number")
            for d, s, e, room in g["dates"]:
                if d0.isoformat() <= d <= d1.isoformat():
                    ev.append({"kind": st, "date": d, "start": s, "end": e,
                               "room": room, "title": title,
                               "number": c.get("number", ""),
                               "subtitle": c.get("title", ""), "unit_id": uid,
                               "idx": g["idx"]})
            for f, l, s, e, room in g["spans"]:
                for dd in daterange(max(f, d0.isoformat()), min(l, d1.isoformat())) \
                        if f <= d1.isoformat() and l >= d0.isoformat() else []:
                    ev.append({"kind": st, "date": dd.isoformat(), "start": s, "end": e,
                               "room": room, "title": title + " [Block?]",
                               "number": c.get("number", ""),
                               "subtitle": c.get("title", ""), "unit_id": uid,
                               "idx": g["idx"], "uncertain": True})
    win_all = semester_core(con, period)
    for b in blks:
        first = b["first_date"] or win_all[0] or d0.isoformat()
        last = b["last_date"] or win_all[1] or d1.isoformat()
        for d in daterange(max(first, d0.isoformat()), min(last, d1.isoformat())) \
                if first <= d1.isoformat() and last >= d0.isoformat() else []:
            if b["kind"] == "weekly" and d.weekday() != b["weekday"]:
                continue
            ev.append({"kind": "blocker", "date": d.isoformat(),
                       "start": b["start_time"], "end": b["end_time"],
                       "title": b["title"], "subtitle": "Blocker", "room": "",
                       "blocker_id": b["id"]})
    win = semester_window(con, period)
    con.close()
    ev.sort(key=lambda e: (e["date"], e["start"]))
    return {"start": d0.isoformat(), "end": d1.isoformat(), "events": ev,
            "window": {"first": win[0], "last": win[1]}}


def api_set_entry(body):
    period = int(body["period"]); uid = int(body["unit_id"]); idx = int(body["idx"])
    state = body.get("state")
    with _write_lock, plan() as p:
        if state in (None, "", "none"):
            p.execute("DELETE FROM entries WHERE period_id=? AND unit_id=? AND idx=?",
                      (period, uid, idx))
        else:
            if state not in ("selected", "favorite"):
                raise ValueError("state")
            p.execute(
                "INSERT INTO entries (period_id,unit_id,idx,state,added_at)"
                " VALUES (?,?,?,?,?) ON CONFLICT(period_id,unit_id,idx)"
                " DO UPDATE SET state=excluded.state",
                (period, uid, idx, state, dt.datetime.now().isoformat(timespec="seconds")))
    save_json()
    return {"ok": True}


def api_blocker(body):
    with _write_lock, plan() as p:
        if body.get("delete"):
            p.execute("DELETE FROM blockers WHERE id=?", (int(body["id"]),))
        else:
            kind = body.get("kind", "weekly")
            t0, t1 = norm_time(body["start_time"]), norm_time(body["end_time"])
            if t0 >= t1:
                raise ValueError("Ende muss nach dem Beginn liegen")
            d0, d1 = norm_date(body.get("first_date")), norm_date(body.get("last_date"))
            if d0 and d1 and d0 > d1:
                raise ValueError("Enddatum liegt vor dem Startdatum")
            vals = (body.get("title", "").strip() or "Blocker", kind,
                    int(body["weekday"]) if kind == "weekly" else None,
                    t0, t1, d0, d1)
            if body.get("id"):
                cur = p.execute(
                    "UPDATE blockers SET title=?,kind=?,weekday=?,start_time=?,"
                    "end_time=?,first_date=?,last_date=? WHERE id=? AND period_id=?",
                    (*vals, int(body["id"]), int(body["period"])))
                if not cur.rowcount:
                    raise ValueError(f"Blocker {body['id']} gibt es nicht")
            else:
                p.execute(
                    "INSERT INTO blockers (period_id,title,kind,weekday,start_time,"
                    "end_time,first_date,last_date) VALUES (?,?,?,?,?,?,?,?)",
                    (int(body["period"]), *vals))
    save_json()
    return {"ok": True}


# ---------------------------------------------------------------- HTTP

class Handler(BaseHTTPRequestHandler):
    server_version = "Stundenplaner/1.0"

    def log_message(self, fmt, *args):
        sys.stderr.write("%s %s\n" % (self.address_string(), fmt % args))

    def _send(self, code, body: bytes, ctype="application/json; charset=utf-8",
              extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"))

    def do_GET(self):
        u = urlparse(self.path)
        qs = parse_qs(u.query)
        try:
            if u.path == "/" or u.path == "/index.html":
                return self._file("index.html", "text/html; charset=utf-8")
            if u.path == "/app.js":
                return self._file("app.js", "application/javascript; charset=utf-8")
            if u.path == "/style.css":
                return self._file("style.css", "text/css; charset=utf-8")
            if u.path == "/api/semesters":
                return self._json(api_semesters())
            if u.path == "/api/types":
                return self._json(api_types(qs))
            if u.path == "/api/search":
                return self._json(api_search(qs))
            if u.path == "/api/course":
                return self._json(api_course(qs))
            if u.path == "/api/state":
                return self._json(api_state(qs))
            if u.path == "/api/week":
                return self._json(api_week(qs))
            if u.path == "/api/ics":
                period = int(qs.get("period", [797])[0])
                what = (qs.get("what", ["selected,favorite,blockers"])[0]).split(",")
                text, cnt = plan_ics(period, what)
                fn = f"stundenplan-{period}.ics"
                return self._send(200, text.encode("utf-8"),
                                  "text/calendar; charset=utf-8",
                                  {"Content-Disposition": f'attachment; filename="{fn}"',
                                   "X-Plan-Counts": json.dumps(cnt)})
            if u.path == "/api/ics/stats":
                period = int(qs.get("period", [797])[0])
                what = (qs.get("what", ["selected,favorite,blockers"])[0]).split(",")
                return self._json(plan_ics(period, what)[1])
            if u.path == "/api/export":
                body = json.dumps(export_plan(), ensure_ascii=False,
                                  indent=2).encode("utf-8")
                return self._send(200, body, extra={
                    "Content-Disposition": 'attachment; filename="plan.json"'})
            self._json({"error": "not found"}, 404)
        except Exception as exc:  # noqa: BLE001
            import traceback; traceback.print_exc()
            self._json({"error": f"{type(exc).__name__}: {exc}"}, 500)

    def do_POST(self):
        u = urlparse(self.path)
        try:
            n = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(n) or b"{}")
            if u.path == "/api/entry":
                return self._json(api_set_entry(body))
            if u.path == "/api/blocker":
                return self._json(api_blocker(body))
            self._json({"error": "not found"}, 404)
        except Exception as exc:  # noqa: BLE001
            import traceback; traceback.print_exc()
            self._json({"error": f"{type(exc).__name__}: {exc}"}, 500)

    def _file(self, name, ctype):
        path = os.path.join(WEB, name)
        with open(path, "rb") as fh:
            self._send(200, fh.read(), ctype)


def main():
    if not os.path.exists(SRC_DB):
        sys.exit(f"Datenbank fehlt: {SRC_DB}")
    init_plan()
    port = int(os.environ.get("PORT", 8765))
    host = os.environ.get("HOST", "127.0.0.1")
    srv = ThreadingHTTPServer((host, port), Handler)
    print(f"Stundenplaner laeuft:  http://{host}:{port}")
    print(f"  Kursdaten (read-only): {SRC_DB}")
    print(f"  Eigener Plan:          {PLAN_DB}  +  {PLAN_JSON}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nTschuess.")


if __name__ == "__main__":
    main()
