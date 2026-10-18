#!/usr/bin/env python3
"""
Client API diretto per William Hill IT (XSportDatastore) - quote 1X2 + Betradar ID.

Cosa non funzionava (verificato sul HAR):
- L'endpoint getEventiPrematchDettaglioTorneo NON esiste -> 404. Il sito usa
  getTorneoCentrale (con idAggregata = campo "ba" del torneo nel menu, di norma 319).
- Gli ID in DEFAULT_WH_LEAGUES erano sbagliati (tranne Serie A). Quelli veri vengono letti
  dal menu getMenuPrematch (categoria -> tornei) e, se il menu non risponde, da un fallback.
- La discovery cercava le chiavi "ic"/"it" (nel menu "ic" e' un'icona e il torneo ha "id"/"idc")
  e leggeva un file di debug locale: non poteva mai funzionare.
- Betradar ID: e' il campo "bid" dell'evento (coincide al 100% con addons.betRadar di Bwin
  sulle partite in comune); "bri"/"idEventoSportradar" non esistono.
- "ts" e' ora italiana ("20261019 18:30:00"): ora viene convertito in UTC ISO, come Bwin.
- Serie B: nel menu di William Hill IT non e' presente. Viene segnalato, non interrogato a vuoto.
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
DEFAULT_AGGREGATE = 319        # idAggregata usato dal sito per i tornei con quote complete
RETRIES = 3

# Etichetta -> slug (ultimo pezzo del percorso SEO nel menu, ammessi piu' alias)
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

# Fallback se il menu non e' raggiungibile (ID letti dal menu reale nel HAR).
# Serie B volutamente assente: non e' offerta.
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
    """'20261019 18:30:00' (ora italiana) -> '2026-10-19T16:30:00Z'. Senza dipendenze da tzdata."""
    try:
        local = datetime.strptime(ts.strip(), "%Y%m%d %H:%M:%S")
    except Exception:
        return ts
    dst_start = _last_sunday(local.year, 3).replace(hour=2)    # 02:00 locali
    dst_end = _last_sunday(local.year, 10).replace(hour=3)     # 03:00 locali
    offset = 2 if dst_start <= local < dst_end else 1
    return (local - timedelta(hours=offset)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _seo_paths(node: Dict) -> List[str]:
    """Estrae tutti i percorsi SEO di un nodo del menu (il campo e' una stringa JSON)."""
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

    # -- discovery ------------------------------------------------------------
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

        # indice slug -> torneo
        by_slug: Dict[str, Dict[str, int]] = {}
        for cat in football.get("cts", []):
            for t in cat.get("tns", []):
                entry = {
                    "categoryId": int(t.get("idc", cat.get("id"))),
                    "tournamentId": int(t["id"]),
                    "aggregate": int(t.get("ba") or DEFAULT_AGGREGATE),
                }
                for p in _seo_paths(t):
                    by_slug[p.split("it-it/")[-1]] = entry     # normalizza '/betting/it-it/calcio/...'

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

    # -- download -------------------------------------------------------------
    def fetch_league(self, league_name: str, cfg: Dict[str, int]) -> Optional[List[Dict[str, Any]]]:
        """None = download fallito (distinto da [] = nessuna partita)."""
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

    # -- parsing --------------------------------------------------------------
    def _parse_payload(self, data: Dict[str, Any], league_name: str) -> List[Dict[str, Any]]:
        events, seen = [], set()
        avs = data.get("avs", []) if isinstance(data, dict) else []
        if not isinstance(avs, list):
            return []

        for av in avs:
            if not isinstance(av, dict):
                continue
            if av.get("lv") is True or av.get("ant") is True:     # live / antepost
                continue

            dsl = av.get("dsl") or {}
            title = str(dsl.get("IT") or dsl.get("ORIGINAL_FROM_DB") or "").strip() if isinstance(dsl, dict) else ""
            if " - " not in title:
                continue
            home, away = [s.strip() for s in title.split(" - ", 1)]

            odds = self._extract_1x2(av)
            if not odds:
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
                "markets": {"1X2": odds},
            })
        return events

    @staticmethod
    def _extract_1x2(av: Dict[str, Any]) -> Optional[Dict[str, float]]:
        """Mercato 1X2 tempo regolamentare: cs == 3, senza handicap (h == 0), quote in centesimi."""
        for sc in av.get("scs", []):
            if not isinstance(sc, dict) or sc.get("cs") != 3 or sc.get("h", 0) != 0:
                continue
            eqs = sc.get("eqs", [])
            if len(eqs) != 3:
                continue
            odds: Dict[str, float] = {}
            for eq in eqs:
                q = eq.get("q", 0) if isinstance(eq, dict) else 0
                if not isinstance(q, (int, float)) or q <= 100:   # 0 / 1.00 = sospesa
                    return None
                key = {1: "1", 2: "X", 3: "2"}.get(eq.get("ce"))
                if key:
                    odds[key] = q / 100.0
            if len(odds) == 3:
                return odds
        return None


# ----------------------------------------------------------------------------- salvataggio
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
                    existing_events.append(ev)          # download fallito: tengo i dati precedenti
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
            print(f"\nEsempio William Hill: {s['home_team']} vs {s['away_team']} "
                  f"(Betradar: {s.get('betradar_id') or 'N/A'}, {s['kickoff']}) -> {s['markets']['1X2']}")
    else:
        print("[!] Nessun evento estratto da William Hill.")