"""Durchläuft den öffentlichen Vorlesungsverzeichnis-Baum von ZEuS per GET."""
from __future__ import annotations

import concurrent.futures as cf
import html as _html
import logging
import re
import threading
import urllib.parse
from dataclasses import dataclass, field

from bs4 import BeautifulSoup

from .session import START_FLOW, ZeusSession

log = logging.getLogger(__name__)

CATALOG_FLOW = "showCourseCatalog-flow"
_UNIT_RE = re.compile(r"unitId=(\d+)")
_PATH_RE = re.compile(r"[?&]path=([^\"&]+)")


@dataclass
class Node:
    """Eine Zeile im Baum des Vorlesungsverzeichnisses."""

    path: str                       # z.B. "title:29191|title:29192"
    name: str
    kind: str                       # Icon-Titel, z.B. "Veranstaltungskonto"/"Veranstaltung"
    unit_id: int | None = None      # gesetzt => Blatt/Veranstaltung mit Detailseite
    parent: str | None = None

    @property
    def depth(self) -> int:
        return self.path.count("|")


@dataclass
class CatalogCrawl:
    """Ergebnis eines Baumdurchlaufs."""

    period_id: int
    nodes: dict[str, Node] = field(default_factory=dict)          # path -> Node
    unit_paths: dict[int, set[str]] = field(default_factory=dict)  # unitId -> Pfade

    @property
    def unit_ids(self) -> list[int]:
        return sorted(self.unit_paths)


def catalog_url(period_id: int, path: str | None = None) -> str:
    q = {"_flowId": CATALOG_FLOW, "periodId": str(period_id)}
    if path:
        q["path"] = path
    return f"{START_FLOW}?" + urllib.parse.urlencode(q)


def semesters(sess: ZeusSession) -> dict[int, str]:
    """periodId -> Semestername, aus dem Semester-Dropdown des Katalogs."""
    html = sess.get(f"{START_FLOW}?_flowId={CATALOG_FLOW}")
    soup = BeautifulSoup(html, "lxml")
    out: dict[int, str] = {}
    for sel in soup.select("select"):
        opts = sel.select("option")
        if not any("semester" in o.get_text(strip=True).lower() for o in opts):
            continue
        for o in opts:
            val, label = o.get("value", ""), o.get_text(" ", strip=True)
            if val.isdigit() and label:
                out[int(val)] = label
        if out:
            break
    return out


def root_path(sess: ZeusSession, period_id: int) -> str | None:
    """Wurzelknoten ("Vorlesungsverzeichnis <Semester>") für ein Semester finden."""
    page = _html.unescape(sess.get(catalog_url(period_id)))
    paths = {urllib.parse.unquote(m.group(1)) for m in _PATH_RE.finditer(page)}
    roots = sorted((p for p in paths if "|" not in p and p.startswith("title:")), key=len)
    return roots[0] if roots else None


def _parse_rows(html: str, parent: str) -> list[Node]:
    """Kindzeilen einer Baumseite lesen: Pfad, Name, Icon-Typ und ggf. unitId."""
    soup = BeautifulSoup(html, "lxml")
    rows: dict[str, Node] = {}
    for tr in soup.select("tr"):
        link = tr.find("input", id="autologinRequestUrl")
        if not link or not link.get("value"):
            continue
        m = _PATH_RE.search(link["value"])
        if not m:
            continue
        path = urllib.parse.unquote(m.group(1))
        # nur direkte Kinder des angefragten Knotens
        if not path.startswith(parent + "|") or path.count("|") != parent.count("|") + 1:
            continue
        name_el = tr.select_one("span.treeElementName")
        name = name_el.get_text(" ", strip=True) if name_el else ""
        icon = name_el.find("img") if name_el else None
        kind = (icon.get("title") or icon.get("alt") or "") if icon else ""
        unit_id = None
        for a in tr.select("a[href]"):
            um = _UNIT_RE.search(a["href"])
            if um and "detailView-flow" in a["href"]:
                unit_id = int(um.group(1))
                break
        rows[path] = Node(path=path, name=name, kind=kind, unit_id=unit_id, parent=parent)
    return list(rows.values())


def crawl(sess: ZeusSession, period_id: int, *, root: str | None = None,
          max_nodes: int | None = None, progress=None, workers: int = 1,
          session_factory=None) -> CatalogCrawl:
    """Kompletten Baum eines Semesters durchlaufen und alle unitIds einsammeln.

    Knoten, deren Zeile bereits eine Detailseite verlinkt (`unitId`), sind Blaetter
    und werden nicht weiter aufgeklappt. Der Durchlauf ist ebenenweise; mit
    `workers > 1` wird jede Ebene parallel geholt - dann muss `session_factory`
    pro Thread eine eigene Session liefern (Cookie-Jar ist nicht teilbar).
    """
    root = root or root_path(sess, period_id)
    if not root:
        raise RuntimeError(f"Kein Wurzelknoten fuer periodId={period_id} gefunden")

    res = CatalogCrawl(period_id=period_id)
    res.nodes[root] = Node(path=root, name="Vorlesungsverzeichnis", kind="root")
    frontier: list[str] = [root]
    seen: set[str] = {root}
    visited = 0
    lock = threading.Lock()

    def fetch(path: str) -> list[Node]:
        s = session_factory() if session_factory else sess
        return _parse_rows(s.get(catalog_url(period_id, path)), path)

    while frontier:
        if max_nodes is not None and visited >= max_nodes:
            log.warning("max_nodes=%s erreicht, Abbruch", max_nodes)
            break
        if max_nodes is not None:
            frontier = frontier[: max_nodes - visited]
        nxt: list[str] = []
        with cf.ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
            for fut in cf.as_completed([ex.submit(fetch, p) for p in frontier]):
                try:
                    children = fut.result()
                except Exception as e:      # ein defekter Knoten darf den Lauf nicht stoppen
                    log.warning("Knoten fehlgeschlagen: %s", e)
                    continue
                with lock:
                    visited += 1
                    for node in children:
                        res.nodes.setdefault(node.path, node)
                        if node.unit_id is not None:
                            res.unit_paths.setdefault(node.unit_id, set()).add(node.path)
                        elif node.path not in seen:
                            seen.add(node.path)
                            nxt.append(node.path)
                    if progress:
                        progress(visited, len(nxt), len(res.unit_paths))
        frontier = nxt
    return res
