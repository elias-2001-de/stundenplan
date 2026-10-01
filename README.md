# Stundenplan Uni Konstanz — Datenbeschaffung aus ZEuS

ZEuS (`zeus.uni-konstanz.de`) läuft auf **HISinOne**. Eine öffentliche API gibt es
nicht (siehe [RESEARCH.md](RESEARCH.md)), aber das komplette Vorlesungsverzeichnis
ist ohne Login über GET-Permalinks erreichbar. Dieses Paket crawlt es und legt
Veranstaltungen, Parallelgruppen und Termine in SQLite ab.

## Installation

```bash
pip install -r requirements.txt
```

## Benutzung

```bash
# Welche Semester gibt es? (periodId brauchst du für alles Weitere)
python -m zeus.cli semesters

# Komplettes Vorlesungsverzeichnis eines Semesters einlesen
python -m zeus.cli crawl --period 797

# Datenbestand ansehen
python -m zeus.cli stats --period 797

# Der VV-Baum ist unvollständig: Veranstaltungen ohne Studiengangs-Zuordnung
# fehlen dort. Dieser Schritt findet sie über die Suchmaske und lädt sie nach.
python -m zeus.cli discover --period 797

# Alle 19 Semester einlesen (rein über die Suche, ohne Baum – dauert Stunden)
python -m zeus.cli discover --all

# Einzelne ältere Semester
python -m zeus.cli discover --periods 794 796

# Registerkarte "Inhalte" nachladen: Beschreibung, Lernziele, Literatur,
# ECTS/Prüfungsnummer. Eigener Lauf, weil er zwei Requests je Veranstaltung
# kostet. Das Rate-Limit ist global, mehr Worker beschleunigen nichts: rund
# 1 s je Veranstaltung, also ~30 min pro Semester und ~10 h für alle 19.
# Wiederaufnehmbar – abbrechen und neu starten macht dort weiter, wo es aufhörte.
python -m zeus.cli contents --period 797 --dry-run   # nur den Aufwand zeigen
python -m zeus.cli contents --period 797
python -m zeus.cli contents --all

# ZEuS liefert die Registerkarte gelegentlich unvollständig (einzelne Abschnitte
# fehlen, ohne dass die Antwort als fehlerhaft erkennbar wäre). Der Lauf nimmt
# nur auf, was noch gar nichts hat – eine kurz geratene Veranstaltung holt man
# gezielt nach:
python -m zeus.cli contents --period 797 --units 106062 --refetch

# Alles noch einmal aus dem HTML-Cache parsen (kein Netzwerk, z.B. nach Parser-Fix)
python -m zeus.cli reparse --period 797

# Gegenprobe: stimmt ein Tag aus "Tagesaktuelle Veranstaltungen" mit dem Bestand überein?
python -m zeus.cli verify 2026-11-10 --period 797

# Alle Termine eines Tages roh als JSON
python -m zeus.cli daily 2026-11-10

# Termine exportieren
python -m zeus.cli export --period 797 --format json --out export/ws2627.json
python -m zeus.cli export --period 797 --format csv  --out export/ws2627.csv
python -m zeus.cli export --period 797 --format ics  --out export/ws2627.ics

# Nur die eigenen Kurse, nur sichere Termine
python -m zeus.cli export --period 797 --query "MAT-" --only-exact \
    --format ics --out export/mathe.ics
python -m zeus.cli export --period 797 --units 20651 61924 --format json
```

### Zwei Wege zu den Daten

| Befehl | findet | Kosten |
|---|---|---|
| `crawl` | Veranstaltungen **mit** Studiengangs-Zuordnung (+ den Baum selbst) | ~1 h pro Semester |
| `discover` | **alle** Veranstaltungen des Semesters, ohne Baum | ~10 min pro Semester |

`discover` zerlegt das Semester nach Veranstaltungsart (78 Werte inkl.
"(nicht gefüllt)") – das ist eine echte Partition, keine Stichprobe. Nur
`crawl` liefert zusätzlich `catalog_nodes`, also die Zuordnung
Studiengang → Veranstaltung.

**Wichtig: `crawl` allein reicht nicht.** Der Baum des Vorlesungsverzeichnisses
enthält nur Veranstaltungen, die einem Studiengang zugeordnet sind; eine
Stichprobe gegen die Tagesliste zeigte rund 13 % Fehlende. `discover` schließt
die Lücke über die Veranstaltungssuche. Reihenfolge also: `crawl`, dann
`discover`, dann `verify` zur Kontrolle.

