# ZEuS (zeus.uni-konstanz.de) — Datenquellen-Recherche

System: **HISinOne** (HIS eG), Kontextpfad `/hioserver`.

## Gibt es eine öffentliche API?
Nein. Geprüft: `/robots.txt` (404), `/hioserver/rest`, `/hioserver/rest/api`,
`/hioserver/qisserver/rest` (alle 404), `/hioserver/api` (403).
HISinOne hat eine SOAP/REST-Schnittstelle nur für angebundene Systeme, nicht öffentlich.
=> Wir brauchen einen Crawler auf den öffentlichen (login-freien) Seiten.

## Was öffentlich (ohne Login) erreichbar ist
Alle Seiten brauchen eine JSESSIONID (Cookie-Jar), dann funktionieren **GET-Permalinks**:

1. **Vorlesungsverzeichnis-Baum** (navigierbar per GET, das ist der Schlüssel):
   `…/hioserver/pages/startFlow.xhtml?_flowId=showCourseCatalog-flow&periodId=<PID>&path=title:29191|title:29192|…`
   - `path` ist der URL-kodierte Pfad durch den Baum, Knoten getrennt mit `|`
   - Knotentypen: `title:<id>` (Ordner), `exam:<id>` (Blatt = Veranstaltung/Modul)
   - Wurzel WS2026/27: `periodId=797`, `path=title:29191`

2. **Detailseite einer Veranstaltung** (enthält alles, was wir brauchen):
   `…/hioserver/pages/startFlow.xhtml?_flowId=detailView-flow&unitId=<ID>&periodId=<PID>`
   Enthält: Titel, Nummer (z.B. `CHE-11520`), Organisationseinheit, Veranstaltungsart,
   Angebotshäufigkeit, alle **Parallelgruppen** und je Gruppe die **Termine**:
   Rhythmus, Wochentag, Von–Bis, Startdatum–Enddatum, Ausfalltermine, Raum, Durchführende.
   (Der ICS-Export auf der Seite ist ein JSF-Postback, kein Link.)

3. **Tagesaktuelle Veranstaltungen** (alle Termine eines Tages, flache Tabelle):
   `…/hioserver/pages/cm/exa/timetable/currentLectures.xhtml?_flowId=showEventsAndExaminationsOnDate-flow`
   Spalten: Titel, Beginn, Ende, Nummer, Parallelgruppe, Art, Dozent*in, Räume (Gebäude), Semester.
   Datumswechsel = JSF-POST (ViewState + authenticity_token nötig).

4. **Veranstaltungssuche**: `_flowId=searchCourseNonStaff-flow` — komplexes JSF-Formular, POST.

## Semester-IDs (periodId)
WS 2026/27 = 797. Weitere aus dem Semester-Dropdown des Katalogs auslesbar.

## Empfohlene Strategie
1. Baum per GET durchlaufen -> `unitId`s einsammeln -> Detailseiten parsen.
   Rein GET, cache-bar, kein JSF-State-Handling nötig.
2. **Zusätzlich** die Veranstaltungssuche auswerten: der Baum ist unvollständig
   (siehe unten), die Suche kennt alle Veranstaltungen des Semesters.

## Bestätigte Zahlen (Stand 22.09.2026, WS 2026/27, periodId 797)
- Baum: **2206 Knoten**, daraus **4160 verlinkte Elemente** (unitIds). Davon sind
  laut Detailseite nur **1588 Veranstaltungen**; der Rest sind **2533 Module**
  und **39 Flexibilisierungsmodule** – Gliederungselemente ohne eigene Termine.
- Knotenarten: `Überschriftenelement`, `Veranstaltungskonto`, `Prüfungsordnung`,
  Blätter mit `detailView`-Link = Veranstaltung.
- Eine Seite zeigt immer den von der Wurzel aufgeklappten Pfad; pro Abruf sind
  nur die direkten Kinder des angefragten Knotens neu.
- Semester-IDs (aus dem Dropdown):
  797 WS26/27 · 796 SoSe26 · 794 WS25/26 · 795 SoSe25 · 793 WS24/25 · 792 SoSe24 ·
  791 WS23/24 · 790 SoSe23 · 556 WS22/23 · 557 SoSe22 · 558 WS21/22 · 555 SoSe21 ·
  181 WS20/21 · 173 SoSe20 · 180 WS19/20 · 179 SoSe19 · 78 WS18/19 · 77 SoSe18 · 76 WS17/18

