#!/usr/bin/env python3
"""
Oddsmatcher Unificato (CLI + Live Web Dashboard + Background Crawlers).

Unisce e corregge tutte le funzionalità di oddsmatcher.py e oddsmatcher_engine.py:
- Supporta sia il formato piatto di betfair_agent.py ('events') sia il formato raw ('eventTypes').
- Compatibile con il nuovo ritorno (events, failed) di bwin_client.py e williamhill_client.py,
  e con il ritorno a lista singola di snai_client.py ed eurobet_client.py.
- Matching a 2 livelli:
    1) Betradar ID esatto [ID] (con propagazione Cross-Bookmaker: se Bwin/WH hanno lo stesso
       Betradar ID, basta che uno dei due si abbini a Betfair per agganciare anche l'altro al 100%).
    2) Fuzzy matching avanzato [FZ] con dizionario alias, controllo varianti (Women/U21),
       verifica orario di inizio e campionato.
- Controllo sanità quote (overround 1X2), verifica liquidità reale contro lay_stake,
  calcolo Qualifying Bet e Freebet SNR.
- Doppia modalità:
    • Stampa tabella in console (default) + esportazione CSV + diagnostica --show-unmatched
    • Server Web Real-Time (--serve) con dashboard interattiva e worker di aggiornamento in background.

Esempi d'uso:
    python oddsmatcher.py                                  # Analizza i JSON e stampa tabella CLI
    python oddsmatcher.py --show-unmatched                 # Mostra anche i non abbinati
    python oddsmatcher.py --serve                          # Avvia Dashboard Web + Crawler automatici
    python oddsmatcher.py --serve --no-crawl               # Solo Dashboard Web sui file JSON locali
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import threading
import time
import unicodedata
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional, Tuple

from rapidfuzz import fuzz
from tabulate import tabulate

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    from zoneinfo import ZoneInfo
    LOCAL_TZ = ZoneInfo("Europe/Rome")
    LOCAL_LABEL = "IT"
except Exception:
    LOCAL_TZ = timezone(timedelta(hours=2))
    LOCAL_LABEL = "IT"

BETFAIR_FILE = "betfair_exchange_data.json"
BOOKIES_FILE = "bookmakers_data.json"
OUTCOMES = ("1", "X", "2")


# --------------------------------------------------------------------------
# Strutture dati
# --------------------------------------------------------------------------

@dataclass
class ExchangeMarketData:
    back_odds: float = 0.0
    back_liquidity: float = 0.0
    lay_odds: float = 0.0
    lay_liquidity: float = 0.0


@dataclass
class BetfairEvent:
    market_id: str
    event_id: str
    competition: str
    home_team: str
    away_team: str
    kickoff: Optional[datetime]
    markets: Dict[str, Dict[str, ExchangeMarketData]]
    betradar_id: Optional[str] = None
    commission: float = 0.045
    acquired_at: Optional[datetime] = None
    norm_home: str = field(init=False, default="")
    norm_away: str = field(init=False, default="")

    def __post_init__(self) -> None:
        self.norm_home = norm_team(self.home_team)
        self.norm_away = norm_team(self.away_team)


@dataclass
class BookieEvent:
    bookmaker: str
    competition: str
    home_team: str
    away_team: str
    kickoff: Optional[datetime]
    markets: Dict[str, Dict[str, float]]
    betradar_id: Optional[str] = None
    acquired_at: Optional[datetime] = None
    acquired_exact: bool = False
    norm_home: str = field(init=False, default="")
    norm_away: str = field(init=False, default="")

    def __post_init__(self) -> None:
        self.norm_home = norm_team(self.home_team)
        self.norm_away = norm_team(self.away_team)


@dataclass
class MatchResult:
    kickoff: Optional[datetime]
    competition: str
    match_name: str
    bookmaker: str
    outcome: str
    match_type: str        # "ID" oppure "FZ 95%"
    betradar_id: Optional[str]
    bookie_back: float
    bf_lay: float
    bf_lay_liq: float
    rating: float
    lay_stake: float
    liability: float
    net_result: float
    bf_age_s: Optional[int]
    bk_age_s: Optional[int]
    bf_age_str: str
    bk_age_str: str
    time_verified: bool

    def to_dict(self) -> Dict[str, Any]:
        return {
            "kickoff": _local_str(self.kickoff),
            "kickoff_iso": self.kickoff.isoformat() if self.kickoff else "",
            "competition": self.competition,
            "match": self.match_name,
            "outcome": self.outcome,
            "match_type": self.match_type,
            "betradar_id": self.betradar_id or "",
            "bookmaker": self.bookmaker,
            "back": round(self.bookie_back, 2),
            "lay": round(self.bf_lay, 2),
            "liq": round(self.bf_lay_liq, 2),
            "rating": round(self.rating, 2),
            "lay_stake": round(self.lay_stake, 2),
            "liability": round(self.liability, 2),
            "net": round(self.net_result, 2),
            "bf_age": self.bf_age_str,
            "bk_age": self.bk_age_str,
            "time_verified": self.time_verified,
        }


# --------------------------------------------------------------------------
# Date e orari
# --------------------------------------------------------------------------

def _from_epoch(v: float) -> Optional[datetime]:
    if v > 1e11:
        v = v / 1000.0
    try:
        dt = datetime.fromtimestamp(v, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None
    return dt if 2000 <= dt.year <= 2100 else None


_DT_FORMATS = (
    "%d/%m/%Y %H:%M:%S",
    "%d/%m/%Y %H:%M",
    "%d-%m-%Y %H:%M:%S",
    "%d-%m-%Y %H:%M",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y%m%d %H:%M:%S",
    "%Y%m%d%H%M%S",
)


def parse_dt(value: Any, naive_tz=LOCAL_TZ) -> Optional[datetime]:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, (int, float)):
        return _from_epoch(float(value))
    else:
        s = str(value).strip()
        if not s:
            return None
        dt = None
        if re.fullmatch(r"\d{14}", s):
            try:
                dt = datetime.strptime(s, "%Y%m%d%H%M%S")
            except ValueError:
                dt = None
        if dt is None and re.fullmatch(r"\d{9,13}(\.\d+)?", s):
            return _from_epoch(float(s))
        if dt is None:
            iso = s[:-1] + "+00:00" if s.endswith("Z") else s
            try:
                dt = datetime.fromisoformat(iso)
            except ValueError:
                for fmt in _DT_FORMATS:
                    try:
                        dt = datetime.strptime(s, fmt)
                        break
                    except ValueError:
                        continue
        if dt is None:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=naive_tz)
    return dt.astimezone(timezone.utc)


def _local_str(dt: Optional[datetime]) -> str:
    if dt is None:
        return "N/D"
    return dt.astimezone(LOCAL_TZ).strftime("%d/%m %H:%M")


def format_age(dt: Optional[datetime], now: datetime, lower_bound: bool = False) -> str:
    if dt is None:
        return "N/D"
    sec = max(0, int((now - dt).total_seconds()))
    if sec < 60:
        s = f"{sec}s"
    elif sec < 3600:
        s = f"{sec // 60}m {sec % 60:02d}s"
    else:
        s = f"{sec // 3600}h {(sec % 3600) // 60}m"
    return ("\u2265" if lower_bound else "") + s


def age_seconds(dt: Optional[datetime], now: datetime) -> Optional[int]:
    return None if dt is None else max(0, int((now - dt).total_seconds()))


# --------------------------------------------------------------------------
# Normalizzazione e confronto nomi squadra
# --------------------------------------------------------------------------

TECH_TOKENS = {
    "fc", "ac", "as", "cf", "ss", "ssc", "sc", "us", "cd", "ud", "fk", "sk",
    "afc", "acf", "rc", "rcd", "ca", "bc", "cfc", "sv", "fsv", "vfb", "vfl",
    "tsg", "bv", "sl", "calcio", "club", "de", "the", "sad", "1899", "1909",
    "1907", "1911", "1913", "1929", "1908", "1893", "1846", "04", "05", "98",
}

AMBIGUOUS_TOKENS = {
    "manchester", "sheffield", "milan", "madrid", "borussia", "bristol",
    "sporting", "real", "atletico", "athletic", "inter", "nottingham",
    "paris", "dynamo", "dinamo", "olympique", "stade", "union", "racing",
    "saint", "st", "red", "young", "royal", "west", "north", "south", "verona",
}

VARIANT_TOKENS = {
    "women", "femminile", "fem", "w", "u17", "u18", "u19", "u20", "u21",
    "u23", "ii", "iii", "b", "reserves", "res", "youth", "primavera",
}

ALIASES: Dict[str, str] = {
    # Italia
    "internazionale": "inter", "internazionale milano": "inter",
    "inter milan": "inter", "inter milano": "inter",
    "hellas verona": "verona", "juve": "juventus",
    " Sudtirol": "sudtirol", "alto adige": "sudtirol",
    "l r vicenza": "vicenza", "lr vicenza": "vicenza", "virtus entella": "entella",
    # Inghilterra
    "manchester utd": "manchester united", "man utd": "manchester united",
    "man united": "manchester united",
    "man city": "manchester city", "manchester c": "manchester city",
    "tottenham hotspur": "tottenham", "spurs": "tottenham",
    "wolverhampton wanderers": "wolverhampton", "wolves": "wolverhampton",
    "newcastle united": "newcastle", "newcastle utd": "newcastle",
    "west ham united": "west ham", "west ham utd": "west ham",
    "brighton and hove albion": "brighton", "brighton hove albion": "brighton",
    "leeds united": "leeds", "nottm forest": "nottingham forest",
    "leicester city": "leicester", "ipswich town": "ipswich",
    # Spagna
    "atletico madrid": "atletico madrid", "atl madrid": "atletico madrid",
    "athletic club": "athletic bilbao", "athletic club bilbao": "athletic bilbao",
    "ath bilbao": "athletic bilbao",
    "real betis": "betis", "real betis balompie": "betis",
    "celta vigo": "celta", "siviglia": "sevilla", "barcellona": "barcelona",
    "deportivo alaves": "alaves", "rayo vallecano": "vallecano",
    "real sociedad": "sociedad", "real valladolid": "valladolid",
    # Germania
    "augsburg": "augusta",
    "bayern monaco": "bayern munich", "bayern munchen": "bayern munich",
    "borussia dortmund": "dortmund",
    "borussia monchengladbach": "gladbach", "borussia m gladbach": "gladbach",
    "borussia mgladbach": "gladbach", "b monchengladbach": "gladbach",
    "monchengladbach": "gladbach", "m gladbach": "gladbach",
    "bayer leverkusen": "leverkusen",
    "koln": "koln", "colonia": "koln", "cologne": "koln",
    "magonza": "mainz",
    "eintracht francoforte": "frankfurt", "eintracht frankfurt": "frankfurt",
    "francoforte": "frankfurt",
    "lipsia": "leipzig", "rb lipsia": "leipzig", "rb leipzig": "leipzig",
    "rasenballsport leipzig": "leipzig",
    "stoccarda": "stuttgart", "friburgo": "freiburg", "amburgo": "hamburg",
    "hamburger": "hamburg", "werder brema": "werder bremen", "brema": "werder bremen",
    "union berlino": "union berlin", "heidenheim": "heidenheim",
    "st pauli": "st pauli", "sankt pauli": "st pauli",
    # Francia
    "paris saint germain": "psg", "paris sg": "psg",
    "olympique marsiglia": "marseille", "olympique marseille": "marseille",
    "marsiglia": "marseille",
    "olympique lione": "lyon", "olympique lyonnais": "lyon", "lione": "lyon",
    "lilla": "lille", "lille osc": "lille", "losc lille": "lille",
    "nizza": "nice", "ogc nice": "nice", "stade rennais": "rennes",
    "strasburgo": "strasbourg", "strasburgo alsazia": "strasbourg",
    "tolosa": "toulouse", "stade brestois": "brest",
    "angers sco": "angers", "aj auxerre": "auxerre",
    "st etienne": "saint etienne",
    # Portogallo & Europa
    "sporting lisbona": "sporting cp", "sporting lisbon": "sporting cp",
    "sporting clube de portugal": "sporting cp", "sporting clube portugal": "sporting cp",
    "vitoria guimaraes": "vitoria guimaraes", "vitoria sc": "vitoria guimaraes",
    "stella rossa": "red star", "stella rossa belgrado": "red star",
    "crvena zvezda": "red star", "red star belgrade": "red star",
    "dinamo zagabria": "dinamo zagreb", "zagabria": "dinamo zagreb",
    "gnk dinamo zagreb": "dinamo zagreb",
    "slavia praga": "slavia prague", "slavia praha": "slavia prague",
    "sparta praga": "sparta prague", "sparta praha": "sparta prague",
    "bruges": "brugge", "club bruges": "brugge", "club brugge": "brugge",
    "anversa": "antwerp", "royal antwerp": "antwerp",
    "salisburgo": "salzburg", "red bull salisburgo": "salzburg",
    "red bull salzburg": "salzburg", "rb salzburg": "salzburg",
    "psv eindhoven": "psv", "ajax amsterdam": "ajax", "feyenoord rotterdam": "feyenoord",
    "shakhtar donetsk": "shakhtar", "sh donetsk": "shakhtar",
    "dynamo kiev": "dynamo kyiv", "dinamo kiev": "dynamo kyiv", "dynamo kyev": "dynamo kyiv",
    "olympiakos": "olympiacos", "olympiakos piraeus": "olympiacos",
    "olympiacos piraeus": "olympiacos", "olympiacos pireo": "olympiacos",
    "aek atene": "aek athens", "nec nimega": "nec nijmegen",
    "nimega n": "nec nijmegen", "levski sofia": "pfc levski sofia",
    "az a": "az alkmaar", "hapoel beer": "hapoel beer sheva",
}


def _ascii_lower(name: str) -> str:
    s = unicodedata.normalize("NFKD", str(name)).encode("ascii", "ignore").decode()
    s = re.sub(r"[^a-z0-9\s]", " ", s.lower())
    return " ".join(s.split())


@lru_cache(maxsize=None)
def norm_team(name: str) -> str:
    s = _ascii_lower(name)
    s = ALIASES.get(s, s)
    toks = [t for t in s.split() if t not in TECH_TOKENS and not re.fullmatch(r"\d{1,4}", t)]
    s2 = " ".join(toks) or s
    return ALIASES.get(s2, s2)


@lru_cache(maxsize=None)
def team_similarity(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    if a == b:
        return 100.0
    ta, tb = set(a.split()), set(b.split())
    if (ta ^ tb) & VARIANT_TOKENS:
        return 0.0
    score = float(fuzz.token_sort_ratio(a, b))
    small, big = (ta, tb) if len(ta) <= len(tb) else (tb, ta)
    if small and small < big and not small <= AMBIGUOUS_TOKENS:
        score = max(score, 92.0)
    return score


def same_competition(a: str, b: str) -> bool:
    na, nb = (a or "").strip().lower(), (b or "").strip().lower()
    if not na or not nb or "calcio" in (na, nb):
        return True
    return na == nb


# --------------------------------------------------------------------------
# Caricamento dati (Supporta tutti i formati JSON prodotti dagli script)
# --------------------------------------------------------------------------

def _safe_float(x: Any, default: float = 0.0) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def _load_json(path: str) -> Optional[Dict[str, Any]]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except (OSError, json.JSONDecodeError) as e:
        print(f"[!] Impossibile leggere {path}: {e}")
        return None


def parse_betfair_events_from_dict(
    data: Dict[str, Any],
    now: datetime,
    commission_override: Optional[float] = None,
    ignore_past: bool = True,
) -> Tuple[List[BetfairEvent], Optional[datetime], Dict[str, int]]:
    """
    Legge sia il formato piatto generato da betfair_agent.py ('events': [...])
    sia l'eventuale formato albero Betfair ('eventTypes': [...]).
    """
    stats: Dict[str, int] = defaultdict(int)
    file_acq = parse_dt(data.get("acquired_at"), naive_tz=timezone.utc)
    comm_default = (commission_override / 100.0) if commission_override is not None else 0.045
    events: List[BetfairEvent] = []
    seen = set()

    # 1) Formato piatto di betfair_agent.py
    if isinstance(data.get("events"), list):
        for ev in data["events"]:
            if not isinstance(ev, dict):
                continue
            stats["mercati_totali"] += 1
            m_id = str(ev.get("market_id") or ev.get("event_id") or "")
            if not m_id or m_id in seen:
                stats["duplicati"] += 1
                continue

            home = str(ev.get("home_team", "")).strip()
            away = str(ev.get("away_team", "")).strip()
            if not home or not away:
                stats["runner_non_validi"] += 1
                continue

            kickoff = parse_dt(ev.get("kickoff"), naive_tz=timezone.utc)
            if ignore_past and kickoff is not None and kickoff <= now:
                stats["gia_iniziati"] += 1
                continue

            m_1x2 = (ev.get("markets") or {}).get("1X2") or {}
            odds_map: Dict[str, ExchangeMarketData] = {}
            for k in OUTCOMES:
                r = m_1x2.get(k)
                if not isinstance(r, dict):
                    continue
                odds_map[k] = ExchangeMarketData(
                    back_odds=_safe_float(r.get("back_odds")),
                    back_liquidity=_safe_float(r.get("back_liquidity")),
                    lay_odds=_safe_float(r.get("lay_odds")),
                    lay_liquidity=_safe_float(r.get("lay_liquidity")),
                )

            if len(odds_map) != 3:
                stats["runner_non_validi"] += 1
                continue

            seen.add(m_id)
            br_id = ev.get("betradar_id")
            events.append(BetfairEvent(
                market_id=m_id,
                event_id=str(ev.get("event_id") or m_id),
                competition=str(ev.get("competition") or "Calcio"),
                home_team=home,
                away_team=away,
                kickoff=kickoff,
                markets={"1X2": odds_map},
                betradar_id=str(br_id).strip() if br_id not in (None, "", "None") else None,
                commission=comm_default,
                acquired_at=parse_dt(ev.get("acquired_at"), naive_tz=timezone.utc) or file_acq,
            ))

    return events, file_acq, stats


def load_betfair(
    path: str, now: datetime, commission_override: Optional[float] = None
) -> Tuple[List[BetfairEvent], Optional[datetime], Dict[str, int]]:
    if not os.path.exists(path):
        return [], None, defaultdict(int)
    data = _load_json(path)
    if data is None:
        return [], None, defaultdict(int)
    return parse_betfair_events_from_dict(data, now, commission_override)


def parse_bookmaker_events_list(
    raw_events: List[Dict[str, Any]],
    now: datetime,
    file_acq: Optional[datetime] = None,
    min_overround: float = 0.97,
    max_overround: float = 1.25,
    ignore_past: bool = True,
) -> Tuple[List[BookieEvent], Dict[str, int]]:
    stats: Dict[str, int] = defaultdict(int)
    best: Dict[Tuple[Any, ...], BookieEvent] = {}

    for ev in raw_events:
        if not isinstance(ev, dict):
            continue
        stats["totali"] += 1
        home = str(ev.get("home_team", "")).strip()
        away = str(ev.get("away_team", "")).strip()
        if not home or not away:
            stats["senza_squadre"] += 1
            continue

        raw = (ev.get("markets") or {}).get("1X2") or {}
        odds = {k: _safe_float(raw.get(k)) for k in OUTCOMES}
        if any(v <= 1.0 for v in odds.values()):
            stats["1x2_incompleto"] += 1
            continue

        overround = sum(1.0 / v for v in odds.values())
        if not min_overround <= overround <= max_overround:
            stats["overround_anomalo"] += 1
            continue

        kickoff = parse_dt(ev.get("kickoff"), naive_tz=LOCAL_TZ)
        if ignore_past and kickoff is not None and kickoff <= now:
            stats["gia_iniziati"] += 1
            continue

        ev_acq = parse_dt(ev.get("acquired_at"), naive_tz=timezone.utc)
        br_id = ev.get("betradar_id")
        betradar_str = str(br_id).strip() if br_id not in (None, "", "None", 0, "0") else None

        be = BookieEvent(
            bookmaker=str(ev.get("bookmaker", "Bookie")),
            competition=str(ev.get("competition", "Calcio")),
            home_team=home,
            away_team=away,
            kickoff=kickoff,
            markets={"1X2": odds},
            betradar_id=betradar_str,
            acquired_at=ev_acq or file_acq,
            acquired_exact=ev_acq is not None,
        )

        key = (be.bookmaker, be.norm_home, be.norm_away, kickoff.date() if kickoff else None)
        prev = best.get(key)
        if prev is not None:
            stats["duplicati"] += 1
            prev_t = prev.acquired_at or datetime.min.replace(tzinfo=timezone.utc)
            new_t = be.acquired_at or datetime.min.replace(tzinfo=timezone.utc)
            if new_t < prev_t:
                continue
        best[key] = be

    return list(best.values()), stats


def load_bookmakers(
    path: str, now: datetime, min_overround: float = 0.97, max_overround: float = 1.25
) -> Tuple[List[BookieEvent], Dict[str, int]]:
    if not os.path.exists(path):
        return [], defaultdict(int)
    data = _load_json(path)
    if data is None:
        return [], defaultdict(int)
    file_acq = parse_dt(data.get("acquired_at"), naive_tz=timezone.utc)
    return parse_bookmaker_events_list(
        data.get("events", []) or [], now, file_acq, min_overround, max_overround
    )


# --------------------------------------------------------------------------
# Matching Ibrido (Betradar ID Cross-Bookmaker + Fuzzy Matching) & Calcolo
# --------------------------------------------------------------------------

def find_fuzzy_match(
    b_ev: BookieEvent,
    bf_events: List[BetfairEvent],
    threshold: float,
    max_time_diff: timedelta,
) -> Optional[Tuple[BetfairEvent, float, bool]]:
    best: Optional[Tuple[Tuple[bool, float, float], BetfairEvent, float, bool]] = None
    for bf in bf_events:
        hs = team_similarity(b_ev.norm_home, bf.norm_home)
        if hs < threshold:
            continue
        as_ = team_similarity(b_ev.norm_away, bf.norm_away)
        if as_ < threshold:
            continue
        score = (hs + as_) / 2.0
        weakest = min(hs, as_)

        if b_ev.kickoff is not None and bf.kickoff is not None:
            delta = abs((b_ev.kickoff - bf.kickoff).total_seconds())
            if delta > max_time_diff.total_seconds():
                continue
            time_verified = True
        else:
            delta = 0.0
            time_verified = False
            if weakest < 90.0:
                continue

        comp_ok = same_competition(b_ev.competition, bf.competition)
        if not comp_ok and not (time_verified and weakest >= 90.0):
            continue

        rank = (comp_ok, score, -delta)
        if best is None or rank > best[0]:
            best = (rank, bf, score, time_verified)

    if best is None:
        return None
    return best[1], best[2], best[3]


def compute_bet(p_book: float, lay_odds: float, comm: float, stake: float, mode: str):
    """
    Qualifying:
        lay_stake = S * p_book / (L - c)
        rating    = p_book * (1 - c) / (L - c)
        net       = S * (rating - 1)
    Freebet SNR:
        lay_stake = S * (p_book - 1) / (L - c)
        rating    = (p_book - 1) * (1 - c) / (L - c)
        net       = lay_stake * (1 - c)
    """
    denom = lay_odds - comm
    if mode == "snr":
        lay_stake = stake * (p_book - 1.0) / denom
        rating = (p_book - 1.0) * (1.0 - comm) / denom
        net = lay_stake * (1.0 - comm)
    else:
        lay_stake = stake * p_book / denom
        rating = p_book * (1.0 - comm) / denom
        net = stake * (rating - 1.0)
    liability = lay_stake * (lay_odds - 1.0)
    return rating * 100.0, lay_stake, liability, net


def match_and_rate(
    bookie_events: List[BookieEvent],
    bf_events: List[BetfairEvent],
    now: datetime,
    stake: float = 100.0,
    mode: str = "qualifying",
    min_liq: float = 10.0,
    threshold: float = 82.0,
    max_time_diff: timedelta = timedelta(hours=4),
) -> Tuple[List[MatchResult], Dict[str, Any]]:
    """
    Esegue il matching in 2 passaggi:
    Passaggio 1: Costruisce la mappa {betradar_id -> BetfairEvent} sia dagli ID nativi
                 di Betfair sia propagando gli ID dei bookmaker che si abbinano con alta
                 confidenza a Betfair (es. Bwin condivide il Betradar ID con William Hill).
    Passaggio 2: Abbina ogni evento bookmaker prima per Betradar ID [ID], poi in fallback
                 tramite Fuzzy Matching [FZ].
    """
    results: List[MatchResult] = []
    stats: Dict[str, Any] = {
        "per_bookie": defaultdict(lambda: [0, 0]),
        "unmatched": [],
        "scartati": defaultdict(int),
    }

    # Mappa Betradar ID -> BetfairEvent
    br_to_bf: Dict[str, BetfairEvent] = {}
    for bf in bf_events:
        if bf.betradar_id:
            br_to_bf[bf.betradar_id] = bf

    # Passaggio 1: Apprendimento Cross-Bookmaker del Betradar ID
    fuzzy_cache: Dict[int, Optional[Tuple[BetfairEvent, float, bool]]] = {}
    for idx, b_ev in enumerate(bookie_events):
        m = find_fuzzy_match(b_ev, bf_events, threshold, max_time_diff)
        fuzzy_cache[idx] = m
        if m is not None and b_ev.betradar_id and b_ev.betradar_id not in br_to_bf:
            bf_ev, score, time_ok = m
            # Se l'abbinamento nome+ora è solido, associamo il Betradar ID al mercato Betfair
            if score >= 86.0 and time_ok:
                br_to_bf[b_ev.betradar_id] = bf_ev
                if not bf_ev.betradar_id:
                    bf_ev.betradar_id = b_ev.betradar_id

    # Passaggio 2: Abbinamento definitivo e calcolo rating
    for idx, b_ev in enumerate(bookie_events):
        stats["per_bookie"][b_ev.bookmaker][0] += 1
        bf_ev: Optional[BetfairEvent] = None
        match_type = ""
        time_verified = False

        # Livello 1: Betradar ID deterministico
        if b_ev.betradar_id and b_ev.betradar_id in br_to_bf:
            cand = br_to_bf[b_ev.betradar_id]
            # Verifica di sicurezza che l'orario non sia distante giorni
            if b_ev.kickoff and cand.kickoff and abs(b_ev.kickoff - cand.kickoff) <= max_time_diff:
                bf_ev = cand
                match_type = "ID"
                time_verified = True
            elif not b_ev.kickoff or not cand.kickoff:
                bf_ev = cand
                match_type = "ID"
                time_verified = False

        # Livello 2: Fallback Fuzzy Matching
        if bf_ev is None:
            m = fuzzy_cache.get(idx)
            if m is not None:
                bf_ev, score, time_verified = m
                match_type = f"FZ {int(round(score))}%"

        if bf_ev is None:
            stats["unmatched"].append(b_ev)
            continue

        stats["per_bookie"][b_ev.bookmaker][1] += 1

        comm = bf_ev.commission
        b_1x2 = b_ev.markets["1X2"]
        bf_1x2 = bf_ev.markets["1X2"]
        bf_age = age_seconds(bf_ev.acquired_at, now)
        bk_age = age_seconds(b_ev.acquired_at, now)

        for out in OUTCOMES:
            p_book = b_1x2[out]
            d = bf_1x2[out]

            if d.lay_odds <= 1.0:
                stats["scartati"]["nessuna_banca"] += 1
                continue
            if d.back_odds > 0 and d.lay_odds < d.back_odds:
                stats["scartati"]["book_incrociato"] += 1
                continue
            if d.lay_liquidity < min_liq:
                stats["scartati"]["liquidita_minima"] += 1
                continue

            rating, lay_stake, liability, net = compute_bet(p_book, d.lay_odds, comm, stake, mode)
            if lay_stake > d.lay_liquidity:
                stats["scartati"]["liquidita_insufficiente"] += 1
                continue

            results.append(MatchResult(
                kickoff=bf_ev.kickoff or b_ev.kickoff,
                competition=b_ev.competition if b_ev.competition != "Calcio" else bf_ev.competition,
                match_name=f"{bf_ev.home_team} - {bf_ev.away_team}",
                bookmaker=b_ev.bookmaker,
                outcome=out,
                match_type=match_type,
                betradar_id=b_ev.betradar_id or bf_ev.betradar_id,
                bookie_back=p_book,
                bf_lay=d.lay_odds,
                bf_lay_liq=d.lay_liquidity,
                rating=rating,
                lay_stake=lay_stake,
                liability=liability,
                net_result=net,
                bf_age_s=bf_age,
                bk_age_s=bk_age,
                bf_age_str=format_age(bf_ev.acquired_at, now),
                bk_age_str=format_age(b_ev.acquired_at, now, lower_bound=not b_ev.acquired_exact),
                time_verified=time_verified,
            ))

    results.sort(key=lambda r: r.rating, reverse=True)
    return results, stats


def nearest_candidate(
    b_ev: BookieEvent, bf_events: List[BetfairEvent], max_time_diff: timedelta
) -> Optional[Tuple[float, BetfairEvent, float, float]]:
    best = None
    for bf in bf_events:
        if b_ev.kickoff and bf.kickoff and abs(b_ev.kickoff - bf.kickoff) > max_time_diff:
            continue
        hs = team_similarity(b_ev.norm_home, bf.norm_home)
        as_ = team_similarity(b_ev.norm_away, bf.norm_away)
        sc = (hs + as_) / 2.0
        if best is None or sc > best[0]:
            best = (sc, bf, hs, as_)
    return best if best and best[0] >= 40.0 else None


# --------------------------------------------------------------------------
# Output CLI e CSV
# --------------------------------------------------------------------------

def print_unmatched(
    unmatched: List[BookieEvent], bf_events: List[BetfairEvent], max_time_diff: timedelta
) -> None:
    rows = []
    for b in sorted(unmatched, key=lambda x: (x.bookmaker, x.competition, x.kickoff or datetime.max.replace(tzinfo=timezone.utc))):
        near = nearest_candidate(b, bf_events, max_time_diff)
        hint = ""
        if near:
            _sc, bf, hs, as_ = near
            hint = f"{bf.home_team} - {bf.away_team} ({hs:.0f}/{as_:.0f})"
        rows.append([
            b.bookmaker,
            b.competition,
            f"{b.home_team} - {b.away_team}",
            b.betradar_id or "-",
            _local_str(b.kickoff),
            hint,
        ])
    print("\n" + "=" * 125)
    print(f"EVENTI BOOKMAKER NON ABBINATI ({len(rows)}) - candidato Betfair più vicino (score casa/trasferta)")
    print("=" * 125)
    if rows:
        print(tabulate(
            rows,
            headers=["Bookmaker", "Campionato", "Partita", "Betradar ID", f"Data ({LOCAL_LABEL})", "Candidato BF"],
            tablefmt="github",
        ))
    else:
        print("Tutti gli eventi sono stati abbinati!")
    print()


def save_csv(path: str, results: List[MatchResult]) -> None:
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow([
            "kickoff_utc", "competition", "match", "match_type", "betradar_id",
            "outcome", "bookmaker", "back_odds", "lay_odds", "lay_liquidity",
            "rating_pct", "lay_stake", "liability", "net_result",
        ])
        for r in results:
            w.writerow([
                r.kickoff.isoformat() if r.kickoff else "",
                r.competition, r.match_name, r.match_type, r.betradar_id or "",
                r.outcome, r.bookmaker, f"{r.bookie_back:.2f}", f"{r.bf_lay:.2f}",
                f"{r.bf_lay_liq:.2f}", f"{r.rating:.2f}", f"{r.lay_stake:.2f}",
                f"{r.liability:.2f}", f"{r.net_result:.2f}",
            ])
    print(f"[+] CSV salvato in {path}")


# --------------------------------------------------------------------------
# Live Engine & Web Dashboard (--serve)
# --------------------------------------------------------------------------

LOCK = threading.Lock()
GLOBAL_STATE: Dict[str, Any] = {
    "bookmakers": {},          # nome_bookie -> {"raw_events": [...], "updated_at": datetime}
    "betfair_raw": None,       # dict caricato da betfair_exchange_data.json
    "results": [],             # lista di MatchResult.to_dict()
    "coverage": [],            # statistiche copertura per bookmaker
    "last_calc": None,
    "sources_status": {},
}

ENGINE_CONFIG: Dict[str, Any] = {
    "stake": 100.0,
    "mode": "qualifying",
    "min_liq": 10.0,
    "threshold": 82.0,
    "max_time_diff_h": 4.0,
    "commission": 4.5,
    "min_overround": 0.97,
    "max_overround": 1.25,
}


def _unpack_client_return(ret: Any) -> Tuple[List[Dict[str, Any]], List[str]]:
    """Gestisce sia i client che ritornano (events, failed) sia quelli che ritornano events."""
    if isinstance(ret, tuple) and len(ret) == 2:
        evs, failed = ret
        return (evs or []), (failed or [])
    if isinstance(ret, list):
        return ret, []
    return [], []


def recalculate_live_state() -> None:
    now = datetime.now(timezone.utc)
    with LOCK:
        bf_data = GLOBAL_STATE["betfair_raw"]
        raw_bookies = [
            (b_name, b_info["raw_events"], b_info["updated_at"])
            for b_name, b_info in GLOBAL_STATE["bookmakers"].items()
        ]

    if not bf_data or not raw_bookies:
        return

    bf_events, _bf_acq, _bf_stats = parse_betfair_events_from_dict(
        bf_data, now, commission_override=ENGINE_CONFIG["commission"]
    )

    all_bookie_events: List[BookieEvent] = []
    for _b_name, evs, upd_at in raw_bookies:
        parsed_bk, _ = parse_bookmaker_events_list(
            evs,
            now,
            file_acq=upd_at,
            min_overround=ENGINE_CONFIG["min_overround"],
            max_overround=ENGINE_CONFIG["max_overround"],
        )
        all_bookie_events.extend(parsed_bk)

    if not bf_events or not all_bookie_events:
        return

    matched, stats = match_and_rate(
        all_bookie_events,
        bf_events,
        now,
        stake=ENGINE_CONFIG["stake"],
        mode=ENGINE_CONFIG["mode"],
        min_liq=ENGINE_CONFIG["min_liq"],
        threshold=ENGINE_CONFIG["threshold"],
        max_time_diff=timedelta(hours=ENGINE_CONFIG["max_time_diff_h"]),
    )

    cov = [
        {"bookmaker": bk, "total": tot, "matched": m, "unmatched": tot - m}
        for bk, (tot, m) in sorted(stats["per_bookie"].items())
    ]

    with LOCK:
        GLOBAL_STATE["results"] = [r.to_dict() for r in matched]
        GLOBAL_STATE["coverage"] = cov
        GLOBAL_STATE["last_calc"] = now.isoformat()


def _run_bookie_worker(name: str, module_name: str, class_name: str, interval: int = 45):
    while True:
        try:
            mod = __import__(module_name)
            cls = getattr(mod, class_name)
            client = cls()
            evs, failed = _unpack_client_return(client.fetch_all())
            now = datetime.now(timezone.utc)
            if evs:
                # Aggiorna anche bookmakers_data.json su disco
                if hasattr(mod, "merge_and_save"):
                    try:
                        if len(mod.merge_and_save.__code__.co_varnames) >= 3:
                            mod.merge_and_save(name, evs, failed)
                        else:
                            mod.merge_and_save(name, evs)
                    except Exception:
                        pass
                with LOCK:
                    GLOBAL_STATE["bookmakers"][name] = {"raw_events": evs, "updated_at": now}
                    GLOBAL_STATE["sources_status"][name] = f"OK ({len(evs)} match, {now.astimezone(LOCAL_TZ).strftime('%H:%M:%S')})"
                recalculate_live_state()
            else:
                with LOCK:
                    GLOBAL_STATE["sources_status"][name] = "Nessun evento ricevuto"
        except Exception as e:
            with LOCK:
                GLOBAL_STATE["sources_status"][name] = f"Errore: {e}"
        time.sleep(interval)


def _run_betfair_file_watcher(path: str, interval: int = 15):
    last_mtime = 0.0
    while True:
        try:
            if os.path.exists(path):
                mtime = os.path.getmtime(path)
                if mtime != last_mtime:
                    data = _load_json(path)
                    if data:
                        last_mtime = mtime
                        evs = data.get("events", [])
                        acq = parse_dt(data.get("acquired_at"), naive_tz=timezone.utc) or datetime.now(timezone.utc)
                        with LOCK:
                            GLOBAL_STATE["betfair_raw"] = data
                            GLOBAL_STATE["sources_status"]["Betfair Exchange"] = (
                                f"OK ({len(evs)} mercati, {acq.astimezone(LOCAL_TZ).strftime('%H:%M:%S')})"
                            )
                        recalculate_live_state()
        except Exception as e:
            with LOCK:
                GLOBAL_STATE["sources_status"]["Betfair Exchange"] = f"Errore: {e}"
        time.sleep(interval)


DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="it">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Oddsmatcher Pro Live</title>
    <style>
        :root { --bg: #0f172a; --panel: #1e293b; --border: #334155; --text: #f8fafc; --accent: #38bdf8; --green: #22c55e; --yellow: #eab308; --red: #ef4444; }
        body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; background: var(--bg); color: var(--text); margin: 0; padding: 20px; }
        .header { display: flex; justify-content: space-between; align-items: center; border-bottom: 1px solid var(--border); padding-bottom: 15px; margin-bottom: 16px; }
        h1 { margin: 0; font-size: 22px; color: var(--accent); }
        .status-bar { display: flex; gap: 10px; flex-wrap: wrap; margin-bottom: 16px; }
        .badge { background: var(--panel); border: 1px solid var(--border); padding: 6px 12px; border-radius: 6px; font-size: 12px; }
        .controls { background: var(--panel); border: 1px solid var(--border); padding: 14px; border-radius: 8px; display: flex; gap: 15px; flex-wrap: wrap; align-items: center; margin-bottom: 16px; }
        input, select { background: var(--bg); border: 1px solid var(--border); color: var(--text); padding: 7px 11px; border-radius: 6px; outline: none; }
        table { width: 100%; border-collapse: collapse; background: var(--panel); border-radius: 8px; overflow: hidden; border: 1px solid var(--border); }
        th, td { padding: 10px 12px; text-align: left; border-bottom: 1px solid var(--border); font-size: 13px; }
        th { background: #182234; color: #94a3b8; font-weight: 600; text-transform: uppercase; font-size: 11px; }
        .num { text-align: right; font-variant-numeric: tabular-nums; }
        .tag { display: inline-block; padding: 2px 8px; border-radius: 4px; font-weight: bold; background: #3b82f6; color: #fff; }
        .badge-id { display: inline-block; padding: 1px 6px; border-radius: 3px; font-size: 10px; font-weight: bold; background: #15803d; color: #fff; margin-left: 6px; }
        .badge-fz { display: inline-block; padding: 1px 6px; border-radius: 3px; font-size: 10px; font-weight: bold; background: #0369a1; color: #fff; margin-left: 6px; }
        .r-high { color: var(--green); font-weight: bold; }
        .r-mid { color: var(--yellow); font-weight: bold; }
        .r-low { color: var(--red); font-weight: bold; }
    </style>
</head>
<body>
    <div class="header">
        <h1>Oddsmatcher Pro Live</h1>
        <div id="last-update" style="color: #94a3b8; font-size: 12px;">Connessione...</div>
    </div>
    <div class="status-bar" id="sources"></div>
    <div class="status-bar" id="coverage"></div>
    <div class="controls">
        <label>Rating min: <input id="min-r" type="number" value="90" step="0.5" style="width: 70px;"></label>
        <label>Bookmaker: <select id="bookie"><option value="">Tutti</option></select></label>
        <label>Campionato: <select id="comp"><option value="">Tutti</option></select></label>
        <label>Cerca Partita: <input id="search" placeholder="es. Parma, Inter..."></label>
        <span id="counter" style="color: #94a3b8; font-size: 12px; margin-left: auto;"></span>
    </div>
    <table>
        <thead>
            <tr>
                <th>Data (IT)</th>
                <th>Partita</th>
                <th>Esito</th>
                <th>Bookmaker</th>
                <th class="num">Punta</th>
                <th class="num">Banca (BF)</th>
                <th class="num">Liquidità</th>
                <th class="num">Rating</th>
                <th class="num">Banco</th>
                <th class="num">Responsabilità</th>
                <th class="num">Netto</th>
                <th class="num">Età BF/BK</th>
            </tr>
        </thead>
        <tbody id="grid"></tbody>
    </table>

    <script>
        let DATA = { results: [], sources: {}, coverage: [] };
        async function fetchState() {
            try {
                const res = await fetch('/api/state');
                DATA = await res.json();
                render();
            } catch(e) {}
        }

        function render() {
            document.getElementById('last-update').innerText = "Ultimo calcolo: " + (DATA.last_calc ? new Date(DATA.last_calc).toLocaleTimeString() : "In attesa...");

            const srcDiv = document.getElementById('sources');
            srcDiv.innerHTML = Object.entries(DATA.sources || {}).map(([k, v]) =>
                `<span class="badge" style="border-left: 3px solid ${v.startsWith('OK') ? 'var(--green)' : 'var(--yellow)'}">${k}: ${v}</span>`
            ).join('');

            const covDiv = document.getElementById('coverage');
            covDiv.innerHTML = (DATA.coverage || []).map(c =>
                `<span class="badge">Copertura <b>${c.bookmaker}</b>: ${c.matched}/${c.total} abbinati</span>`
            ).join('');

            const bmSel = document.getElementById('bookie');
            const currentBM = bmSel.value;
            const bms = [...new Set(DATA.results.map(r => r.bookmaker))].sort();
            if (bmSel.options.length !== bms.length + 1) {
                bmSel.innerHTML = '<option value="">Tutti</option>' + bms.map(b => `<option value="${b}">${b}</option>`).join('');
                bmSel.value = currentBM;
            }

            const compSel = document.getElementById('comp');
            const currentComp = compSel.value;
            const comps = [...new Set(DATA.results.map(r => r.competition))].sort();
            if (compSel.options.length !== comps.length + 1) {
                compSel.innerHTML = '<option value="">Tutti</option>' + comps.map(c => `<option value="${c}">${c}</option>`).join('');
                compSel.value = currentComp;
            }

            const minR = parseFloat(document.getElementById('min-r').value) || 0;
            const search = document.getElementById('search').value.toLowerCase();
            const filtered = DATA.results.filter(r =>
                r.rating >= minR &&
                (!currentBM || r.bookmaker === currentBM) &&
                (!currentComp || r.competition === currentComp) &&
                (!search || r.match.toLowerCase().includes(search))
            );

            document.getElementById('counter').innerText = `${filtered.length} opportunità visualizzate`;

            const tbody = document.getElementById('grid');
            tbody.innerHTML = filtered.map(r => {
                const rClass = r.rating >= 97 ? 'r-high' : (r.rating >= 93 ? 'r-mid' : 'r-low');
                const badgeMatch = r.match_type === 'ID'
                    ? `<span class="badge-id" title="Betradar ID: ${r.betradar_id}">ID</span>`
                    : `<span class="badge-fz" title="Fuzzy Match">${r.match_type}</span>`;

                return `<tr>
                    <td>${r.kickoff}</td>
                    <td><b>${r.match}</b>${badgeMatch}<div style="font-size:11px;color:#64748b;">${r.competition}</div></td>
                    <td><span class="tag">${r.outcome}</span></td>
                    <td>${r.bookmaker}</td>
                    <td class="num">${r.back.toFixed(2)}</td>
                    <td class="num">${r.lay.toFixed(2)}</td>
                    <td class="num">€${Math.round(r.liq)}</td>
                    <td class="num ${rClass}">${r.rating.toFixed(2)}%</td>
                    <td class="num">€${r.lay_stake.toFixed(2)}</td>
                    <td class="num">€${r.liability.toFixed(2)}</td>
                    <td class="num" style="color:${r.net >= 0 ? 'var(--green)' : '#94a3b8'}">${r.net >= 0 ? '+' : ''}€${r.net.toFixed(2)}</td>
                    <td class="num" style="font-size:11px;color:#94a3b8;">${r.bf_age} / ${r.bk_age}</td>
                </tr>`;
            }).join('');
        }

        ['min-r', 'bookie', 'comp', 'search'].forEach(id => document.getElementById(id).addEventListener('input', render));
        setInterval(fetchState, 3000);
        fetchState();
    </script>
</body>
</html>
"""


class EngineHTTPHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/api/state":
            with LOCK:
                payload = {
                    "results": GLOBAL_STATE["results"],
                    "sources": GLOBAL_STATE["sources_status"],
                    "coverage": GLOBAL_STATE["coverage"],
                    "last_calc": GLOBAL_STATE["last_calc"],
                }
            body = json.dumps(payload).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            body = DASHBOARD_HTML.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    def log_message(self, format, *args):
        pass


def run_server_mode(args: argparse.Namespace) -> int:
    ENGINE_CONFIG["stake"] = args.stake
    ENGINE_CONFIG["mode"] = args.mode
    ENGINE_CONFIG["min_liq"] = args.min_liq
    ENGINE_CONFIG["threshold"] = args.threshold
    ENGINE_CONFIG["max_time_diff_h"] = args.max_time_diff_h
    ENGINE_CONFIG["commission"] = args.commission if args.commission is not None else 4.5
    ENGINE_CONFIG["min_overround"] = args.min_overround
    ENGINE_CONFIG["max_overround"] = args.max_overround

    # Pre-carica i file locali se presenti
    if os.path.exists(args.bookies_file):
        d = _load_json(args.bookies_file)
        if d:
            acq = parse_dt(d.get("acquired_at"), naive_tz=timezone.utc) or datetime.now(timezone.utc)
            by_bk: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
            for ev in d.get("events", []):
                if isinstance(ev, dict):
                    by_bk[str(ev.get("bookmaker", "Bookie"))].append(ev)
            with LOCK:
                for bm, evs in by_bk.items():
                    GLOBAL_STATE["bookmakers"][bm] = {"raw_events": evs, "updated_at": acq}
                    GLOBAL_STATE["sources_status"][bm] = f"OK ({len(evs)} da file)"

    if os.path.exists(args.betfair_file):
        d = _load_json(args.betfair_file)
        if d:
            evs = d.get("events", [])
            acq = parse_dt(d.get("acquired_at"), naive_tz=timezone.utc) or datetime.now(timezone.utc)
            with LOCK:
                GLOBAL_STATE["betfair_raw"] = d
                GLOBAL_STATE["sources_status"]["Betfair Exchange"] = (
                    f"OK ({len(evs)} mercati, {acq.astimezone(LOCAL_TZ).strftime('%H:%M:%S')})"
                )

    recalculate_live_state()

    # Avvia monitor sul file Betfair sempre, e i crawler REST se non disabilitati
    threading.Thread(target=_run_betfair_file_watcher, args=(args.betfair_file, 10), daemon=True).start()
    if not args.no_crawl:
        threading.Thread(target=_run_bookie_worker, args=("Bwin", "bwin_client", "BwinDirectClient", 45), daemon=True).start()
        threading.Thread(target=_run_bookie_worker, args=("William Hill", "williamhill_client", "WilliamHillDirectClient", 45), daemon=True).start()
        threading.Thread(target=_run_bookie_worker, args=("Snai", "snai_client", "SnaiDirectClient", 45), daemon=True).start()

    server = ThreadingHTTPServer(("127.0.0.1", args.port), EngineHTTPHandler)
    print("\n" + "=" * 75)
    print(f"[*] Oddsmatcher Web Server attivo su: http://127.0.0.1:{args.port}")
    print(f"[*] Modalità: {args.mode.upper()} | Stake: €{args.stake:.0f} | Liq Min: €{args.min_liq:.0f}")
    print(f"[*] Background API Crawlers (Bwin, WH, Snai): {'DISABILITATI' if args.no_crawl else 'ATTIVI'}")
    print("=" * 75 + "\n")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[*] Arresto server...")
    return 0


