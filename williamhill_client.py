#!/usr/bin/env python3
"""
Client API diretto per William Hill IT (XSportDatastore).
Quote estratte:
- 1X2 (Esito Finale)
- Under/Over (1.5, 2.5, 3.5, 4.5)
- Goal/No Goal (GG/NG)
- Doppia Chance (1X, X2, 12)
- Betradar ID (bid)
- Kickoff normalizzato in UTC ISO
"""

import os
import json
import time
import tempfile
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Any, Optional
from curl_cffi import requests

OUTPUT_FILE = "bookmakers_data.json"
BASE_URL = "https://sports.williamhill.it/XSportDatastore"
SPORT_ID_FOOTBALL = 1
DEFAULT_AGGREGATE = 319
RETRIES = 3

# Etichetta -> slug SEO nel menu
WH_LEAGUE_SLUGS: Dict[str, List[str]] = {
    "Serie A (IT)":        ["calcio/italia/serie-a"],
    "Serie B (IT)":        ["calcio/italia/serie-b"],
    "Premier League (UK)": ["calcio/inghilterra/premier-league"],
    "La Liga (ES)":        ["calcio/spagna/la-liga", "calcio/spagna/laliga"],
    "Bundesliga (DE)":     ["calcio/germania/bundesliga"],
    "Ligue 1 (FR)":        ["calcio/francia/ligue-1"],
    "Champions League":    ["calcio/coppe-europee/uefa-champions-league"],
    "Europa League":       ["calcio/coppe-europee/uefa-europa-league"],
}

# Fallback se il menu live non risponde
FALLBACK_LEAGUES: Dict[str, Dict[str, int]] = {
    "Serie A (IT)":        {"categoryId": 31, "tournamentId": 33,  "aggregate": 319},
    "Premier League (UK)": {"categoryId": 1,  "tournamentId": 1,   "aggregate": 319},
    "La Liga (ES)":        {"categoryId": 32, "tournamentId": 36,  "aggregate": 319},
    "Bundesliga (DE)":     {"categoryId": 30, "tournamentId": 42,  "aggregate": 319},
    "Ligue 1 (FR)":        {"categoryId": 7,  "tournamentId": 4,   "aggregate": 319},
    "Champions League":    {"categoryId": -3, "tournamentId": -7,  "aggregate": 319},
    "Europa League":       {"categoryId": -3, "tournamentId": -8,  "aggregate": 319},
}


# ----------------------------------------------------------------------------- helpers
def _last_sunday(year: int, month: int) -> datetime:
    nxt = datetime(year + (month == 12), (month % 12) + 1, 1)
    d = nxt - timedelta(days=1)
    return d - timedelta(days=(d.weekday() + 1) % 7)


