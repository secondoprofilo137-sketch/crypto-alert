#!/usr/bin/env python3
# monitor_bybit_flask.py
# Crypto Alert Bot — SOLO percentuale + analisi giornaliera Bybit

from __future__ import annotations
import os
import time
import threading
from datetime import datetime, timezone, timedelta
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

FAST_TFS = os.getenv("FAST_TFS", "1m,3m").split(",")
SLOW_TFS = os.getenv("SLOW_TFS", "1h").split(",")
LOOP_DELAY = int(os.getenv("LOOP_DELAY", "10"))
MAX_CANDIDATES_PER_CYCLE = int(os.getenv("MAX_CANDIDATES_PER_CYCLE", "1000"))

THRESH_1M = float(os.getenv("THRESH_1M", "7.5"))
THRESH_3M = float(os.getenv("THRESH_3M", "11.5"))
THRESH_1H = float(os.getenv("THRESH_1H", "17.0"))
COOLDOWN_SECONDS = int(os.getenv("COOLDOWN_SECONDS", "180"))
HEARTBEAT_INTERVAL = int(os.getenv("HEARTBEAT_INTERVAL", "3600"))

# Orario analisi giornaliera (default 23:59)
DAILY_ANALYSIS_HOUR = int(os.getenv("DAILY_ANALYSIS_HOUR", "23"))
DAILY_ANALYSIS_MINUTE = int(os.getenv("DAILY_ANALYSIS_MINUTE", "59"))

# -------------------------
# Exchange
# -------------------------
exchange = ccxt.bybit({
    "enableRateLimit": True,
    "options": {"defaultType": "swap"}
})

# -------------------------
# Telegram helper
# -------------------------
def send_telegram(text: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_IDS:
        print("Telegram NON configurato — messaggio:", text)
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
# Percent-change monitor
# -------------------------
last_prices = {}
last_alert_time = {}
last_hb = 0

TF_THRESH = {"1m": THRESH_1M, "3m": THRESH_3M, "1h": THRESH_1H}

def safe_fetch_ohlcv(symbol: str, timeframe: str, limit: int = 3):
    try:
        return exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
    except Exception:
        return None

def get_perpetual_symbols():
    try:
        markets = exchange.load_markets()
        return sorted([s for s in markets if s.endswith("/USDT")])
    except Exception as e:
        print("Errore caricamento simboli:", e)
        return []

def percent_monitor_loop():
    global last_hb
    symbols = get_perpetual_symbols()
    print(f"[percent] Loaded {len(symbols)} perpetual symbols")

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

        now = time.time()
        if now - last_hb >= HEARTBEAT_INTERVAL:
            send_telegram("💓 Heartbeat — bot attivo")
            last_hb = now

        time.sleep(LOOP_DELAY)

# -------------------------
# DAILY ANALYSIS
# -------------------------

last_daily_report_date = None

def perform_daily_analysis():
    global last_daily_report_date

    today = datetime.now().date()
    if last_daily_report_date == today:
        return  # già fatto oggi

    symbols = get_perpetual_symbols()

    results = []

    for s in symbols:
        try:
            ohlcv = exchange.fetch_ohlcv(s, timeframe="1h", limit=25)
            if len(ohlcv) < 25:
                continue

            closes = np.array([c[4] for c in ohlcv])
            vol_24h = exchange.fetch_ticker(s)["quoteVolume"]

            pct_24h = ((closes[-1] - closes[0]) / closes[0]) * 100
            volatility = (max(closes) - min(closes)) / closes[0] * 100

            ma_trend = closes[-1] - np.mean(closes[-6:])

            score = pct_24h * 0.5 + volatility * 0.3 + ma_trend * 0.2

            results.append((s, pct_24h, vol_24h, volatility, score))

        except Exception:
            continue

    if not results:
        return

    results.sort(key=lambda x: x[4], reverse=True)
    top = results[:10]

    report = "*📊 ANALISI GIORNALIERA — Top coin promettenti per domani*\n\n"
    for s, pct, vol, volat, score in top:
        report += (
            f"• *{s}*\n"
            f"  24h: {pct:+.2f}%\n"
            f"  Vol 24h: {vol:,.0f}\n"
            f"  Volatilità: {volat:.2f}%\n"
            f"  Score: {score:.2f}\n\n"
        )

    send_telegram(report)
    last_daily_report_date = today

def daily_analysis_loop():
    while True:
        now = datetime.now()
        if now.hour == DAILY_ANALYSIS_HOUR and now.minute == DAILY_ANALYSIS_MINUTE:
            perform_daily_analysis()
            time.sleep(60)
        time.sleep(10)

# -------------------------
# Flask
# -------------------------
app = Flask(__name__)

@app.route("/")
def home():
    return "Bybit monitor bot attivo"

# -------------------------
# Start
# -------------------------
if __name__ == "__main__":
    threading.Thread(target=percent_monitor_loop, daemon=True).start()
    threading.Thread(target=daily_analysis_loop, daemon=True).start()
    app.run(host="0.0.0.0", port=PORT)
