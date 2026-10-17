#!/usr/bin/env python3
"""
check_bookmakers.py - Health check dei client di scraping (Snai, William Hill,
Eurobet, Betfair e, opzionalmente, Bwin).

Serve a scoprire SUBITO quando un sito cambia endpoint o struttura dei dati,
prima che l'oddsmatcher produca rating sbagliati. Usa il codice reale dei tuoi
client (snai_client.py, williamhill_client.py, eurobet_client.py) e le funzioni
di oddsmatcher.py, quindi se il check passa, passano anche i client.

Cosa controlla, per ogni sito:
  1. HTTP        richieste riuscite, status, JSON valido, latenza
  2. Contratto   i campi che il parser usa esistono ancora e hanno il tipo giusto
                 (se un campo sparisce propone i campi "vicini": possibile rinomina)
  3. Baseline    confronto della struttura con quella salvata (--update-baseline):
                 elenca campi spariti / comparsi / cambiati di tipo
  4. Discovery   i campionati attesi vengono ancora trovati (menu/alberatura/slug)
  5. Estrazione  quanti eventi 1X2 estrae il client, overround, orari, nomi, duplicati
  6. Coerenza    confronto con Betfair sulle stesse partite: scala delle quote,
                 esiti 1/X/2 invertiti, fuso orario dei kickoff, tasso di abbinamento
Se un sito fallisce, salva la risposta grezza in health_reports/raw/ cosi' puoi
adattare il parser senza rifare le richieste.

Uso:
    python check_bookmakers.py                      # live: Snai, William Hill, Eurobet (HTTP), Betfair (file)
    python check_bookmakers.py --site snai wh       # solo alcuni siti
    python check_bookmakers.py --offline            # usa i campioni locali (campioni_3_siti.json, schema_finale.json)
    python check_bookmakers.py --update-baseline    # salva la struttura attuale come riferimento
    python check_bookmakers.py --eurobet-browser    # esegue l'EurobetClient completo con Playwright
    python check_bookmakers.py --bwin-file bwin_serie_a_raw.json

Codici di uscita: 0 = tutto ok, 1 = ci sono WARN, 2 = ci sono FAIL.
Utile in cron o prima di lanciare gli agenti:  python check_bookmakers.py && python oddsmatcher.py
"""

from __future__ import annotations

import argparse
import importlib
import json
import os
import re
import statistics
import sys
import textwrap
import time
import traceback
import types
from collections import Counter
from contextlib import contextmanager, nullcontext
from datetime import datetime, timedelta, timezone
from itertools import permutations
from math import log
from typing import Any, Dict, List, Optional, Tuple
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
for _p in (HERE, os.getcwd()):
    if _p not in sys.path:
        sys.path.insert(0, _p)

try:
    import oddsmatcher as om
    from rapidfuzz import fuzz, process
    from tabulate import tabulate
except Exception as _e:  # pragma: no cover
    print(f"[!] Dipendenza mancante o oddsmatcher.py non trovato nella stessa cartella: {_e}")
    print("    pip install rapidfuzz tabulate curl_cffi")
    sys.exit(2)

BASELINE_DIR = "health_baselines"
REPORT_DIR = "health_reports"

EXPECTED_LEAGUES = [
    "Serie A (IT)", "Serie B (IT)", "Premier League (UK)", "La Liga (ES)",
    "Bundesliga (DE)", "Ligue 1 (FR)", "Champions League", "Europa League",
]

EUROBET_PROBES = {
    "next-events": "https://www.eurobet.it/prematch-homepage-service/api/v2/sport-schedule/services/prematch-homepage/next-events/calcio/10",
    "sport-list": "https://www.eurobet.it/prematch-menu-service/api/v2/sport-schedule/services/sport-list/calcio",
}

# --------------------------------------------------------------------------
# Contratti: percorso (notazione a punti, [] = elementi di lista, * = valori di
# un dict), tipi ammessi, livello (req = obbligatorio, opt = facoltativo)
# --------------------------------------------------------------------------

CONTRACTS: Dict[str, Dict[str, List[Tuple[str, str, str]]]] = {
    "snai": {
        "league": [
            ("avvenimentoFeList", "list", "req"),
            ("avvenimentoFeList[].key", "str", "req"),
            ("avvenimentoFeList[].descrizione", "str", "req"),
            ("avvenimentoFeList[].data", "str", "req"),
            ("avvenimentoFeList[].live", "bool", "req"),
            ("avvenimentoFeList[].eventType", "str", "req"),
            ("avvenimentoFeList[].firstCompetitor.description", "str", "req"),
            ("avvenimentoFeList[].secondCompetitor.description", "str", "req"),
            ("infoAggiuntivaMap", "dict", "req"),
            ("infoAggiuntivaMap.*.esitoList", "list", "req"),
            ("infoAggiuntivaMap.*.esitoList[].descrizione", "str", "req"),
            ("infoAggiuntivaMap.*.esitoList[].quota", "num", "req"),
        ],
    },
    "williamhill": {
        "league": [
            ("avs", "list", "req"),
            ("avs[].dsl.IT", "str", "req"),
            ("avs[].ts", "str", "req"),
            ("avs[].lv", "bool", "req"),
            ("avs[].ant", "bool", "opt"),
            ("avs[].scs", "list", "req"),
            ("avs[].scs[].cs", "int", "req"),
            ("avs[].scs[].h", "num", "req"),
            ("avs[].scs[].eqs", "list", "req"),
            ("avs[].scs[].eqs[].ce", "int", "req"),
            ("avs[].scs[].eqs[].q", "num", "req"),
        ],
    },
    "eurobet": {
        "next-events": [
            ("result.events", "list", "req"),
            ("result.events[].eventInfo.eventDescription", "str", "req"),
            ("result.events[].eventInfo.eventData", "num", "req"),
            ("result.events[].eventInfo.live", "bool", "req"),
            ("result.events[].eventInfo.meetingDescription", "str", "opt"),
            ("result.events[].betGroupList", "list", "req"),
        ],
        "sport-list": [
            ("result.itemList", "list", "req"),
            ("result.itemList[].code", "str", "req"),
            ("result.itemList[].description", "str", "req"),
            ("result.itemList[].itemType", "str", "req"),
            ("result.itemList[].aliasUrl", "str", "req"),
        ],
    },
    "betfair": {
        "file": [
            ("eventTypes[].eventNodes", "list", "req"),
            ("eventTypes[].eventNodes[].competition", "str", "req"),
            ("eventTypes[].eventNodes[].marketNodes[].marketId", "str", "req"),
            ("eventTypes[].eventNodes[].marketNodes[].description.marketType", "str", "req"),
            ("eventTypes[].eventNodes[].marketNodes[].description.marketTime", "str", "req"),
            ("eventTypes[].eventNodes[].marketNodes[].state.status", "str", "req"),
            ("eventTypes[].eventNodes[].marketNodes[].state.inplay", "bool", "req"),
            ("eventTypes[].eventNodes[].marketNodes[].rates.marketBaseRate", "num", "req"),
            ("eventTypes[].eventNodes[].marketNodes[].runners[].description.runnerName", "str", "req"),
            ("eventTypes[].eventNodes[].marketNodes[].runners[].state.sortPriority", "int", "req"),
            ("eventTypes[].eventNodes[].marketNodes[].runners[].state.status", "str", "req"),
            ("eventTypes[].eventNodes[].marketNodes[].runners[].exchange.availableToBack[].price", "num", "opt"),
            ("eventTypes[].eventNodes[].marketNodes[].runners[].exchange.availableToLay[].price", "num", "req"),
            ("eventTypes[].eventNodes[].marketNodes[].runners[].exchange.availableToLay[].size", "num", "req"),
        ],
    },
    "bwin": {
        "file": [
            ("fixtures", "list", "req"),
            ("fixtures[].id", "str", "req"),
            ("fixtures[].name.value", "str", "req"),
            ("fixtures[].startDate", "str", "req"),
            ("fixtures[].stage", "str", "req"),
            ("fixtures[].competition.name.value", "str", "req"),
            ("fixtures[].games[].name.value", "str", "req"),
            ("fixtures[].games[].results[].odds", "num", "req"),
            ("fixtures[].games[].results[].name.value", "str", "req"),
        ],
    },
}


