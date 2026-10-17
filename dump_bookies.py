#!/usr/bin/env python3
"""
Dumper diagnostico intelligente per Eurobet, William Hill e Snai.
- Blocca telemetria/Partytown
- Intercetta e filtra i JSON di rete
- Evidenzia con prefisso 'MATCH_' i file che contengono squadre di Serie A
- Salva l'HTML renderizzato (alleggerito) e comprime tutto in 'debug_bookies.zip'
"""

import os
import re
import json
import time
import shutil
import zipfile
import argparse
from urllib.parse import urlparse
from playwright.sync_api import sync_playwright, Response, Page

DUMP_DIR = "debug_bookies_raw"
ZIP_OUTPUT = "debug_bookies.zip"

# URL Serie A per i 3 bookmaker da analizzare
TARGETS = {
    "eurobet": "https://www.eurobet.it/it/scommesse/#!/calcio/it-serie-a/",
    "williamhill": "https://sports.williamhill.it/betting/it-it/football/competitions/OB_TY321/Serie-A/matches/OB_MGMB/Esito-Finale",
    "snai": "https://www.snai.it/sport/CALCIO/SERIE%20A"
}

# Domini e script spazzatura da bloccare a monte
BLOCKED_PATTERNS = [
    "*partytown*", "*proxytown*", "*google-analytics*", "*googletagmanager*",
    "*doubleclick*", "*facebook*", "*criteo*", "*hotjar*", "*newrelic*",
    "*adobedtm*", "*optimizely*", "*tiktok*", "*bing*", "*clarity*"
]

# Regex per riconoscere istantaneamente i payload che contengono le partite di Serie A
TEAM_REGEX = re.compile(r"\b(milan|juventus|napoli|inter|roma|lazio|fiorentina|atalanta|torino|bologna|lecce|udinese)\b", re.IGNORECASE)


def clean_html_for_dump(html_text: str) -> str:
    """Rimuove SVG, <style> e <script> lunghi per mantenere il file HTML leggero ma ispezionabile."""
    html_text = re.sub(r"<svg.*?</svg>", "", html_text, flags=re.DOTALL | re.IGNORECASE)
    html_text = re.sub(r"<style.*?</style>", "", html_text, flags=re.DOTALL | re.IGNORECASE)
    return html_text


def accept_cookies_and_popups(page: Page):
    """Accetta i cookie banner senza toccare il contenuto della pagina."""
    selectors = [
        "#onetrust-accept-btn-handler",
        "button.cookie-disclaimer__button",
        "#cookie-accept",
        ".cookie-consent-accept",
        "#acceptAllCookies",
        "button:has-text('Accetta tutti')",
        "button:has-text('Accetta')"
    ]
    for sel in selectors:
        try:
            loc = page.locator(sel)
            if loc.count() > 0 and loc.first.is_visible(timeout=200):
                loc.first.click(timeout=500, force=True)
        except Exception:
            pass


def make_network_handler(bookie: str, bookie_dir: str):
    counter = 0

    def handle_response(response: Response):
        nonlocal counter
        if response.status not in (200, 206):
            return

        url = response.url
        c_type = response.headers.get("content-type", "").lower()

        # Ignora file statici (immagini, font, css)
        if any(ext in url.lower() for ext in [".png", ".jpg", ".svg", ".woff", ".woff2", ".css", ".ico"]):
            return

        # Prova a leggere come JSON o testo strutturato
        raw_text = ""
        data = None
        try:
            if "json" in c_type:
                data = response.json()
                raw_text = json.dumps(data, ensure_ascii=False)
            else:
                # Alcuni endpoint (es. Snai/Eurobet) inviano JSON con content-type text/plain
                body_bytes = response.body()
                if len(body_bytes) > 300 and body_bytes.strip().startswith((b"{", b"[")):
                    raw_text = body_bytes.decode("utf-8", errors="ignore")
                    data = json.loads(raw_text)
        except Exception:
            return

        if not data or len(raw_text) < 350:
            return

        counter += 1
        found_teams = list(set(m.lower() for m in TEAM_REGEX.findall(raw_text)))

        parsed_url = urlparse(url)
        clean_path = re.sub(r"[^\w\-]", "_", parsed_url.path.strip("/"))[-40:] or "root"

        # Se contiene nomi di squadre di Serie A, antepone 'MATCH_' al nome file!
        prefix = f"MATCH_{counter:02d}" if len(found_teams) >= 2 else f"{counter:02d}"
        filename = f"{prefix}_{clean_path}.json"
        filepath = os.path.join(bookie_dir, filename)

        try:
            with open(filepath, "w", encoding="utf-8") as f:
                json.dump({
                    "url": url,
                    "status": response.status,
                    "teams_detected": found_teams,
                    "payload": data
                }, f, indent=2, ensure_ascii=False)

            tag = f"[★ SQUADRE TROVATE: {', '.join(found_teams[:4])}]" if len(found_teams) >= 2 else ""
            print(f"    [DUMP {prefix}] ({len(raw_text)/1024:.1f} KB) {filename} {tag}")
        except Exception:
            pass

    return handle_response


