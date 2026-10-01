"""Kommandozeile: Semester listen, Vorlesungsverzeichnis crawlen, Daten exportieren."""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import json
import logging
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path

from . import catalog, daily, detail, ics, search as searchmod
from .session import ZeusSession
from .store import Store

log = logging.getLogger("zeus")
_local = threading.local()


def _session(args) -> ZeusSession:
    """Pro Thread eine eigene Session: Flow-State ist nicht thread-sicher."""
    s = getattr(_local, "sess", None)
    if s is None:
        s = _local.sess = ZeusSession(cache_dir=args.cache, delay=args.delay)
    return s


def cmd_semesters(args) -> int:
    sess = _session(args)
    sems = catalog.semesters(sess)
    store = Store(args.db)
    store.save_semesters(sems)
    store.close()
    for pid, name in sorted(sems.items(), key=lambda kv: -kv[0]):
        print(f"{pid:>5}  {name}")
    return 0


def cmd_crawl(args) -> int:
    store = Store(args.db)
    sess = _session(args)
    sems = catalog.semesters(sess)
    store.save_semesters(sems)
    period = args.period or max(sems)
    print(f"Semester {period} ({sems.get(period, '?')})", file=sys.stderr)

    if args.units:
        unit_ids = args.units
    else:
        print("Durchlaufe Vorlesungsverzeichnis …", file=sys.stderr)

        def progress(visited, queued, units):
            print(f"\r  Knoten {visited} besucht, {queued} offen, "
                  f"{units} Veranstaltungen gefunden", end="", file=sys.stderr)

        crawl = catalog.crawl(sess, period, max_nodes=args.max_nodes, progress=progress,
                              workers=args.workers,
                              session_factory=lambda: _session(args))
        print(file=sys.stderr)
        store.save_catalog(crawl)
        unit_ids = crawl.unit_ids

    if not args.refetch:
        known = store.known_units(period)
        unit_ids = [u for u in unit_ids if u not in known]
    print(f"{len(unit_ids)} Detailseiten zu holen", file=sys.stderr)

    lock = threading.Lock()
    done = 0

    def work(uid: int):
        return detail.fetch(_session(args), uid, period)

    with cf.ThreadPoolExecutor(max_workers=args.workers) as ex:
        for fut in cf.as_completed({ex.submit(work, u): u for u in unit_ids}):
            try:
                course = fut.result()
            except Exception as e:  # einzelne Seite darf den Lauf nicht killen
                log.warning("Detailseite fehlgeschlagen: %s", e)
                continue
            with lock:
                store.save_course(course)
                done += 1
                if done % 25 == 0 or done == len(unit_ids):
                    print(f"\r  {done}/{len(unit_ids)} Veranstaltungen gespeichert",
                          end="", file=sys.stderr)
    print(file=sys.stderr)
    print(json.dumps(store.stats(period), indent=1))
    store.close()
    return 0