def rome_ts_to_utc_iso(ts: str) -> str:
    """'20261019 18:30:00' (ora italiana) -> '2026-10-19T16:30:00Z'."""
    try:
        local = datetime.strptime(ts.strip(), "%Y%m%d %H:%M:%S")
    except Exception:
        return ts
    dst_start = _last_sunday(local.year, 3).replace(hour=2)
    dst_end = _last_sunday(local.year, 10).replace(hour=3)
    offset = 2 if dst_start <= local < dst_end else 1
    return (local - timedelta(hours=offset)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _seo_paths(node: Dict) -> List[str]:
    raw = node.get("seo")
    try:
        data = json.loads(raw) if isinstance(raw, str) else (raw or {})
    except Exception:
        return []
    out = []
    for v in data.values() if isinstance(data, dict) else []:
        if isinstance(v, dict):
            out += [str(x).strip("/").lower() for x in v.values()]
    return out


# ----------------------------------------------------------------------------- client
class WilliamHillDirectClient:
    def __init__(self):
        self.session = requests.Session(impersonate="chrome120")
        self.headers = {
            "Accept": "*/*",
            "Accept-Language": "it-IT,it;q=0.9",
            "Referer": "https://sports.williamhill.it/betting/it-it/calcio",
        }

    def _get_json(self, endpoint: str, params: Dict[str, Any], label: str) -> Optional[Any]:
        url = f"{BASE_URL}/{endpoint}"
        for attempt in range(1, RETRIES + 1):
            try:
                res = self.session.get(url, params=params, headers=self.headers, timeout=15)
                if res.status_code == 200:
                    return json.loads(res.text)
                if res.status_code == 404:
                    print(f"[!] William Hill HTTP 404 su {label}: endpoint o ID inesistente")
                    return None
                print(f"[!] William Hill HTTP {res.status_code} su {label} (tentativo {attempt}/{RETRIES})")
            except Exception as e:
                print(f"[!] Errore connessione {label} (tentativo {attempt}/{RETRIES}): {e}")
            time.sleep(1.5 * attempt)
        return None

    def discover_leagues(self) -> Dict[str, Dict[str, int]]:
        menu = self._get_json("getMenuPrematch",
                              {"systemCode": "WILLIAMHILL", "lingua": "IT", "hash": ""}, "menu")
        if not isinstance(menu, dict):
            print("[!] Menu non disponibile: uso gli ID di fallback")
            return dict(FALLBACK_LEAGUES)

        football = next((s for s in menu.get("sps", []) if s.get("id") == SPORT_ID_FOOTBALL), None)
        if not football:
            print("[!] Calcio non trovato nel menu: uso gli ID di fallback")
            return dict(FALLBACK_LEAGUES)

        by_slug: Dict[str, Dict[str, int]] = {}
        for cat in football.get("cts", []):
            for t in cat.get("tns", []):
                entry = {
                    "categoryId": int(t.get("idc", cat.get("id"))),
                    "tournamentId": int(t["id"]),
                    "aggregate": int(t.get("ba") or DEFAULT_AGGREGATE),
                }
                for p in _seo_paths(t):
                    by_slug[p.split("it-it/")[-1]] = entry

        leagues: Dict[str, Dict[str, int]] = {}
        for label, slugs in WH_LEAGUE_SLUGS.items():
            hit = next((by_slug[s] for s in slugs if s in by_slug), None)
            if hit:
                leagues[label] = hit
            elif label in FALLBACK_LEAGUES and label != "Serie B (IT)":
                print(f"[!] {label}: non trovata nel menu, uso l'ID di fallback")
                leagues[label] = FALLBACK_LEAGUES[label]
            else:
                print(f"[-] {label}: non offerta da William Hill (assente dal menu), salto")
        return leagues

    def fetch_league(self, league_name: str, cfg: Dict[str, int]) -> Optional[List[Dict[str, Any]]]:
        params = {
            "systemCode": "WILLIAMHILL",
            "lingua": "IT",
            "hash": "",
            "sportId": str(SPORT_ID_FOOTBALL),
            "categoryId": str(cfg["categoryId"]),
            "tournamentId": str(cfg["tournamentId"]),
            "idAggregata": str(cfg.get("aggregate", DEFAULT_AGGREGATE)),
        }
        data = self._get_json("getTorneoCentrale", params, league_name)
        if data is None:
            return None
        return self._parse_payload(data, league_name)

    def fetch_all(self):
        leagues = self.discover_leagues()
        print(f"[*] Campionati William Hill configurati: {list(leagues.keys())}")
        all_events: List[Dict[str, Any]] = []
        failed: List[str] = []
        for name, cfg in leagues.items():
            evs = self.fetch_league(name, cfg)
            if evs is None:
                failed.append(name)
                continue
            print(f"[+] William Hill API: estratti {len(evs)} match per {name}")
            all_events.extend(evs)
        return all_events, failed

    def _parse_payload(self, data: Dict[str, Any], league_name: str) -> List[Dict[str, Any]]:
        events, seen = [], set()
        avs = data.get("avs", []) if isinstance(data, dict) else []
        if not isinstance(avs, list):
            return []

        for av in avs:
            if not isinstance(av, dict):
                continue
            if av.get("lv") is True or av.get("ant") is True:  # Salta live e antepost
                continue

            dsl = av.get("dsl") or {}
            title = str(dsl.get("IT") or dsl.get("ORIGINAL_FROM_DB") or "").strip() if isinstance(dsl, dict) else ""
            if " - " not in title:
                continue
            home, away = [s.strip() for s in title.split(" - ", 1)]

            # Estrazione di tutti i mercati disponibili (1X2, U/O, GG/NG, DC)
            markets = self._extract_all_markets(av)
            if not markets.get("1X2"):
                continue

            bid = av.get("bid")
            betradar_id = str(bid).strip() if bid not in (None, "", 0, "0") else None

            key = betradar_id or f"{home}|{away}|{av.get('ts')}"
            if key in seen:
                continue
            seen.add(key)

            events.append({
                "bookmaker": "William Hill",
                "competition": league_name,
                "home_team": home,
                "away_team": away,
                "kickoff": rome_ts_to_utc_iso(str(av.get("ts", ""))),
                "betradar_id": betradar_id,
                "markets": markets,
            })
        return events

    @classmethod
    def _extract_all_markets(cls, av: Dict[str, Any]) -> Dict[str, Dict[str, float]]:
        """
        Estrae:
        - 1X2 ('1X2'): {'1': ..., 'X': ..., '2': ...}
        - Under/Over ('OVER_UNDER_15', 'OVER_UNDER_25', 'OVER_UNDER_35', 'OVER_UNDER_45'):
            {'Under': ..., 'Over': ...}
        - Goal / No Goal ('GOAL_NOGOAL'): {'Goal': ..., 'NoGoal': ...}
        - Doppia Chance ('DOUBLE_CHANCE'): {'1X': ..., 'X2': ..., '12': ...}
        """
        markets: Dict[str, Dict[str, float]] = {}
        double_chance: Dict[str, float] = {}

        for sc in av.get("scs", []):
            if not isinstance(sc, dict):
                continue
            cs = sc.get("cs")
            h = sc.get("h", 0)
            eqs = sc.get("eqs", [])

            # 1. ESITO FINALE 1X2 (cs=3, h=0)
            if cs == 3 and h == 0 and len(eqs) == 3:
                odds_1x2: Dict[str, float] = {}
                for eq in eqs:
                    q = eq.get("q", 0) if isinstance(eq, dict) else 0
                    if not isinstance(q, (int, float)) or q <= 100:
                        odds_1x2 = {}
                        break
                    key = {1: "1", 2: "X", 3: "2"}.get(eq.get("ce"))
                    if key:
                        odds_1x2[key] = q / 100.0
                if len(odds_1x2) == 3:
                    markets["1X2"] = odds_1x2

            # 2. UNDER / OVER (cs=7989, h in [150, 250, 350, 450])
            elif cs == 7989 and h in (150, 250, 350, 450) and len(eqs) == 2:
                line_map = {150: "OVER_UNDER_15", 250: "OVER_UNDER_25", 350: "OVER_UNDER_35", 450: "OVER_UNDER_45"}
                mkt_key = line_map[h]
                uo_odds: Dict[str, float] = {}
                for eq in eqs:
                    q = eq.get("q", 0) if isinstance(eq, dict) else 0
                    if not isinstance(q, (int, float)) or q <= 100:
                        uo_odds = {}
                        break
                    # ce=1 -> Under, ce=2 -> Over
                    key = {1: "Under", 2: "Over"}.get(eq.get("ce"))
                    if key:
                        uo_odds[key] = q / 100.0
                if len(uo_odds) == 2:
                    markets[mkt_key] = uo_odds

            # 3. GOAL / NO GOAL (cs=18, h=0)
            elif cs == 18 and h == 0 and len(eqs) == 2:
                gg_odds: Dict[str, float] = {}
                for eq in eqs:
                    q = eq.get("q", 0) if isinstance(eq, dict) else 0
                    if not isinstance(q, (int, float)) or q <= 100:
                        gg_odds = {}
                        break
                    # ce=1 -> Goal (GG), ce=2 -> No Goal (NG)
                    key = {1: "Goal", 2: "NoGoal"}.get(eq.get("ce"))
                    if key:
                        gg_odds[key] = q / 100.0
                if len(gg_odds) == 2:
                    markets["GOAL_NOGOAL"] = gg_odds

            # 4. DOPPIA CHANCE (cs=15 per 1X, cs=16 per X2, cs=17 per 12)
            elif cs in (15, 16, 17) and h == 0 and eqs:
                eq = eqs[0] if isinstance(eqs, list) else None
                if isinstance(eq, dict):
                    q = eq.get("q", 0)
                    if isinstance(q, (int, float)) and q > 100:
                        label = {15: "1X", 16: "X2", 17: "12"}.get(cs)
                        if label:
                            double_chance[label] = q / 100.0

        if len(double_chance) == 3:
            markets["DOUBLE_CHANCE"] = double_chance

        return markets


# ----------------------------------------------------------------------------- salvataggio atomico
def merge_and_save(bookmaker_name: str, new_events: List[Dict[str, Any]], failed_competitions: List[str]):
    existing_events: List[Dict[str, Any]] = []
    if os.path.exists(OUTPUT_FILE):
        try:
            with open(OUTPUT_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            for ev in data.get("events", []):
                if ev.get("bookmaker") != bookmaker_name:
                    existing_events.append(ev)
                elif ev.get("competition") in failed_competitions:
                    existing_events.append(ev)
        except Exception as e:
            print(f"[!] File esistente illeggibile ({e}): lo ricreo da zero.")
            existing_events = []

    combined = existing_events + new_events
    payload = {
        "acquired_at": datetime.now(timezone.utc).isoformat(),
        "total_events": len(combined),
        "events": combined,
    }

    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(os.path.abspath(OUTPUT_FILE)), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, ensure_ascii=False)
        os.replace(tmp, OUTPUT_FILE)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)

    print("\n" + "=" * 65)
    print(f"[+] SALVATO: {len(new_events)} match di {bookmaker_name} aggiunti a {OUTPUT_FILE}")
    print(f"[+] Totale complessivo eventi nel file: {len(combined)}")
    if failed_competitions:
        print(f"[!] Dati precedenti mantenuti per: {', '.join(failed_competitions)}")
    print("=" * 65)


if __name__ == "__main__":
    print("[*] Avvio scaricamento quote William Hill tramite API nativa...")
    client = WilliamHillDirectClient()
    extracted, failed = client.fetch_all()
    if extracted or failed:
        merge_and_save("William Hill", extracted, failed)
        if extracted:
            s = extracted[0]
            print(f"\nEsempio William Hill: {s['home_team']} vs {s['away_team']} (Betradar: {s.get('betradar_id') or 'N/A'})")
            print(f"  • 1X2:             {s['markets'].get('1X2')}")
            print(f"  • Under/Over 2.5:  {s['markets'].get('OVER_UNDER_25')}")
            print(f"  • Goal/No Goal:    {s['markets'].get('GOAL_NOGOAL')}")
            print(f"  • Doppia Chance:   {s['markets'].get('DOUBLE_CHANCE')}")
    else:
        print("[!] Nessun evento estratto da William Hill.")