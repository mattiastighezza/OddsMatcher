#!/usr/bin/env python3
"""
Client API Diretto per William Hill IT (XSportDatastore).
Nessun browser richiesto: interroga l'endpoint JSON getTorneoCentrale
e aggiorna 'bookmakers_data.json'.
"""

import os
import json
from datetime import datetime, timezone
from typing import Dict, List, Any
from curl_cffi import requests

OUTPUT_FILE = "bookmakers_data.json"
BASE_URL = "https://sports.williamhill.it/XSportDatastore"

# Parametri verificati dal dump per la Serie A (sportId=1, categoryId=31, tournamentId=33, idAggregata=319)
DEFAULT_WH_LEAGUES = {
    "Serie A (IT)": {"categoryId": 31, "tournamentId": 33, "idAggregata": 319}
}


class WilliamHillDirectClient:
    def __init__(self):
        self.session = requests.Session(impersonate="chrome120")
        self.headers = {
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "it-IT,it;q=0.9",
            "Referer": "https://sports.williamhill.it/"
        }

    def discover_leagues(self) -> Dict[str, Dict[str, int]]:
        """
        Estrae dinamicamente categoryId, tournamentId e idAggregata
        dal dump locale 08_getMenuPrematch.json o dall'endpoint live.
        """
        leagues = dict(DEFAULT_WH_LEAGUES)
        menu_data = None

        local_menu = "debug_bookies_raw/williamhill/08_XSportDatastore_getMenuPrematch.json"
        if os.path.exists(local_menu):
            try:
                with open(local_menu, "r", encoding="utf-8") as f:
                    menu_data = json.load(f).get("payload", {})
            except Exception:
                pass

        if not menu_data:
            try:
                url = f"{BASE_URL}/getMenuPrematch?systemCode=WILLIAMHILL&lingua=IT&hash="
                res = self.session.get(url, headers=self.headers, timeout=10)
                if res.status_code == 200:
                    menu_data = res.json()
            except Exception:
                pass

        if not menu_data:
            return leagues

        # Ricerca ricorsiva dei tornei nel menu di William Hill
        def walk_menu(node, cat_id=None, cat_name=""):
            if isinstance(node, dict):
                # Identifica se è una categoria/nazione o un torneo
                c_id = node.get("ic", node.get("categoryId", cat_id))
                t_id = node.get("it", node.get("tournamentId"))
                agg_id = node.get("oa", node.get("idAggregata", 319))

                dsl = node.get("dsl", {})
                name = str(dsl.get("IT", node.get("d", node.get("name", "")))).lower().strip() if isinstance(dsl, dict) else ""
                seo = str(node.get("seo", "")).lower()

                if c_id and t_id and (name or seo):
                    if ("italia/serie-b" in seo or (name == "serie b" and "ital" in cat_name)) and "Serie B (IT)" not in leagues:
                        leagues["Serie B (IT)"] = {"categoryId": int(c_id), "tournamentId": int(t_id), "idAggregata": int(agg_id or 319)}
                    elif ("premier-league" in seo or (name == "premier league" and "inghil" in cat_name)) and "Premier League (UK)" not in leagues:
                        leagues["Premier League (UK)"] = {"categoryId": int(c_id), "tournamentId": int(t_id), "idAggregata": int(agg_id or 319)}
                    elif ("spagna/la-liga" in seo or "spagna/liga" in seo or (name in ["la liga", "liga"] and "spagn" in cat_name)) and "La Liga (ES)" not in leagues:
                        leagues["La Liga (ES)"] = {"categoryId": int(c_id), "tournamentId": int(t_id), "idAggregata": int(agg_id or 319)}
                    elif ("germania/bundesliga" in seo or (name == "bundesliga" and "german" in cat_name)) and "Bundesliga (DE)" not in leagues:
                        leagues["Bundesliga (DE)"] = {"categoryId": int(c_id), "tournamentId": int(t_id), "idAggregata": int(agg_id or 319)}
                    elif ("francia/ligue-1" in seo or (name == "ligue 1" and "franc" in cat_name)) and "Ligue 1 (FR)" not in leagues:
                        leagues["Ligue 1 (FR)"] = {"categoryId": int(c_id), "tournamentId": int(t_id), "idAggregata": int(agg_id or 319)}
                    elif ("champions-league" in seo or "champions league" in name) and "Champions League" not in leagues:
                        leagues["Champions League"] = {"categoryId": int(c_id), "tournamentId": int(t_id), "idAggregata": int(agg_id or 319)}
                    elif ("europa-league" in seo or "europa league" in name) and "Europa League" not in leagues:
                        leagues["Europa League"] = {"categoryId": int(c_id), "tournamentId": int(t_id), "idAggregata": int(agg_id or 319)}

                next_cat_name = name if (c_id and not t_id and name) else cat_name
                for v in node.values():
                    if isinstance(v, (dict, list)):
                        walk_menu(v, c_id, next_cat_name)

            elif isinstance(node, list):
                for item in node:
                    walk_menu(item, cat_id, cat_name)

        walk_menu(menu_data)
        return leagues

    def fetch_league(self, league_name: str, cfg: Dict[str, int]) -> List[Dict[str, Any]]:
        url = f"{BASE_URL}/getTorneoCentrale"
        params = {
            "systemCode": "WILLIAMHILL",
            "lingua": "IT",
            "hash": "",
            "sportId": "1",
            "categoryId": str(cfg["categoryId"]),
            "tournamentId": str(cfg["tournamentId"]),
            "idAggregata": str(cfg.get("idAggregata", 319))
        }

        try:
            res = self.session.get(url, params=params, headers=self.headers, timeout=10)
            if res.status_code != 200:
                print(f"[!] William Hill HTTP {res.status_code} su {league_name}")
                return []
            return self._parse_payload(res.json(), league_name)
        except Exception as e:
            print(f"[!] Errore William Hill su {league_name}: {e}")
            return []

    def _parse_payload(self, data: Dict[str, Any], league_name: str) -> List[Dict[str, Any]]:
        events = []
        avs = data.get("avs", [])
        if not isinstance(avs, list):
            return []

        for av in avs:
            if not isinstance(av, dict):
                continue
            # Esclude eventi live (lv=True) e antepost (ant=True)
            if av.get("lv") is True or av.get("ant") is True:
                continue

            dsl = av.get("dsl", {})
            match_title = str(dsl.get("IT", dsl.get("ORIGINAL_FROM_DB", ""))).strip() if isinstance(dsl, dict) else ""
            if " - " not in match_title:
                continue

            home, away = [s.strip() for s in match_title.split(" - ", 1)]
            kickoff = str(av.get("ts", ""))

            odds_1x2 = {}
            for sc in av.get("scs", []):
                if not isinstance(sc, dict):
                    continue
                # Mercato 1X2 principale: cs == 3 e h == 0 (nessun handicap)
                if sc.get("cs") == 3 and sc.get("h", 0) == 0:
                    for eq in sc.get("eqs", []):
                        if not isinstance(eq, dict):
                            continue
                        ce = eq.get("ce")
                        q_raw = eq.get("q", 0)
                        if q_raw and q_raw > 100:
                            q_val = float(q_raw) / 100.0
                            if ce == 1:
                                odds_1x2["1"] = q_val
                            elif ce == 2:
                                odds_1x2["X"] = q_val
                            elif ce == 3:
                                odds_1x2["2"] = q_val
                    if len(odds_1x2) == 3:
                        break

            if len(odds_1x2) == 3:
                events.append({
                    "bookmaker": "William Hill",
                    "competition": league_name,
                    "home_team": home,
                    "away_team": away,
                    "kickoff": kickoff,
                    "markets": {"1X2": odds_1x2}
                })

        return events

    def fetch_all(self) -> List[Dict[str, Any]]:
        leagues = self.discover_leagues()
        print(f"[*] Campionati William Hill configurati: {list(leagues.keys())}")
        all_events = []
        for name, cfg in leagues.items():
            evs = self.fetch_league(name, cfg)
            print(f"[+] William Hill API: estratti {len(evs)} match per {name}")
            all_events.extend(evs)
        return all_events


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
    print(f"[+] SALVATO: {len(new_events)} match di {bookmaker_name} aggiunti a {OUTPUT_FILE}")
    print(f"[+] Totale complessivo eventi nel file: {len(combined)}")
    print("=" * 65)


if __name__ == "__main__":
    print("[*] Avvio scaricamento quote William Hill tramite API nativa...")
    client = WilliamHillDirectClient()
    extracted = client.fetch_all()
    if extracted:
        merge_and_save("William Hill", extracted)
        s = extracted[0]
        print(f"\nEsempio William Hill: {s['home_team']} vs {s['away_team']} -> {s['markets']['1X2']}")
    else:
        print("[!] Nessun evento estratto da William Hill.")