def cmd_contents(args) -> int:
    """Registerkarte "Inhalte" nachladen: Beschreibung, Lernziele, Literatur …

    Eigener Lauf, weil er zwei zusaetzliche Requests je Veranstaltung kostet und
    der Flow-Key nicht aus dem Cache kommen darf. Wiederaufnehmbar: was schon
    Inhalte hat, wird uebersprungen (`--refetch` erzwingt das Neuladen).
    """
    store = Store(args.db)
    names = dict(store.db.execute("SELECT period_id, name FROM semesters"))
    if args.all:
        periods = sorted(names, reverse=True)
    elif args.periods:
        periods = list(args.periods)
    else:
        periods = [args.period or (max(names) if names else 0)]

    todo: list[tuple[int, list[int]]] = []
    for period in periods:
        if args.units:
            unit_ids = list(args.units)
        else:
            where = "period_id=? AND element_type='Veranstaltung'"
            if not args.refetch:
                where += " AND contents IS NULL"
            unit_ids = [r[0] for r in store.db.execute(
                f"SELECT unit_id FROM courses WHERE {where} ORDER BY number", (period,))]
        if args.limit:
            unit_ids = unit_ids[:args.limit]
        if unit_ids:
            todo.append((period, unit_ids))

    # Das Rate-Limit ist global (Klassenattribut der Session), mehr Worker
    # machen den Lauf also *nicht* schneller – nur die Wartezeit zaehlt.
    total = sum(len(u) for _, u in todo)
    hours = 2 * total * args.delay / 3600
    print(f"{total} Veranstaltungen in {len(todo)} Semester(n), 2 Requests je"
          f" Stueck = {2 * total} Requests – grob {hours:.1f} h", file=sys.stderr)
    if args.dry_run or not todo:
        for period, unit_ids in todo:
            print(f"  {period} {names.get(period, '?')}: {len(unit_ids)}", file=sys.stderr)
        store.close()
        return 0

    lock = threading.Lock()
    for period, unit_ids in todo:
        print(f"== {period} {names.get(period, '?')}: {len(unit_ids)} Veranstaltungen",
              file=sys.stderr)
        done = withtext = 0

        def work(uid: int, _p=period):
            return uid, detail.fetch_contents(_session(args), uid, _p)

        with cf.ThreadPoolExecutor(max_workers=args.workers) as ex:
            for fut in cf.as_completed({ex.submit(work, u): u for u in unit_ids}):
                try:
                    uid, (sections, reqs) = fut.result()
                except Exception as e:  # eine Seite darf den Lauf nicht killen
                    log.warning("Inhalte fehlgeschlagen: %s", e)
                    continue
                with lock:
                    store.save_contents(uid, period, sections, reqs)
                    done += 1
                    withtext += bool(sections or reqs)
                    if done % 25 == 0 or done == len(unit_ids):
                        print(f"\r  {done}/{len(unit_ids)} geholt, {withtext} mit Inhalt",
                              end="", file=sys.stderr)
        print(file=sys.stderr)
    store.close()
    return 0


def _prefixes(store: Store, period: int) -> list[str]:
    import re as _re
    out = set()
    for (num,) in store.db.execute(
            "SELECT number FROM courses WHERE period_id=? AND number<>''", (period,)):
        m = _re.match(r"([A-Za-zÄÖÜ0-9]{2,5})-", num or "")
        if m:
            out.add(m.group(1))
    return sorted(out)


def cmd_discover(args) -> int:
    """Veranstaltungen über die Suchmaske einsammeln und Detailseiten laden.

    Die Suche zerlegt das Semester nach Veranstaltungsart – das ist eine echte
    Partition und findet auch die Veranstaltungen, die im VV-Baum fehlen.
    """
    import re as _re
    store = Store(args.db)
    sess = _session(args)
    sems = dict(store.db.execute("SELECT period_id, name FROM semesters"))
    if not sems:
        sems = catalog.semesters(sess)
        store.save_semesters(sems)

    if args.all:
        periods = sorted(sems, reverse=True)
    elif args.periods:
        periods = args.periods
    elif args.period:
        periods = [args.period]
    else:
        periods = [max(sems)]

    for period in periods:
        try:
            _import_period(args, store, sess, period, sems.get(period, ""))
        except Exception as e:      # ein Semester darf den Gesamtlauf nicht stoppen
            log.warning("Semester %s abgebrochen: %s", period, e)
    store.close()
    return 0


