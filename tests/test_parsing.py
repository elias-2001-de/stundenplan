"""Tests gegen gespeicherte ZEuS-Seiten (keine Netzwerkzugriffe)."""
import gzip
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from zeus import catalog, cli, detail, ics, search  # noqa: E402
from zeus.detail import Appointment  # noqa: E402
from zeus.expand import EXACT, SPAN, expand  # noqa: E402

FIX = pathlib.Path(__file__).parent / "fixtures"


def fixture(name: str) -> str:
    return gzip.decompress((FIX / name).read_bytes()).decode()


class TestDetailParsing(unittest.TestCase):
    def test_course_with_two_parallel_groups(self):
        c = detail.parse(fixture("detail_20651_797.html.gz"), 20651, 797)
        self.assertEqual(c.number, "CHE-11520")
        self.assertEqual(c.course_type, "Einführungsveranstaltung")
        self.assertEqual(c.org_units, ["FB Chemie (Verantwortlicher)"])
        self.assertEqual(len(c.groups), 2)
        self.assertEqual(c.exam_groups, [])

        first = c.groups[0].appointments[0]
        self.assertEqual(first.rhythm, "Einzeltermin")
        self.assertEqual(first.weekday, "Mo")
        self.assertEqual((first.start_time, first.end_time), ("09:00", "10:00"))
        self.assertEqual(first.first_date, "2026-10-05")
        self.assertEqual(first.room, "M629")
        self.assertEqual(first.lecturers, ["Prof. Dr. Alexander Wittemann"])

        # zweite Gruppe hat zwei Termine mit unterschiedlichen Bemerkungen
        second = c.groups[1]
        self.assertEqual(len(second.appointments), 2)
        self.assertIn("Life Science", second.appointments[0].note)

    def test_exam_dates_are_kept_apart_from_lectures(self):
        c = detail.parse(fixture("detail_61924_796.html.gz"), 61924, 796)
        self.assertEqual(c.number, "PHY-10490")
        self.assertEqual(c.groups, [])            # keine Lehrtermine auf dieser Seite
        self.assertEqual(len(c.exam_groups), 2)
        ap = c.exam_groups[0].appointments[0]
        self.assertTrue(all(g.is_exam for g in c.exam_groups))
        self.assertEqual(ap.first_date, "2026-07-30")
        self.assertEqual((ap.start_time, ap.end_time), ("08:00", "11:00"))
        self.assertEqual(ap.room, "R712")
        # Prüfer*in steht in einer Liste, nicht kommagetrennt
        self.assertEqual(len(ap.lecturers), 1)


class TestContentsTab(unittest.TestCase):
    """Registerkarte "Inhalte" – wird per Formular-POST nachgeladen."""

    def setUp(self):
        self.sections, self.reqs = detail.parse_contents(
            fixture("contents_106062_797.html.gz"))

    def test_all_sections_are_found(self):
        self.assertEqual(list(self.sections), [
            "Arbeitsaufwand", "Kommentar", "Inhalte", "Lernziele",
            "Empfohlene Voraussetzung", "Leistungsnachweis", "Zielgruppe",
            "Lehrmethoden", "Literatur"])

    def test_structural_fieldsets_are_not_sections(self):
        for frame in ("Semesterauswahl", "Semesterplanung", "Grunddaten", "Termine"):
            self.assertNotIn(frame, self.sections)

    def test_line_breaks_survive(self):
        # <br>-getrennte Wochentabelle: eine Zeile je Woche, keine Leerzeilen
        lines = self.sections["Inhalte"].split("\n")
        self.assertEqual(lines[0], "Week Topic Notes")
        self.assertEqual(lines[1], "1 Introduction of robotics")
        self.assertEqual(lines[-1], "13-14 Hydrodynamics in biology and robotics")

    def test_bullet_lists_survive(self):
        self.assertIn("\n• Understand key concepts", self.sections["Lernziele"])

    def test_examination_table(self):
        self.assertEqual(self.reqs, [{"ECTS": "6", "Art": "Lecture",
                                      "Prüfungsnummer": "INF-16850"}])


