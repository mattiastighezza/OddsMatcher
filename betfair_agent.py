#!/usr/bin/env python3
"""
Agente Browser Playwright Multi-Campionato per Betfair Exchange IT.
URL aggiornati e gestione automatica degli errori 404.
"""

import json
import argparse
from datetime import datetime, timezone
from typing import Dict, Any
from playwright.sync_api import sync_playwright, Response

OUTPUT_FILE = "betfair_exchange_data.json"

# ID ufficiali aggiornati per Betfair Exchange IT
MAJOR_LEAGUES = {
    "Serie A (IT)": "https://www.betfair.it/exchange/plus/football/competition/81",
    "Serie B (IT)": "https://www.betfair.it/exchange/plus/it/calcio/italia-serie-b-scommesse-12199689",
	"Serie C (IT)": "https://www.betfair.it/exchange/football/competition/12199513",
    "Premier League (UK)": "https://www.betfair.it/exchange/plus/football/competition/10932509",
    "La Liga (ES)": "https://www.betfair.it/exchange/plus/football/competition/117",
    "Bundesliga (DE)": "https://www.betfair.it/exchange/plus/football/competition/59",
    "Ligue 1 (FR)": "https://www.betfair.it/exchange/plus/football/competition/55",
    "Champions League": "https://www.betfair.it/exchange/plus/football/competition/228",
    "Europa League": "https://www.betfair.it/exchange/plus/football/competition/2005",
    "Conference League": "https://www.betfair.it/exchange/plus/it/calcio/uefa-conference-league-scommesse-12375833",
	"Primeira Liga (POR)": "https://www.betfair.it/exchange/football/competition/99"
}

class BetfairMultiLeagueAgent:
    def __init__(self, headless: bool = False, wait_per_league: int = 3):
        self.headless = headless
        self.wait_per_league = wait_per_league
        self.captured_markets: Dict[str, Any] = {}
        self.current_league: str = "Calcio"

    def handle_response(self, response: Response):
        if "bymarket" in response.url and response.status == 200:
            try:
                data = response.json()
                if "eventTypes" in data:
                    self._extract_markets(data, self.current_league)
            except Exception:
                pass

    def _extract_markets(self, payload: Dict[str, Any], league_name: str):
        new_count = 0
        for event_type in payload.get("eventTypes", []):
            for event_node in event_type.get("eventNodes", []):
                for market_node in event_node.get("marketNodes", []):
                    m_id = market_node.get("marketId")
                    if m_id and m_id not in self.captured_markets:
                        self.captured_markets[m_id] = {
                            "competition": league_name,
                            "event_node": event_node,
                            "market_node": market_node
                        }
                        new_count += 1
        if new_count > 0:
            print(f"    [+] {league_name}: intercettati {new_count} mercati.")

    def run(self, selected_league: str = None):
        leagues_to_scan = MAJOR_LEAGUES
        if selected_league:
            leagues_to_scan = {k: v for k, v in MAJOR_LEAGUES.items() if selected_league.lower() in k.lower()}
            if not leagues_to_scan:
                print(f"[!] Nessuna competizione trovata per: '{selected_league}'")
                return

        print(f"[*] Avvio agente browser su {len(leagues_to_scan)} competizioni (headless={self.headless})...")
        
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=self.headless,
                args=["--disable-blink-features=AutomationControlled"]
            )
            context = browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                viewport={"width": 1400, "height": 900},
                locale="it-IT"
            )

            page = context.new_page()
            page.on("response", self.handle_response)

            cookie_handled = False

            for league_name, url in leagues_to_scan.items():
                self.current_league = league_name
                print(f"[*] Scansione: {league_name}...")
                
                try:
                    resp = page.goto(url, wait_until="domcontentloaded", timeout=20000)
                    
                    # Rilevamento immediato 404 (status code HTTP o testo pagina)
                    if resp and resp.status == 404:
                        print(f"    [!] 404: Pagina non disponibile su Betfair IT.")
                        continue

                    page_title = page.title().lower()
                    if "404" in page_title or "non trovata" in page_title:
                        print(f"    [!] 404: Pagina non attiva al momento.")
                        continue
                except Exception as e:
                    print(f"    [!] Timeout su {league_name}: {e}")
                    continue

                if not cookie_handled:
                    page.wait_for_timeout(1500)
                    try:
                        cookie_btn = page.locator("#onetrust-accept-btn-handler")
                        if cookie_btn.is_visible(timeout=2500):
                            cookie_btn.click()
                            print("[+] Cookie accettati.")
                            cookie_handled = True
                    except Exception:
                        cookie_handled = True

                page.mouse.wheel(0, 400)
                page.wait_for_timeout(self.wait_per_league * 1000)

            browser.close()

        self._save_output()

    def _save_output(self):
        if not self.captured_markets:
            print("[!] Nessun mercato intercettato.")
            return

        reconstructed_events = []
        for m_id, item in self.captured_markets.items():
            ev = item["event_node"]
            reconstructed_events.append({
                "eventId": ev.get("eventId"),
                "competition": item.get("competition", "Calcio"),
                "marketNodes": [item["market_node"]]
            })

        now_utc = datetime.now(timezone.utc)
        final_payload = {
            "acquired_at": now_utc.isoformat(),
            "currencyCode": "EUR",
            "total_markets": len(self.captured_markets),
            "eventTypes": [
                {
                    "eventTypeId": 1,
                    "eventNodes": reconstructed_events
                }
            ]
        }

        with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
            json.dump(final_payload, f, indent=2, ensure_ascii=False)

        print("\n" + "=" * 60)
        print(f"[+] COMPLETATO: {len(self.captured_markets)} mercati salvati in: {OUTPUT_FILE}")
        print(f"[+] Timestamp acquisizione: {now_utc.strftime('%Y-%m-%d %H:%M:%S UTC')}")
        print("=" * 60 + "\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Betfair Multi-League Interceptor")
    parser.add_argument("--league", type=str, default=None, help="Scansiona un campionato singolo")
    parser.add_argument("--headless", action="store_true", help="Esegui senza GUI")
    parser.add_argument("--wait", type=int, default=3, help="Secondi di attesa per pagina (default: 3)")
    args = parser.parse_args()

    agent = BetfairMultiLeagueAgent(headless=args.headless, wait_per_league=args.wait)
    agent.run(selected_league=args.league)