# --------------------------------------------------------------------------
# Report
# --------------------------------------------------------------------------

STATUS_RANK = {"OK": 0, "INFO": 0, "SKIP": 0, "WARN": 1, "FAIL": 2}
COLORS = {"OK": "\033[32m", "WARN": "\033[33m", "FAIL": "\033[31m", "SKIP": "\033[90m", "INFO": "\033[36m"}


class Report:
    def __init__(self) -> None:
        self.rows: List[Dict[str, str]] = []

    def add(self, site: str, check: str, status: str, detail: str = "", hint: str = "") -> None:
        self.rows.append({"site": site, "check": check, "status": status, "detail": detail, "hint": hint})

    def site_worst(self, site: str) -> int:
        return max([STATUS_RANK[r["status"]] for r in self.rows if r["site"] == site] or [0])

    def worst(self) -> int:
        return max([STATUS_RANK[r["status"]] for r in self.rows] or [0])


def short(s: Any, n: int = 100) -> str:
    s = str(s)
    return s if len(s) <= n else s[: n - 3] + "..."


# --------------------------------------------------------------------------
# Utilita' sulla struttura JSON
# --------------------------------------------------------------------------

def _tname(v: Any) -> str:
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "bool"
    if isinstance(v, int):
        return "int"
    if isinstance(v, float):
        return "float"
    if isinstance(v, str):
        return "str"
    if isinstance(v, list):
        return "list"
    if isinstance(v, dict):
        return "dict"
    return type(v).__name__


def _type_ok(v: Any, spec: str, allow_null: bool) -> bool:
    names = spec.split("|")
    t = _tname(v)
    if t == "null":
        return allow_null or "null" in names
    if "num" in names and t in ("int", "float"):
        return True
    return t in names


def resolve(node: Any, path: str) -> List[Any]:
    """Valori trovati seguendo il percorso (es. 'avs[].scs[].eqs[].q')."""
    cur = [node]
    for part in path.split("."):
        is_list = part.endswith("[]")
        key = part[:-2] if is_list else part
        nxt: List[Any] = []
        for n in cur:
            if key == "*":
                vals = list(n.values()) if isinstance(n, dict) else []
            else:
                vals = [n[key]] if isinstance(n, dict) and key in n else []
            for v in vals:
                if is_list:
                    if isinstance(v, list):
                        nxt.extend(v)
                else:
                    nxt.append(v)
        cur = nxt
    return cur


def count_key(node: Any, key: str) -> int:
    if isinstance(node, dict):
        return (1 if key in node else 0) + sum(count_key(v, key) for v in node.values() if isinstance(v, (dict, list)))
    if isinstance(node, list):
        return sum(count_key(v, key) for v in node if isinstance(v, (dict, list)))
    return 0


_DYNAMIC_KEY = re.compile(r"^[\d\-_:.]+$")


def fingerprint(node: Any, max_depth: int = 9, max_items: int = 300) -> Dict[str, set]:
    acc: Dict[str, set] = {}

    def walk(n: Any, path: str, depth: int) -> None:
        acc.setdefault(path or "$", set()).add(_tname(n))
        if depth >= max_depth:
            return
        if isinstance(n, dict):
            dynamic = bool(n) and all(_DYNAMIC_KEY.match(str(k)) for k in n)
            for k, v in n.items():
                key = "*" if dynamic else str(k)
                walk(v, f"{path}.{key}" if path else key, depth + 1)
        elif isinstance(n, list):
            for it in n[:max_items]:
                walk(it, path + "[]", depth + 1)

    walk(node, "", 0)
    return acc


def merge_fp(fps: List[Dict[str, set]]) -> Dict[str, set]:
    out: Dict[str, set] = {}
    for fp in fps:
        for p, t in fp.items():
            out.setdefault(p, set()).update(t)
    return out


def suggest_paths(missing: str, fp_paths: Dict[str, set], n: int = 3) -> List[str]:
    parent = missing.rsplit(".", 1)[0] if "." in missing else ""
    leaf = missing.rsplit(".", 1)[-1]
    cands = [p for p in fp_paths
             if p != missing and p != "$" and (p.rsplit(".", 1)[0] if "." in p else "") == parent]
    if not cands:
        return []
    scored = process.extract(leaf, [c.rsplit(".", 1)[-1] for c in cands], scorer=fuzz.ratio, limit=n)
    return [cands[idx] for (_txt, score, idx) in scored if score >= 40]


# --------------------------------------------------------------------------
# HTTP: sessione che registra le chiamate
# --------------------------------------------------------------------------

class _UrlResp:
    def __init__(self, status: int, headers: Dict[str, str], content: bytes):
        self.status_code, self.headers, self.content = status, headers, content

    def json(self) -> Any:
        return json.loads(self.content.decode("utf-8"))


