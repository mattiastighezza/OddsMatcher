#!/usr/bin/env python3
"""
Agente Multi-Bookmaker Playwright con Debug Automatico e Scroll Mirato.
- Scroll isolato sul corpo centrale (evita barre laterali/accordion)
- Chiusura popup circoscritta ai modali (non tocca quote o menu)
- Dump automatico di tutti i payload JSON intercettati in debug_dumps/
"""

import os
import json
import time
import argparse
import re
from urllib.parse import urlparse
from datetime import datetime, timezone
from typing import Dict, List, Any
from playwright.sync_api import sync_playwright, Response, Page

OUTPUT_FILE = "bookmakers_data.json"
DEBUG_DIR = "debug_dumps"

CATALOG = {
    "bwin": {
        "name": "Bwin",
        "leagues": {
            "Serie A (IT)": "https://sports.bwin.it/it/sports/calcio-4/scommesse/italia-20/serie-a-46",
            "Premier League (UK)": "https://sports.bwin.it/it/sports/calcio-4/scommesse/inghilterra-14/premier-league-10284",
            "La Liga (ES)": "https://sports.bwin.it/it/sports/calcio-4/scommesse/spagna-28/la-liga-10282",
            "Bundesliga (DE)": "https://sports.bwin.it/it/sports/calcio-4/scommesse/germania-17/bundesliga-10283",
            "Ligue 1 (FR)": "https://sports.bwin.it/it/sports/calcio-4/scommesse/francia-16/ligue-1-10285"
        }
    },
    "snai": {
        "name": "Snai",
        "leagues": {
            "Serie A (IT)": "https://www.snai.it/sport/CALCIO/SERIE%20A",
            "Premier League (UK)": "https://www.snai.it/sport/CALCIO/PREMIER%20LEAGUE",
            "La Liga (ES)": "https://www.snai.it/sport/CALCIO/LIGA",
            "Bundesliga (DE)": "https://www.snai.it/sport/CALCIO/BUNDESLIGA",
            "Ligue 1 (FR)": "https://www.snai.it/sport/CALCIO/LIGUE%201"
        }
    },
    "eurobet": {
        "name": "Eurobet",
        "leagues": {
            "Serie A (IT)": "https://www.eurobet.it/it/scommesse/#!/calcio/italia/serie-a/",
            "Premier League (UK)": "https://www.eurobet.it/it/scommesse/#!/calcio/inghilterra/premier-league/",
            "La Liga (ES)": "https://www.eurobet.it/it/scommesse/#!/calcio/spagna/la-liga/",
            "Bundesliga (DE)": "https://www.eurobet.it/it/scommesse/#!/calcio/germania/bundesliga/",
            "Ligue 1 (FR)": "https://www.eurobet.it/it/scommesse/#!/calcio/francia/ligue-1/"
        }
    },
    "sisal": {
        "name": "Sisal",
        "leagues": {
            "Serie A (IT)": "https://www.sisal.it/scommesse-matchpoint/calcio/italia/serie-a",
            "Premier League (UK)": "https://www.sisal.it/scommesse-matchpoint/calcio/inghilterra/premier-league",
            "La Liga (ES)": "https://www.sisal.it/scommesse-matchpoint/calcio/spagna/la-liga",
            "Bundesliga (DE)": "https://www.sisal.it/scommesse-matchpoint/calcio/germania/bundesliga",
            "Ligue 1 (FR)": "https://www.sisal.it/scommesse-matchpoint/calcio/francia/ligue-1"
        }
    }
}


def dismiss_popups_safely(page: Page):
    """Chiude popup e banner cookie SOLO se dentro modali o overlay specifici."""
    try:
        page.keyboard.press("Escape")
    except Exception:
        pass

    # 1. Cookie Banners
    cookie_selectors = [
        "#onetrust-accept-btn-handler",
        "#cookie-accept",
        ".cookie-consent-accept",
        "#acceptAllCookies",
        "#btn-cookie-policy-accept",
        "button:has-text('Accetta tutti')"
    ]
    for sel in cookie_selectors:
        try:
            loc = page.locator(sel)
            if loc.count() > 0 and loc.first.is_visible(timeout=100):
                loc.first.click(timeout=200, force=True)
                return
        except Exception:
            pass

    # 2. Chiudi modali (solo dentro contenitori di tipo dialog/modal)
    modal_close_selectors = [
        "[role='dialog'] button[aria-label*='close' i]",
        "[role='dialog'] button[aria-label*='chiudi' i]",
        "[role='dialog'] .btn-close",
        "[role='dialog'] .modal-close",
        ".modal [aria-label*='chiudi' i]",
        ".modal .btn-close",
        ".modal-header .close"
    ]
    for sel in modal_close_selectors:
        try:
            loc = page.locator(sel)
            if loc.count() > 0 and loc.first.is_visible(timeout=100):
                loc.first.click(timeout=200, force=True)
                break
        except Exception:
            pass


