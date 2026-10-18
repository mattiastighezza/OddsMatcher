#!/usr/bin/env python3
"""
Client API diretto per Bwin IT (quote 1X2 + Betradar ID).

Modifiche rispetto alla versione precedente:
- Premier League inglese = 102841 (101409 e' la Premier League israeliana).
- Il mercato 1X2 viene riconosciuto dai PARAMETRI (MarketType=3way, Period=RegularTime,
  senza MarketSubType), non dal nome o dall'ordine: il mercato "2up - Vincita istantanea"
  e' anch'esso isMain/3 esiti ma ha quote diverse, e a volte compare prima del 1X2 vero.
- Betradar ID letto da addons.betRadar (l'ID dopo "2:" e' l'ID INTERNO di Bwin, non Betradar).
- Niente piu' soglia "almeno 15 mercati" (scartava partite valide): conta solo trovare il 1X2.
- Squadre da participants (HomeTeam/AwayTeam), non dallo split del nome.
- Partite live escluse di default, paginazione, retry, controllo regione/competizione,
  salvataggio atomico, e i dati vecchi di una lega NON vengono cancellati se il download fallisce.
"""

import os
import json
import time
import tempfile
from datetime import datetime, timezone
from typing import Dict, List, Any, Optional, Tuple
from curl_cffi import requests

OUTPUT_FILE = "bookmakers_data.json"
BASE_URL = "https://www.bwin.it/cds-api/bettingoffer/fixtures"
# Chiave pubblica incorporata nel sito: puo' cambiare. Sovrascrivibile da variabile d'ambiente.
ACCESS_ID = os.getenv("BWIN_ACCESS_ID", "NzIyNGU2ZTctMzhkYi00NDM2LThlZmMtMjExNzg4ZDE1YjVh")

INCLUDE_LIVE = False     # True = salva anche le partite in corso (quote in-play)
PAGE_SIZE = 50
MAX_PAGES = 10
RETRIES = 3

# nome -> (competitionId, regione attesa). La regione serve a intercettare ID sbagliati.
BWIN_LEAGUES: Dict[str, Tuple[str, str]] = {
    "Serie A (IT)":        ("102846", "Italia"),
    "Serie B (IT)":        ("102848", "Italia"),
    "Premier League (UK)": ("102841", "Inghilterra"),
    "La Liga (ES)":        ("102829", "Spagna"),
    "Bundesliga (DE)":     ("102842", "Germania"),
    "Ligue 1 (FR)":        ("102843", "Francia"),
    "Champions League":    ("102855", "Europa"),
}

DRAW_NAMES = {"x", "pareggio", "parità", "parita", "draw"}
NAMES_1X2_FALLBACK = {"risultato della partita", "1x2", "1 x 2", "esito finale",
                      "risultato finale", "match result"}
ALLOWED_PARAM_KEYS = {"Happening", "MarketType", "Period"}


# ----------------------------------------------------------------------------- helpers
def get_val(obj: Any, key: str = "name") -> str:
    if not isinstance(obj, dict):
        return str(obj).strip() if obj is not None else ""
    v = obj.get(key)
    if isinstance(v, dict):
        return str(v.get("value", "")).strip()
    return str(v).strip() if v is not None else ""


def get_odds(opt: Dict) -> Optional[float]:
    if not isinstance(opt, dict):
        return None
    p = opt.get("price")
    if isinstance(p, (int, float)):
        return float(p)
    if isinstance(p, dict):
        v = p.get("odds", p.get("decimal"))
        if v is not None:
            return float(v)
    o = opt.get("odds")
    if isinstance(o, (int, float)):
        return float(o)
    return None


def _params(market: Dict) -> Dict[str, str]:
    return {p.get("key"): p.get("value") for p in market.get("parameters", []) if isinstance(p, dict)}


def find_1x2_market(markets: List[Dict]) -> Optional[Dict]:
    """Restituisce il vero mercato 1X2 (tempo regolamentare), mai il 2Up."""
    def usable(m):
        return (isinstance(m, dict) and m.get("status", "Visible") == "Visible"
                and len(m.get("options", m.get("results", []))) == 3)

    # 1) riconoscimento strutturale, indipendente da lingua e nome
    structural = []
    for m in markets:
        if not usable(m):
            continue
        p = _params(m)
        if (p.get("MarketType") == "3way"
                and p.get("Period") == "RegularTime"
                and p.get("Happening", "Goal") == "Goal"
                and set(p) <= ALLOWED_PARAM_KEYS):       # esclude 2Up, handicap, ecc.
            structural.append(m)
    if structural:
        return next((m for m in structural if m.get("isMain")), structural[0])

    # 2) fallback per nome esatto (se Bwin dovesse cambiare i parametri)
    for m in markets:
        if usable(m) and get_val(m).lower() in NAMES_1X2_FALLBACK:
            return m
    return None