class UrllibSession:
    """Ripiego senza curl_cffi (puo' essere bloccato dai WAF)."""

    def get(self, url: str, params: Optional[Dict[str, Any]] = None,
            headers: Optional[Dict[str, str]] = None, timeout: int = 15, **_: Any) -> _UrlResp:
        import urllib.error
        import urllib.parse
        import urllib.request
        if params:
            url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers=headers or {"User-Agent": "Mozilla/5.0"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return _UrlResp(r.status, dict(r.headers), r.read())
        except urllib.error.HTTPError as e:
            return _UrlResp(e.code, dict(e.headers or {}), e.read() or b"")


def make_session() -> Any:
    try:
        from curl_cffi import requests as cr
        return cr.Session(impersonate="chrome120")
    except Exception:
        return UrllibSession()


class RecordingSession:
    """Avvolge la sessione del client e registra ogni chiamata (URL, status, tempi, JSON)."""

    def __init__(self, inner: Any, calls: List[Dict[str, Any]]):
        self._inner, self.calls = inner, calls

    def get(self, url: str, **kw: Any) -> Any:
        rec: Dict[str, Any] = {"url": url, "params": kw.get("params"), "status": None, "ms": None,
                               "ctype": "", "size": 0, "json": None, "error": None}
        self.calls.append(rec)
        t0 = time.time()
        try:
            r = self._inner.get(url, **kw)
        except Exception as e:
            rec["error"] = f"{type(e).__name__}: {e}"
            rec["ms"] = int((time.time() - t0) * 1000)
            raise
        rec["ms"] = int((time.time() - t0) * 1000)
        rec["status"] = r.status_code
        try:
            rec["ctype"] = r.headers.get("content-type", "")
        except Exception:
            pass
        try:
            rec["size"] = len(r.content)
        except Exception:
            pass
        try:
            rec["json"] = r.json()
        except Exception as e:
            rec["error"] = f"JSON non valido: {type(e).__name__}"
        return r

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


@contextmanager
def ignore_local_dumps():
    """Impedisce ai client di leggere i dump locali (debug_bookies_raw/...), cosi'
    la discovery viene testata davvero sull'API live."""
    real = os.path.exists

    def fake(p: Any) -> bool:
        return False if "debug_bookies_raw" in str(p).replace("\\", "/") else real(p)

    with mock.patch("os.path.exists", fake):
        yield


def import_client(modname: str) -> Tuple[Optional[types.ModuleType], Optional[str]]:
    try:
        return importlib.import_module(modname), None
    except ModuleNotFoundError as e:
        if modname == "eurobet_client" and (e.name or "").startswith("playwright"):
            # Per il solo test HTTP non serve Playwright: modulo finto per poter importare il client.
            pw = types.ModuleType("playwright")
            sync = types.ModuleType("playwright.sync_api")
            sync.sync_playwright = None  # type: ignore[attr-defined]
            sync.Response = object  # type: ignore[attr-defined]
            sys.modules.setdefault("playwright", pw)
            sys.modules["playwright.sync_api"] = sync
            try:
                return importlib.import_module(modname), None
            except Exception as e2:
                return None, f"{type(e2).__name__}: {e2}"
        hint = " (pip install curl_cffi)" if e.name == "curl_cffi" else ""
        return None, f"modulo '{e.name}' non trovato{hint}. Il file {modname}.py e' nella stessa cartella?"
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"


# --------------------------------------------------------------------------
# Check riutilizzabili
# --------------------------------------------------------------------------

def check_http(rep: Report, site: str, calls: List[Dict[str, Any]]) -> None:
    if not calls:
        rep.add(site, "HTTP", "SKIP", "nessuna richiesta (modalita' offline o sito non interrogato)")
        return
    bad = [c for c in calls if c["error"] or c["status"] != 200]
    ms = [c["ms"] for c in calls if c["ms"] is not None]
    detail = f"{len(calls) - len(bad)}/{len(calls)} richieste OK"
    if ms:
        detail += f", mediana {int(statistics.median(ms))} ms, max {max(ms)} ms"
    hint = ""
    for c in bad[:5]:
        detail += f"\n  {short(c['url'], 80)} -> status {c['status']} {c['error'] or ''}"
    codes = {c["status"] for c in bad}
    if codes & {403, 429}:
        hint = "Blocco anti-bot o rate limit: aumenta --delay, verifica impersonate (curl_cffi) o cambia IP."
    elif codes & {404, 410}:
        hint = "Endpoint cambiato: apri il sito con DevTools > Network > Fetch/XHR e individua il nuovo URL."
    elif bad and any(c["error"] and "JSON" in c["error"] for c in bad):
        hint = "Risposta non JSON (pagina di blocco o HTML): guarda il dump in health_reports/raw/."
    if not bad:
        status = "WARN" if ms and statistics.median(ms) > 5000 else "OK"
    else:
        status = "FAIL" if len(bad) == len(calls) else "WARN"
    rep.add(site, "HTTP", status, detail, hint)


def check_contract(rep: Report, site: str, role: str, payloads: List[Any],
                   contract: List[Tuple[str, str, str]], fp: Dict[str, set]) -> None:
    ok = 0
    problems: List[Tuple[str, str, str, str]] = []  # (status, path, detail, hint)
    for path, types_, level in contract:
        vals = [v for p in payloads for v in resolve(p, path)]
        if not vals:
            sugg = suggest_paths(path, fp)
            hint = ("Possibile rinomina: " + ", ".join(sugg)) if sugg else ""
            problems.append(("FAIL" if level == "req" else "WARN", path, "campo assente in tutte le risposte", hint))
            continue
        good = sum(1 for v in vals if _type_ok(v, types_, allow_null=(level == "opt")))
        if good == len(vals):
            ok += 1
        else:
            seen = Counter(_tname(v) for v in vals if not _type_ok(v, types_, level == "opt"))
            ratio = good / len(vals)
            st = "WARN" if ratio >= 0.9 or level == "opt" else "FAIL"
            problems.append((st, path, f"tipo atteso {types_}, trovati {dict(seen)} ({len(vals) - good}/{len(vals)})", ""))
    total = len(contract)
    worst = "OK"
    for st, *_ in problems:
        if STATUS_RANK[st] > STATUS_RANK[worst]:
            worst = st
    rep.add(site, f"Contratto [{role}]", worst, f"{ok}/{total} percorsi conformi")
    for st, path, detail, hint in problems:
        rep.add(site, f"  campo {path}", st, detail, hint)


def baseline_path(site_key: str, role: str) -> str:
    return os.path.join(BASELINE_DIR, f"{site_key}__{role}.json")


def check_baseline(rep: Report, site: str, site_key: str, role: str, fp: Dict[str, set]) -> None:
    path = baseline_path(site_key, role)
    if not os.path.exists(path):
        rep.add(site, f"Baseline [{role}]", "INFO", "nessuna baseline salvata (usa --update-baseline quando tutto funziona)")
        return
    try:
        with open(path, "r", encoding="utf-8") as f:
            base = {k: set(v) for k, v in json.load(f)["paths"].items()}
    except Exception as e:
        rep.add(site, f"Baseline [{role}]", "WARN", f"baseline illeggibile: {e}")
        return
    removed = sorted(p for p in base if p not in fp)
    added = sorted(p for p in fp if p not in base)
    changed = sorted(p for p in base if p in fp and not fp[p] <= (base[p] | {"null"}))
    if not (removed or changed):
        extra = f", {len(added)} campi nuovi (informativo)" if added else ""
        rep.add(site, f"Baseline [{role}]", "OK", f"struttura invariata ({len(base)} percorsi){extra}")
        return
    parts = []
    if removed:
        parts.append(f"spariti {len(removed)}: " + ", ".join(removed[:6]) + (" ..." if len(removed) > 6 else ""))
    if changed:
        parts.append(f"tipo cambiato {len(changed)}: " + ", ".join(f"{p} {sorted(base[p])}->{sorted(fp[p])}" for p in changed[:4]))
    if added:
        parts.append(f"nuovi {len(added)}: " + ", ".join(added[:6]) + (" ..." if len(added) > 6 else ""))
    hint = ""
    if removed and added:
        pairs = []
        for r in removed[:5]:
            s = suggest_paths(r, {a: set() for a in added}, n=1)
            if s:
                pairs.append(f"{r} -> {s[0]}")
        if pairs:
            hint = "Possibili rinomine: " + "; ".join(pairs)
    rep.add(site, f"Baseline [{role}]", "WARN", "\n".join(parts), hint)


def save_baseline(site_key: str, role: str, fp: Dict[str, set]) -> None:
    os.makedirs(BASELINE_DIR, exist_ok=True)
    with open(baseline_path(site_key, role), "w", encoding="utf-8") as f:
        json.dump({"site": site_key, "role": role, "saved_at": datetime.now(timezone.utc).isoformat(),
                   "paths": {k: sorted(v) for k, v in sorted(fp.items())}}, f, indent=1, ensure_ascii=False)


def save_raw(site_key: str, role: str, payload: Any) -> str:
    d = os.path.join(REPORT_DIR, "raw")
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, f"{site_key}_{role}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json")
    with open(p, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)
    return p


