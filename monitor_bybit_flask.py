#!/usr/bin/env python3
# monitor_bybit_flask.py
# Crypto Alert Bot — Percent monitor + ASCENSORE SAFE (equilibrato) + Heartbeat

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


# ===============================
# Load environment
# ===============================
load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_IDS = [c.strip() for c in os.getenv("TELEGRAM_CHAT_ID", "").split(",") if c.strip()]

PORT = int(os.getenv("PORT", "10000"))

FAST_TFS = os.getenv("FAST_TFS", "1m,3m").split(",")
SLOW_TFS = os.getenv("SLOW_TFS", "1h").split(",")
LOOP_DELAY = int(os.getenv("LOOP_DELAY", "10"))
MAX_CANDIDATES_PER_CYCLE = int(os.getenv("MAX_CANDIDATES_PER_CYCLE", "250"))

THRESH_1M = float(os.getenv("THRESH_1M", "7.5"))
THRESH_3M = float(os.getenv("THRESH_3M", "11.5"))
THRESH_1H = float(os.getenv("THRESH_1H", "17.0"))
COOLDOWN_SECONDS = int(os.getenv("COOLDOWN_SECONDS", "180"))
HEARTBEAT_INTERVAL = int(os.getenv("HEARTBEAT_INTERVAL", "3600"))

# ASCENSORE SAFE equilibrato
ASC_MIN_SPIKE_PCT = float(os.getenv("ASC_MIN_SPIKE_PCT", "2.0"))
ASC_MIN_VOLUME_USD = float(os.getenv("ASC_MIN_VOLUME_USD", "150000"))
ASC_MIN_LIQUIDITY_DEPTH = float(os.getenv("ASC_MIN_LIQUIDITY_DEPTH", "80000"))
ASC_DELTA_MIN = float(os.getenv("ASC_DELTA_MIN", "20000"))
ASC_COOLDOWN = int(os.getenv("ASC_COOLDOWN", "600"))
ASC_PER_SYMBOL_DELAY = float(os.getenv("ASC_PER_SYMBOL_DELAY", "0.05"))


# ===============================
# Exchange setup
# ===============================
exchange = ccxt.bybit({
    "enableRateLimit": True,
    "options": {"defaultType": "swap"}
})


# ===============================
# Telegram
# ===============================
def send_telegram(text: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_IDS:
        print("Telegram non configurato — messaggio:", text)
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    for cid in TELEGRAM_CHAT_IDS:
        try:
            requests.post(
                url,
                data={
                    "chat_id": cid,
                    "text": text,
                    "parse_mode": "Markdown",
                    "disable_web_page_preview": True,
                },
                timeout=10,
            )
        except Exception as e:
            print("Errore invio Telegram:", e)


# ===============================
# Helpers
# ===============================
def safe_fetch_ohlcv(symbol, timeframe, limit=3):
    try:
        return exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
    except Exception:
        return None


def safe_fetch_orderbook(symbol):
    try:
        return exchange.fetch_order_book(symbol, limit=25)
    except Exception:
        return None


def safe_fetch_ticker(symbol):
    try:
        return exchange.fetch_ticker(symbol)
    except Exception:
        return None


def get_perpetual_symbols():
    try:
        markets = exchange.load_markets()
        return sorted([s for s in markets if s.endswith("/USDT")])
    except Exception as e:
        print("Errore caricamento simboli:", e)
        return []


# ===============================
# Percent monitor
# ===============================
last_prices = {}
last_alert_time = {}
last_hb = 0

TF_THRESH = {
    "1m": THRESH_1M,
    "3m": THRESH_3M,
    "1h": THRESH_1H,
}


def percent_monitor_loop():
    global last_hb

    symbols = get_perpetual_symbols()
    print(f"[percent] Loaded {len(symbols)} perpetual symbols")

    while True:
        for s in symbols[:MAX_CANDIDATES_PER_CYCLE]:

            for tf in (FAST_TFS + SLOW_TFS):
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
                        direction = "📈 Rialzo" if variation > 0 else "📉 Ribasso"
                        nowtxt = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

                        msg = (
                            f"🔔 *ALERT* — {s} ({tf})\n"
                            f"Variazione: {variation:+.2f}%\n"
                            f"Prezzo: {price:.6f}\n"
                            f"{direction}\n"
                            f"[{nowtxt}]"
                        )

                        send_telegram(msg)
                        last_alert_time[key] = nowt

                last_prices[key] = price

        # Heartbeat
        now = time.time()
        if now - last_hb >= HEARTBEAT_INTERVAL:
            send_telegram("💓 Heartbeat — bot attivo")
            last_hb = now

        time.sleep(LOOP_DELAY)


# ===============================
# ASCENSORE SAFE equilibrato
# ===============================
last_asc_alert = {}

def ascensore_safe_loop():

    symbols = get_perpetual_symbols()
    print(f"[ascensore] Loaded {len(symbols)} perpetual symbols")

    while True:
        for s in symbols:

            # Spike check
            ohlcv = safe_fetch_ohlcv(s, "1m")
            if not ohlcv or len(ohlcv) < 2:
                continue

            price_prev = float(ohlcv[-2][4])
            price_now = float(ohlcv[-1][4])
            spike_pct = ((price_now - price_prev) / (price_prev + 1e-12)) * 100

            if abs(spike_pct) < ASC_MIN_SPIKE_PCT:
                continue

            # Volume check
            ticker = safe_fetch_ticker(s)
            if not ticker or "quoteVolume" not in ticker:
                continue

            vol_usd = float(ticker["quoteVolume"])
            if vol_usd < ASC_MIN_VOLUME_USD:
                continue

            # Liquidity check
            ob = safe_fetch_orderbook(s)
            if not ob or "bids" not in ob or "asks" not in ob:
                continue

            bid_depth = sum([b[1] for b in ob["bids"][:5]]) if ob["bids"] else 0
            ask_depth = sum([a[1] for a in ob["asks"][:5]]) if ob["asks"] else 0
            liquidity = (bid_depth + ask_depth) * price_now

            if liquidity < ASC_MIN_LIQUIDITY_DEPTH:
                continue

            # Delta minimal
            delta = abs(bid_depth - ask_depth) * price_now
            if delta < ASC_DELTA_MIN:
                continue

            nowt = time.time()
            last_ts = last_asc_alert.get(s, 0)

            if nowt - last_ts >= ASC_COOLDOWN:

                direction = "📈 UP" if spike_pct > 0 else "📉 DOWN"
                nowtxt = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

                msg = (
                    f"🚨 *ASCENSORE SAFE* — {s}\n"
                    f"Spike: {spike_pct:+.2f}%\n"
                    f"Volume USD: {vol_usd:.0f}\n"
                    f"Liquidity: {liquidity:.0f}\n"
                    f"Delta: {delta:.0f}\n"
                    f"{direction}\n"
                    f"[{nowtxt}]"
                )

                send_telegram(msg)
                last_asc_alert[s] = nowt

            time.sleep(ASC_PER_SYMBOL_DELAY)

        time.sleep(1)


# ===============================
# Flask
# ===============================
app = Flask(__name__)

@app.route("/")
def home():
    return "Bot attivo"


# ===============================
# Start threads
# ===============================
def start_threads():
    threading.Thread(target=percent_monitor_loop, daemon=True).start()
    threading.Thread(target=ascensore_safe_loop, daemon=True).start()


if __name__ == "__main__":
    start_threads()
    app.run(host="0.0.0.0", port=PORT)
