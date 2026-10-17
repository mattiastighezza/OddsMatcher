#!/usr/bin/env python3
"""
Oddsmatcher Live Engine con Matching Ibrido Betradar ID + Fuzzy Matching.
Include calcolo automatico rating (Qualificante / Freebet SNR), server API e Dashboard Web locale.
"""

from __future__ import annotations
import os
import sys
import json
import time
import threading
import argparse
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from rapidfuzz import fuzz

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

LOCK = threading.Lock()
GLOBAL_STATE = {
    "bookmakers": {},
    "betfair": {"events": [], "updated_at": None},
    "results": [],
    "last_calc": None,
    "sources_status": {}
}

CONFIG = {
    "stake": 100.0,
    "mode": "qualifying",
    "min_liq": 10.0,
    "max_age_sec": 300,
    "port": 8000,
    "commission": 0.045
}


def find_matching_betfair_event(bookie_event: dict, betfair_events_list: list):
    b_id = bookie_event.get("betradar_id")

    # Livello 1: Betradar ID esatto
    if b_id:
        for bf_ev in betfair_events_list:
            bf_id = bf_ev.get("betradar_id")
            if bf_id and str(bf_id) == str(b_id):
                return bf_ev, "ID"

    # Livello 2: Fallback Fuzzy Matching
    b_home = bookie_event.get("home_team", "").lower()
    b_away = bookie_event.get("away_team", "").lower()

    best_match = None
    best_score = 0.0

    for bf_ev in betfair_events_list:
        bf_home = bf_ev.get("home_team", "").lower()
        bf_away = bf_ev.get("away_team", "").lower()

        h_score = fuzz.token_sort_ratio(b_home, bf_home)
        a_score = fuzz.token_sort_ratio(b_away, bf_away)
        avg_score = (h_score + a_score) / 2.0

        if avg_score >= 72 and avg_score > best_score:
            best_score = avg_score
            best_match = bf_ev

    if best_match:
        return best_match, f"FZ {int(best_score)}%"

    return None, None


def calculate_ratings():
    now = datetime.now(timezone.utc)
    all_bookie_events = []
    bf_events = []

    with LOCK:
        bf_events = GLOBAL_STATE["betfair"].get("events", [])
        for b_name, b_data in GLOBAL_STATE["bookmakers"].items():
            age = (now - b_data["updated_at"]).total_seconds()
            if age <= CONFIG["max_age_sec"]:
                all_bookie_events.extend(b_data["events"])
            else:
                GLOBAL_STATE["sources_status"][b_name] = f"Obsoleto ({int(age)}s fa)"

    if not all_bookie_events or not bf_events:
        return

    c = CONFIG["commission"]
    mode = CONFIG["mode"]
    stake = CONFIG["stake"]
    matched_rows = []

    for b_ev in all_bookie_events:
        bf_ev, match_type = find_matching_betfair_event(b_ev, bf_events)
        if not bf_ev:
            continue

        b_1x2 = b_ev.get("markets", {}).get("1X2", {})
        bf_1x2 = bf_ev.get("markets", {}).get("1X2", {})

        for out_key in ["1", "X", "2"]:
            if out_key in b_1x2 and out_key in bf_1x2:
                p_back = float(b_1x2[out_key])
                bf_runner = bf_1x2[out_key]
                p_lay = float(bf_runner.get("lay_odds", 0.0))
                liq = float(bf_runner.get("lay_liquidity", 0.0))

                if p_lay > 1.0 and liq >= CONFIG["min_liq"]:
                    if mode == "qualifying":
                        rating = (p_back * (1.0 - c)) / (p_lay - c) * 100.0
                        lay_stake = (stake * p_back) / (p_lay - c)
                    else:
                        rating = ((p_back - 1.0) * (1.0 - c)) / (p_lay - c) * 100.0
                        lay_stake = (stake * (p_back - 1.0)) / (p_lay - c)

                    liability = lay_stake * (p_lay - 1.0)
                    net = (lay_stake * (1.0 - c)) - stake if mode == "qualifying" else (lay_stake * (1.0 - c))

                    matched_rows.append({
                        "kickoff": str(bf_ev.get("kickoff", ""))[:16].replace("T", " "),
                        "competition": b_ev.get("competition", "Calcio"),
                        "match": f"{b_ev.get('home_team')} - {b_ev.get('away_team')}",
                        "outcome": out_key,
                        "match_type": match_type,
                        "bookmaker": b_ev.get("bookmaker", "Bookie"),
                        "back": p_back,
                        "lay": p_lay,
                        "liq": liq,
                        "rating": round(rating, 2),
                        "lay_stake": round(lay_stake, 2),
                        "liability": round(liability, 2),
                        "net": round(net, 2)
                    })

    matched_rows.sort(key=lambda x: x["rating"], reverse=True)

    with LOCK:
        GLOBAL_STATE["results"] = matched_rows
        GLOBAL_STATE["last_calc"] = now.isoformat()


