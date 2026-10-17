#!/usr/bin/env python3
"""
Client API Diretto per Snai IT (Piattaforma Flutter SeaTech).
Nessun browser richiesto: scarica partite e quote 1X2 via REST in millisecondi
e aggiorna 'bookmakers_data.json'.
"""

import os
import json
from datetime import datetime, timezone
from typing import Dict, List, Any
from curl_cffi import requests

OUTPUT_FILE = "bookmakers_data.json"
BASE_API = "https://betting-snai.flutterseatech.it/api/lettura-palinsesto-sport/palinsesto/prematch"

# Codici manifestazione base (1-209 = Serie A verificato dal dump)
DEFAULT_SNAI_LEAGUES = {
    "Serie A (IT)": "1-209"
}


class SnaiDirectClient:
    def __init__(self):
        self.session = requests.Session(impersonate="chrome120")
        self.headers = {
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "it-IT,it;q=0.9",
            "Origin": "https://www.snai.it",
            "Referer": "https://www.snai.it/"
        }

    def discover_leagues(self) -> Dict[str, str]:
        """
        Cerca dinamicamente i codici '1-XXX' per i principali campionati
        dal file locale di dump o dall'endpoint alberaturaPrematch.
        """
        leagues = dict(DEFAULT_SNAI_LEAGUES)
        tree_data = None

        # 1. Prova a leggere dal dump locale se già scaricato
        local_tree = "debug_bookies_raw/snai/17_t_palinsesto_prematch_alberaturaPrematch.json"
        if os.path.exists(local_tree):
            try:
                with open(local_tree, "r", encoding="utf-8") as f:
                    tree_data = json.load(f).get("payload", {})
            except Exception:
                pass

        # 2. Altrimenti richiede l'alberatura via API
        if not tree_data:
            try:
                url = f"{BASE_API}/alberaturaPrematch?offerId=0"
                res = self.session.get(url, headers=self.headers, timeout=10)
                if res.status_code == 200:
                    tree_data = res.json()
            except Exception:
                pass

        if not tree_data:
            return leagues

        # Mappa di ricerca per i campionati target (Calcio = disciplina 1)
        targets = {
            "Serie A (IT)": ("italia", "serie a"),
            "Serie B (IT)": ("italia", "serie b"),
            "Premier League (UK)": ("inghilterra", "premier league"),
            "La Liga (ES)": ("spagna", "liga"),
            "Bundesliga (DE)": ("germania", "bundesliga"),
            "Ligue 1 (FR)": ("francia", "ligue 1"),
            "Champions League": ("europa", "champions league"),
            "Europa League": ("europa", "europa league")
        }

        # Esplora ricorsivamente l'alberatura di Snai
        def walk_tree(node, parent_desc=""):
            if isinstance(node, dict):
                desc = str(node.get("descrizione", node.get("description", ""))).lower().strip()
                key = str(node.get("key", ""))
                cod_man = node.get("codiceManifestazione")
                cod_disc = node.get("codiceDisciplina")

                # Verifica se è un nodo campionato di Calcio (1-XXX)
                man_key = ""
                if key.startswith("1-") and key.count("-") == 1:
                    man_key = key
                elif cod_disc == 1 and cod_man:
                    man_key = f"1-{cod_man}"

                if man_key and desc:
                    for label, (country_kw, league_kw) in targets.items():
                        if label in leagues:
                            continue
                        if desc == league_kw or (league_kw in desc and "2" not in desc and "femm" not in desc):
                            if not parent_desc or country_kw in parent_desc or "uefa" in desc or "champions" in desc:
                                leagues[label] = man_key

                next_parent = desc if desc else parent_desc
                for v in node.values():
                    if isinstance(v, (dict, list)):
                        walk_tree(v, next_parent)

            elif isinstance(node, list):
                for item in node:
                    walk_tree(item, parent_desc)

        walk_tree(tree_data)
        return leagues

    def fetch_league(self, league_name: str, man_key: str) -> List[Dict[str, Any]]:
        url = f"{BASE_API}/v1/schedaManifestazione/0/{man_key}"
        params = {
            "offerId": "0",
            "metaTplEnabled": "true",
            "deep": "true"
        }

        try:
            res = self.session.get(url, params=params, headers=self.headers, timeout=10)
            if res.status_code != 200:
                print(f"[!] Snai HTTP {res.status_code} su {league_name} ({man_key})")
                return []
            return self._parse_payload(res.json(), league_name)
        except Exception as e:
            print(f"[!] Errore connessione Snai su {league_name}: {e}")
            return []

    def _parse_payload(self, data: Dict[str, Any], league_name: str) -> List[Dict[str, Any]]:
        events = []
        avv_list = data.get("avvenimentoFeList", [])
        info_map = data.get("infoAggiuntivaMap", {})

        if not isinstance(avv_list, list) or not isinstance(info_map, dict):
            return []

        for avv in avv_list:
            if not isinstance(avv, dict):
                continue
            if avv.get("live") is True or avv.get("eventType") != "MATCH":
                continue

            avv_key = avv.get("key")
            if not avv_key:
                continue

            # Estrazione nomi squadre da firstCompetitor / secondCompetitor o descrizione
            fc = avv.get("firstCompetitor") or {}
            sc = avv.get("secondCompetitor") or {}
            home = str(fc.get("description", "")).strip()
            away = str(sc.get("description", "")).strip()

            if not home or not away:
                desc = str(avv.get("descrizione", "")).strip()
                if " - " in desc:
                    home, away = [s.strip() for s in desc.split(" - ", 1)]

            if not home or not away:
                continue

            kickoff = str(avv.get("data", ""))

            # Chiave deterministica per il mercato Esito Finale 1X2 (codiceScommessa = 3, info = 0)
            mkt_key = f"{avv_key}-3-0"
            mkt_obj = info_map.get(mkt_key)

            # Fallback: cerca qualsiasi chiave che inizi con '{avv_key}-3-'
            if not mkt_obj:
                fallback_k = next((k for k in info_map if k.startswith(f"{avv_key}-3-")), None)
                if fallback_k:
                    mkt_obj = info_map[fallback_k]

            if not isinstance(mkt_obj, dict):
                continue

            odds_1x2 = {}
            for esito in mkt_obj.get("esitoList", []):
                if not isinstance(esito, dict):
                    continue
                segno = str(esito.get("descrizione", "")).strip().upper()
                quota_raw = esito.get("quota")
                if quota_raw is not None:
                    try:
                        # Snai invia 325 per indicare 3.25
                        q_val = float(quota_raw) / 100.0
                        if q_val > 1.0 and segno in ["1", "X", "2"]:
                            odds_1x2[segno] = q_val
                    except ValueError:
                        pass

            if len(odds_1x2) == 3:
                events.append({
                    "bookmaker": "Snai",
                    "competition": league_name,
                    "home_team": home,
                    "away_team": away,
                    "kickoff": kickoff,
                    "markets": {"1X2": odds_1x2}
                })

        return events

    def fetch_all(self) -> List[Dict[str, Any]]:
        leagues = self.discover_leagues()
        print(f"[*] Campionati Snai configurati: {leagues}")
        all_events = []
        for name, m_key in leagues.items():
            evs = self.fetch_league(name, m_key)
            print(f"[+] Snai API: estratti {len(evs)} match per {name}")
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
    print("[*] Avvio scaricamento quote Snai tramite API nativa...")
    client = SnaiDirectClient()
    extracted = client.fetch_all()
    if extracted:
        merge_and_save("Snai", extracted)
        s = extracted[0]
        print(f"\nEsempio Snai: {s['home_team']} vs {s['away_team']} -> {s['markets']['1X2']}")
    else:
        print("[!] Nessun evento estratto da Snai.")