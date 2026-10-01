"""SQLite-Persistenz für Kurse, Parallelgruppen, Terminmuster und Einzeltermine."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .catalog import CatalogCrawl
from .detail import Course
from .expand import expand

SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS semesters (
    period_id INTEGER PRIMARY KEY,
    name      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS courses (
    unit_id     INTEGER NOT NULL,
    period_id   INTEGER NOT NULL,
    title        TEXT,
    number       TEXT,
    element_type TEXT,     -- Veranstaltung | Modul | …
    course_type  TEXT,
    org_units   TEXT,      -- JSON-Liste
    frequency   TEXT,
    fields      TEXT,      -- JSON-Objekt aller Grunddaten-Felder
    contents    TEXT,      -- JSON: Registerkarte "Inhalte" (NULL = nie abgerufen)
    url         TEXT,
    fetched_at  TEXT,
    PRIMARY KEY (unit_id, period_id)
);
CREATE INDEX IF NOT EXISTS idx_courses_number ON courses(number);
CREATE INDEX IF NOT EXISTS idx_courses_title  ON courses(title);

CREATE TABLE IF NOT EXISTS catalog_nodes (
    period_id INTEGER NOT NULL,
    path      TEXT NOT NULL,
    name      TEXT,
    kind      TEXT,
    parent    TEXT,
    unit_id   INTEGER,
    PRIMARY KEY (period_id, path)
);
CREATE INDEX IF NOT EXISTS idx_nodes_unit ON catalog_nodes(unit_id);

CREATE TABLE IF NOT EXISTS groups (
    id          INTEGER PRIMARY KEY,
    unit_id     INTEGER NOT NULL,
    period_id   INTEGER NOT NULL,
    idx         INTEGER,
    name        TEXT,
    is_exam     INTEGER DEFAULT 0,
    responsible TEXT,    -- JSON-Liste
    UNIQUE (unit_id, period_id, idx, is_exam)
);

CREATE TABLE IF NOT EXISTS appointments (
    id         INTEGER PRIMARY KEY,
    group_id   INTEGER NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
    rhythm     TEXT,
    weekday    TEXT,
    start_time TEXT,
    end_time   TEXT,
    first_date TEXT,
    last_date  TEXT,
    room       TEXT,
    lecturers  TEXT,     -- JSON-Liste
    note       TEXT,
    cancelled  TEXT,     -- JSON-Liste
    status     TEXT      -- exact | span | unknown (Auflösung des Rhythmus)
);

CREATE TABLE IF NOT EXISTS occurrences (
    id             INTEGER PRIMARY KEY,
    appointment_id INTEGER NOT NULL REFERENCES appointments(id) ON DELETE CASCADE,
    unit_id        INTEGER NOT NULL,
    period_id      INTEGER NOT NULL,
    group_id       INTEGER NOT NULL,
    date           TEXT NOT NULL,
    start_time     TEXT,
    end_time       TEXT,
    room           TEXT,
    cancelled      INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_occ_date ON occurrences(period_id, date);
CREATE INDEX IF NOT EXISTS idx_occ_unit ON occurrences(unit_id, period_id);
"""