def run_dumper(selected_bookies: list, wait_sec: int = 12):
    # Pulisce la cartella temporanea precedente
    if os.path.exists(DUMP_DIR):
        shutil.rmtree(DUMP_DIR)
    os.makedirs(DUMP_DIR, exist_ok=True)

    print(f"[*] Avvio diagnostica su: {', '.join(selected_bookies)}")
    print(f"[*] NOTA: Mentre il browser è aperto ({wait_sec}s per sito), se vedi un popup o")
    print(f"    se il sito non è già sulla Serie A, puoi cliccare tu stesso su 'Serie A':")
    print(f"    lo script catturerà in automatico tutte le chiamate generate!\n")

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=False,
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox"]
        )
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            viewport={"width": 1400, "height": 900},
            locale="it-IT"
        )
        context.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")

        # Blocca tutti gli script di tracciamento/partytown
        for pattern in BLOCKED_PATTERNS:
            context.route(pattern, lambda route: route.abort())

        for bookie in selected_bookies:
            if bookie not in TARGETS:
                continue

            url = TARGETS[bookie]
            bookie_dir = os.path.join(DUMP_DIR, bookie)
            os.makedirs(bookie_dir, exist_ok=True)

            print("=" * 70)
            print(f"[*] SCANSIONE: {bookie.upper()} -> {url}")
            print("=" * 70)

            page = context.new_page()
            page.on("response", make_network_handler(bookie, bookie_dir))

            try:
                page.goto(url, wait_until="commit", timeout=25000)

                for sec in range(wait_sec):
                    if sec in (2, 4):
                        accept_cookies_and_popups(page)
                    
                    # Scroll leggero al centro pagina
                    if sec % 3 == 0 and sec > 0:
                        page.mouse.move(750, 500)
                        page.evaluate("window.scrollBy(0, 350)")

                    page.wait_for_timeout(1000)

                # Salva anche il DOM HTML finale renderizzato
                raw_html = page.content()
                cleaned_html = clean_html_for_dump(raw_html)
                html_teams = list(set(m.lower() for m in TEAM_REGEX.findall(cleaned_html)))

                html_path = os.path.join(bookie_dir, "rendered_dom.html")
                with open(html_path, "w", encoding="utf-8") as hf:
                    hf.write(f"<!-- URL FINALE: {page.url} | SQUADRE NEL DOM: {html_teams} -->\n")
                    hf.write(cleaned_html)

                print(f"    [HTML SALVATO] rendered_dom.html ({len(cleaned_html)/1024:.1f} KB) | Squadre visibili nell'HTML: {html_teams[:5]}")

            except Exception as e:
                print(f"    [!] Errore su {bookie}: {e}")
            finally:
                page.close()
                time.sleep(1)

        browser.close()

    # Creazione automatica del file ZIP
    print("\n[*] Creazione archivio compresso...")
    with zipfile.ZipFile(ZIP_OUTPUT, "w", zipfile.ZIP_DEFLATED) as zf:
        for root, _, files in os.walk(DUMP_DIR):
            for file in files:
                full_path = os.path.join(root, file)
                rel_path = os.path.relpath(full_path, DUMP_DIR)
                zf.write(full_path, rel_path)

    zip_size_mb = os.path.getsize(ZIP_OUTPUT) / (1024 * 1024)
    print("=" * 70)
    print(f"[+] COMPLETATO! Archivio creato: {ZIP_OUTPUT} ({zip_size_mb:.2f} MB)")
    print("    Carica direttamente 'debug_bookies.zip' nella chat!")
    print("=" * 70)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Smart Bookie Dumper")
    parser.add_argument("--bookies", type=str, default="eurobet,williamhill,snai", help="Bookmaker da scansionare")
    parser.add_argument("--wait", type=int, default=12, help="Secondi di ascolto per sito")
    args = parser.parse_args()

    b_list = [b.strip().lower() for b in args.bookies.split(",")]
    run_dumper(b_list, wait_sec=args.wait)