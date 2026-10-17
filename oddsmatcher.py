#!/usr/bin/env python3
"""
Oddsmatcher multi-bookmaker: confronta le quote di Betfair Exchange
(betfair_exchange_data.json) con quelle dei bookmaker (bookmakers_data.json).

Rispetto alla versione precedente:
  - normalizzazione nomi squadra senza collisioni (niente stopword identificative,
    accenti, alias IT/EN, esclusione squadre femminili/giovanili);
  - abbinamento per nome + orario (+ campionato), una sola corrispondenza migliore
    per evento bookmaker (niente duplicati);
  - orario di inizio dei bookmaker interpretato (epoch s/ms, ISO, dd/mm/yyyy);
  - Betfair: scartati in-play, sospesi, gia' iniziati, senza orario (mai "now()" inventato);
    miglior prezzo calcolato davvero (max back / min lay) e controllo book incrociato;
  - controllo di sanita' sulle quote dei bookmaker (overround) per scartare
    mercati sbagliati (Doppia Chance, handicap...);
  - liquidita' verificata sullo stake reale richiesto, con responsabilita';
  - modalita' qualifying bet e free bet (SNR);
  - eta' del dato Betfair E del bookmaker (se i client scrivono "acquired_at"
    per evento viene usato quello, altrimenti il valore globale come limite minimo);
  - riepilogo di copertura per bookmaker e, con --show-unmatched, l'elenco
    degli eventi non abbinati con il candidato Betfair piu' vicino
    (utile per aggiungere alias).

Esempi:
    python oddsmatcher.py
    python oddsmatcher.py --stake 50 --min-rating 95 --top 30
    python oddsmatcher.py --mode snr --bookmaker snai
    python oddsmatcher.py --show-unmatched --csv risultati.csv
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import unicodedata
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from typing import Any, Dict, List, Optional, Tuple

from rapidfuzz import fuzz
from tabulate import tabulate

try:
    from zoneinfo import ZoneInfo

    LOCAL_TZ = ZoneInfo("Europe/Rome")
    LOCAL_LABEL = "IT"
except Exception:  # tzdata mancante (es. Windows) -> ripiega su UTC
    LOCAL_TZ = timezone.utc
    LOCAL_LABEL = "UTC"

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
    event_id: str
    competition: str
    home_team: str
    away_team: str
    kickoff: datetime
    markets: Dict[str, Dict[str, ExchangeMarketData]]
    commission: float = 0.05
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
    acquired_at: Optional[datetime] = None
    acquired_exact: bool = False  # True se il timestamp e' per-evento
    norm_home: str = field(init=False, default="")
    norm_away: str = field(init=False, default="")

    def __post_init__(self) -> None:
        self.norm_home = norm_team(self.home_team)
        self.norm_away = norm_team(self.away_team)


@dataclass
class MatchResult:
    kickoff: datetime
    competition: str
    match_name: str
    bookmaker: str
    outcome: str
    bookie_back: float
    bf_lay: float
    bf_lay_liq: float
    rating: float          # percentuale
    lay_stake: float       # stake da bancare per lo stake scelto
    liability: float       # responsabilita' su Betfair
    net_result: float      # qualifying: risultato atteso (negativo = perdita); snr: profitto
    bf_age_s: Optional[int]
    bk_age_s: Optional[int]
    bf_age_str: str
    bk_age_str: str
    time_verified: bool


# --------------------------------------------------------------------------
# Date e orari
# --------------------------------------------------------------------------

def _from_epoch(v: float) -> Optional[datetime]:
    if v > 1e11:  # millisecondi
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
    "%Y%m%d%H%M%S",
)


def parse_dt(value: Any, naive_tz=timezone.utc) -> Optional[datetime]:
    """Converte epoch (s/ms), ISO o formati italiani in datetime UTC.
    Le date senza fuso vengono interpretate in `naive_tz`."""
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
        if dt is None and re.fullmatch(r"\d{9,14}(\.\d+)?", s):
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

# Token "tecnici" che non identificano la squadra. NON includere parole come
# inter / united / city / real / madrid / sporting: creano collisioni.
TECH_TOKENS = {
    "fc", "ac", "as", "cf", "ss", "ssc", "sc", "us", "cd", "ud", "fk", "sk",
    "afc", "acf", "rc", "rcd", "ca", "bc", "cfc", "sv", "fsv", "vfb", "vfl",
    "tsg", "bv", "sl", "calcio", "club", "de", "the", "sad",
}

# Token che distinguono squadre di una stessa citta'/famiglia: se una squadra
# e' composta solo da questi token NON si considera "sottoinsieme" dell'altra.
AMBIGUOUS_TOKENS = {
    "manchester", "sheffield", "milan", "madrid", "borussia", "bristol",
    "sporting", "real", "atletico", "athletic", "inter", "nottingham",
    "paris", "dynamo", "dinamo", "olympique", "stade", "union", "racing",
    "saint", "st", "red", "young", "royal", "west", "north", "south",
}

# Se un nome contiene uno di questi token e l'altro no, non sono la stessa squadra.
VARIANT_TOKENS = {
    "women", "femminile", "fem", "w", "u17", "u18", "u19", "u20", "u21",
    "u23", "ii", "iii", "b", "reserves", "res", "youth", "primavera",
}

# Chiavi nella forma normalizzata (ascii, minuscolo, senza token tecnici/numeri).
# I valori sono la forma canonica (anch'essa gia' senza token tecnici).
ALIASES: Dict[str, str] = {
    # Italia
    "internazionale": "inter", "internazionale milano": "inter",
    "inter milan": "inter", "inter milano": "inter",
    "hellas verona": "verona", "juve": "juventus",
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
    # Spagna
    "atletico madrid": "atletico madrid", "atl madrid": "atletico madrid",
    "athletic club": "athletic bilbao", "athletic club bilbao": "athletic bilbao",
    "real betis": "betis", "real betis balompie": "betis",
    "celta vigo": "celta", "siviglia": "sevilla", "barcellona": "barcelona",
    "deportivo alaves": "alaves",
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
    "eintracht francoforte": "eintracht frankfurt", "eintracht francoforte": "francoforte",
    "lipsia": "leipzig", "lipsia": "rb lipsia", "rb leipzig": "leipzig", "rasenballsport leipzig": "leipzig",
    "stoccarda": "stuttgart", "friburgo": "freiburg", "amburgo": "hamburg",
    "hamburger": "hamburg", "werder brema": "werder bremen", "brema": "werder bremen",
    "union berlino": "union berlin",
    # Francia
    "paris saint germain": "psg", "paris saint-germain": "psg", "paris sg": "psg",
    "olympique marsiglia": "marseille", "olympique marseille": "marseille",
    "marsiglia": "marseille",
    "olympique lione": "lyon", "olympique lyonnais": "lyon", "lione": "lyon",
    "lilla": "lille", "lille osc": "lille", "losc lille": "lille",
    "nizza": "nice", "ogc nice": "nice", "stade rennais": "rennes",
    "strasburgo": "strasbourg", "strasburgo alsazia": "strasbourg", 
	"tolosa": "toulouse", "stade brestois": "brest",
    "angers sco": "angers", "aj auxerre": "auxerre",
    # Portogallo
    "sporting lisbona": "sporting cp", "sporting lisbon": "sporting cp",
    "sporting clube de portugal": "sporting cp", "sporting clube portugal": "sporting cp",
    "vitoria guimaraes": "vitoria guimaraes", "vitoria sc": "vitoria guimaraes",
    # Europa
    "stella rossa": "red star", "stella rossa belgrado": "red star",
    "crvena zvezda": "red star", "red star belgrade": "red star",
    "dinamo zagabria": "dinamo zagreb", "dinamo zagabria": "zagabria", "gnk dinamo zagreb": "dinamo zagreb",
    "slavia praga": "slavia prague", "slavia praha": "slavia prague",
    "sparta praga": "sparta prague", "sparta praha": "sparta prague",
    "bruges": "brugge", "club bruges": "brugge",
    "anversa": "antwerp", "royal antwerp": "antwerp",
    "salisburgo": "salzburg", "salisburgo": "red bull salisburgo", "red bull salzburg": "salzburg", "rb salzburg": "salzburg",
    "psv eindhoven": "psv", "ajax amsterdam": "ajax", "feyenoord rotterdam": "feyenoord",
    "shakhtar donetsk": "shakhtar", "sh donetsk": "shakhtar",
    "dynamo kiev": "dynamo kyiv", "dinamo kiev": "dynamo kyiv", "dynamo kyev": "dynamo kyiv",
    "olympiakos": "olympiacos", "olympiakos piraeus": "olympiacos",
    "olympiacos piraeus": "olympiacos", "olympiacos pireo": "olympiacos",
    "aek atene": "aek athens", "young boys": "young boys",
	"nec nimega": "nec nijmegen", "nimega n.": "nec nijmegen", "levski sofia": "pfc levski sofia",
	"az a.": "az alkmaar", "hapoel beer": "hapoel be'er sheva",
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
    """Similarita' 0-100 tra due nomi gia' normalizzati."""
    if not a or not b:
        return 0.0
    if a == b:
        return 100.0
    ta, tb = set(a.split()), set(b.split())
    if (ta ^ tb) & VARIANT_TOKENS:  # femminile / giovanile / riserve
        return 0.0
    score = float(fuzz.token_sort_ratio(a, b))
    small, big = (ta, tb) if len(ta) <= len(tb) else (tb, ta)
    # "verona" vs "hellas verona", "leverkusen" vs "bayer leverkusen", ...
    if small < big and not small <= AMBIGUOUS_TOKENS:
        score = max(score, 92.0)
    return score


def same_competition(a: str, b: str) -> bool:
    na, nb = (a or "").strip().lower(), (b or "").strip().lower()
    if not na or not nb or "calcio" in (na, nb):
        return True  # etichetta generica/sconosciuta: non scartare
    return na == nb


# --------------------------------------------------------------------------
# Caricamento dati
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


def _levels(lst: Any) -> List[Tuple[float, float]]:
    out: List[Tuple[float, float]] = []
    for x in lst or []:
        if not isinstance(x, dict):
            continue
        p, s = _safe_float(x.get("price")), _safe_float(x.get("size"))
        if p > 1.0 and s > 0:
            out.append((p, s))
    return out


DRAW_RE = re.compile(r"\b(the draw|draw|pareggio)\b")


def _parse_runners(runners: List[Any]) -> Optional[Tuple[str, str, Dict[str, ExchangeMarketData]]]:
    if len(runners) != 3:
        return None
    items = []
    for r in runners:
        if not isinstance(r, dict):
            return None
        state = r.get("state") or {}
        if str(state.get("status", "ACTIVE")).upper() != "ACTIVE":
            return None
        prio = state.get("sortPriority")
        if not isinstance(prio, (int, float)):
            return None  # senza priorita' non si distingue casa/trasferta
        name = str((r.get("description") or {}).get("runnerName", "")).strip()
        ex = r.get("exchange") or {}
        backs = _levels(ex.get("availableToBack"))
        lays = _levels(ex.get("availableToLay"))

        data = ExchangeMarketData()
        if backs:
            best_b = max(p for p, _ in backs)  # miglior prezzo di punta = il piu' alto
            data.back_odds = best_b
            data.back_liquidity = sum(s for p, s in backs if p == best_b)
        if lays:
            best_l = min(p for p, _ in lays)  # miglior prezzo di banca = il piu' basso
            data.lay_odds = best_l
            data.lay_liquidity = sum(s for p, s in lays if p == best_l)
        items.append((prio, name, data))

    items.sort(key=lambda x: x[0])
    draw_idx = [i for i, (_, n, _) in enumerate(items) if DRAW_RE.search(n.lower())]
    if len(draw_idx) > 1:
        return None
    if len(draw_idx) == 1:
        draw = items.pop(draw_idx[0])
        teams = items
    else:
        teams, draw = items[:2], items[2]
    if not teams[0][1] or not teams[1][1]:
        return None
    return teams[0][1], teams[1][1], {"1": teams[0][2], "2": teams[1][2], "X": draw[2]}


def load_betfair(
    path: str, now: datetime, commission_override: Optional[float] = None
) -> Tuple[List[BetfairEvent], Optional[datetime], Dict[str, int]]:
    stats: Dict[str, int] = defaultdict(int)
    if not os.path.exists(path):
        return [], None, stats
    data = _load_json(path)
    if data is None:
        return [], None, stats

    file_acq = parse_dt(data.get("acquired_at"))
    events: List[BetfairEvent] = []
    seen = set()

    for et in data.get("eventTypes", []) or []:
        for en in et.get("eventNodes", []) or []:
            comp = en.get("competition") or "Calcio"
            ev_meta = en.get("event") or {}
            for mn in en.get("marketNodes", []) or []:
                stats["mercati_totali"] += 1
                desc = mn.get("description") or {}
                if desc.get("marketType") != "MATCH_ODDS":
                    stats["non_match_odds"] += 1
                    continue

                m_id = str(mn.get("marketId"))
                if m_id in seen:
                    stats["duplicati"] += 1
                    continue
                seen.add(m_id)

                state = mn.get("state") or {}
                if state.get("inplay") is True:
                    stats["in_play"] += 1
                    continue
                if str(state.get("status", "OPEN")).upper() != "OPEN":
                    stats["sospesi_chiusi"] += 1
                    continue

                kickoff = parse_dt(desc.get("marketTime")) or parse_dt(ev_meta.get("openDate"))
                if kickoff is None:
                    stats["senza_orario"] += 1
                    continue
                if kickoff <= now:
                    stats["gia_iniziati"] += 1
                    continue

                parsed = _parse_runners(mn.get("runners") or [])
                if parsed is None:
                    stats["runner_non_validi"] += 1
                    continue
                home, away, odds = parsed

                if commission_override is not None:
                    comm_pct = commission_override
                else:
                    comm_pct = _safe_float((mn.get("rates") or {}).get("marketBaseRate"), 5.0)
                if not 0.0 <= comm_pct <= 20.0:
                    comm_pct = 5.0

                acq = parse_dt(mn.get("acquired_at")) or file_acq
                events.append(BetfairEvent(
                    event_id=m_id,
                    competition=comp,
                    home_team=home,
                    away_team=away,
                    kickoff=kickoff,
                    markets={"1X2": odds},
                    commission=comm_pct / 100.0,
                    acquired_at=acq,
                ))
    return events, file_acq, stats


def load_bookmakers(
    path: str, now: datetime, min_overround: float, max_overround: float
) -> Tuple[List[BookieEvent], Dict[str, int]]:
    stats: Dict[str, int] = defaultdict(int)
    if not os.path.exists(path):
        return [], stats
    data = _load_json(path)
    if data is None:
        return [], stats

    file_acq = parse_dt(data.get("acquired_at"))
    best: Dict[Tuple[Any, ...], BookieEvent] = {}

    for ev in data.get("events", []) or []:
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
        if kickoff is not None and kickoff <= now:
            stats["gia_iniziati"] += 1
            continue

        ev_acq = parse_dt(ev.get("acquired_at"))
        be = BookieEvent(
            bookmaker=str(ev.get("bookmaker", "Bookie")),
            competition=str(ev.get("competition", "Calcio")),
            home_team=home,
            away_team=away,
            kickoff=kickoff,
            markets={"1X2": odds},
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


# --------------------------------------------------------------------------
# Matching e rating
# --------------------------------------------------------------------------

def find_best_match(
    b_ev: BookieEvent,
    bf_events: List[BetfairEvent],
    threshold: float,
    max_time_diff: timedelta,
) -> Optional[Tuple[BetfairEvent, float, bool]]:
    """Ritorna (evento Betfair, score, orario_verificato) oppure None."""
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

        if b_ev.kickoff is not None:
            delta = abs((b_ev.kickoff - bf.kickoff).total_seconds())
            if delta > max_time_diff.total_seconds():
                continue
            time_verified = True
        else:
            delta = 0.0
            time_verified = False
            if weakest < 92.0:  # senza orario serve un abbinamento molto netto
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
    """Ritorna (rating%, stake_banco, responsabilita', risultato_netto).

    qualifying: lay = S*p/(L-c);  rating = p(1-c)/(L-c);  risultato = S*(rating-1)
    snr (free bet stake non rimborsata): lay = S(p-1)/(L-c);  rating = (p-1)/(L-c);
                                         profitto = lay*(1-c)
    """
    denom = lay_odds - comm
    if mode == "snr":
        lay_stake = stake * (p_book - 1.0) / denom
        rating = (p_book - 1.0) / denom
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
    threshold: float = 85.0,
    max_time_diff: timedelta = timedelta(hours=3),
) -> Tuple[List[MatchResult], Dict[str, Any]]:
    results: List[MatchResult] = []
    stats: Dict[str, Any] = {
        "per_bookie": defaultdict(lambda: [0, 0]),  # [totali, abbinati]
        "unmatched": [],
        "scartati": defaultdict(int),
    }

    for b_ev in bookie_events:
        stats["per_bookie"][b_ev.bookmaker][0] += 1
        m = find_best_match(b_ev, bf_events, threshold, max_time_diff)
        if m is None:
            stats["unmatched"].append(b_ev)
            continue
        bf_ev, _score, time_verified = m
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
                kickoff=bf_ev.kickoff,
                competition=bf_ev.competition,
                match_name=f"{bf_ev.home_team} - {bf_ev.away_team}",
                bookmaker=b_ev.bookmaker,
                outcome=out,
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
        if b_ev.kickoff is not None and abs(b_ev.kickoff - bf.kickoff) > max_time_diff:
            continue
        if not same_competition(b_ev.competition, bf.competition):
            continue
        hs = team_similarity(b_ev.norm_home, bf.norm_home)
        as_ = team_similarity(b_ev.norm_away, bf.norm_away)
        sc = (hs + as_) / 2.0
        if best is None or sc > best[0]:
            best = (sc, bf, hs, as_)
    return best if best and best[0] >= 40.0 else None


# --------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------

def _local(dt: datetime) -> str:
    return dt.astimezone(LOCAL_TZ).strftime("%d/%m %H:%M")


def print_unmatched(
    unmatched: List[BookieEvent], bf_events: List[BetfairEvent], max_time_diff: timedelta
) -> None:
    bf_comps = {e.competition.strip().lower() for e in bf_events}
    rows = []
    for b in sorted(unmatched, key=lambda x: (x.bookmaker, x.competition, x.kickoff or datetime.max.replace(tzinfo=timezone.utc))):
        comp = b.competition.strip().lower()
        if comp != "calcio" and comp not in bf_comps:
            continue  # campionato non coperto da Betfair: rumore
        near = nearest_candidate(b, bf_events, max_time_diff)
        hint = ""
        if near:
            sc, bf, hs, as_ = near
            hint = f"{bf.home_team} - {bf.away_team} ({hs:.0f}/{as_:.0f})"
        rows.append([
            b.bookmaker, b.competition,
            f"{b.home_team} - {b.away_team}",
            _local(b.kickoff) if b.kickoff else "N/D",
            hint,
        ])
    print("\n" + "=" * 120)
    print(f"EVENTI BOOKMAKER NON ABBINATI ({len(rows)}) - candidato Betfair piu' vicino (score casa/trasferta)")
    print("=" * 120)
    if rows:
        print(tabulate(rows, headers=["Bookmaker", "Campionato", "Partita", f"Data ({LOCAL_LABEL})", "Candidato BF"], tablefmt="github"))
    else:
        print("Nessuno.")
    print()


def save_csv(path: str, results: List[MatchResult]) -> None:
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow([
            "kickoff_utc", "competition", "match", "outcome", "bookmaker", "back_odds",
            "lay_odds", "lay_liquidity", "rating_pct", "lay_stake", "liability",
            "net_result", "bf_age_s", "bookie_age_s", "time_verified",
        ])
        for r in results:
            w.writerow([
                r.kickoff.isoformat(), r.competition, r.match_name, r.outcome, r.bookmaker,
                f"{r.bookie_back:.2f}", f"{r.bf_lay:.2f}", f"{r.bf_lay_liq:.2f}",
                f"{r.rating:.2f}", f"{r.lay_stake:.2f}", f"{r.liability:.2f}",
                f"{r.net_result:.2f}", r.bf_age_s, r.bk_age_s, int(r.time_verified),
            ])
    print(f"[+] CSV salvato in {path}")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Oddsmatcher Betfair Exchange vs bookmaker")
    p.add_argument("--betfair-file", default=BETFAIR_FILE)
    p.add_argument("--bookies-file", default=BOOKIES_FILE)
    p.add_argument("--stake", type=float, default=100.0, help="Stake sul bookmaker (default 100)")
    p.add_argument("--mode", choices=["qualifying", "snr"], default="qualifying",
                   help="qualifying = puntata reale; snr = free bet a stake non rimborsata")
    p.add_argument("--min-liq", type=float, default=10.0, help="Liquidita' minima al miglior prezzo di banca")
    p.add_argument("--min-rating", type=float, default=0.0, help="Rating minimo da mostrare (%%)")
    p.add_argument("--top", type=int, default=0, help="Mostra solo i primi N risultati (0 = tutti)")
    p.add_argument("--bookmaker", default=None, help="Filtra per bookmaker (sottostringa)")
    p.add_argument("--competition", default=None, help="Filtra per campionato (sottostringa)")
    p.add_argument("--threshold", type=float, default=85.0, help="Soglia similarita' nomi squadra (default 85)")
    p.add_argument("--max-time-diff-h", type=float, default=3.0, help="Scarto massimo orario in ore (default 3)")
    p.add_argument("--commission", type=float, default=None, help="Forza la commissione Betfair (%%)")
    p.add_argument("--max-age-min", type=float, default=0.0,
                   help="Scarta risultati con dati piu' vecchi di N minuti (0 = disattivato)")
    p.add_argument("--min-overround", type=float, default=0.97, help="Scarta 1X2 bookmaker con sum(1/q) sotto questo valore")
    p.add_argument("--max-overround", type=float, default=1.25, help="Scarta 1X2 bookmaker con sum(1/q) sopra questo valore")
    p.add_argument("--show-unmatched", action="store_true", help="Elenca gli eventi bookmaker non abbinati")
    p.add_argument("--csv", default=None, help="Salva i risultati in CSV")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    if args.stake <= 0:
        print("[!] --stake deve essere positivo.")
        return 1

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
        print("       scartati: " + ", ".join(f"{k}={v}" for k, v in skipped_bf.items()))
    print(f"    -> Bookmaker: {len(bookie_events)} eventi utilizzabili "
          f"(su {bk_stats.get('totali', 0)} letti).")
    skipped_bk = {k: v for k, v in bk_stats.items() if k != "totali" and v}
    if skipped_bk:
        print("       scartati: " + ", ".join(f"{k}={v}" for k, v in skipped_bk.items()))

    if not bf_events or not bookie_events:
        print("[!] File dati mancanti o incompleti. Esegui prima gli agenti Betfair e bookmaker.")
        return 1

    if bf_acq is not None and (now - bf_acq) > timedelta(minutes=10):
        print(f"[!] ATTENZIONE: dati Betfair vecchi di {format_age(bf_acq, now)}. Le quote potrebbero non essere piu' valide.")

    results, stats = match_and_rate(
        bookie_events, bf_events, now,
        stake=args.stake, mode=args.mode, min_liq=args.min_liq,
        threshold=args.threshold, max_time_diff=max_time_diff,
    )

    # Copertura per bookmaker
    cov_rows = [[bk, tot, matched, tot - matched]
                for bk, (tot, matched) in sorted(stats["per_bookie"].items())]
    print("\nCopertura abbinamenti:")
    print(tabulate(cov_rows, headers=["Bookmaker", "Eventi", "Abbinati", "Non abbinati"], tablefmt="github"))
    if stats["scartati"]:
        print("Quote scartate: " + ", ".join(f"{k}={v}" for k, v in stats["scartati"].items()))

    # Filtri sui risultati
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

    if args.mode == "snr":
        net_header = "Profitto"
        title = "ODDSMATCHER - FREE BET SNR (ordinato per Rating decrescente)"
    else:
        net_header = "Risultato"
        title = "ODDSMATCHER - QUALIFYING BET (ordinato per Rating decrescente)"

    rows = []
    for r in filtered:
        flag = "" if r.time_verified else " (?)"
        rows.append([
            _local(r.kickoff),
            r.competition,
            r.match_name + flag,
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
        f"Data ({LOCAL_LABEL})", "Campionato", "Partita", "Esito", "Bookmaker",
        "Punta", "Banca (BF)", "Liq. Disp.", "Rating",
        f"Banco ({args.stake:.0f}\u20ac)", "Resp.", net_header, "Et\u00e0 BF", "Et\u00e0 Book",
    ]

    print("\n" + "=" * 140)
    print(title)
    print("=" * 140)
    if rows:
        print(tabulate(rows, headers=headers, tablefmt="github"))
    else:
        print("Nessun risultato con i filtri correnti.")
    print()
    print("Note: (?) = orario del bookmaker non disponibile, abbinamento solo per nome. "
          "\u2265 = et\u00e0 minima (timestamp globale del file).")
    print("Verifica sempre le quote sui siti prima di piazzare le giocate.\n")

    if args.show_unmatched:
        print_unmatched(stats["unmatched"], bf_events, max_time_diff)

    if args.csv:
        save_csv(args.csv, filtered)

    return 0


if __name__ == "__main__":
    sys.exit(main())