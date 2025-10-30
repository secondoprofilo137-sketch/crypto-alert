#!/usr/bin/env python3
# monitor_service.py
# Bybit derivatives monitor (1m, 3m, 1h) + Flask status (italiano) + Telegram alerts
# Usa ccxt, Flask, requests. Pensato per Render as web service.

import os
import time
import threading
import requests
import ccxt
from datetime import datetime, timezone
from flask import Flask, jsonify

# -------------------------
# CONFIG (ENV)
# -------------------------
BOT_TOKEN = os.getenv("TELEGRAM_TOKEN") or os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID") or os.getenv("TELEGRAM_CHAT_ID")

LOOP_DELAY = int(os.getenv("LOOP_DELAY", 10))          # secondi tra i cicli veloci
FAST_TFS = os.getenv("FAST_TFS", "1m,3m").split(",")  # es. "1m,3m"
SLOW_TFS = os.getenv("SLOW_TFS", "1h").split(",")     # es. "1h"
FAST_THRESHOLD = float(os.getenv("FAST_THRESHOLD", 5.0))
SLOW_THRESHOLD = float(os.getenv("SLOW_THRESHOLD", 3.0))

REFRESH_MARKETS_EVERY = int(os.getenv("REFRESH_MARKETS_EVERY", 3600))  # secondi
HEARTBEAT_INTERVAL_SEC = int(os.getenv("HEARTBEAT_INTERVAL_SEC", 3600))  # invio stato via telegram (opzionale)

PORT = int(os.getenv("PORT", 10000))

# -------------------------
# STATE (condiviso)
# -------------------------
last_cycle_time = None
symbols_list = []
symbols_count = 0
previous_prices = {}   # key -> last price seen
last_markets_refresh = 0

_state_lock = threading.Lock()

# -------------------------
# Inizializza exchange (Bybit derivatives)
# -------------------------
exchange = ccxt.bybit({"options": {"defaultType": "swap"}})

# -------------------------
# Flask web app (Italiano)
# -------------------------
app = Flask(__name__)

@app.route("/")
def home():
    with _state_lock:
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        return (
            f"🟢 Bot attivo — simboli analizzati: {symbols_count} — timeframe: {', '.join(FAST_TFS + SLOW_TFS)}"
            f" — ultimo ciclo: {last_cycle_time or 'mai'} (UTC)\n"
            f"Ultimo aggiornamento mercati: {datetime.fromtimestamp(last_markets_refresh, timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC') if last_markets_refresh else 'mai'}"
        )

@app.route("/status")
def status():
    with _state_lock:
        return jsonify({
            "status": "online",
            "symbols": symbols_count,
            "timeframes": FAST_TFS + SLOW_TFS,
            "last_cycle": last_cycle_time,
            "last_markets_refresh": datetime.fromtimestamp(last_markets_refresh, timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC") if last_markets_refresh else None,
        })

# -------------------------
# Telegram helper
# -------------------------
def send_telegram(message: str):
    if not BOT_TOKEN or not CHAT_ID:
        print("❌ Errore Telegram: TOKEN o CHAT_ID mancanti. Messaggio non inviato.")
        return False
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": message, "parse_mode": "Markdown"}
    try:
        r = requests.post(url, data=payload, timeout=15)
        if r.status_code != 200:
            print("❌ Errore Telegram", r.status_code, r.text[:500])
            return False
        return True
    except Exception as e:
        print("❌ Eccezione invio Telegram:", e)
        return False

# -------------------------
# Bybit helpers
# -------------------------
def refresh_symbols():
    global symbols_list, symbols_count, last_markets_refresh
    try:
        markets = exchange.load_markets()
        # filtriamo i simboli in stile derivati USDT: es. "BTC/USDT:USDT" o "BTC/USDT"
        pairs = []
        for s in markets:
            if isinstance(s, str) and (s.endswith("/USDT:USDT") or s.endswith("/USDT")):
                pairs.append(s)
        with _state_lock:
            symbols_list = sorted(list(set(pairs)))
            symbols_count = len(symbols_list)
            last_markets_refresh = int(time.time())
        print(f"✅ Aggiornati simboli: {symbols_count}")
    except Exception as e:
        print("⚠️ Errore refresh_symbols:", e)

def fetch_last_price(symbol, timeframe):
    try:
        ohlcv = exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=2)
        if not ohlcv or len(ohlcv) == 0:
            return None
        return float(ohlcv[-1][4])
    except Exception as e:
        # non loggare troppo verbosamente ma mostrare errore
        print(f"⚠️ Errore fetch_price {symbol} {timeframe}: {e}")
        return None