def extract_betradar_id(fix: Dict) -> Optional[str]:
    addons = fix.get("addons") or {}
    br = addons.get("betRadar")
    if br is None:
        br = (addons.get("liveMatchTracker") or {}).get("sportradarId")
    return str(br).strip() if br not in (None, "") else None


def extract_teams(fix: Dict) -> Optional[Tuple[str, str]]:
    home = away = ""
    for p in fix.get("participants", []):
        t = (p.get("properties") or {}).get("type")
        if t == "HomeTeam":
            home = get_val(p)
        elif t == "AwayTeam":
            away = get_val(p)
    if home and away:
        return home, away
    name = get_val(fix)
    if " - " in name:
        h, a = name.split(" - ", 1)
        return h.strip(), a.strip()
    return None


def extract_1x2_odds(market: Dict, home: str, away: str) -> Optional[Dict[str, float]]:
    options = market.get("options", market.get("results", []))
    named: Dict[str, float] = {}
    for opt in options:
        if opt.get("status", "Visible") != "Visible":
            return None                                   # esito sospeso: quota inaffidabile
        odd = get_odds(opt)
        if odd is None or odd < 1.01:
            return None
        n = get_val(opt).lower()
        if n in ("1", home.lower()):
            named["1"] = odd
        elif n in DRAW_NAMES:
            named["X"] = odd
        elif n in ("2", away.lower()):
            named["2"] = odd

    if len(named) == 3:
        return named

    # ultima spiaggia: ordine posizionale, solo se i nomi non si sono risolti affatto
    if not named:
        vals = [get_odds(o) for o in options]
        if all(v is not None and v >= 1.01 for v in vals):
            return {"1": vals[0], "X": vals[1], "2": vals[2]}
    return None


# ----------------------------------------------------------------------------- client
class BwinDirectClient:
    def __init__(self):
        self.session = requests.Session(impersonate="chrome120")
        self.headers = {
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "it-IT,it;q=0.9",
            "Origin": "https://sports.bwin.it",
            "Referer": "https://sports.bwin.it/",
        }
        self._warned_region = set()

    def _get_json(self, params: Dict[str, str], label: str) -> Optional[Dict]:
        for attempt in range(1, RETRIES + 1):
            try:
                res = self.session.get(BASE_URL, params=params, headers=self.headers, timeout=15)
                if res.status_code == 200:
                    return res.json()
                if res.status_code in (401, 403):
                    print(f"[!] Bwin HTTP {res.status_code} per {label}: ACCESS_ID scaduto o IP bloccato?")
                    return None
                print(f"[!] Bwin HTTP {res.status_code} per {label} (tentativo {attempt}/{RETRIES})")
            except Exception as e:
                print(f"[!] Errore connessione {label} (tentativo {attempt}/{RETRIES}): {e}")
            time.sleep(1.5 * attempt)
        return None

    def fetch_league(self, league_name: str, comp_id: str, region: str) -> Optional[List[Dict[str, Any]]]:
        """None = download fallito (distinto da [] = nessuna partita)."""
        fixtures: List[Dict] = []
        skip = 0
        for _ in range(MAX_PAGES):
            params = {
                "x-bwin-accessid": ACCESS_ID,
                "lang": "it",
                "country": "IT",
                "userCountry": "IT",
                "sportIds": "4",
                "competitionIds": comp_id,
                "fixtureTypes": "Standard",
                "offerMapping": "Filtered",
                "skip": str(skip),
                "take": str(PAGE_SIZE),
            }
            data = self._get_json(params, league_name)
            if data is None:
                return None if not fixtures else self._parse_fixtures(fixtures, league_name, region)
            page = data.get("fixtures", [])
            fixtures.extend(page)
            skip += len(page)
            total = data.get("totalCount")
            if not page or len(page) < PAGE_SIZE or (isinstance(total, int) and skip >= total):
                break
        return self._parse_fixtures(fixtures, league_name, region)

    def fetch_all(self) -> Tuple[List[Dict[str, Any]], List[str]]:
        all_events: List[Dict[str, Any]] = []
        failed: List[str] = []
        for name, (c_id, region) in BWIN_LEAGUES.items():
            events = self.fetch_league(name, c_id, region)
            if events is None:
                failed.append(name)
                print(f"[!] Bwin API: download FALLITO per {name}")
                continue
            print(f"[+] Bwin API: estratti {len(events)} match per {name}")
            all_events.extend(events)
        return all_events, failed

    def _parse_fixtures(self, fixtures: List[Dict], league_name: str, region: str) -> List[Dict[str, Any]]:
        parsed, seen = [], set()
        for fix in fixtures:
            fid = fix.get("id")
            if fid in seen:
                continue
            seen.add(fid)

            # controllo anti-ID-sbagliato (es. Premier League israeliana al posto di quella inglese)
            fx_region = get_val(fix.get("region"))
            if region and fx_region and fx_region.lower() != region.lower():
                if league_name not in self._warned_region:
                    self._warned_region.add(league_name)
                    print(f"[!] ATTENZIONE {league_name}: regione '{fx_region}' != attesa '{region}'. "
                          f"Controlla l'ID competizione!")
                continue

            if not INCLUDE_LIVE and fix.get("stage") == "Live":
                continue
            if fix.get("isOpenForBetting") is False:
                continue

            teams = extract_teams(fix)
            if not teams:
                continue
            home, away = teams

            market = find_1x2_market(fix.get("optionMarkets", []))
            if not market:
                continue
            odds = extract_1x2_odds(market, home, away)
            if not odds:
                continue

            parsed.append({
                "bookmaker": "Bwin",
                "competition": league_name,
                "home_team": home,
                "away_team": away,
                "kickoff": fix.get("startDate", ""),
                "betradar_id": extract_betradar_id(fix),
                "bwin_id": str(fid) if fid is not None else None,
                "markets": {"1X2": odds},
            })
        return parsed