def _to_bookie(e: Dict[str, Any]) -> "om.BookieEvent":
    odds = (e.get("markets") or {}).get("1X2") or {}
    return om.BookieEvent(
        bookmaker=str(e.get("bookmaker", "?")),
        competition=str(e.get("competition", "Calcio")),
        home_team=str(e.get("home_team", "")),
        away_team=str(e.get("away_team", "")),
        kickoff=om.parse_dt(e.get("kickoff"), naive_tz=om.LOCAL_TZ),
        markets={"1X2": {k: float(odds.get(k) or 0) for k in om.OUTCOMES}},
    )


def check_quality(rep: Report, site: str, events: List[Dict[str, Any]], args: argparse.Namespace,
                  now: datetime, offline: bool) -> None:
    n = len(events)
    if n == 0:
        rep.add(site, "Eventi 1X2 estratti", "FAIL", "0 eventi estratti",
                hint="Il client non estrae piu' nulla: controlla contratto/baseline sopra e il dump grezzo.")
        return
    rep.add(site, "Eventi 1X2 estratti", "OK" if n >= args.min_events else "WARN",
            f"{n} eventi (minimo atteso {args.min_events})")

    ors, extremes, bad = [], 0, 0
    for e in events:
        o = (e.get("markets") or {}).get("1X2") or {}
        vals = [o.get(k) for k in om.OUTCOMES]
        if any(not isinstance(v, (int, float)) or v <= 1.0 for v in vals):
            bad += 1
            continue
        ors.append(sum(1.0 / v for v in vals))
        if max(vals) > 200:
            extremes += 1
    if ors:
        med = statistics.median(ors)
        out = sum(1 for x in ors if not args.min_overround <= x <= args.max_overround)
        st = "OK"
        hint = ""
        if not args.min_overround <= med <= args.max_overround or out / len(ors) > 0.2:
            st = "FAIL"
            hint = ("Somma 1/quota anomala: scala delle quote cambiata (es. /100) o mercato sbagliato "
                    "(Doppia Chance ~2.0, handicap).")
        elif out or extremes or bad:
            st = "WARN"
        rep.add(site, "Sanita' quote (overround)", st,
                f"mediana {med:.3f}, fuori range {out}/{len(ors)}, quote >200: {extremes}, incomplete: {bad}", hint)

    kos = [om.parse_dt(e.get("kickoff"), naive_tz=om.LOCAL_TZ) for e in events]
    parsed = [k for k in kos if k is not None]
    rate = len(parsed) / n
    st = "OK" if rate >= 0.95 else ("WARN" if rate >= 0.5 else "FAIL")
    detail = f"{len(parsed)}/{n} orari interpretati"
    if not offline and parsed:
        fut = sum(1 for k in parsed if k > now)
        detail += f", {fut} nel futuro"
        if fut / len(parsed) < 0.8 and st == "OK":
            st = "WARN"
    sample = next((e.get("kickoff") for e, k in zip(events, kos) if k is None), None)
    rep.add(site, "Orari kickoff", st, detail + (f"; esempio non letto: {short(sample, 40)!r}" if sample is not None else ""),
            hint="Aggiungi il formato a oddsmatcher.parse_dt (_DT_FORMATS)." if st != "OK" else "")

    badnames = [e for e in events
                if not str(e.get("home_team", "")).strip() or not str(e.get("away_team", "")).strip()
                or str(e.get("home_team")).strip() == str(e.get("away_team")).strip()
                or " - " in str(e.get("home_team")) or " - " in str(e.get("away_team"))]
    keys = Counter((om.norm_team(str(e.get("home_team"))), om.norm_team(str(e.get("away_team"))),
                    (k.date() if k else None)) for e, k in zip(events, kos))
    dups = sum(c - 1 for c in keys.values() if c > 1)
    st = "OK" if not badnames and not dups else "WARN"
    ex = f"; es. {badnames[0].get('home_team')!r} / {badnames[0].get('away_team')!r}" if badnames else ""
    rep.add(site, "Nomi squadra e duplicati", st, f"nomi anomali {len(badnames)}, duplicati {dups}{ex}")


def check_leagues(rep: Report, site: str, ebl: Dict[str, List[Dict[str, Any]]],
                  found: Optional[Dict[str, Any]], offline: bool) -> None:
    if found is not None:
        miss = [l for l in EXPECTED_LEAGUES if l not in found]
        extra = len(found)
        if miss:
            rep.add(site, "Discovery campionati", "WARN" if len(miss) < len(EXPECTED_LEAGUES) - 1 else "FAIL",
                    f"trovati {extra}; mancanti: {', '.join(miss)}",
                    hint="Menu/alberatura cambiati o nomi diversi: apri il dump grezzo e correggi discover_leagues().")
        else:
            rep.add(site, "Discovery campionati", "OK", f"tutti i {len(EXPECTED_LEAGUES)} campionati attesi trovati")
    if offline:
        return
    counts = {l: len(ebl.get(l, [])) for l in (found if found is not None else ebl)}
    if not counts:
        return
    empty = [l for l, c in counts.items() if c == 0]
    detail = ", ".join(f"{l}: {c}" for l, c in counts.items())
    if len(empty) == len(counts):
        rep.add(site, "Eventi per campionato", "FAIL", detail)
    elif empty:
        rep.add(site, "Eventi per campionato", "WARN", detail,
                "Campionato a 0: pausa/fuori stagione oppure ID/slug non piu' validi.")
    else:
        rep.add(site, "Eventi per campionato", "OK", detail)