Der Crawl ist **wiederaufnehmbar**: jede abgerufene Seite landet im HTML-Cache
(`--cache`, Default `cache/`), bereits gespeicherte Veranstaltungen werden
übersprungen (`--refetch` erzwingt das Neuladen).

## Datenmodell

```
courses        Element (unit_id + period_id): Titel, Nummer, Fachbereich und
 │             element_type = "Veranstaltung" | "Modul" | "Prüfung".
 │             contents = Registerkarte "Inhalte" als JSON (NULL = nie geholt,
 │             kommt aus `zeus.cli contents`; crawl/reparse lassen sie stehen).
 │             Nur Veranstaltungen/Prüfungen haben Termine; Module sind
 │             Gliederungselemente des Studiengangs.
 └ groups      Parallelgruppen (und getrennt davon Prüfungstermin-Gruppen)
    └ appointments   Terminmuster: Rhythmus, Wochentag, Uhrzeit, Zeitraum, Raum,
    │                Dozent*innen, Ausfalltermine
    └ occurrences    daraus ausgerechnete Einzeltermine (Datum + Uhrzeit + Raum)
catalog_nodes  Baum des Vorlesungsverzeichnisses; verbindet Studiengang-Pfade
               mit unit_id (n:m — dieselbe Veranstaltung hängt in mehreren Pfaden)
```

Die planbare Einheit ist die **Parallelgruppe**, nicht die Veranstaltung: eine
Vorlesung kann mehrere Gruppen zu verschiedenen Zeiten in verschiedenen Räumen haben.

`appointments.status` sagt, wie zuverlässig die Einzeltermine sind:

| Status    | Bedeutung                                                              |
|-----------|------------------------------------------------------------------------|
| `exact`   | Rhythmus verstanden (Einzeltermin, wöchentlich, 14-täglich, …)          |
| `span`    | Blockveranstaltung o.ä. — nur der Zeitraum ist bekannt, ein Eintrag     |
| `unknown` | Muster nicht interpretierbar; Rohdaten stehen in `appointments`         |

## Tests

```bash
python -m unittest discover -s tests
```

Die Tests laufen gegen gespeicherte ZEuS-Seiten unter `tests/fixtures/`,
also ohne Netzwerkzugriff.

## Fairness gegenüber dem Server

Default 0,4–0,5 s Pause zwischen Requests, 3 Worker, jeder mit eigener Session
(Spring-Webflow-State ist nicht thread-sicher), alles gecacht. Bitte nicht
hochdrehen — ZEuS ist ein Produktivsystem der Universität.

## Kein Login nötig

Alles hier Genutzte ist öffentlich. Zugangsdaten bräuchte man nur für den
persönlichen Studienplaner/Prüfungsanmeldung — dafür ist dieses Werkzeug nicht da.

## Stundenplaner (Web-App)

```bash
unzip stundenplan.sqlite3.zip   # mitgelieferte Kurs-DB entpacken (einmalig)
# oder: gunzip -k stundenplan.sqlite3.gz
python3 app.py          # http://127.0.0.1:8765  (nur Standardbibliothek)
```

Liest `stundenplan.sqlite3` **read-only**, geht nie ins Netz und legt den
eigenen Plan in `plan.sqlite3` ab; nach jeder Änderung wird zusätzlich
`plan.json` geschrieben (gleicher Inhalt auch unter `/api/export`).

* **Suchen** über Nummer und Titel (`BIO-`, `MAT-10`, `Analysis`), zusätzlich
  filterbar nach Veranstaltungsart. Geplant wird die **Parallelgruppe**, nicht
  die Veranstaltung.
* **Belegen** (`+ belegen`) blockiert die Zeit. **Favorit** (`☆`) merkt nur vor
  und blockiert nichts.