def _import_period(args, store: Store, sess: ZeusSession, period: int, name: str) -> None:
    import re as _re
    if True:
        try:
            term = searchmod.term_value(name)
        except ValueError:
            log.warning("Semester %s (%r) übersprungen: kein Jahr erkennbar", period, name)
            return
        total, _ = searchmod.search(sess, term, rows=10)
        print(f"\n== {name} (periodId {period}): Suchmaske meldet {total} "
              f"Veranstaltungen", file=sys.stderr)
        if not total:
            return

        def progress(label, tot, rows, cum):
            print(f"\r  Suche: {cum} gefunden ({label[:30]})           ",
                  end="", file=sys.stderr)

        found = searchmod.enumerate_by_type(sess, term, progress=progress)
        if len(found) < total:      # Rest über Nummernpräfixe nachfassen
            prefixes = sorted({m.group(1) for h in found.values()
                               if (m := _re.match(r"([A-Za-zÄÖÜ0-9]{2,5})-", h.number or ""))})
            found.update(searchmod.enumerate_units(sess, term, prefixes))
        print(f"\r  Suche: {len(found)} von {total} Veranstaltungen gefunden"
              f"                    ", file=sys.stderr)

        known = store.known_units(period) if not args.refetch else set()
        missing = sorted(set(found) - known)
        if args.dry_run:
            for uid in missing[:50]:
                h = found[uid]
                print(f"  {uid} {h.number} {h.title[:60]}")
            print(f"  ({len(missing)} neu)", file=sys.stderr)
            return
        if not missing:
            print("  nichts Neues zu laden", file=sys.stderr)
            return

        lock = threading.Lock()
        done = 0

        def work(uid: int):
            return detail.fetch(_session(args), uid, period)

        with cf.ThreadPoolExecutor(max_workers=args.workers) as ex:
            for fut in cf.as_completed({ex.submit(work, u): u for u in missing}):
                try:
                    course = fut.result()
                except Exception as e:
                    log.warning("Detailseite fehlgeschlagen: %s", e)
                    continue
                with lock:
                    store.save_course(course)
                    done += 1
                    if done % 25 == 0 or done == len(missing):
                        print(f"\r  {done}/{len(missing)} nachgeladen", end="",
                              file=sys.stderr)
        print(file=sys.stderr)
        print(json.dumps(store.stats(period), indent=1))


def cmd_reparse(args) -> int:
    """Alle Detailseiten erneut aus dem HTML-Cache parsen (ohne Netzwerkzugriff)."""
    store = Store(args.db)
    sess = _session(args)
    period = args.period or max(int(r[0]) for r in store.db.execute(
        "SELECT DISTINCT period_id FROM courses"))
    units = store.catalog_units(period) or sorted(store.known_units(period))
    print(f"{len(units)} Detailseiten neu parsen (Semester {period})", file=sys.stderr)
    done = failed = 0
    for uid in units:
        try:
            store.save_course(detail.parse(sess.get(detail.detail_url(uid, period)),
                                           uid, period))
            done += 1
        except Exception as e:
            failed += 1
            log.warning("unitId=%s: %s", uid, e)
        if done % 250 == 0:
            print(f"\r  {done}/{len(units)}", end="", file=sys.stderr)
    print(f"\r  {done} neu geparst, {failed} Fehler", file=sys.stderr)
    print(json.dumps(store.stats(period), indent=1))
    store.close()
    return 0


def cmd_verify(args) -> int:
    """Gegenprobe: liegen die Termine eines Tages auch im gecrawlten Bestand?"""
    store = Store(args.db)
    sess = _session(args)
    events = daily.fetch_day(sess, args.date)
    known = store.known_units(args.period) if args.period else {
        r[0] for r in store.db.execute("SELECT unit_id FROM courses")}
    seen = {e.unit_id for e in events if e.unit_id}
    missing = sorted(seen - known)
    print(f"{args.date}: {len(events)} Termine, {len(seen)} Veranstaltungen laut Tagesliste")
    print(f"davon im Bestand: {len(seen) - len(missing)}, fehlend: {len(missing)}")
    if len(events) >= daily.MAX_ROWS:
        print(f"Hinweis: Tagesliste bei {daily.MAX_ROWS} Zeilen abgeschnitten "
              f"(Serverlimit) – die Gegenprobe ist damit nur eine Stichprobe.")
    for uid in missing[:40]:
        ev = next(e for e in events if e.unit_id == uid)
        print(f"  fehlt: unitId={uid} {ev.number} {ev.title[:60]}")
    store.close()
    return 0


def cmd_daily(args) -> int:
    sess = _session(args)
    events = daily.fetch_day(sess, args.date)
    print(json.dumps([e.__dict__ for e in events], ensure_ascii=False, indent=1))
    return 0


def cmd_stats(args) -> int:
    store = Store(args.db)
    print(json.dumps(store.stats(args.period), indent=1))
    store.close()
    return 0