def check_crosscheck(rep: Report, site: str, events: List[Dict[str, Any]], bf_events: List["om.BetfairEvent"],
                     args: argparse.Namespace, strict_rate: bool = True) -> None:
    if not events:
        return
    if not bf_events:
        rep.add(site, "Coerenza con Betfair", "SKIP", "nessun evento Betfair valido (file mancante o vuoto)")
        return
    bf_comps = {e.competition.strip().lower() for e in bf_events}
    considered, matched = 0, []
    for e in events:
        b = _to_bookie(e)
        comp = b.competition.strip().lower()
        if comp != "calcio" and comp not in bf_comps:
            continue
        considered += 1
        m = om.find_best_match(b, bf_events, args.threshold, timedelta(hours=3))
        if m:
            matched.append((b, m[0]))

    if considered == 0:
        rep.add(site, "Abbinamento con Betfair", "INFO", "nessun evento in campionati coperti da Betfair")
        return
    rate = len(matched) / considered
    if strict_rate:
        st = "OK" if rate >= 0.7 else ("WARN" if rate >= 0.4 else ("FAIL" if considered >= 5 else "WARN"))
        rep.add(site, "Abbinamento con Betfair", st, f"{len(matched)}/{considered} eventi abbinati ({rate:.0%})",
                hint="Alias mancanti o nomi cambiati: python oddsmatcher.py --show-unmatched" if st != "OK" else "")
    else:
        rep.add(site, "Abbinamento con Betfair", "INFO", f"{len(matched)}/{considered} abbinati (campione generico, non significativo)")

    if len(matched) < 3:
        rep.add(site, "Coerenza quote/esiti vs Betfair", "INFO",
                f"solo {len(matched)} eventi abbinati: servono almeno 3 per validare scala ed ordine degli esiti")
        return

    idx = {"1": 0, "X": 1, "2": 2}
    devs, best_perms, offsets = [], Counter(), []
    for b, bf in matched:
        bk = [b.markets["1X2"][k] for k in om.OUTCOMES]
        mids = []
        for k in om.OUTCOMES:
            d = bf.markets["1X2"][k]
            v = [x for x in (d.back_odds, d.lay_odds) if x > 1.0]
            mids.append(sum(v) / len(v) if v else 0.0)
        if min(bk) <= 1.0 or min(mids) <= 1.0:
            continue
        costs = {p: sum(abs(log(bk[i] / mids[p[i]])) for i in range(3)) / 3 for p in permutations(range(3))}
        best = min(costs, key=costs.get)
        best_perms[best] += 1
        devs.append(costs[(0, 1, 2)])
        if b.kickoff is not None:
            offsets.append((b.kickoff - bf.kickoff).total_seconds() / 60.0)
    if not devs:
        rep.add(site, "Coerenza quote/esiti vs Betfair", "INFO", "quote Betfair non utilizzabili per il confronto")
        return
    ident = best_perms[(0, 1, 2)] / len(devs)
    med = statistics.median(devs)
    detail = f"{len(devs)} eventi, deviazione mediana {med:.1%}, esiti nell'ordine giusto {ident:.0%}"
    if ident < 0.6:
        top, cnt = best_perms.most_common(1)[0]
        lab = {0: "1", 1: "X", 2: "2"}
        mapping = ", ".join(f"{lab[i]}->{lab[top[i]]}" for i in range(3) if top[i] != i)
        rep.add(site, "Coerenza quote/esiti vs Betfair", "FAIL", detail + f"; mappatura probabile: {mapping}",
                hint="Gli esiti 1/X/2 sembrano scambiati nel parser (ordine o codici esito cambiati).")
    elif med > 0.35:
        rep.add(site, "Coerenza quote/esiti vs Betfair", "FAIL", detail,
                hint="Quote molto distanti da Betfair: scala (es. /100), mercato sbagliato o abbinamento errato.")
    elif med > 0.15:
        rep.add(site, "Coerenza quote/esiti vs Betfair", "WARN", detail,
                hint="Deviazione alta: controlla a campione le quote sul sito.")
    else:
        rep.add(site, "Coerenza quote/esiti vs Betfair", "OK", detail)

    if offsets:
        mo = statistics.median(offsets)
        if abs(mo) >= 30:
            rep.add(site, "Fuso orario kickoff", "WARN", f"scarto mediano {mo:+.0f} min rispetto a Betfair",
                    hint="Orari senza fuso interpretati con il fuso sbagliato (UTC vs Europe/Rome). "
                         "Impatto basso sull'abbinamento, ma gli orari del bookmaker sono sfasati.")
        else:
            rep.add(site, "Fuso orario kickoff", "OK", f"scarto mediano {mo:+.0f} min rispetto a Betfair")


def evaluate_site(rep: Report, site: str, site_key: str, *, calls: List[Dict[str, Any]],
                  payloads: Dict[str, List[Any]], ebl: Dict[str, List[Dict[str, Any]]],
                  found: Optional[Dict[str, Any]], bf_events: List["om.BetfairEvent"],
                  args: argparse.Namespace, now: datetime, offline: bool, strict_rate: bool = True) -> None:
    check_http(rep, site, calls)
    fps: Dict[str, Dict[str, set]] = {}
    for role, plist in payloads.items():
        if not plist:
            if not offline:
                rep.add(site, f"Payload [{role}]", "FAIL", "nessuna risposta JSON valida",
                        hint="Vedi riga HTTP: endpoint cambiato o richiesta bloccata.")
            continue
        fps[role] = merge_fp([fingerprint(p) for p in plist])
        contract = CONTRACTS.get(site_key, {}).get(role)
        if contract:
            check_contract(rep, site, role, plist, contract, fps[role])
        check_baseline(rep, site, site_key, role, fps[role])

    events = [e for evs in ebl.values() for e in evs]
    check_leagues(rep, site, ebl, found, offline)
    check_quality(rep, site, events, args, now, offline)
    check_crosscheck(rep, site, events, bf_events, args, strict_rate)

    worst = rep.site_worst(site)
    if worst == 2 and not offline:
        for role, plist in payloads.items():
            if plist:
                p = save_raw(site_key, role, plist[0])
                rep.add(site, "Dump grezzo", "INFO", f"salvato {p}")
    if args.update_baseline:
        if worst < 2 or args.force:
            for role, fp in fps.items():
                save_baseline(site_key, role, fp)
            rep.add(site, "Baseline", "INFO", f"salvata per {', '.join(fps) or 'nessun ruolo'}")
        else:
            rep.add(site, "Baseline", "WARN", "NON aggiornata: ci sono FAIL (usa --force per forzare)")


# --------------------------------------------------------------------------
# Siti
# --------------------------------------------------------------------------

