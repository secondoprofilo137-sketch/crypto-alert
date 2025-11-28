#!/usr/bin/env python3
# monitor_bybit_flask.py
# Crypto Bot — Percent Monitor + Ascensore SAFE + Heartbeat

from __future__ import annotations
import os
import time
import threading
from datetime import datetime, timezone
from dotenv import load_dotenv
from flask import Flask
import requests
import ccxt
import numpy as np

# -------------------------
# Load environment
# -------------------------
load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_IDS = [c.strip() for c in os.getenv("TELEGRAM_CHAT_ID", "").split(",") if c.strip()]

PORT = int(os.getenv("PORT", "10000"))

# Updated timeframes
FAST_TFS = ["5m"]
SLOW_TFS = ["15m"]

LOOP_DELAY = int(os.getenv("LOOP_DELAY", "10"))
MAX_CANDIDATES_PER_CYCLE = int(os.getenv("MAX_CANDIDATES_PER_CYCLE", "1000"))

THRESH_5M = float(os.getenv("THRESH_5M", "5.5"))
THRESH_15M = float(os.getenv("THRESH_15M", "9.0"))

COOLDOWN_SECONDS = int(os.getenv("COOLDOWN_SECONDS", "180"))
HEARTBEAT_INTERVAL = int(os.getenv("HEARTBEAT_INTERVAL", "3600"))

# ASCENSORE SAFE PARAMETERS
ASC_MIN_SPIKE_PCT = float(os.getenv("ASC_MIN_SPIKE_PCT", "2.0"))
ASC_MIN_VOLUME_USD = float(os.getenv("ASC_MIN_VOLUME_USD", "150000"))
ASC_MIN_LIQUIDITY_DEPTH = float(os.getenv("ASC_MIN_LIQUIDITY_DEPTH", "80000"))
ASC_DELTA_MIN = float(os.getenv("ASC_DELTA_MIN", "20000"))
ASC_COOLDOWN = int(os.getenv("ASC_COOLDOWN", "600"))
ASC_PER_SYMBOL_DELAY = float(os.getenv("ASC_PER_SYMBOL_DELAY", "0.05"))

# -------------------------
# Exchange
# -------------------------
exchange = ccxt.bybit({"enableRateLimit": True, "options": {"defaultType": "swap"}})

# -------------------------
# Telegram
# -------------------------
def send_telegram(text: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_IDS:
        print("Telegram non configurato —", text)
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    for cid in TELEGRAM_CHAT_IDS:
        try:
            requests.post(url, data={
                "chat_id": cid,
                "text": text,
                "parse_mode": "Markdown",
                "disable_web_page_preview": True
            }, timeout=10)
        except Exception as e:
            print("Errore invio Telegram:", e)

# -------------------------
# Percent monitor
# -------------------------
last_prices = {}
last_alert_time = {}
last_hb = 0

TF_THRESH = {"5m": THRESH_5M, "15m": THRESH_15M}

def safe_fetch_ohlcv(symbol: str, timeframe: str, limit: int = 3):
    try:
        return exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
    except Exception:
        return None

def get_perpetual_symbols():
    try:
        markets = exchange.load_markets()
        syms = [s for s in markets if s.endswith("/USDT")]
        return sorted(syms)
    except Exception as e:
        print("Errore caricamento simboli:", e)
        return []

def percent_monitor_loop():
    global last_hb
    symbols = get_perpetual_symbols()
    print(f"[percent] Loaded {len(symbols)} symbols")

    # Heartbeat immediato all’avvio
    send_telegram("💓 Heartbeat — bot avviato")

    while True:
        for s in symbols[:MAX_CANDIDATES_PER_CYCLE]:
            for tf in FAST_TFS + SLOW_TFS:

                ohlcv = safe_fetch_ohlcv(s, tf)
                if not ohlcv or len(ohlcv) < 2:
                    continue

                price = float(ohlcv[-1][4])
                key = f"{s}_{tf}"

                if key not in last_prices:
                    last_prices[key] = price
                    continue

                prev = last_prices[key]
                variation = ((price - prev) / (prev + 1e-12)) * 100
                threshold = TF_THRESH.get(tf, 999)

                if abs(variation) >= threshold:
                    nowt = time.time()
                    last_ts = last_alert_time.get(key, 0)
                    if nowt - last_ts >= COOLDOWN_SECONDS:
                        direction = "📈 LONG" if variation > 0 else "📉 SHORT"
                        nowtxt = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

                        msg = (
                            f"🔔 *ALERT {tf}*\n"
                            f"{s}\n"
                            f"Variazione: {variation:+.2f}%\n"
                            f"Prezzo: {price:.6f}\n"
                            f"{direction}\n"
                            f"[{nowtxt}]"
                        )

                        send_telegram(msg)
                        last_alert_time[key] = nowt

                last_prices[key] = price

        # Heartbeat ogni tot secondi
        now = time.time()
        if now - last_hb >= HEARTBEAT_INTERVAL:
            send_telegram("💓 Heartbeat — bot attivo")
            last_hb = now

        time.sleep(LOOP_DELAY)

# -------------------------
# Flask
# -------------------------
app = Flask(__name__)

@app.route("/")
def home():
    return "Bot attivo"

# -------------------------
# Thread avvio
# -------------------------
def start_threads():
    t1 = threading.Thread(target=percent_monitor_loop, daemon=True)
    t1.start()

start_threads()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)
