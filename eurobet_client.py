#!/usr/bin/env python3
"""
Client per Eurobet IT.
Usa i nuovi URL Next.js puliti (senza '#!/') per intercettare i JSON
del microservizio 'sport-schedule/services' ed estrarre le quote 1X2.
"""

import os
import json
from datetime import datetime, timezone
from typing import Dict, List, Any, Optional
from playwright.sync_api import sync_playwright, Response

OUTPUT_FILE = "bookmakers_data.json"

# Nuovi URL ufficiali Next.js di Eurobet (senza '#!/')
EUROBET_LEAGUES = {
    "Serie A (IT)": "https://www.eurobet.it/it/scommesse/calcio/it-serie-a",
    "Serie B (IT)": "https://www.eurobet.it/it/scommesse/calcio/ita-serie-b",
    "Premier League (UK)": "https://www.eurobet.it/it/scommesse/calcio/ing-premier-league",
    "La Liga (ES)": "https://www.eurobet.it/it/scommesse/calcio/es-liga",
    "Bundesliga (DE)": "https://www.eurobet.it/it/scommesse/calcio/de-bundesliga",
    "Ligue 1 (FR)": "https://www.eurobet.it/it/scommesse/calcio/fr-ligue-1",
    "Champions League": "https://www.eurobet.it/it/scommesse/calcio/eu-champions-league",
    "Europa League": "https://www.eurobet.it/it/scommesse/calcio/eu-europa-league"
}


def parse_odd(val: Any) -> Optional[float]:
    if val is None:
        return None
    try:
        f = float(str(val).replace(",", "."))
        if f > 50:
            f = f / 100.0
        return f if f > 1.0 else None
    except ValueError:
        return None


class EurobetClient:
    def __init__(self, headless: bool = False):
        self.headless = headless
        self.extracted_events: Dict[str, Dict[str, Any]] = {}
        self.current_league: str = "Calcio"

    def _handle_response(self, response: Response):
        if response.status not in (200, 206):
            return
        url = response.url.lower()
        if "sport-schedule/services" not in url and "betting-service" not in url:
            return

        try:
            data = response.json()
            self._walk_and_extract(data)
        except Exception:
            pass

    def _walk_and_extract(self, node: Any):
        if isinstance(node, dict):
            # Struttura vista in schema_finale.json: {'eventInfo': {...}, 'betGroupList': [...]}
            if "eventInfo" in node and isinstance(node["eventInfo"], dict):
                ev_info = node["eventInfo"]
                desc = str(ev_info.get("eventDescription", "")).strip()
                if " - " in desc and not ev_info.get("live", False):
                    home, away = [s.strip() for s in desc.split(" - ", 1)]
                    odds_1x2 = self._find_1x2_in_event(node)
                    if len(odds_1x2) == 3:
                        key = f"{home}_{away}"
                        self.extracted_events[key] = {
                            "bookmaker": "Eurobet",
                            "competition": self.current_league,
                            "home_team": home,
                            "away_team": away,
                            "kickoff": str(ev_info.get("eventData", "")),
                            "markets": {"1X2": odds_1x2}
                        }
                return

            for v in node.values():
                if isinstance(v, (dict, list)):
                    self._walk_and_extract(v)

        elif isinstance(node, list):
            for item in node:
                if isinstance(item, (dict, list)):
                    self._walk_and_extract(item)

    def _find_1x2_in_event(self, ev_dict: Dict[str, Any]) -> Dict[str, float]:
        """Cerca il primo gruppo di quote a 3 esiti (1, X, 2) dentro l'oggetto evento."""
        odds = {}

        def scan_markets(obj):
            if isinstance(obj, list):
                # Verifica se è direttamente una lista di 3 esiti (1, X, 2)
                if len(obj) == 3 and all(isinstance(x, dict) for x in obj):
                    temp = {}
                    for idx, item in enumerate(obj):
                        q_raw = item.get("oddValue", item.get("odd", item.get("quota", item.get("value"))))
                        q_val = parse_odd(q_raw)
                        if q_val:
                            if idx == 0:
                                temp["1"] = q_val
                            elif idx == 1:
                                temp["X"] = q_val
                            elif idx == 2:
                                temp["2"] = q_val
                    if len(temp) == 3:
                        return temp

                for el in obj:
                    res = scan_markets(el)
                    if res:
                        return res

            elif isinstance(obj, dict):
                for k, v in obj.items():
                    if k == "eventInfo":
                        continue
                    res = scan_markets(v)
                    if res:
                        return res
            return None

        found = scan_markets(ev_dict)
        return found if found else odds

    def fetch_all(self) -> List[Dict[str, Any]]:
        print("[*] Avvio acquisizione Eurobet su URL Next.js diretti...")
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=self.headless,
                args=["--disable-blink-features=AutomationControlled", "--no-sandbox"]
            )
            context = browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                viewport={"width": 1400, "height": 900},
                locale="it-IT"
            )
            context.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")

            page = context.new_page()
            page.on("response", self._handle_response)

            cookie_done = False
            for league_name, url in EUROBET_LEAGUES.items():
                self.current_league = league_name
                before = len(self.extracted_events)
                print(f"[*] [Eurobet] Navigazione: {league_name} -> {url}")

                try:
                    page.goto(url, wait_until="domcontentloaded", timeout=20000)
                    if not cookie_done:
                        page.wait_for_timeout(1500)
                        try:
                            btn = page.locator("#onetrust-accept-btn-handler")
                            if btn.count() > 0 and btn.first.is_visible(timeout=1000):
                                btn.first.click()
                                cookie_done = True
                        except Exception:
                            pass

                    page.wait_for_timeout(3500)
                    diff = len(self.extracted_events) - before
                    print(f"    [+] Eurobet: estratti {diff} match per {league_name}")
                except Exception as e:
                    print(f"    [!] Errore su {league_name}: {e}")

            browser.close()

        return list(self.extracted_events.values())


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
    client = EurobetClient(headless=False)
    extracted = client.fetch_all()
    if extracted:
        merge_and_save("Eurobet", extracted)
    else:
        print("[!] Nessun evento estratto da Eurobet.")