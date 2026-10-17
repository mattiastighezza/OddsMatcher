#!/usr/bin/env python3
"""
Agente Playwright per Betfair Exchange Italia.
Utilizza gli URL ufficiali verificati, apre il browser (headless=False) e intercetta
tutti i nodi 'bymarket' estraendo i mercati 1X2 senza filtri distruttivi.
"""

import os
import json
from datetime import datetime, timezone
from typing import Dict, Any, List, Optional
from playwright.sync_api import sync_playwright, Response

OUTPUT_FILE = "betfair_exchange_data.json"

# ID ufficiali aggiornati per Betfair Exchange IT
BETFAIR_URLS = {
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
    def __init__(self, headless: bool = False):
        self.headless = headless
        self.markets: Dict[str, Dict[str, Any]] = {}
        self.current_competition: str = "Calcio"

    def _handle_response(self, response: Response):
        if response.status not in (200, 206):
            return
        url = response.url.lower()

        if any(k in url for k in ["bymarket", "byevent", "ero.betfair", "readonly"]):
            try:
                data = response.json()
                self._walk_payload(data)
            except Exception:
                pass

    def _walk_payload(self, node: Any, current_event: Optional[Dict[str, Any]] = None):
        """
        Attraversa ricorsivamente qualsiasi struttura JSON di Betfair
        (eventTypes -> eventNodes -> marketNodes) mantenendo il contesto dell'evento padre.
        """
        if isinstance(node, dict):
            # Se siamo su un eventNode che contiene 'event' e 'marketNodes'
            if "event" in node and isinstance(node["event"], dict):
                current_event = node["event"]

            # Se siamo su un marketNode valido
            if "marketId" in node and "runners" in node and isinstance(node["runners"], list):
                self._extract_market_node(node, current_event or {})
                return

            for v in node.values():
                if isinstance(v, (dict, list)):
                    self._walk_payload(v, current_event)

        elif isinstance(node, list):
            for item in node:
                if isinstance(item, (dict, list)):
                    self._walk_payload(item, current_event)

    def _extract_market_node(self, m_node: Dict[str, Any], ev_info: Dict[str, Any]):
        mkt_id = str(m_node.get("marketId", "")).strip()
        if not mkt_id:
            return

        runners = m_node.get("runners", [])
        # Il mercato 1X2 su Betfair ha sempre esattamente 3 runner
        if len(runners) != 3:
            return

        desc = m_node.get("description", {})
        mkt_type = str(desc.get("marketType", "")).upper()
        mkt_name = str(desc.get("marketName", "")).upper()

        # Se marketType è specificato, verifica che sia MATCH_ODDS (altrimenti accetta se ha 3 runner con The Draw/Pareggio)
        if mkt_type and mkt_type not in ["MATCH_ODDS", "ESITO_FINALE", "1X2"]:
            return

        # Ricava il nome del match da event.eventName oppure dai nomi dei primi 2 runner
        event_name = str(ev_info.get("eventName", desc.get("eventName", ""))).strip()
        home, away = "", ""

        for sep in [" - ", " v ", " vs ", " vs. "]:
            if sep in event_name:
                parts = event_name.split(sep, 1)
                home, away = parts[0].strip(), parts[1].strip()
                break

        # Se eventName non era nel nodo padre, ricostruiamolo direttamente dai runner 1 e 2
        draw_keywords = {"pareggio", "the draw", "draw", "x"}
        non_draw_runners = []
        for r in runners:
            r_name = str(r.get("description", {}).get("runnerName", "")).strip()
            if r_name and r_name.lower() not in draw_keywords:
                non_draw_runners.append(r_name)

        if (not home or not away) and len(non_draw_runners) == 2:
            home, away = non_draw_runners[0], non_draw_runners[1]

        if not home or not away:
            return

        kickoff_str = (
            ev_info.get("openDate")
            or desc.get("marketTime")
            or desc.get("marketStartTime")
            or ""
        )

        # Estrazione opzionale e sicura del Betradar ID
        betradar_id = None
        raw_br = ev_info.get("betradarId") or ev_info.get("externalId")
        if raw_br:
            betradar_id = str(raw_br).strip()

        runners_data = {}
        for idx, r in enumerate(runners):
            r_desc = r.get("description", {})
            r_name = str(r_desc.get("runnerName", "")).strip()
            sel_id = r.get("selectionId")
            sort_prio = r.get("sortPriority", idx + 1)

            # Determinazione esito: X per pareggio, altrimenti per nome o sortPriority Betfair (1=Casa, 2=Ospite, 3=Pareggio)
            key = None
            if r_name.lower() in draw_keywords or sel_id == 58805:
                key = "X"
            elif r_name.lower() == home.lower() or sort_prio == 1:
                key = "1"
            elif r_name.lower() == away.lower() or sort_prio == 2:
                key = "2"
            elif sort_prio == 3:
                key = "X"

            ex = r.get("exchange", {})
            avail_back = ex.get("availableToBack", [])
            avail_lay = ex.get("availableToLay", [])

            back_price = float(avail_back[0].get("price", 0.0)) if avail_back else 0.0
            back_size = float(avail_back[0].get("size", 0.0)) if avail_back else 0.0
            lay_price = float(avail_lay[0].get("price", 0.0)) if avail_lay else 0.0
            lay_size = float(avail_lay[0].get("size", 0.0)) if avail_lay else 0.0

            if key:
                runners_data[key] = {
                    "runner_name": r_name if r_name else key,
                    "selection_id": sel_id,
                    "back_odds": back_price,
                    "back_liquidity": back_size,
                    "lay_odds": lay_price,
                    "lay_liquidity": lay_size
                }

        if len(runners_data) == 3:
            # Sovrascrive/aggiorna solo se il nuovo nodo ha quotazioni attive o se il mercato non esisteva ancora
            has_prices = any(v["lay_odds"] > 1.0 or v["back_odds"] > 1.0 for v in runners_data.values())
            if mkt_id not in self.markets or has_prices:
                self.markets[mkt_id] = {
                    "market_id": mkt_id,
                    "event_id": str(ev_info.get("eventId", "")),
                    "competition": self.current_competition,
                    "home_team": home,
                    "away_team": away,
                    "kickoff": str(kickoff_str),
                    "betradar_id": betradar_id,
                    "markets": {"1X2": runners_data}
                }

    def run(self):
        print(f"[*] Avvio agente browser su {len(BETFAIR_URLS)} competizioni (headless={self.headless})...")
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

            cookie_accepted = False
            for comp_name, url in BETFAIR_URLS.items():
                self.current_competition = comp_name
                before = len(self.markets)
                print(f"[*] Scansione: {comp_name}...")

                try:
                    page.goto(url, wait_until="domcontentloaded", timeout=25000)

                    if not cookie_accepted:
                        page.wait_for_timeout(1500)
                        try:
                            btn = page.locator("#onetrust-accept-btn-handler")
                            if btn.count() > 0 and btn.first.is_visible(timeout=1500):
                                btn.first.click()
                                cookie_accepted = True
                                print("[+] Cookie accettati.")
                        except Exception:
                            pass

                    page.wait_for_timeout(3500)
                    diff = len(self.markets) - before
                    print(f"    [+] {comp_name}: intercettati {diff} mercati.")
                except Exception as e:
                    print(f"    [!] Errore su {comp_name}: {e}")

            browser.close()

        now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        payload = {
            "acquired_at": datetime.now(timezone.utc).isoformat(),
            "total_markets": len(self.markets),
            "events": list(self.markets.values())
        }

        with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, ensure_ascii=False)

        print("\n" + "=" * 60)
        print(f"[+] COMPLETATO: {len(self.markets)} mercati salvati in: {OUTPUT_FILE}")
        print(f"[+] Timestamp acquisizione: {now_str}")
        print("=" * 60)


if __name__ == "__main__":
    agent = BetfairMultiLeagueAgent(headless=False)
    agent.run()