class Store:
    def __init__(self, path: Path | str = "stundenplan.sqlite3"):
        self.path = Path(path)
        self.db = sqlite3.connect(self.path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self._migrate()

    def _migrate(self) -> None:
        """Fehlende Spalten in bestehenden Datenbanken nachziehen."""
        have = {r[1] for r in self.db.execute("PRAGMA table_info(courses)")}
        for col in ("element_type", "contents"):
            if col not in have:
                self.db.execute(f"ALTER TABLE courses ADD COLUMN {col} TEXT")
        self.db.commit()

    def close(self) -> None:
        self.db.commit()
        self.db.close()

    # -- schreiben ------------------------------------------------------
    def save_semesters(self, sems: dict[int, str]) -> None:
        self.db.executemany("INSERT OR REPLACE INTO semesters VALUES (?,?)", sems.items())
        self.db.commit()

    def save_catalog(self, crawl: CatalogCrawl) -> None:
        rows = [(crawl.period_id, n.path, n.name, n.kind, n.parent, n.unit_id)
                for n in crawl.nodes.values()]
        self.db.executemany(
            "INSERT OR REPLACE INTO catalog_nodes VALUES (?,?,?,?,?,?)", rows)
        self.db.commit()

    def save_course(self, course: Course) -> None:
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        # `contents` kommt aus einem eigenen Lauf (`cli contents`). Ein crawl/
        # reparse ohne diesen Schritt darf es nicht ueberschreiben, deshalb:
        # None = "nicht abgerufen" -> alten Wert behalten, {} = "leer" -> setzen.
        if course.contents is None and course.requirements is None:
            row = self.db.execute(
                "SELECT contents FROM courses WHERE unit_id=? AND period_id=?",
                (course.unit_id, course.period_id)).fetchone()
            contents = row["contents"] if row else None
        else:
            contents = json.dumps({"sections": course.contents or {},
                                   "requirements": course.requirements or []},
                                  ensure_ascii=False)
        self.db.execute(
            "INSERT OR REPLACE INTO courses (unit_id, period_id, title, number,"
            " element_type, course_type, org_units, frequency, fields, contents,"
            " url, fetched_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (course.unit_id, course.period_id, course.title, course.number,
             course.element_type, course.course_type, json.dumps(course.org_units, ensure_ascii=False),
             course.frequency, json.dumps(course.fields, ensure_ascii=False),
             contents, course.url, now))
        # alte Gruppen/Termine dieses Kurses ersetzen
        old = [r["id"] for r in self.db.execute(
            "SELECT id FROM groups WHERE unit_id=? AND period_id=?",
            (course.unit_id, course.period_id))]
        if old:
            q = ",".join("?" * len(old))
            self.db.execute(f"DELETE FROM occurrences WHERE group_id IN ({q})", old)
            self.db.execute(f"DELETE FROM appointments WHERE group_id IN ({q})", old)
            self.db.execute(f"DELETE FROM groups WHERE id IN ({q})", old)

        for group in course.groups + course.exam_groups:
            cur = self.db.execute(
                "INSERT INTO groups (unit_id,period_id,idx,name,is_exam,responsible)"
                " VALUES (?,?,?,?,?,?)",
                (course.unit_id, course.period_id, group.index, group.name,
                 int(group.is_exam), json.dumps(group.responsible, ensure_ascii=False)))
            gid = cur.lastrowid
            for ap in group.appointments:
                occs, status = expand(ap)
                acur = self.db.execute(
                    "INSERT INTO appointments (group_id,rhythm,weekday,start_time,end_time,"
                    "first_date,last_date,room,lecturers,note,cancelled,status)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (gid, ap.rhythm, ap.weekday, ap.start_time, ap.end_time,
                     ap.first_date, ap.last_date, ap.room,
                     json.dumps(ap.lecturers, ensure_ascii=False), ap.note,
                     json.dumps(ap.cancelled, ensure_ascii=False), status))
                aid = acur.lastrowid
                self.db.executemany(
                    "INSERT INTO occurrences (appointment_id,unit_id,period_id,group_id,"
                    "date,start_time,end_time,room,cancelled) VALUES (?,?,?,?,?,?,?,?,?)",
                    [(aid, course.unit_id, course.period_id, gid, o.date,
                      o.start_time, o.end_time, o.room, int(o.cancelled)) for o in occs])
        self.db.commit()

    def save_contents(self, unit_id: int, period_id: int,
                      sections: dict[str, str], requirements: list[dict]) -> None:
        """Nur die Registerkarte "Inhalte" schreiben, der Rest bleibt stehen."""
        self.db.execute(
            "UPDATE courses SET contents=? WHERE unit_id=? AND period_id=?",
            (json.dumps({"sections": sections, "requirements": requirements},
                        ensure_ascii=False), unit_id, period_id))
        self.db.commit()

    # -- lesen ----------------------------------------------------------
    def catalog_units(self, period_id: int) -> list[int]:
        """unitIds aus dem gespeicherten Katalogbaum (ohne erneuten Crawl)."""
        return [r[0] for r in self.db.execute(
            "SELECT DISTINCT unit_id FROM catalog_nodes"
            " WHERE period_id=? AND unit_id IS NOT NULL ORDER BY unit_id", (period_id,))]

    def known_units(self, period_id: int) -> set[int]:
        return {r[0] for r in self.db.execute(
            "SELECT unit_id FROM courses WHERE period_id=?", (period_id,))}

    def stats(self, period_id: int | None = None) -> dict[str, int]:
        where, args = ("WHERE period_id=?", (period_id,)) if period_id else ("", ())
        q = lambda t: self.db.execute(f"SELECT COUNT(*) FROM {t} {where}", args).fetchone()[0]
        return {"courses": q("courses"), "groups": q("groups"),
                "occurrences": q("occurrences"),
                "nodes": q("catalog_nodes")}