def run_snai(rep: Report, args: argparse.Namespace, bf_events: List[Any], now: datetime, fx: Optional[Dict[str, Any]]) -> None:
    site = "Snai"
    mod, err = import_client("snai_client")
    if err:
        rep.add(site, "Import client", "FAIL", err)
        return
    client = mod.SnaiDirectClient()
    calls: List[Dict[str, Any]] = []
    payloads: Dict[str, List[Any]] = {"tree": [], "league": []}
    ebl: Dict[str, List[Dict[str, Any]]] = {}
    found = None
    if fx is not None:
        data = fx.get("snai")
        if not data:
            rep.add(site, "Campione offline", "SKIP", "nessun campione Snai disponibile")
            return
        payloads = {"league": [data]}
        ebl["Serie A (IT)"] = client._parse_payload(data, "Serie A (IT)")
    else:
        client.session = RecordingSession(client.session, calls)
        with (nullcontext() if args.use_local_dumps else ignore_local_dumps()):
            found = client.discover_leagues()
        payloads["tree"] = [c["json"] for c in calls if "alberaturaPrematch" in c["url"] and c["json"] is not None]
        if not payloads["tree"]:
            rep.add(site, "Alberatura prematch", "FAIL", "alberaturaPrematch non ottenuta: la discovery usa solo i default",
                    hint="Endpoint cambiato o bloccato (vedi HTTP).")
        for name, key in found.items():
            n0 = len(calls)
            ebl[name] = client.fetch_league(name, key)
            payloads["league"] += [c["json"] for c in calls[n0:] if c["json"] is not None]
            time.sleep(args.delay)
    evaluate_site(rep, site, "snai", calls=calls, payloads=payloads, ebl=ebl, found=found,
                  bf_events=bf_events, args=args, now=now, offline=fx is not None)


def run_williamhill(rep: Report, args: argparse.Namespace, bf_events: List[Any], now: datetime, fx: Optional[Dict[str, Any]]) -> None:
    site = "William Hill"
    mod, err = import_client("williamhill_client")
    if err:
        rep.add(site, "Import client", "FAIL", err)
        return
    client = mod.WilliamHillDirectClient()
    calls: List[Dict[str, Any]] = []
    payloads: Dict[str, List[Any]] = {"menu": [], "league": []}
    ebl: Dict[str, List[Dict[str, Any]]] = {}
    found = None
    if fx is not None:
        data = fx.get("williamhill")
        if not data:
            rep.add(site, "Campione offline", "SKIP", "nessun campione William Hill disponibile")
            return
        payloads = {"league": [data]}
        ebl["Serie A (IT)"] = client._parse_payload(data, "Serie A (IT)")
    else:
        client.session = RecordingSession(client.session, calls)
        with (nullcontext() if args.use_local_dumps else ignore_local_dumps()):
            found = client.discover_leagues()
        payloads["menu"] = [c["json"] for c in calls if "getMenuPrematch" in c["url"] and c["json"] is not None]
        if not payloads["menu"]:
            rep.add(site, "Menu prematch", "FAIL", "getMenuPrematch non ottenuto: la discovery usa solo i default",
                    hint="Endpoint cambiato o bloccato (vedi HTTP).")
        for name, cfg in found.items():
            n0 = len(calls)
            ebl[name] = client.fetch_league(name, cfg)
            payloads["league"] += [c["json"] for c in calls[n0:] if c["json"] is not None]
            time.sleep(args.delay)
    evaluate_site(rep, site, "williamhill", calls=calls, payloads=payloads, ebl=ebl, found=found,
                  bf_events=bf_events, args=args, now=now, offline=fx is not None)


def _eurobet_slug_check(rep: Report, site: str, mod: Any, sportlist: Any) -> None:
    meetings: List[Dict[str, Any]] = []

    def walk(n: Any, country: str = "") -> None:
        if isinstance(n, dict):
            if n.get("itemType") == "country":
                country = str(n.get("description", ""))
            if n.get("itemType") == "meeting":
                meetings.append({"alias": str(n.get("aliasUrl", "")), "desc": str(n.get("description", "")),
                                 "country": country, "n": n.get("eventsNr", 0)})
            for v in n.values():
                if isinstance(v, (dict, list)):
                    walk(v, country)
        elif isinstance(n, list):
            for v in n:
                walk(v, country)

    walk(sportlist)
    if not meetings:
        rep.add(site, "Slug campionati (sport-list)", "FAIL", "nessun 'meeting' trovato nel menu",
                hint="Struttura di sport-list cambiata: vedi contratto/baseline.")
        return
    by_alias = {m["alias"]: m for m in meetings}
    leagues = getattr(mod, "EUROBET_LEAGUES", {})
    country_map = {"IT": "Italia", "UK": "Inghilterra", "ES": "Spagna", "DE": "Germania", "FR": "Francia"}
    missing, empty = [], []
    for label, url in leagues.items():
        slug = url.rstrip("/").rsplit("/", 1)[-1]
        m = by_alias.get(slug)
        if m is None:
            core = re.sub(r"\s*\(.*?\)", "", label)
            cc = re.search(r"\((\w+)\)", label)
            query = f"{country_map.get(cc.group(1), '') if cc else ''} {core}".strip()
            pool = [f"{x['country']} {x['desc']}" for x in meetings]
            best = process.extract(query, pool, scorer=fuzz.token_set_ratio, limit=2)
            cand = "; ".join(f"{meetings[i]['alias']} ({meetings[i]['country']} / {meetings[i]['desc']}, {meetings[i]['n']} ev.)"
                             for (_t, sc, i) in best if sc >= 60)
            missing.append(f"{label} [{slug}]" + (f" -> candidati: {cand}" if cand else ""))
        elif not m["n"]:
            empty.append(label)
    if missing:
        rep.add(site, "Slug campionati (sport-list)", "FAIL" if len(missing) == len(leagues) else "WARN",
                f"{len(leagues) - len(missing)}/{len(leagues)} slug validi. Non trovati:\n  " + "\n  ".join(missing),
                hint="Aggiorna EUROBET_LEAGUES in eurobet_client.py con gli slug suggeriti.")
    else:
        rep.add(site, "Slug campionati (sport-list)", "OK" if not empty else "WARN",
                f"tutti i {len(leagues)} slug esistono" + (f"; senza eventi: {', '.join(empty)}" if empty else ""))