## Fallstricke, die beim Bau aufgetaucht sind
- **Session-Pflicht**: ohne JSESSIONID läuft jeder Permalink in eine
  Redirect-Schleife (`curl: (47)`). Das ist gleichzeitig das Retry-Signal:
  Cookie-Jar wegwerfen, neue Session, einmal wiederholen.
- **Kein Thread-Sharing**: eine Session = eine Spring-Webflow-Execution. Pro
  Worker ein eigener Cookie-Jar; das Rate-Limit dagegen global.
- **Prüfungstermine sehen aus wie Lehrtermine**: Container-ID enthält dann
  `examinationPeriod_`, und die Tabelle hat andere Spalten (`Prüfungsdatum`,
  `Anmeldefrist`, `Prüfer*in`). Spalten deshalb über die Überschriften zuordnen,
  nie über feste Indizes.
- **ICS-Export und "Einzeltermine anzeigen"** auf der Detailseite sind
  JSF-Postbacks; der ICS-Button liefert nachgebaut nur die Seite zurück. Deshalb
  wird der Rhythmus selbst aufgelöst (`zeus/expand.py`).
- **Tagesaktuelle Veranstaltungen** zeigen nur laufendes/kommendes Semester und
  maximal 300 Zeilen pro Seite (Blättern nur per AJAX). Taugt als Stichprobe,
  nicht als vollständige Quelle.

## Der VV-Baum ist unvollständig
Gegenprobe mit der Tagesliste für den 10.11.2026: von 268 Veranstaltungen des
WS 2026/27 fehlten **36 (rund 13 %)** im Baum – Veranstaltungen, die keinem
Studiengangsknoten zugeordnet sind (Kolloquien, Mitarbeiterseminare, Gremien,
einzelne Advanced Courses).

Die **Veranstaltungssuche** (`_flowId=searchCourseNonStaff-flow`) meldet für
WS 2026/27 **1818 Veranstaltungen** gegenüber 1588 aus dem Baum. Sie ist damit
die vollständige Quelle, aber sperriger:
- POST auf das Formular `genericSearchMask`; die Ergebnisseite trägt ein
  *anderes* Formular (`genSearchRes`) – beim Umstellen der Zeilenzahl muss man
  dessen Felder senden, sonst kommt das Suchergebnis zurückgesetzt zurück.
- Semesterwert der Auswahl ist nicht die periodId, sondern `eq|<1=SoSe,2=WiSe>|<Jahr>`,
  z.B. `eq|2|2026` für das WS 2026/27.
- Maximal **300 Zeilen pro Seite**, Weiterblättern nur per AJAX. Die Suche muss
  also in Scheiben zerlegt werden. Die brauchbarste Scheibe ist die
  **Veranstaltungsart**: das Auswahlfeld hat 78 Werte inklusive
  "(nicht gefüllt)" (`ISNULL`) und zerlegt die Treffermenge damit lückenlos –
  jede Veranstaltung hat genau eine Art. Für WS 2026/27 liefert das 1817 der
  1818 Treffer in rund 165 Sekunden; den Rest fängt eine zweite Runde nach
  Nummernpräfix ab. Einzelne Arten über 300 Treffer ("Seminar": 432) werden
  zusätzlich nach Anfangsbuchstabe zerlegt.
- Das Feld "Suchbegriffe" ist eine Volltextsuche über Nummer, Titel und
  Beteiligte – Überschneidungen zwischen Scheiben sind normal und werden über
  die `unitId` dedupliziert.
- Nummern sind nicht immer `XXX-12345`: es gibt rein numerische Fachcodes
  (`186-11710`) und Freitext (`Platzvergabe`). Ein Präfix-Regex auf
  Großbuchstaben allein verliert diese Einträge.
- Umfang pro Semester: WS 2026/27 1818, SoSe 2026 1940, WS 2025/26 1916,
  SoSe 2020 1853, WS 2017/18 2103 Veranstaltungen.