def run_bwin_worker(interval: int = 30):
    while True:
        try:
            import bwin_client
            client = bwin_client.BwinDirectClient()
            events = client.fetch_all()
            if events:
                now = datetime.now(timezone.utc)
                with LOCK:
                    GLOBAL_STATE["bookmakers"]["Bwin"] = {"events": events, "updated_at": now}
                    GLOBAL_STATE["sources_status"]["Bwin"] = f"OK ({len(events)} match, {now.strftime('%H:%M:%S')})"
                calculate_ratings()
        except Exception as e:
            with LOCK:
                GLOBAL_STATE["sources_status"]["Bwin"] = f"Errore: {e}"
        time.sleep(interval)


def run_snai_worker(interval: int = 30):
    while True:
        try:
            import snai_client
            client = snai_client.SnaiDirectClient()
            events = client.fetch_all()
            if events:
                now = datetime.now(timezone.utc)
                with LOCK:
                    GLOBAL_STATE["bookmakers"]["Snai"] = {"events": events, "updated_at": now}
                    GLOBAL_STATE["sources_status"]["Snai"] = f"OK ({len(events)} match, {now.strftime('%H:%M:%S')})"
                calculate_ratings()
        except Exception as e:
            with LOCK:
                GLOBAL_STATE["sources_status"]["Snai"] = f"Errore: {e}"
        time.sleep(interval)


def run_williamhill_worker(interval: int = 30):
    while True:
        try:
            import williamhill_client
            client = williamhill_client.WilliamHillDirectClient()
            events = client.fetch_all()
            if events:
                now = datetime.now(timezone.utc)
                with LOCK:
                    GLOBAL_STATE["bookmakers"]["William Hill"] = {"events": events, "updated_at": now}
                    GLOBAL_STATE["sources_status"]["William Hill"] = f"OK ({len(events)} match, {now.strftime('%H:%M:%S')})"
                calculate_ratings()
        except Exception as e:
            with LOCK:
                GLOBAL_STATE["sources_status"]["William Hill"] = f"Errore: {e}"
        time.sleep(interval)


def run_betfair_worker(interval: int = 60):
    while True:
        try:
            if os.path.exists("betfair_exchange_data.json"):
                with open("betfair_exchange_data.json", "r", encoding="utf-8") as f:
                    d = json.load(f)
                    events = d.get("events", [])
                    acq = d.get("acquired_at")
                    dt_acq = datetime.fromisoformat(acq) if acq else datetime.now(timezone.utc)
                    with LOCK:
                        GLOBAL_STATE["betfair"] = {"events": events, "updated_at": dt_acq}
                        GLOBAL_STATE["sources_status"]["Betfair Exchange"] = f"OK ({len(events)} mercati, {dt_acq.strftime('%H:%M:%S')})"
                    calculate_ratings()
        except Exception as e:
            with LOCK:
                GLOBAL_STATE["sources_status"]["Betfair Exchange"] = f"Errore: {e}"
        time.sleep(interval)


DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="it">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Oddsmatcher Pro Live</title>
    <style>
        :root { --bg: #0f172a; --panel: #1e293b; --border: #334155; --text: #f8fafc; --accent: #38bdf8; --green: #22c55e; --yellow: #eab308; --red: #ef4444; }
        body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; background: var(--bg); color: var(--text); margin: 0; padding: 20px; }
        .header { display: flex; justify-content: space-between; align-items: center; border-bottom: 1px solid var(--border); padding-bottom: 15px; margin-bottom: 20px; }
        h1 { margin: 0; font-size: 22px; color: var(--accent); }
        .status-bar { display: flex; gap: 10px; flex-wrap: wrap; margin-bottom: 20px; }
        .badge { background: var(--panel); border: 1px solid var(--border); padding: 6px 12px; border-radius: 6px; font-size: 12px; }
        .controls { background: var(--panel); border: 1px solid var(--border); padding: 15px; border-radius: 8px; display: flex; gap: 15px; flex-wrap: wrap; align-items: center; margin-bottom: 20px; }
        input, select { background: var(--bg); border: 1px solid var(--border); color: var(--text); padding: 8px 12px; border-radius: 6px; outline: none; }
        table { width: 100%; border-collapse: collapse; background: var(--panel); border-radius: 8px; overflow: hidden; border: 1px solid var(--border); }
        th, td { padding: 10px 12px; text-align: left; border-bottom: 1px solid var(--border); font-size: 13px; }
        th { background: #182234; color: #94a3b8; font-weight: 600; text-transform: uppercase; font-size: 11px; }
        .num { text-align: right; font-variant-numeric: tabular-nums; }
        .tag { display: inline-block; padding: 2px 8px; border-radius: 4px; font-weight: bold; background: #3b82f6; color: #fff; }
        .badge-id { display: inline-block; padding: 1px 5px; border-radius: 3px; font-size: 10px; font-weight: bold; background: #15803d; color: #fff; margin-left: 5px; }
        .badge-fz { display: inline-block; padding: 1px 5px; border-radius: 3px; font-size: 10px; font-weight: bold; background: #0369a1; color: #fff; margin-left: 5px; }
        .r-high { color: var(--green); font-weight: bold; }
        .r-mid { color: var(--yellow); font-weight: bold; }
        .r-low { color: var(--red); font-weight: bold; }
    </style>
</head>
<body>
    <div class="header">
        <h1>Oddsmatcher Pro Live</h1>
        <div id="last-update" style="color: #94a3b8; font-size: 12px;">Connessione...</div>
    </div>
    <div class="status-bar" id="sources"></div>
    <div class="controls">
        <label>Rating min: <input id="min-r" type="number" value="90" step="0.5" style="width: 70px;"></label>
        <label>Bookmaker: <select id="bookie"><option value="">Tutti</option></select></label>
        <label>Filtra Partita: <input id="search" placeholder="es. Milan..."></label>
        <span id="counter" style="color: #94a3b8; font-size: 12px; margin-left: auto;"></span>
    </div>
    <table>
        <thead>
            <tr>
                <th>Data</th>
                <th>Partita</th>
                <th>Esito</th>
                <th>Bookmaker</th>
                <th class="num">Punta</th>
                <th class="num">Banca (BF)</th>
                <th class="num">Liquidità</th>
                <th class="num">Rating</th>
                <th class="num">Banco</th>
                <th class="num">Responsabilità</th>
                <th class="num">Netto</th>
            </tr>
        </thead>
        <tbody id="grid"></tbody>
    </table>

    <script>
        let DATA = { results: [], sources: {} };
        async function fetchState() {
            try {
                const res = await fetch('/api/state');
                DATA = await res.json();
                render();
            } catch(e) {}
        }

        function render() {
            document.getElementById('last-update').innerText = "Ultimo calcolo: " + (DATA.last_calc ? new Date(DATA.last_calc).toLocaleTimeString() : "In attesa...");
            
            const srcDiv = document.getElementById('sources');
            srcDiv.innerHTML = Object.entries(DATA.sources).map(([k, v]) => 
                `<span class="badge" style="border-left: 3px solid ${v.startsWith('OK') ? 'var(--green)' : 'var(--red)'}">${k}: ${v}</span>`
            ).join('');

            const bmSel = document.getElementById('bookie');
            const currentBM = bmSel.value;
            const bms = [...new Set(DATA.results.map(r => r.bookmaker))].sort();
            if (bmSel.options.length !== bms.length + 1) {
                bmSel.innerHTML = '<option value="">Tutti</option>' + bms.map(b => `<option value="${b}">${b}</option>`).join('');
                bmSel.value = currentBM;
            }

            const minR = parseFloat(document.getElementById('min-r').value) || 0;
            const search = document.getElementById('search').value.toLowerCase();
            const filtered = DATA.results.filter(r => 
                r.rating >= minR && 
                (!currentBM || r.bookmaker === currentBM) &&
                (!search || r.match.toLowerCase().includes(search))
            );

            document.getElementById('counter').innerText = `${filtered.length} opportunità trovate`;

            const tbody = document.getElementById('grid');
            tbody.innerHTML = filtered.map(r => {
                const rClass = r.rating >= 96 ? 'r-high' : (r.rating >= 92 ? 'r-mid' : 'r-low');
                const badgeMatch = r.match_type === 'ID' 
                    ? '<span class="badge-id" title="Betradar ID Verificato">ID</span>' 
                    : `<span class="badge-fz" title="Fuzzy Name Match">${r.match_type}</span>`;

                return `<tr>
                    <td>${r.kickoff}</td>
                    <td><b>${r.match}</b>${badgeMatch}<div style="font-size:11px;color:#64748b;">${r.competition}</div></td>
                    <td><span class="tag">${r.outcome}</span></td>
                    <td>${r.bookmaker}</td>
                    <td class="num">${r.back.toFixed(2)}</td>
                    <td class="num">${r.lay.toFixed(2)}</td>
                    <td class="num">€${Math.round(r.liq)}</td>
                    <td class="num ${rClass}">${r.rating.toFixed(2)}%</td>
                    <td class="num">€${r.lay_stake.toFixed(2)}</td>
                    <td class="num">€${r.liability.toFixed(2)}</td>
                    <td class="num" style="color:${r.net >= 0 ? 'var(--green)' : '#94a3b8'}">${r.net >= 0 ? '+' : ''}€${r.net.toFixed(2)}</td>
                </tr>`;
            }).join('');
        }

        ['min-r', 'bookie', 'search'].forEach(id => document.getElementById(id).addEventListener('input', render));
        setInterval(fetchState, 3000);
        fetchState();
    </script>
</body>
</html>
"""


class EngineHTTPHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/api/state":
            with LOCK:
                payload = {
                    "results": GLOBAL_STATE["results"],
                    "sources": GLOBAL_STATE["sources_status"],
                    "last_calc": GLOBAL_STATE["last_calc"]
                }
            body = json.dumps(payload).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            body = DASHBOARD_HTML.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    def log_message(self, format, *args):
        pass


def main():
    parser = argparse.ArgumentParser(description="Oddsmatcher Live Engine")
    parser.add_argument("--port", type=int, default=8000, help="Porta web (default: 8000)")
    parser.add_argument("--stake", type=float, default=100.0, help="Stake punta standard (default: 100)")
    parser.add_argument("--mode", choices=["qualifying", "snr"], default="qualifying", help="Modalità calcolo")
    parser.add_argument("--min-liq", type=float, default=10.0, help="Liquidità minima banco in €")
    parser.add_argument("--no-crawl", action="store_true", help="Non avviare i thread di scraping (usa i file locali)")
    args = parser.parse_args()

    CONFIG["port"] = args.port
    CONFIG["stake"] = args.stake
    CONFIG["mode"] = args.mode
    CONFIG["min_liq"] = args.min_liq

    if os.path.exists("bookmakers_data.json"):
        try:
            with open("bookmakers_data.json", "r", encoding="utf-8") as f:
                d = json.load(f)
                now = datetime.now(timezone.utc)
                for ev in d.get("events", []):
                    bm = ev.get("bookmaker", "Bookie")
                    GLOBAL_STATE["bookmakers"].setdefault(bm, {"events": [], "updated_at": now})["events"].append(ev)
                for bm in GLOBAL_STATE["bookmakers"]:
                    GLOBAL_STATE["sources_status"][bm] = "Caricato da file locale"
        except Exception:
            pass

    if os.path.exists("betfair_exchange_data.json"):
        try:
            with open("betfair_exchange_data.json", "r", encoding="utf-8") as f:
                d = json.load(f)
                events = d.get("events", [])
                acq = d.get("acquired_at")
                dt_acq = datetime.fromisoformat(acq) if acq else datetime.now(timezone.utc)
                GLOBAL_STATE["betfair"] = {"events": events, "updated_at": dt_acq}
                GLOBAL_STATE["sources_status"]["Betfair Exchange"] = f"File locale ({len(events)} mercati)"
        except Exception:
            pass

    calculate_ratings()

    if not args.no_crawl:
        threading.Thread(target=run_bwin_worker, args=(30,), daemon=True).start()
        threading.Thread(target=run_snai_worker, args=(30,), daemon=True).start()
        threading.Thread(target=run_williamhill_worker, args=(30,), daemon=True).start()
        threading.Thread(target=run_betfair_worker, args=(60,), daemon=True).start()

    server = ThreadingHTTPServer(("127.0.0.1", args.port), EngineHTTPHandler)
    print("\n" + "=" * 70)
    print(f"[*] Oddsmatcher Engine attivo su: http://127.0.0.1:{args.port}")
    print(f"[*] Modalità: {args.mode.upper()} | Stake: €{args.stake} | Liq Minima: €{args.min_liq}")
    print(f"[*] Background crawlers: {'DISABILITATI' if args.no_crawl else 'ATTIVI'}")
    print("=" * 70 + "\n")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[*] Arresto motore...")


if __name__ == "__main__":
    main()