def run_eurobet(rep: Report, args: argparse.Namespace, bf_events: List[Any], now: datetime, fx: Optional[Dict[str, Any]]) -> None:
    site = "Eurobet"
    mod, err = import_client("eurobet_client")
    if err:
        rep.add(site, "Import client", "FAIL", err)
        return

    if args.eurobet_browser and fx is None:
        if getattr(sys.modules.get("playwright.sync_api"), "sync_playwright", None) is None:
            rep.add(site, "Playwright", "FAIL", "playwright non installato", hint="pip install playwright && playwright install chromium")
            return
        client = mod.EurobetClient(headless=args.headless)
        events = client.fetch_all()
        ebl: Dict[str, List[Dict[str, Any]]] = {}
        for e in events:
            ebl.setdefault(e.get("competition", "?"), []).append(e)
        evaluate_site(rep, site, "eurobet", calls=[], payloads={}, ebl=ebl,
                      found={l: None for l in getattr(mod, "EUROBET_LEAGUES", {})}, bf_events=bf_events,
                      args=args, now=now, offline=False)
        return

    client = mod.EurobetClient()
    client.current_league = "Calcio"
    calls: List[Dict[str, Any]] = []
    payloads: Dict[str, List[Any]] = {}
    if fx is not None:
        data = fx.get("eurobet")
        if not data:
            rep.add(site, "Campione offline", "SKIP",
                    "il campione disponibile e' la pagina Next.js (config AEM) senza quote: serve un dump di "
                    "'sport-schedule/services' (usa il test live o --eurobet-browser)")
            return
        payloads = {"next-events": [data]}
    else:
        sess = RecordingSession(make_session(), calls)
        hdrs = {"Accept": "application/json, text/plain, */*", "Accept-Language": "it-IT,it;q=0.9",
                "Referer": "https://www.eurobet.it/", "User-Agent": "Mozilla/5.0"}
        for role, url in EUROBET_PROBES.items():
            try:
                sess.get(url, headers=hdrs, timeout=20)
            except Exception:
                pass
            payloads[role] = [c["json"] for c in calls if c["url"] == url and c["json"] is not None]
            time.sleep(args.delay)

    ebl = {"Calcio": []}
    seen = 0
    for p in payloads.get("next-events", []):
        seen += count_key(p, "eventInfo")
        client.extracted_events.clear()
        try:
            client._walk_and_extract(p)
        except Exception as e:
            rep.add(site, "Parser _walk_and_extract", "FAIL", f"{type(e).__name__}: {e}")
        ebl["Calcio"] += list(client.extracted_events.values())
    evaluate_site(rep, site, "eurobet", calls=calls, payloads=payloads, ebl=ebl, found=None,
                  bf_events=bf_events, args=args, now=now, offline=fx is not None, strict_rate=False)
    if payloads.get("next-events"):
        got = len(ebl["Calcio"])
        rep.add(site, "Parser vs eventi visti", "OK" if got else "FAIL",
                f"{seen} eventi (eventInfo) nel payload, {got} estratti con 1X2 prematch",
                hint="" if got else "Il parser non trova piu' le quote 1X2 dentro betGroupList: controlla _find_1x2_in_event.")
    if payloads.get("sport-list"):
        _eurobet_slug_check(rep, site, mod, payloads["sport-list"][0])


def run_betfair(rep: Report, args: argparse.Namespace, now: datetime) -> None:
    site = "Betfair (file)"
    path = args.betfair_file
    if not os.path.exists(path):
        rep.add(site, "File dati", "FAIL", f"{path} non trovato", hint="Esegui prima betfair_agent.py")
        return
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        rep.add(site, "File dati", "FAIL", f"JSON non valido: {e}")
        return
    acq = om.parse_dt(data.get("acquired_at"))
    age = om.age_seconds(acq, now)
    if age is None:
        rep.add(site, "Eta' del file", "WARN", "acquired_at mancante")
    else:
        rep.add(site, "Eta' del file", "OK" if age < 1800 else "WARN", om.format_age(acq, now),
                hint="Dati vecchi: rilancia betfair_agent.py prima di usare l'oddsmatcher." if age >= 1800 else "")

    fp = fingerprint(data)
    check_contract(rep, site, "file", [data], CONTRACTS["betfair"]["file"], fp)
    check_baseline(rep, site, "betfair", "file", fp)

    counts: Counter = Counter()
    for et in data.get("eventTypes", []) or []:
        for en in et.get("eventNodes", []) or []:
            counts[en.get("competition", "?")] += len(en.get("marketNodes", []) or [])
    missing = [l for l in EXPECTED_LEAGUES if counts.get(l, 0) == 0]
    detail = ", ".join(f"{k}: {v}" for k, v in sorted(counts.items()))
    rep.add(site, "Mercati per campionato", "OK" if not missing else "WARN",
            detail + (f"\n  senza mercati: {', '.join(missing)}" if missing else ""),
            hint="Pagina 404/cambiata o nessun evento: controlla MAJOR_LEAGUES in betfair_agent.py." if missing else "")

    events, _acq, stats = om.load_betfair(path, now, None)
    total = stats.get("mercati_totali", 0)
    skipped = {k: v for k, v in stats.items() if k != "mercati_totali" and v}
    st = "OK" if events else "FAIL"
    rep.add(site, "Parsing oddsmatcher", st, f"{len(events)}/{total} mercati validi" +
            (f"; scartati: {', '.join(f'{k}={v}' for k, v in skipped.items())}" if skipped else ""),
            hint="" if events else "Struttura Betfair cambiata: vedi contratto e load_betfair().")
    if args.update_baseline and rep.site_worst(site) < 2:
        save_baseline("betfair", "file", fp)
        rep.add(site, "Baseline", "INFO", "salvata per file")


def run_bwin(rep: Report, args: argparse.Namespace, now: datetime) -> None:
    site = "Bwin (file)"
    path = args.bwin_file
    if not path or not os.path.exists(path):
        rep.add(site, "File dati", "SKIP", "nessun file Bwin (usa --bwin-file)")
        return
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        rep.add(site, "File dati", "FAIL", f"JSON non valido: {e}")
        return
    fp = fingerprint(data)
    check_contract(rep, site, "file", [data], CONTRACTS["bwin"]["file"], fp)
    check_baseline(rep, site, "bwin", "file", fp)
    fixtures = data.get("fixtures", []) or []
    sports = Counter(str(((f.get("sport") or {}).get("name") or {}).get("value", "?")) for f in fixtures)
    football_1x2 = 0
    for f in fixtures:
        if ((f.get("sport") or {}).get("name") or {}).get("value", "").lower() not in ("calcio", "football", "soccer"):
            continue
        if any(len(g.get("results", [])) == 3 for g in f.get("games", [])):
            football_1x2 += 1
    rep.add(site, "Contenuto", "INFO",
            f"{len(fixtures)} fixture (sport: {dict(sports)}); calcio con mercato a 3 esiti: {football_1x2}. "
            "Non esiste un client Bwin: verificata solo la struttura. Il campione contiene solo mercati antepost: "
            "serve un dump di una pagina campionato per validare il 1X2.")
    if args.update_baseline and rep.site_worst(site) < 2:
        save_baseline("bwin", "file", fp)
        rep.add(site, "Baseline", "INFO", "salvata per file")


# --------------------------------------------------------------------------
# Fixture offline
# --------------------------------------------------------------------------

def _read_json(path: Optional[str]) -> Optional[Dict[str, Any]]:
    if not path or not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def load_fixtures(args: argparse.Namespace) -> Dict[str, Any]:
    camp = _read_json(args.campioni) or {}
    sch = _read_json(args.schema) or {}
    fx: Dict[str, Any] = {}

    # I campioni di campioni_3_siti.json sono spesso versioni ridotte (poche chiavi per evento):
    # li uso solo se contengono i campi che il parser legge, altrimenti ricostruisco da schema_finale.json.
    s = (camp.get("snai_serie_a") or {}).get("sample_payload")
    complete = (
        isinstance(s, dict)
        and any(a.get("key") and a.get("firstCompetitor") for a in s.get("avvenimentoFeList", []) if isinstance(a, dict))
        and any(isinstance(m, dict) and m.get("esitoList") for m in (s.get("infoAggiuntivaMap") or {}).values())
    )
    if complete:
        fx["snai"] = s
    elif sch.get("snai"):
        sn = sch["snai"]
        avv = dict(sn["primo_avvenimento_completo"])
        key = sn["chiave_1x2"].rsplit("-", 2)[0]
        avv["key"] = key
        fx["snai"] = {"avvenimentoFeList": [avv], "infoAggiuntivaMap": {sn["chiave_1x2"]: sn["mercato_1x2_completo"]}}

    w_full = (sch.get("williamhill") or {}).get("primo_avs_completo")
    if isinstance(w_full, dict):
        root = dict((sch["williamhill"].get("altri_campi_radice") or {}))
        root["avs"] = [w_full]
        fx["williamhill"] = root
    else:
        w = (camp.get("williamhill_serie_a") or {}).get("sample_payload")
        if isinstance(w, dict):
            fx["williamhill"] = w

    e = (camp.get("eurobet_serie_a") or {}).get("sample_payload")
    if isinstance(e, dict) and count_key(e, "eventInfo") > 0:
        fx["eurobet"] = e
    return fx