def _rows(store: Store, period: int | None, *, query: str | None = None,
          kind: str = "alle", unit_ids: list[int] | None = None,
          only_exact: bool = False):
    clauses, args = [], []
    if period:
        clauses.append("o.period_id = ?")
        args.append(period)
    if query:
        clauses.append("(c.title LIKE ? OR c.number LIKE ? OR g.name LIKE ?"
                       " OR a.lecturers LIKE ?)")
        args += [f"%{query}%"] * 4
    if only_exact:
        clauses.append("a.status = 'exact'")
    if kind == "lehre":
        clauses.append("g.is_exam = 0")
    elif kind == "pruefung":
        clauses.append("g.is_exam = 1")
    if unit_ids:
        clauses.append("o.unit_id IN (%s)" % ",".join("?" * len(unit_ids)))
        args += unit_ids
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    return store.db.execute(f"""
        SELECT c.number, c.title, c.course_type, g.name AS group_name, g.is_exam,
               o.date, o.start_time, o.end_time, o.room, o.cancelled,
               a.lecturers, a.rhythm, a.status, a.first_date, a.last_date,
               c.unit_id, c.period_id, c.url
        FROM occurrences o
        JOIN courses c      ON c.unit_id = o.unit_id AND c.period_id = o.period_id
        JOIN groups g       ON g.id = o.group_id
        JOIN appointments a ON a.id = o.appointment_id
        {where}
        ORDER BY o.date, o.start_time, c.number
    """, args)


def cmd_export(args) -> int:
    store = Store(args.db)
    out = Path(args.out) if args.out else None
    rows = [dict(r) for r in _rows(store, args.period, query=args.query,
                                   kind=args.type, unit_ids=args.units,
                                   only_exact=args.only_exact)]
    for r in rows:
        r["lecturers"] = json.loads(r["lecturers"] or "[]")

    if args.format == "json":
        text = json.dumps(rows, ensure_ascii=False, indent=1)
    elif args.format == "csv":
        import csv
        import io
        fields = list(rows[0]) if rows else [
            "number", "title", "course_type", "group_name", "is_exam", "date",
            "start_time", "end_time", "room", "cancelled", "lecturers", "rhythm",
            "status", "first_date", "last_date", "unit_id", "period_id", "url"]
        buf = io.StringIO()
        w = csv.DictWriter(buf, fieldnames=fields)
        w.writeheader()
        for r in rows:
            w.writerow({**r, "lecturers": "; ".join(r["lecturers"])})
        text = buf.getvalue()
    else:  # ics
        text = _ics(rows)

    if out:
        out.write_text(text, encoding="utf-8")
        print(f"{len(rows)} Termine -> {out}", file=sys.stderr)
    else:
        sys.stdout.write(text)
    store.close()
    return 0