# ----------------------------------------------------------------------------- salvataggio
def merge_and_save(bookmaker_name: str, new_events: List[Dict[str, Any]], failed_competitions: List[str]):
    existing_events: List[Dict[str, Any]] = []
    if os.path.exists(OUTPUT_FILE):
        try:
            with open(OUTPUT_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            for ev in data.get("events", []):
                if ev.get("bookmaker") != bookmaker_name:
                    existing_events.append(ev)                       # altri bookmaker: intatti
                elif ev.get("competition") in failed_competitions:
                    existing_events.append(ev)                       # download fallito: tengo i vecchi dati
        except Exception as e:
            print(f"[!] File esistente illeggibile ({e}): lo ricreo da zero.")
            existing_events = []

    combined = existing_events + new_events
    payload = {
        "acquired_at": datetime.now(timezone.utc).isoformat(),
        "total_events": len(combined),
        "events": combined,
    }

    # scrittura atomica: un crash a meta' non corrompe il file
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(os.path.abspath(OUTPUT_FILE)), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, ensure_ascii=False)
        os.replace(tmp, OUTPUT_FILE)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)

    print("\n" + "=" * 65)
    print(f"[+] COMPLETATO: {len(new_events)} partite {bookmaker_name} salvate in: {OUTPUT_FILE}")
    print(f"[+] Totale complessivo eventi nel file: {len(combined)}")
    if failed_competitions:
        print(f"[!] Dati precedenti mantenuti per: {', '.join(failed_competitions)}")
    print("=" * 65)


def main():
    print("[*] Avvio scaricamento quote Bwin tramite API nativa...")
    client = BwinDirectClient()
    events, failed = client.fetch_all()

    if not events and not failed:
        print("[!] Nessun evento estratto.")
        return

    merge_and_save("Bwin", events, failed)

    print("\n[VERIFICA QUOTE ESTRATTE]:")
    for ev in events:
        if ev["competition"] in ("Serie A (IT)", "Serie B (IT)", "Premier League (UK)"):
            q = ev["markets"]["1X2"]
            print(f"  • [{ev['competition']}] {ev['home_team']} vs {ev['away_team']} "
                  f"(Betradar: {ev.get('betradar_id') or 'N/A'}) -> "
                  f"1: {q['1']:.2f} | X: {q['X']:.2f} | 2: {q['2']:.2f}")


if __name__ == "__main__":
    main()