# --------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------

def print_report(rep: Report, args: argparse.Namespace) -> None:
    use_color = sys.stdout.isatty() and not args.no_color

    def tag(st: str) -> str:
        t = f"[{st:^4}]"
        return f"{COLORS[st]}{t}\033[0m" if use_color else t

    sites: List[str] = []
    for r in rep.rows:
        if r["site"] not in sites:
            sites.append(r["site"])
    for site in sites:
        rows = [r for r in rep.rows if r["site"] == site]
        worst = max(STATUS_RANK[r["status"]] for r in rows)
        label = {0: "OK", 1: "WARN", 2: "FAIL"}[worst]
        print("\n" + "=" * 100)
        print(f"{site}  {tag(label)}")
        print("=" * 100)
        table = []
        for r in rows:
            if args.quiet and STATUS_RANK[r["status"]] == 0 and r["status"] != "INFO":
                continue
            table.append([tag(r["status"]), r["check"], "\n".join(textwrap.fill(l, 72) for l in r["detail"].split("\n"))])
        if table:
            print(tabulate(table, headers=["Esito", "Controllo", "Dettaglio"], tablefmt="simple"))
        else:
            print("Tutto ok.")

    todo = [r for r in rep.rows if r["hint"] and STATUS_RANK[r["status"]] > 0]
    if todo:
        print("\n" + "=" * 100)
        print("AZIONI SUGGERITE")
        print("=" * 100)
        for r in todo:
            print(f"- [{r['site']}] {r['check'].strip()}: {textwrap.fill(r['hint'], 90, subsequent_indent='    ')}")
    c = Counter(r["status"] for r in rep.rows)
    print("\nRiepilogo: " + ", ".join(f"{k}={c.get(k, 0)}" for k in ("OK", "WARN", "FAIL", "INFO", "SKIP")))


def save_report(rep: Report) -> str:
    os.makedirs(REPORT_DIR, exist_ok=True)
    payload = {"generated_at": datetime.now(timezone.utc).isoformat(), "worst": rep.worst(), "checks": rep.rows}
    p = os.path.join(REPORT_DIR, f"report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json")
    for path in (p, os.path.join(REPORT_DIR, "latest.json")):
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=1, ensure_ascii=False)
    return p


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Health check dei client di scraping")
    ap.add_argument("--site", nargs="*", default=None,
                    help="snai williamhill(wh) eurobet betfair bwin (default: tutti)")
    ap.add_argument("--offline", action="store_true", help="usa i campioni locali invece delle richieste live")
    ap.add_argument("--campioni", default="campioni_3_siti.json")
    ap.add_argument("--schema", default="schema_finale.json")
    ap.add_argument("--betfair-file", default=om.BETFAIR_FILE)
    ap.add_argument("--bwin-file", default="bwin_serie_a_raw.json")
    ap.add_argument("--update-baseline", action="store_true", help="salva la struttura attuale come riferimento")
    ap.add_argument("--force", action="store_true", help="con --update-baseline: salva anche se ci sono FAIL")
    ap.add_argument("--eurobet-browser", action="store_true", help="esegue EurobetClient.fetch_all (Playwright)")
    ap.add_argument("--headless", action="store_true", help="con --eurobet-browser")
    ap.add_argument("--use-local-dumps", action="store_true",
                    help="lascia che i client leggano debug_bookies_raw/ (di default viene ignorato per testare la discovery live)")
    ap.add_argument("--min-events", type=int, default=5)
    ap.add_argument("--min-overround", type=float, default=0.97)
    ap.add_argument("--max-overround", type=float, default=1.25)
    ap.add_argument("--threshold", type=float, default=85.0, help="soglia similarita' nomi squadra")
    ap.add_argument("--delay", type=float, default=0.3, help="pausa tra richieste (s)")
    ap.add_argument("--quiet", action="store_true", help="mostra solo i controlli non OK")
    ap.add_argument("--no-color", action="store_true")
    return ap.parse_args()


def main() -> int:
    args = parse_args()
    now = datetime.now(timezone.utc)
    aliases = {"snai": "snai", "wh": "williamhill", "williamhill": "williamhill", "eurobet": "eurobet",
               "betfair": "betfair", "bwin": "bwin"}
    if args.site:
        sites = [aliases.get(s.lower(), s.lower()) for s in args.site]
    else:
        sites = ["snai", "williamhill", "eurobet", "betfair"] + (["bwin"] if os.path.exists(args.bwin_file) else [])
    unknown = [s for s in sites if s not in aliases.values()]
    if unknown:
        print(f"[!] Siti sconosciuti: {unknown}")
        return 2

    rep = Report()
    fx = load_fixtures(args) if args.offline else None
    if args.offline:
        print(f"[*] Modalita' offline: campioni disponibili per {sorted(fx) or 'nessun sito'}")
    else:
        print("[*] Modalita' live: richieste reali ai siti (usa --offline per i campioni locali)")

    bf_events: List[Any] = []
    if os.path.exists(args.betfair_file):
        try:
            bf_events, _a, _s = om.load_betfair(args.betfair_file, now, None)
        except Exception:
            bf_events = []

    runners = {
        "snai": lambda: run_snai(rep, args, bf_events, now, fx),
        "williamhill": lambda: run_williamhill(rep, args, bf_events, now, fx),
        "eurobet": lambda: run_eurobet(rep, args, bf_events, now, fx),
        "betfair": lambda: run_betfair(rep, args, now),
        "bwin": lambda: run_bwin(rep, args, now),
    }
    names = {"snai": "Snai", "williamhill": "William Hill", "eurobet": "Eurobet",
             "betfair": "Betfair (file)", "bwin": "Bwin (file)"}
    for s in sites:
        print(f"[*] Controllo {names[s]}...")
        try:
            runners[s]()
        except Exception as e:
            tb = traceback.format_exc(limit=3)
            rep.add(names[s], "Errore imprevisto nel controllo", "FAIL", f"{type(e).__name__}: {e}\n{tb}",
                    hint="Se l'eccezione viene dal client, il parser non gestisce la nuova struttura.")

    print_report(rep, args)
    p = save_report(rep)
    print(f"[+] Report salvato in {p}")
    return rep.worst()


if __name__ == "__main__":
    sys.exit(main())