def _ics(rows: list[dict]) -> str:
    esc = ics.esc
    stamp = ics.stamp()
    lines = []
    for i, r in enumerate(rows):
        if r["cancelled"] or not r["start_time"]:
            continue
        d = r["date"].replace("-", "")
        # Termine, deren Rhythmus nicht eindeutig aufgelöst werden konnte, werden
        # gekennzeichnet – sonst stünden sie im Kalender wie ein bestätigter
        # wöchentlicher Termin, obwohl nur ein Zeitraum bekannt ist.
        mark = {"span": "[Block] ", "unknown": "[Termin unklar] "}.get(r["status"], "")
        note = ""
        if r["status"] == "span" and r["last_date"] and r["last_date"] != r["date"]:
            note = f" – Zeitraum {r['first_date']} bis {r['last_date']}"
        elif r["status"] == "unknown":
            note = f" – Rhythmus laut ZEuS: {r['rhythm']}"
        lines += [
            "BEGIN:VEVENT",
            f"UID:zeus-{r['unit_id']}-{r['period_id']}-{i}@uni-konstanz.de",
            f"DTSTAMP:{stamp}",
            f"DTSTART;TZID=Europe/Berlin:{d}T{r['start_time'].replace(':', '')}00",
            f"DTEND;TZID=Europe/Berlin:{d}T{(r['end_time'] or r['start_time']).replace(':', '')}00",
            f"SUMMARY:{esc(mark)}{esc((r['number'] + ' ') if r['number'] else '')}{esc(r['title'])}",
            f"LOCATION:{esc(r['room'])}",
            f"DESCRIPTION:{esc(r['group_name'])} / {esc('; '.join(r['lecturers']))}"
            f"{esc(note)}",
            f"URL:{r['url']}",
            "END:VEVENT",
        ]
    return ics.calendar(lines)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="zeus", description=__doc__)
    p.add_argument("--db", default="stundenplan.sqlite3")
    p.add_argument("--cache", default="cache", help="HTML-Cache-Verzeichnis ('' = aus)")
    p.add_argument("--delay", type=float, default=0.5, help="Pause zwischen Requests (s)")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("semesters", help="verfügbare Semester (periodId) auflisten")
    sp.set_defaults(func=cmd_semesters)

    sp = sub.add_parser("crawl", help="Vorlesungsverzeichnis eines Semesters einlesen")
    sp.add_argument("--period", type=int, help="periodId (Default: neuestes Semester)")
    sp.add_argument("--workers", type=int, default=3)
    sp.add_argument("--max-nodes", type=int, default=None, help="Baumknoten begrenzen (Test)")
    sp.add_argument("--units", type=int, nargs="*", help="nur diese unitIds holen")
    sp.add_argument("--refetch", action="store_true", help="bereits gespeicherte neu holen")
    sp.set_defaults(func=cmd_crawl)

    sp = sub.add_parser("contents", help='Registerkarte "Inhalte" nachladen'
                                        " (Beschreibung, Lernziele, Literatur …)")
    sp.add_argument("--period", type=int)
    sp.add_argument("--periods", type=int, nargs="*", help="mehrere periodIds")
    sp.add_argument("--all", action="store_true", help="alle bekannten Semester")
    sp.add_argument("--units", type=int, nargs="*", help="nur diese unitIds")
    sp.add_argument("--workers", type=int, default=3)
    sp.add_argument("--limit", type=int, help="nur die ersten N (zum Ausprobieren)")
    sp.add_argument("--refetch", action="store_true", help="auch Vorhandenes neu holen")
    sp.add_argument("--dry-run", action="store_true", help="nur den Aufwand zeigen")
    sp.set_defaults(func=cmd_contents)

    sp = sub.add_parser("stats", help="Datenbestand zeigen")
    sp.add_argument("--period", type=int)
    sp.set_defaults(func=cmd_stats)

    sp = sub.add_parser("discover", help="Veranstaltungen über die Suchmaske finden"
                                        " und nachladen (auch die, die im Baum fehlen)")
    sp.add_argument("--period", type=int)
    sp.add_argument("--periods", type=int, nargs="*", help="mehrere periodIds")
    sp.add_argument("--all", action="store_true", help="alle bekannten Semester")
    sp.add_argument("--workers", type=int, default=4)
    sp.add_argument("--refetch", action="store_true", help="auch Bekanntes neu laden")
    sp.add_argument("--dry-run", action="store_true", help="nur anzeigen, nichts laden")
    sp.set_defaults(func=cmd_discover)

    sp = sub.add_parser("reparse", help="Detailseiten aus dem Cache neu parsen")
    sp.add_argument("--period", type=int)
    sp.set_defaults(func=cmd_reparse)

    sp = sub.add_parser("verify", help="Tagesliste gegen gecrawlten Bestand prüfen")
    sp.add_argument("date", help="Datum ISO, z.B. 2026-11-10")
    sp.add_argument("--period", type=int)
    sp.set_defaults(func=cmd_verify)

    sp = sub.add_parser("daily", help="alle Termine eines Tages als JSON (nur aktuelles/"
                                      "kommendes Semester)")
    sp.add_argument("date", help="Datum ISO, z.B. 2026-11-10")
    sp.set_defaults(func=cmd_daily)

    sp = sub.add_parser("export", help="Termine als json/csv/ics ausgeben")
    sp.add_argument("--period", type=int)
    sp.add_argument("--format", choices=("json", "csv", "ics"), default="json")
    sp.add_argument("--query", help="Filter auf Titel, Nummer, Gruppe oder Dozent*in")
    sp.add_argument("--type", choices=("alle", "lehre", "pruefung"), default="alle",
                    help="nur Lehrtermine oder nur Prüfungstermine")
    sp.add_argument("--units", type=int, nargs="*", help="nur diese unitIds")
    sp.add_argument("--only-exact", action="store_true",
                    help="nur Termine mit eindeutig aufgelöstem Rhythmus"
                         " (ohne Blockveranstaltungen und unklare Muster)")
    sp.add_argument("--out")
    sp.set_defaults(func=cmd_export)

    args = p.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(levelname)s %(message)s")
    if args.cache == "":
        args.cache = None
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