class TestCatalogParsing(unittest.TestCase):
    PARENT = "title:29191|title:29192|title:29685"

    def test_child_rows(self):
        nodes = catalog._parse_rows(fixture("catalog_node.html.gz"), self.PARENT)
        self.assertEqual(len(nodes), 5)
        self.assertTrue(all(n.parent == self.PARENT for n in nodes))
        self.assertTrue(all(n.depth == 3 for n in nodes))
        kinds = {n.kind for n in nodes}
        self.assertIn("Veranstaltungskonto", kinds)
        self.assertIn("Prüfungsordnung", kinds)
        names = [n.name for n in nodes]
        self.assertIn("Mathematische Vorbereitungskurse", names)

    def test_ancestor_levels_are_visible_on_the_same_page(self):
        # Die Seite zeigt den aufgeklappten Pfad ab der Wurzel, also auch die
        # Geschwister der Vorfahren. _parse_rows liefert je Aufruf genau die
        # direkten Kinder des angefragten Knotens.
        nodes = catalog._parse_rows(fixture("catalog_node.html.gz"), "title:29191")
        self.assertTrue(nodes)
        self.assertTrue(all(n.depth == 1 for n in nodes))
        self.assertIn("Bachelor Studiengänge - Hauptfach", [n.name for n in nodes])

    def test_unrelated_parent_yields_nothing(self):
        self.assertEqual(catalog._parse_rows(fixture("catalog_node.html.gz"),
                                             "title:99999"), [])


class TestCourseSearch(unittest.TestCase):
    def test_term_value(self):
        self.assertEqual(search.term_value("Wintersemester 2026/27"), "eq|2|2026")
        self.assertEqual(search.term_value("Sommersemester 2026"), "eq|1|2026")
        with self.assertRaises(ValueError):
            search.term_value("Wintersemester")

    def test_result_count_and_rows(self):
        html = fixture("search_min.html.gz")
        self.assertEqual(search._count(html), 216)
        hits = search.parse_hits(html)
        self.assertEqual(len(hits), 216)
        self.assertEqual(len({h.unit_id for h in hits}), 216)
        # Nummer und Titel landen in getrennten Feldern, nicht in einer Zelle.
        # Nummern sind meist "BIO-15960", es gibt aber auch rein numerische
        # Fachcodes ("186-11710") und Freitext ("Platzvergabe").
        self.assertTrue(all(h.number for h in hits))
        for h in hits:
            self.assertNotIn(h.number, h.title)
        coded = [h for h in hits if "-" in h.number]
        self.assertGreater(len(coded), 200)
        self.assertIn("MIN", {h.number.split("-")[0] for h in coded})


