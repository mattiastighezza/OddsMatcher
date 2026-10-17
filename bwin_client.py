#!/usr/bin/env python3
"""
Client API Diretto per Bwin IT.
Ripristinato con il tuo codice originale funzionante (URL, ID Campionati e parametri),
con l'aggiunta dell'estrazione del Betradar ID e il salvataggio in append.
"""

import os
import json
from datetime import datetime, timezone
from typing import Dict, List, Any, Optional
from curl_cffi import requests

OUTPUT_FILE = "bookmakers_data.json"
BASE_URL = "https://www.bwin.it/cds-api/bettingoffer/fixtures"
ACCESS_ID = "NzIyNGU2ZTctMzhkYi00NDM2LThlZmMtMjExNzg4ZDE1YjVh"

BWIN_LEAGUES = {
    "Serie A (IT)": "102846",
    "Serie B (IT)": "102848",
    "Premier League (UK)": "101409",
    "La Liga (ES)": "102829",
    "Bundesliga (DE)": "102842",
    "Ligue 1 (FR)": "102843",
    "Champions League": "102855"
}


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


class BwinDirectClient:
    def __init__(self):
        self.session = requests.Session(impersonate="chrome120")
        self.headers = {
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "it-IT,it;q=0.9",
            "Origin": "https://sports.bwin.it",
            "Referer": "https://sports.bwin.it/"
        }

    def fetch_league(self, league_name: str, comp_id: str) -> List[Dict[str, Any]]:
        params = {
            "x-bwin-accessid": ACCESS_ID,
            "lang": "it",
            "country": "IT",
            "userCountry": "IT",
            "sportIds": "4",
            "competitionIds": comp_id,
            "fixtureTypes": "Standard",
            "offerMapping": "Filtered",
            "take": "50"
        }

        try:
            res = self.session.get(BASE_URL, params=params, headers=self.headers, timeout=10)
            if res.status_code != 200:
                print(f"[!] Bwin HTTP {res.status_code} per {league_name}")
                return []
            return self._parse_fixtures(res.json().get("fixtures", []), league_name)
        except Exception as e:
            print(f"[!] Errore connessione {league_name}: {e}")
            return []

    def fetch_all(self) -> List[Dict[str, Any]]:
        all_events = []
        for name, c_id in BWIN_LEAGUES.items():
            events = self.fetch_league(name, c_id)
            print(f"[+] Bwin API: estratti {len(events)} match per {name}")
            all_events.extend(events)
        return all_events

    def _parse_fixtures(self, fixtures: List[Dict], league_name: str) -> List[Dict[str, Any]]:
        parsed = []
        for fix in fixtures:
            name_val = get_val(fix)
            opt_mkts = fix.get("optionMarkets", [])

            if " - " not in name_val or len(opt_mkts) < 15:
                continue

            home, away = [s.strip() for s in name_val.split(" - ", 1)]
            start_str = fix.get("startDate", "")
            odds_1x2 = {}

            # --- ESTRAZIONE BETRADAR ID ---
            betradar_id = None
            cut_id = str(fix.get("id", ""))
            if ":" in cut_id:
                cand = cut_id.split(":")[-1].strip()
                if cand.isdigit() and len(cand) >= 7:
                    betradar_id = cand

            if not betradar_id:
                ext_refs = fix.get("externalReferences", [])
                if isinstance(ext_refs, list):
                    for ref in ext_refs:
                        if isinstance(ref, dict) and str(ref.get("source", "")).lower() in ["betradar", "sportradar"]:
                            betradar_id = str(ref.get("id", "")).strip()
                            break

            # Priorità 1: cerca il mercato principale contrassegnato da isMain == True
            main_markets = [m for m in opt_mkts if isinstance(m, dict) and m.get("isMain") is True]
            candidate_markets = main_markets if main_markets else opt_mkts

            for om in candidate_markets:
                om_name = get_val(om).lower()

                # Scarta sempre mercati parziali o con handicap
                if any(bad in om_name for bad in ["handicap", "1°", "2°", "primo", "secondo", "doppia", "over", "under", "gol", "combo"]):
                    continue

                if om.get("isMain") is True or any(k in om_name for k in ["1 x 2", "1x2", "esito finale", "tempo regolamentare"]):
                    options = om.get("options", om.get("results", []))
                    if len(options) != 3:
                        continue

                    temp_odds = {}
                    for idx, opt in enumerate(options):
                        opt_name = get_val(opt).lower()
                        odd_val = get_odds(opt)
                        if odd_val is None:
                            continue

                        if opt_name in ["1", home.lower()]:
                            temp_odds["1"] = odd_val
                        elif opt_name in ["x", "parità", "pareggio", "draw"]:
                            temp_odds["X"] = odd_val
                        elif opt_name in ["2", away.lower()]:
                            temp_odds["2"] = odd_val
                        elif idx == 0:
                            temp_odds["1"] = odd_val
                        elif idx == 1:
                            temp_odds["X"] = odd_val
                        elif idx == 2:
                            temp_odds["2"] = odd_val

                    if len(temp_odds) == 3:
                        odds_1x2 = temp_odds
                        # BREAK FONDAMENTALE: impedisce ai mercati successivi di sovrascrivere le quote!
                        break

            if len(odds_1x2) == 3:
                parsed.append({
                    "bookmaker": "Bwin",
                    "competition": league_name,
                    "home_team": home,
                    "away_team": away,
                    "kickoff": start_str,
                    "betradar_id": betradar_id,  # <-- Aggiunto al dizionario
                    "markets": {"1X2": odds_1x2}
                })

        return parsed


def merge_and_save(bookmaker_name: str, new_events: List[Dict[str, Any]]):
    existing_events = []
    if os.path.exists(OUTPUT_FILE):
        try:
            with open(OUTPUT_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                existing_events = [
                    ev for ev in data.get("events", [])
                    if ev.get("bookmaker") != bookmaker_name
                ]
        except Exception:
            existing_events = []

    combined = existing_events + new_events
    payload = {
        "acquired_at": datetime.now(timezone.utc).isoformat(),
        "total_events": len(combined),
        "events": combined
    }

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)

    print("\n" + "=" * 65)
    print(f"[+] COMPLETATO: {len(new_events)} partite {bookmaker_name} salvate in: {OUTPUT_FILE}")
    print(f"[+] Totale complessivo eventi nel file: {len(combined)}")
    print("=" * 65)


def main():
    print("[*] Avvio scaricamento quote Bwin tramite API nativa...")
    client = BwinDirectClient()
    events = client.fetch_all()

    if not events:
        print("[!] Nessun evento estratto.")
        return

    merge_and_save("Bwin", events)

    # Stampa di controllo sulle prime partite di Serie A e Serie B
    print("\n[VERIFICA QUOTE ESTRATTE]:")
    for ev in events:
        if ev["competition"] in ["Serie A (IT)", "Serie B (IT)"]:
            q = ev["markets"]["1X2"]
            br_id = ev.get("betradar_id", "N/A")
            print(f"  • [{ev['competition']}] {ev['home_team']} vs {ev['away_team']} (ID: {br_id}) -> 1: {q['1']:.2f} | X: {q['X']:.2f} | 2: {q['2']:.2f}")


if __name__ == "__main__":
    main()