def scroll_main_content(page: Page, offset: int = 450):
    """
    Sposta il mouse al centro della finestra ed esegue lo scroll dell'area
    centrale evitando le barre di navigazione laterali.
    """
    try:
        # Posiziona il puntatore nel centro dell'area quote (offset 750px orizzontale)
        page.mouse.move(750, 450)
        
        # Esegue lo scroll via JS sul container principale se presente, altrimenti su window
        page.evaluate(f"""
            (() => {{
                const mainEl = document.querySelector('main, [role="main"], .main-content, #main-content');
                if (mainEl && mainEl.scrollHeight > mainEl.clientHeight) {{
                    mainEl.scrollBy(0, {offset});
                }} else {{
                    window.scrollBy(0, {offset});
                }}
            }})()
        """)
    except Exception:
        pass


class MultiBookmakerAgent:
    def __init__(self, headless: bool = False, wait_per_league: int = 8, enable_debug: bool = True):
        self.headless = headless
        self.wait_per_league = wait_per_league
        self.enable_debug = enable_debug
        self.normalized_events: List[Dict[str, Any]] = []
        self.current_bookie: str = ""
        self.current_league: str = ""
        self.dump_counters: Dict[str, int] = {}

    def _auto_dump_debug(self, url: str, data: Any):
        """Salva il JSON su disco nella cartella di debug per ispezione."""
        if not self.enable_debug:
            return

        bookie_name = self.current_bookie.lower()
        self.dump_counters[bookie_name] = self.dump_counters.get(bookie_name, 0) + 1
        idx = self.dump_counters[bookie_name]

        bookie_dir = os.path.join(DEBUG_DIR, bookie_name)
        os.makedirs(bookie_dir, exist_ok=True)

        parsed_url = urlparse(url)
        clean_endpoint = re.sub(r"[^\w\-]", "_", parsed_url.path.strip("/"))
        filename = f"{idx:02d}_{clean_endpoint[-35:]}.json"
        full_path = os.path.join(bookie_dir, filename)

        try:
            with open(full_path, "w", encoding="utf-8") as f:
                json.dump({"url": url, "payload": data}, f, indent=2, ensure_ascii=False)
            print(f"    [DEBUG DUMP] Salvato payload #{idx:02d} ({len(json.dumps(data))/1024:.1f} KB) in: {filename}")
        except Exception:
            pass

    def run(self, selected_bookies: List[str]):
        print(f"[*] Avvio agente Bookmaker su: {', '.join(selected_bookies)} (headless={self.headless})...")
        if self.enable_debug:
            print(f"[*] Debug attivo: i payload JSON verranno salvati in '{DEBUG_DIR}/'")

        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=self.headless,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--no-sandbox"
                ]
            )

            context = browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                viewport={"width": 1400, "height": 900},
                locale="it-IT"
            )

			# Blocca Partytown, analytics e tracciatori alla radice
			context.route("**/*partytown*", lambda route: route.abort())
			context.route("**/*doubleclick*", lambda route: route.abort())
			context.route("**/*google-analytics*", lambda route: route.abort())
			context.route("**/*scorecardresearch*", lambda route: route.abort())
            context.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")

            for b_key in selected_bookies:
                if b_key not in CATALOG:
                    continue

                bookie_info = CATALOG[b_key]
                self.current_bookie = bookie_info["name"]

                print("\n" + "=" * 65)
                print(f"[*] SESSIONE BOOKMAKER: {self.current_bookie.upper()}")
                print("=" * 65)

                page = context.new_page()
                page.on("response", self._handle_network_response)

                for league_name, url in bookie_info["leagues"].items():
                    self.current_league = league_name
                    print(f"\n[*] [{self.current_bookie}] Navigazione: {league_name}...")

                    count_before = len(self.normalized_events)

                    try:
                        page.goto(url, wait_until="domcontentloaded", timeout=25000)

                        page_title = page.title().lower()
                        if "access denied" in page_title or "403 forbidden" in page_title:
                            print(f"    [!] Errore blocco anti-bot (Access Denied) su {league_name}.")
                            time.sleep(2)
                            continue

                        # Loop di attesa attiva: gestisce modali e scrolla l'area centrale
                        for sec in range(self.wait_per_league):
                            dismiss_popups_safely(page)
                            
                            # Alterna scroll in giù e lieve risalita per innescare lazy loading
                            scroll_offset = 450 if sec % 2 == 0 else -150
                            scroll_main_content(page, offset=scroll_offset)
                            
                            page.wait_for_timeout(1000)

                            if len(self.normalized_events) > count_before and sec >= 5:
                                break

                    except Exception as e:
                        print(f"    [!] Errore navigazione su {league_name}: {e}")

                page.close()
                time.sleep(1.5)

            browser.close()

        self._save_output()

    def _handle_network_response(self, response: Response):
        if response.status not in (200, 206):
            return

        c_type = response.headers.get("content-type", "")
        if "json" not in c_type and "javascript" not in c_type:
            return

        try:
            data = response.json()
        except Exception:
            return

        url = response.url.lower()

        # Ispezione e salvataggio payload per debug
        if len(json.dumps(data)) > 600:
            print(f"    [NET JSON] {self.current_bookie} -> {response.url[:70]}...")
            self._auto_dump_debug(response.url, data)

        try:
            if self.current_bookie == "Bwin" and isinstance(data, dict) and "fixtures" in data:
                self._parse_bwin(data)
            elif self.current_bookie == "Snai":
                self._parse_snai(data)
            elif self.current_bookie == "Eurobet":
                self._parse_eurobet(data)
            elif self.current_bookie == "Sisal":
                self._parse_sisal(data)
        except Exception as e:
            if self.enable_debug:
                print(f"    [!] Errore parsing {self.current_bookie}: {e}")

    # ================= PARSER BWIN =================
    def _parse_bwin(self, data: Dict[str, Any]):
        fixtures = data.get("fixtures", [])
        if not isinstance(fixtures, list):
            return

        added = 0
        for fix in fixtures:
            if not isinstance(fix, dict):
                continue
            if fix.get("stage") == "Live":
                continue

            parts = fix.get("participants", [])
            if not isinstance(parts, list) or len(parts) < 2:
                continue

            p1 = parts[0] if isinstance(parts[0], dict) else {}
            p2 = parts[1] if isinstance(parts[1], dict) else {}

            home = p1.get("name", {}).get("value", "").strip() if isinstance(p1.get("name"), dict) else ""
            away = p2.get("name", {}).get("value", "").strip() if isinstance(p2.get("name"), dict) else ""
            
            if not home or not away:
                continue

            start_str = fix.get("startDate")
            odds_1x2 = {}

            games = fix.get("games", [])
            if not isinstance(games, list):
                continue

            for g in games:
                if not isinstance(g, dict):
                    continue
                g_name = g.get("name", {}).get("value", "").lower() if isinstance(g.get("name"), dict) else ""
                
                if any(k in g_name for k in ["1x2", "esito finale", "tempo regolamentare", "1 x 2"]):
                    results = g.get("results", [])
                    if not isinstance(results, list):
                        continue

                    for r in results:
                        if not isinstance(r, dict):
                            continue
                        r_name = r.get("name", {}).get("value", "") if isinstance(r.get("name"), dict) else ""
                        odd = r.get("odds")
                        if odd is not None:
                            try:
                                f_odd = float(odd)
                                if r_name in ["1", home]:
                                    odds_1x2["1"] = f_odd
                                elif r_name in ["X", "Pareggio", "Draw"]:
                                    odds_1x2["X"] = f_odd
                                elif r_name in ["2", away]:
                                    odds_1x2["2"] = f_odd
                            except ValueError:
                                pass

            if len(odds_1x2) == 3:
                self._add_event("Bwin", self.current_league, home, away, start_str, odds_1x2)
                added += 1

        if added > 0:
            print(f"    [+] Bwin: estratti {added} match per {self.current_league}.")

    # ================= PARSER SNAI =================
    def _parse_snai(self, data: Any):
        events_list = []
        if isinstance(data, list):
            events_list = data
        elif isinstance(data, dict):
            events_list = data.get("eventi", data.get("events", data.get("data", [])))
            if not events_list and "manifestazione" in data and isinstance(data["manifestazione"], dict):
                events_list = data["manifestazione"].get("eventi", [])

        if not isinstance(events_list, list):
            return

        added = 0
        for ev in events_list:
            if not isinstance(ev, dict):
                continue
            home = str(ev.get("team1", ev.get("descrizioneCasa", ev.get("home", "")))).strip()
            away = str(ev.get("team2", ev.get("descrizioneTrasferta", ev.get("away", "")))).strip()
            start_str = str(ev.get("dataOra", ev.get("startDate", "")))

            if not home or not away:
                continue

            odds_1x2 = {}
            quote_list = ev.get("quote", ev.get("esiti", []))
            if not isinstance(quote_list, list):
                continue

            for q in quote_list:
                if not isinstance(q, dict):
                    continue
                desc = str(q.get("descrizione", q.get("sigla", "")))
                val = q.get("valore", q.get("quota"))
                if val is not None:
                    try:
                        f_val = float(val)
                        f_val = f_val / 100.0 if f_val > 50 else f_val
                        if desc in ["1", "1X2 1", "Squadra 1"]:
                            odds_1x2["1"] = f_val
                        elif desc in ["X", "1X2 X", "Pareggio"]:
                            odds_1x2["X"] = f_val
                        elif desc in ["2", "1X2 2", "Squadra 2"]:
                            odds_1x2["2"] = f_val
                    except ValueError:
                        pass

            if len(odds_1x2) == 3:
                self._add_event("Snai", self.current_league, home, away, start_str, odds_1x2)
                added += 1

        if added > 0:
            print(f"    [+] Snai: estratti {added} match per {self.current_league}.")

    # ================= PARSER EUROBET =================
    def _parse_eurobet(self, data: Any):
        event_items = []
        if isinstance(data, dict):
            event_items = data.get("eventList", data.get("events", data.get("data", [])))
        elif isinstance(data, list):
            event_items = data

        if not isinstance(event_items, list):
            return

        added = 0
        for item in event_items:
            if not isinstance(item, dict):
                continue
            desc_event = str(item.get("description", item.get("name", "")))
            if " - " not in desc_event:
                continue
            home, away = [s.strip() for s in desc_event.split(" - ", 1)]

            odds_1x2 = {}
            bet_types = item.get("betTypeList", item.get("markets", []))
            if not isinstance(bet_types, list):
                continue

            for bet in bet_types:
                if not isinstance(bet, dict):
                    continue
                bet_name = str(bet.get("betTypeDescription", bet.get("name", ""))).lower()
                if "1x2" in bet_name or "esito finale" in bet_name:
                    outcomes = bet.get("outcomeList", bet.get("outcomes", []))
                    if not isinstance(outcomes, list):
                        continue
                    for out in outcomes:
                        if not isinstance(out, dict):
                            continue
                        label = str(out.get("outcomeDescription", out.get("name", "")))
                        odd = out.get("odd", out.get("quota"))
                        if odd is not None:
                            try:
                                f_odd = float(odd)
                                f_odd = f_odd / 100.0 if f_odd > 50 else f_odd
                                if label in ["1", home]:
                                    odds_1x2["1"] = f_odd
                                elif label in ["X", "Pareggio"]:
                                    odds_1x2["X"] = f_odd
                                elif label in ["2", away]:
                                    odds_1x2["2"] = f_odd
                            except ValueError:
                                pass

            if len(odds_1x2) == 3:
                self._add_event("Eurobet", self.current_league, home, away, str(item.get("eventDate", "")), odds_1x2)
                added += 1

        if added > 0:
            print(f"    [+] Eurobet: estratti {added} match per {self.current_league}.")

    # ================= PARSER SISAL =================
    def _parse_sisal(self, data: Any):
        events_list = []
        if isinstance(data, dict):
            events_list = data.get("events", data.get("matches", []))
            if not events_list and "data" in data and isinstance(data["data"], dict):
                events_list = data["data"].get("events", [])
        elif isinstance(data, list):
            events_list = data

        if not isinstance(events_list, list):
            return

        added = 0
        for ev in events_list:
            if not isinstance(ev, dict):
                continue
            home = str(ev.get("homeTeam", ev.get("teamHome", ev.get("squadraCasa", "")))).strip()
            away = str(ev.get("awayTeam", ev.get("teamAway", ev.get("squadraTrasferta", "")))).strip()
            if not home or not away:
                desc = str(ev.get("description", ""))
                if " - " in desc:
                    home, away = [s.strip() for s in desc.split(" - ", 1)]

            if not home or not away:
                continue

            odds_1x2 = {}
            markets = ev.get("markets", ev.get("scommesse", []))
            if not isinstance(markets, list):
                continue

            for bet in markets:
                if not isinstance(bet, dict):
                    continue
                bet_desc = str(bet.get("description", bet.get("nome", ""))).lower()
                if "1x2" in bet_desc:
                    outcomes = bet.get("outcomes", bet.get("esiti", []))
                    if not isinstance(outcomes, list):
                        continue
                    for out in outcomes:
                        if not isinstance(out, dict):
                            continue
                        name = str(out.get("description", out.get("segno", "")))
                        quote = out.get("quota", out.get("price"))
                        if quote is not None:
                            try:
                                f_q = float(quote)
                                f_q = f_q / 100.0 if f_q > 50 else f_q
                                if name in ["1", home]:
                                    odds_1x2["1"] = f_q
                                elif name in ["X", "Pareggio"]:
                                    odds_1x2["X"] = f_q
                                elif name in ["2", away]:
                                    odds_1x2["2"] = f_q
                            except ValueError:
                                pass

            if len(odds_1x2) == 3:
                self._add_event("Sisal", self.current_league, home, away, str(ev.get("startDate", "")), odds_1x2)
                added += 1

        if added > 0:
            print(f"    [+] Sisal: estratti {added} match per {self.current_league}.")

    def _add_event(self, bookie: str, comp: str, home: str, away: str, kickoff: str, odds: Dict[str, float]):
        self.normalized_events.append({
            "bookmaker": bookie,
            "competition": comp,
            "home_team": home,
            "away_team": away,
            "kickoff": kickoff,
            "markets": {"1X2": odds}
        })

    def _save_output(self):
        if not self.normalized_events:
            print("\n[!] Nessun evento estratto dai bookmaker. Controlla i dump salvati in 'debug_dumps/'.")
            return

        unique = {}
        for ev in self.normalized_events:
            key = f"{ev['bookmaker']}_{ev['competition']}_{ev['home_team']}_{ev['away_team']}"
            unique[key] = ev

        now_utc = datetime.now(timezone.utc)
        final_payload = {
            "acquired_at": now_utc.isoformat(),
            "total_events": len(unique),
            "events": list(unique.values())
        }

        with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
            json.dump(final_payload, f, indent=2, ensure_ascii=False)

        print("\n" + "=" * 65)
        print(f"[+] COMPLETATO: {len(unique)} eventi estratti e salvati in: {OUTPUT_FILE}")
        print(f"[+] Timestamp: {now_utc.strftime('%Y-%m-%d %H:%M:%S UTC')}")
        print("=" * 65 + "\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Multi-Bookmaker Interceptor con Debug")
    parser.add_argument("--bookies", type=str, default="bwin,snai", help="Bookmaker separati da virgola")
    parser.add_argument("--headless", action="store_true", help="Esegui senza GUI")
    parser.add_argument("--wait", type=int, default=8, help="Secondi di attesa per campionato (default: 8)")
    parser.add_argument("--no-debug", action="store_true", help="Disabilita salvataggio JSON di debug")
    args = parser.parse_args()

    targets = [b.strip().lower() for b in args.bookies.split(",")]
    agent = MultiBookmakerAgent(
        headless=args.headless, 
        wait_per_league=args.wait,
        enable_debug=not args.no_debug
    )
    agent.run(targets)