# --------------------------------------------------------------------------
# CLI Main
# --------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Oddsmatcher Unificato Betfair Exchange vs Bookmaker")
    p.add_argument("--betfair-file", default=BETFAIR_FILE)
    p.add_argument("--bookies-file", default=BOOKIES_FILE)
    p.add_argument("--stake", type=float, default=100.0, help="Stake sul bookmaker (default: 100)")
    p.add_argument("--mode", choices=["qualifying", "snr"], default="qualifying",
                   help="qualifying = puntata reale; snr = free bet a stake non rimborsata")
    p.add_argument("--min-liq", type=float, default=10.0, help="Liquidità minima al miglior prezzo di banca (€)")
    p.add_argument("--min-rating", type=float, default=0.0, help="Rating minimo da mostrare (%%)")
    p.add_argument("--top", type=int, default=40, help="Mostra i primi N risultati in CLI (0 = tutti, default: 40)")
    p.add_argument("--bookmaker", default=None, help="Filtra per bookmaker (es. bwin, snai, william, eurobet)")
    p.add_argument("--competition", default=None, help="Filtra per campionato")
    p.add_argument("--threshold", type=float, default=82.0, help="Soglia similarità nomi squadra (default: 82)")
    p.add_argument("--max-time-diff-h", type=float, default=4.0, help="Scarto massimo orario in ore (default: 4)")
    p.add_argument("--commission", type=float, default=4.5, help="Commissione Betfair %% (default: 4.5)")
    p.add_argument("--max-age-min", type=float, default=0.0, help="Scarta dati più vecchi di N minuti (0 = disattivato)")
    p.add_argument("--min-overround", type=float, default=0.97, help="Min overround 1X2 bookmaker")
    p.add_argument("--max-overround", type=float, default=1.25, help="Max overround 1X2 bookmaker")
    p.add_argument("--show-unmatched", action="store_true", help="Elenca gli eventi bookmaker non abbinati")
    p.add_argument("--csv", default=None, help="Salva i risultati in un file CSV")
    p.add_argument("--serve", action="store_true", help="Avvia la Web Dashboard locale e i crawler in background")
    p.add_argument("--port", type=int, default=8000, help="Porta HTTP per --serve (default: 8000)")
    p.add_argument("--no-crawl", action="store_true", help="Con --serve, usa solo i JSON senza interrogare le API")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    if args.stake <= 0:
        print("[!] --stake deve essere positivo.")
        return 1

    if args.serve:
        return run_server_mode(args)

    now = datetime.now(timezone.utc)
    max_time_diff = timedelta(hours=args.max_time_diff_h)

    print(f"[*] Caricamento dati da {args.betfair_file} e {args.bookies_file}...")
    bf_events, bf_acq, bf_stats = load_betfair(args.betfair_file, now, args.commission)
    bookie_events, bk_stats = load_bookmakers(
        args.bookies_file, now, args.min_overround, args.max_overround
    )

    print(f"    -> Betfair Exchange: {len(bf_events)} mercati 1X2 validi "
          f"(su {bf_stats.get('mercati_totali', 0)} letti).")
    skipped_bf = {k: v for k, v in bf_stats.items() if k != "mercati_totali" and v}
    if skipped_bf:
        print("       scartati BF: " + ", ".join(f"{k}={v}" for k, v in skipped_bf.items()))

    print(f"    -> Bookmaker: {len(bookie_events)} eventi validi "
          f"(su {bk_stats.get('totali', 0)} letti).")
    skipped_bk = {k: v for k, v in bk_stats.items() if k != "totali" and v}
    if skipped_bk:
        print("       scartati BK: " + ", ".join(f"{k}={v}" for k, v in skipped_bk.items()))

    if not bf_events or not bookie_events:
        print("[!] File dati mancanti o vuoti. Esegui prima gli script di acquisizione.")
        return 1

    if bf_acq is not None and (now - bf_acq) > timedelta(minutes=15):
        print(f"[!] NOTA: dati Betfair acquisiti {format_age(bf_acq, now)} fa.")

    results, stats = match_and_rate(
        bookie_events, bf_events, now,
        stake=args.stake, mode=args.mode, min_liq=args.min_liq,
        threshold=args.threshold, max_time_diff=max_time_diff,
    )

    cov_rows = [
        [bk, tot, matched, tot - matched, f"{(matched / tot * 100):.0f}%" if tot else "0%"]
        for bk, (tot, matched) in sorted(stats["per_bookie"].items())
    ]
    print("\nCopertura abbinamenti per Bookmaker:")
    print(tabulate(cov_rows, headers=["Bookmaker", "Eventi", "Abbinati", "Non abbinati", "%"], tablefmt="github"))
    if stats["scartati"]:
        print("Esiti scartati: " + ", ".join(f"{k}={v}" for k, v in stats["scartati"].items()))

    filtered = results
    if args.bookmaker:
        filtered = [r for r in filtered if args.bookmaker.lower() in r.bookmaker.lower()]
    if args.competition:
        filtered = [r for r in filtered if args.competition.lower() in r.competition.lower()]
    if args.min_rating > 0:
        filtered = [r for r in filtered if r.rating >= args.min_rating]
    if args.max_age_min > 0:
        limit = args.max_age_min * 60
        filtered = [
            r for r in filtered
            if (r.bf_age_s is None or r.bf_age_s <= limit)
            and (r.bk_age_s is None or r.bk_age_s <= limit)
        ]
    if args.top > 0:
        filtered = filtered[: args.top]

    net_header = "Profitto" if args.mode == "snr" else "Risultato"
    title = (
        "ODDSMATCHER - FREE BET SNR (ordinato per Rating decrescente)"
        if args.mode == "snr"
        else "ODDSMATCHER - QUALIFYING BET (ordinato per Rating decrescente)"
    )

    rows = []
    for r in filtered:
        rows.append([
            _local_str(r.kickoff),
            r.competition,
            r.match_name,
            r.match_type,
            r.outcome,
            r.bookmaker,
            f"{r.bookie_back:.2f}",
            f"{r.bf_lay:.2f}",
            f"\u20ac{r.bf_lay_liq:.0f}",
            f"{r.rating:.2f}%",
            f"\u20ac{r.lay_stake:.2f}",
            f"\u20ac{r.liability:.2f}",
            f"{r.net_result:+.2f}\u20ac",
            r.bf_age_str,
            r.bk_age_str,
        ])

    headers = [
        f"Data ({LOCAL_LABEL})", "Campionato", "Partita", "Match", "Esito", "Bookmaker",
        "Punta", "Banca (BF)", "Liq. Disp.", "Rating",
        f"Banco ({args.stake:.0f}\u20ac)", "Resp.", net_header, "Et\u00e0 BF", "Et\u00e0 Book",
    ]

    print("\n" + "=" * 145)
    print(title)
    print("=" * 145)
    if rows:
        print(tabulate(rows, headers=headers, tablefmt="github"))
    else:
        print("Nessun risultato con i filtri correnti.")
    print()

    if args.show_unmatched:
        print_unmatched(stats["unmatched"], bf_events, max_time_diff)

    if args.csv:
        save_csv(args.csv, filtered)

    return 0


if __name__ == "__main__":
    sys.exit(main())