# -------------------------
# Logica alert
# -------------------------
def analyze_symbol_tf(symbol, timeframe, threshold):
    key = f"{symbol}__{timeframe}"
    price = fetch_last_price(symbol, timeframe)
    if price is None:
        return None
    old = previous_prices.get(key, price)
    if old == 0:
        pct = 0.0
    else:
        pct = ((price - old) / old) * 100.0
    # aggiornare la memoria prima di inviare per evitare duplicati in loop stretti
    previous_prices[key] = price
    if abs(pct) >= threshold:
        direzione = "rialzo" if pct > 0 else "crollo"
        emoji = "🔥" if timeframe in FAST_TFS else "⚡"
        icon = "📈" if pct > 0 else "📉"
        message = (
            f"🔔 ALERT -> {emoji} *{symbol}* — variazione {pct:+.2f}% (ref {timeframe})\n"
            f"Prezzo attuale: {price:.6f}\n"
            f"{icon} {direzione.capitalize()} rilevato — attenzione al trend.\n"
            f"[{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}]"
        )
        print(message)
        ok = send_telegram(message)
        return {"symbol": symbol, "tf": timeframe, "pct": pct, "sent": ok}
    return None

# -------------------------
# Monitor loop (thread)
# -------------------------
def monitor_loop():
    global last_cycle_time
    refresh_symbols()
    last_markets_check = time.time()
    cycle = 0
    while True:
        t0 = time.time()
        cycle += 1
        # refresh mercati periodicamente
        if time.time() - last_markets_check > REFRESH_MARKETS_EVERY:
            refresh_symbols()
            last_markets_check = time.time()

        sent_any = 0
        with _state_lock:
            syms = list(symbols_list)

        for sym in syms:
            # fast timeframes
            for tf in FAST_TFS:
                try:
                    res = analyze_symbol_tf(sym, tf, FAST_THRESHOLD)
                    if res and res.get("sent"):
                        sent_any += 1
                except Exception as e:
                    print("⚠️ Errore analyze fast:", e)
            # slow timeframes eseguite meno frequentemente: solo se il ciclo è multiplo di (HEARTBEAT or configurable)
            # per semplicità eseguiamo slow quando cycle % (HEARTBEAT_INTERVAL_SEC/LOOP_DELAY) == 0
            if HEARTBEAT_INTERVAL_SEC and (cycle * LOOP_DELAY) % HEARTBEAT_INTERVAL_SEC == 0:
                for tf in SLOW_TFS:
                    try:
                        res = analyze_symbol_tf(sym, tf, SLOW_THRESHOLD)
                        if res and res.get("sent"):
                            sent_any += 1
                    except Exception as e:
                        print("⚠️ Errore analyze slow:", e)

        with _state_lock:
            last_cycle_time = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

        print(f"[{last_cycle_time}] Ciclo completato — simboli: {len(syms)} — alert inviati questo ciclo: {sent_any}")
        # sleep fino al prossimo ciclo
        elapsed = time.time() - t0
        to_sleep = max(0.1, LOOP_DELAY - elapsed)
        time.sleep(to_sleep)

# -------------------------
# Heartbeat worker (opzionale)
# -------------------------
def heartbeat_worker():
    if not BOT_TOKEN or not CHAT_ID:
        print("ℹ️ Heartbeat disabilitato: manca BOT_TOKEN/CHAT_ID.")
        return
    while True:
        time.sleep(HEARTBEAT_INTERVAL_SEC)
        with _state_lock:
            now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
            msg = (
                f"💓 Heartbeat — bot attivo\n"
                f"Simboli analizzati: {symbols_count}\n"
                f"Timeframe: {', '.join(FAST_TFS + SLOW_TFS)}\n"
                f"Ultimo ciclo: {last_cycle_time}"
            )
        send_telegram(msg)

# -------------------------
# Entrypoint
# -------------------------
if __name__ == "__main__":
    print("🚀 Avvio Bybit Derivatives Monitor — timeframes:", FAST_TFS + SLOW_TFS, " — loop:", LOOP_DELAY, "s")
    # avvia Flask in thread
    flask_thread = threading.Thread(target=lambda: app.run(host="0.0.0.0", port=PORT), daemon=True)
    flask_thread.start()

    # avvia monitor in thread (non-blocking)
    monitor_thread = threading.Thread(target=monitor_loop, daemon=True)
    monitor_thread.start()

    # avvia heartbeat opzionale
    hb_thread = threading.Thread(target=heartbeat_worker, daemon=True)
    hb_thread.start()

    # main thread: solo log e keepalive
    try:
        while True:
            time.sleep(60)
    except KeyboardInterrupt:
        print("🔔 Terminazione richiesta, uscita...")
        raise SystemExit(0)