* **Details** klappt unter jeder Veranstaltung auf, was die DB sonst noch
  hergibt (`/api/course?period=797&unit=3530`, erst beim Aufklappen geladen):
  SWS, Angebotshäufigkeit, abweichender Langtext, Kurzkommentar, vorgesehenes
  Studiensemester, Beleg-/Abmeldefristen, alle Veranstalter, Bemerkungen und
  Ausfalltermine je Terminmuster, die Liste **aller** Einzeltermine und der
  Permalink in ZEuS.

  Dazu die Registerkarte **Inhalte** (Beschreibung, Lernziele, empfohlene
  Voraussetzungen, Leistungsnachweis, Zielgruppe, Lehrmethoden, Literatur und
  die Tabelle „Zu erbringende Leistung" mit ECTS und Prüfungsnummer) – sofern
  `zeus.cli contents` für das Semester gelaufen ist, sonst steht dort der
  Hinweis darauf. Angezeigt wird immer das, was vorhanden ist; welche
  Abschnitte ZEuS füllt, ist von Veranstaltung zu Veranstaltung verschieden.

  **Prüfungstermine** stehen nicht drin: ZEuS führt Prüfungen als eigene
  Elemente (`element_type='Prüfung'`, eigene `unitId`), die weder `crawl` noch
  `discover` einsammeln – `groups.is_exam=1` ist in allen Semestern leer. Der
  Ausklapper sagt das und verlinkt die Originalseite. Die Registerkarte
  „Gekoppelte Prüfungen" wäre der nächste Schritt dorthin, ist aber noch nicht
  umgesetzt.
* **Blocker** für alles ausserhalb der Uni (Pfadfinder, Job, Zeltlager):
  wöchentlich an einem Wochentag oder als Zeitraum am Stück. Blocker zählen
  bei der Überschneidungsprüfung wie belegte Kurse und lassen sich über
  "bearbeiten" nachträglich ändern (die Kalender-UID bleibt dabei gleich, ein
  erneuter ICS-Import korrigiert den Termin also, statt ihn zu verdoppeln).
* **"nur ohne Überschneidung"** blendet alle Parallelgruppen aus, die mit dem
  Belegten oder einem Blocker kollidieren – so zeigt z.B. `BIO-` nur noch das,
  was wirklich noch passt.
* **Wochenraster** mit echten Daten (nicht "Woche 1–14"); kollidierende
  Einträge sind rot umrandet. Datum überall `TT.MM.JJJJ`, Uhrzeit 24-stündig.
* **Kalender-Export**: `.ics` unter "Mein Plan" (oder `/api/ics?period=797&what=selected,favorite,blockers`),
  wahlweise nur Belegtes, Favoriten und/oder Blocker. Wöchentliche Blocker
  werden als `RRULE` exportiert, Blockveranstaltungen – deren echte Tage ZEuS
  nicht nennt – als ganztägiger Eintrag über den Zeitraum mit Hinweis im Text.
  Zeitzone `Europe/Berlin` liegt der Datei bei. Favoriten werden als
  `TRANSP:TRANSPARENT` exportiert, belegen im Kalender also keine Zeit.
  Die UIDs hängen an Kurs, Parallelgruppe und Termin – ein erneuter Import
  aktualisiert vorhandene Einträge, statt sie zu verdoppeln. Entfernt man einen
  Kurs aus dem Plan, verschwindet er dadurch aber **nicht** aus dem Kalender:
  ICS-Import fügt hinzu und löscht nie; solche Einträge muss man im
  Kalenderprogramm selbst wegwerfen.

### Wie die Überschneidung geprüft wird

Verglichen werden **Einzeltermine** (`occurrences`, ohne Ausfalltermine), nicht
Wochentagsmuster. Damit stimmt auch 14-täglich: zwei Kurse im selben Slot in
abwechselnden Wochen kollidieren nicht. Intervalle sind halboffen, 10:00–11:30
und 11:30–13:00 gehen also nebeneinander.

Was ZEuS nicht sauber hergibt, wird als solches markiert statt stillschweigend
als "frei" gewertet:

| Badge | Bedeutung |
|---|---|
| `frei` | keine Kollision, Termine exakt bekannt |
| `Überschneidung` | echte Kollision an konkreten Daten (mit Angabe womit und wie oft) |
| `evtl. Überschneidung` | Gegenüber ist eine Blockveranstaltung – nur der Zeitraum ist bekannt |
| `Block – nicht sicher prüfbar` | eigener Termin ist Block/unklarer Rhythmus |
| `keine Termine` | in ZEuS ist kein Termin hinterlegt |

Belegt man selbst eine **Blockveranstaltung**, deren Zeitraum länger als eine
Woche ist, belegt die App die Zeit *nicht* automatisch – sonst stünde der halbe
Katalog auf "evtl. Überschneidung" und der Filter wäre wertlos. Stattdessen
steht der Block in "Mein Plan" mit einem Knopf, daraus einen echten Blocker zu
machen (Schwelle: `SPAN_MAX_DAYS` in `app.py`).

Der Plan ist an `(unit_id, period_id, Parallelgruppen-Index)` gebunden, nicht an
die `groups.id` – die wird bei `reparse`/`crawl` neu vergeben.