class TestRhythmExpansion(unittest.TestCase):
    def test_single_date(self):
        occ, status = expand(Appointment(rhythm="Einzeltermin", weekday="Mo",
                                         start_time="09:00", end_time="10:00",
                                         first_date="2026-10-05"))
        self.assertEqual(status, EXACT)
        self.assertEqual([o.date for o in occ], ["2026-10-05"])

    def test_weekly_with_cancellation(self):
        ap = Appointment(rhythm="wöchentlich", weekday="Di", start_time="10:00",
                         end_time="11:30", first_date="2026-10-20",
                         last_date="2026-11-17", cancelled=["2026-11-03"])
        occ, status = expand(ap)
        self.assertEqual(status, EXACT)
        self.assertEqual([o.date for o in occ],
                         ["2026-10-20", "2026-10-27", "2026-11-03",
                          "2026-11-10", "2026-11-17"])
        self.assertEqual([o.date for o in occ if o.cancelled], ["2026-11-03"])

    def test_fortnightly(self):
        occ, _ = expand(Appointment(rhythm="14-täglich", weekday="Di",
                                    first_date="2026-10-20", last_date="2026-12-15"))
        self.assertEqual([o.date for o in occ],
                         ["2026-10-20", "2026-11-03", "2026-11-17",
                          "2026-12-01", "2026-12-15"])

    def test_weekday_mismatch_snaps_forward(self):
        # Startdatum ist ein Dienstag, Muster sagt Mittwoch -> erster Mittwoch danach
        occ, _ = expand(Appointment(rhythm="wöchentlich", weekday="Mi",
                                    first_date="2026-10-20", last_date="2026-11-04"))
        self.assertEqual([o.date for o in occ], ["2026-10-21", "2026-10-28", "2026-11-04"])

    def test_block_event_reports_span_only(self):
        occ, status = expand(Appointment(rhythm="Blockveranstaltung",
                                         first_date="2025-11-03", last_date="2026-12-11"))
        self.assertEqual(status, SPAN)
        self.assertEqual(len(occ), 1)

    def test_appointment_without_date(self):
        occ, status = expand(Appointment(rhythm="wöchentlich", weekday="Mo"))
        self.assertEqual((occ, status), ([], "unknown"))


class TestIcsExport(unittest.TestCase):
    ROW = {
        "number": "MAT-10062", "title": "Maßtheorie; Teil 1", "course_type": "Vorlesung",
        "group_name": "Maßtheorie (1. Parallelgruppe)", "is_exam": 0,
        "date": "2026-10-20", "start_time": "10:00", "end_time": "11:30",
        "room": "A 701", "cancelled": 0, "lecturers": ["Prof. Dr. Lang"],
        "rhythm": "wöchentlich", "status": "exact",
        "first_date": "2026-10-20", "last_date": "2027-02-02",
        "unit_id": 42, "period_id": 797, "url": "https://example.invalid/x",
    }

    def test_fold_respects_75_octets(self):
        long = "DESCRIPTION:" + "ä" * 200
        folded = ics.fold(long)
        self.assertTrue(all(len(f.encode()) <= 75 for f in folded))
        self.assertTrue(all(f.startswith(" ") for f in folded[1:]))
        self.assertEqual("".join([folded[0]] + [f[1:] for f in folded[1:]]), long)

    def test_event_structure(self):
        text = cli._ics([dict(self.ROW)])
        self.assertTrue(text.endswith("\r\n"))
        lines = text.split("\r\n")
        self.assertTrue(all(len(l.encode()) <= 75 for l in lines))
        self.assertEqual(lines[0], "BEGIN:VCALENDAR")
        self.assertIn("BEGIN:VTIMEZONE", lines)
        self.assertEqual(sum(1 for l in lines if l == "BEGIN:VEVENT"), 1)
        self.assertIn("DTSTART;TZID=Europe/Berlin:20261020T100000", lines)
        self.assertIn("DTEND;TZID=Europe/Berlin:20261020T113000", lines)
        self.assertTrue(any(l.startswith("DTSTAMP:") for l in lines))
        # Semikolon im Titel muss maskiert sein
        self.assertIn(r"SUMMARY:MAT-10062 Maßtheorie\; Teil 1", lines)

    def test_cancelled_and_timeless_rows_are_skipped(self):
        cancelled = dict(self.ROW, cancelled=1)
        timeless = dict(self.ROW, start_time=None)
        text = cli._ics([cancelled, timeless])
        self.assertNotIn("BEGIN:VEVENT", text)

    def test_unsure_rhythms_are_marked(self):
        block = dict(self.ROW, status="span", rhythm="Blockveranstaltung")
        unknown = dict(self.ROW, status="unknown", rhythm="nach Vereinbarung")
        text = cli._ics([block, unknown])
        self.assertIn("[Block]", text)
        self.assertIn("[Termin unklar]", text)
        self.assertIn("Zeitraum 2026-10-20 bis 2027-02-02", text.replace("\r\n ", ""))


if __name__ == "__main__":
    unittest.main()
