#!/usr/bin/env python3
# monitor_bybit_flask.py
# Bybit monitor — Flask + multi-chat Telegram + spike percentuali 1m, 3m, 1h

from __future__ import annotations
import os, time, threading
from datetime import datetime, timezone
import requests, ccxt
from flask import Flask

# ---------------- CONFIG ----------------
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_IDS = [cid.strip() for cid in os.getenv("TELEGRAM_CHAT_ID", "").split(",") if cid.strip()]
PORT = int(os.getenv("PORT", 10000))

FAST_TFS = os.getenv("FAST_TFS", "1m,3m").split(",")
SLOW_TFS = os.getenv("SLOW_TFS", "1h").split(",")
LOOP_DELAY = int(os.getenv("LOOP_DELAY", 10))
HEARTBEAT_INTERVAL_SEC = int(os.getenv("HEARTBEAT_INTERVAL_SEC", 3600))
MAX_CANDIDATES_PER_CYCLE = int(os.getenv("MAX_CANDIDATES_PER_CYCLE", 1000))
COOLDOWN_SECONDS = int(os.getenv("COOLDOWN_SECONDS", 180))

THRESHOLDS = {
    "1m": float(os.getenv("THRESH_1M", 7.5)),
    "3m": float(os.getenv("THRESH_3M", 11.5)),
    "1h": float(os.getenv("THRESH_1H", 17.0)),
}

exchange = ccxt.bybit({"options": {"defaultType": "swap"}})
last_prices, last_alert_time = {}, {}
app = Flask(__name__)

# ---------------- TELEGRAM ----------------
def send_telegram(text: str):
    if not TELEGRAM_TOKEN or not CHAT_IDS:
        print("❌ Telegram non configurato\n", text)
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    for cid in CHAT_IDS:
        try:
            requests.post(url, data={"chat_id": cid, "text": text, "parse_mode": "Markdown"}, timeout=10)
        except Exception as e:
            print("⚠️ Telegram error:", e)

# ---------------- BYBIT HELPERS ----------------
def safe_fetch_ohlcv(symbol, tf, limit=120):
    try:
        o = exchange.fetch_ohlcv(symbol, timeframe=tf, limit=limit)
        return [x[4] for x in o]
    except Exception as e:
        print(f"⚠️ fetch {symbol} {tf}: {e}")
        return None

def get_bybit_derivative_symbols():
    try:
        m = exchange.load_markets()
        return [s for s, x in m.items() if x.get("type") == "swap" or s.endswith(":USDT") or "/USDT" in s]
    except Exception as e:
        print("⚠️ load_markets:", e)
        return []

# ---------------- MONITOR LOOP ----------------
@app.route("/")
def home(): 
    return "✅ Crypto Alert Bot — running"

@app.route("/test")
def test(): 
    send_telegram("🧪 TEST OK")
    return "Test sent", 200

def monitor_loop():
    syms = get_bybit_derivative_symbols()
    print(f"✅ Simboli derivati trovati: {len(syms)}")
    last_hb = 0
    first = True

    while True:
        for s in syms[:MAX_CANDIDATES_PER_CYCLE]:
            for tf in FAST_TFS + SLOW_TFS:
                closes = safe_fetch_ohlcv(s, tf, 120)
                if not closes: 
                    continue

                price = closes[-1]
                key = f"{s}_{tf}"
                if first:
                    last_prices[key] = price
                    continue

                prev = last_prices.get(key, price)
                var = ((price - prev) / prev) * 100 if prev else 0
                th = THRESHOLDS.get(tf, 5)

                now = time.time()
                last_time = last_alert_time.get(key, 0)
                if abs(var) >= th and (now - last_time) >= COOLDOWN_SECONDS:
                    trend = "📈 Rialzo" if var > 0 else "📉 Crollo"
                    nowtxt = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
                    msg = (f"🔔 ALERT {s} ({tf}) {var:+.2f}%\n"
                           f"💰 Prezzo: {price:.6f}\n"
                           f"{trend}\n"
                           f"[{nowtxt}]")
                    send_telegram(msg)
                    last_alert_time[key] = now

                last_prices[key] = price

        first = False
        t = time.time()
        if t - last_hb >= HEARTBEAT_INTERVAL_SEC:
            send_telegram("💓 Heartbeat — bot attivo")
            last_hb = t

        time.sleep(LOOP_DELAY)

# ---------------- START ----------------
if __name__ == "__main__":
    threading.Thread(target=lambda: app.run(host="0.0.0.0", port=PORT), daemon=True).start()
    threading.Thread(target=monitor_loop, daemon=True).start()
    while True:
        time.sleep(3600)
