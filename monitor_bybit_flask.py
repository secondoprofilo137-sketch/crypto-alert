#!/usr/bin/env python3
# monitor_bybit_flask.py
# Bybit monitor — SOLO ALERT PERCENTUALI (1m, 3m, 1h)

from __future__ import annotations
import os
import time
import threading
from datetime import datetime, timezone
import requests
import ccxt
from flask import Flask

# -----------------------
# CONFIG
# -----------------------
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_IDS = [cid.strip() for cid in os.getenv("TELEGRAM_CHAT_ID", "").split(",") if cid.strip()]
PORT = int(os.getenv("PORT", 10000))

FAST_TFS = os.getenv("FAST_TFS", "1m,3m").split(",")
SLOW_TFS = os.getenv("SLOW_TFS", "1h").split(",")
LOOP_DELAY = int(os.getenv("LOOP_DELAY", 10))
MAX_CANDIDATES_PER_CYCLE = int(os.getenv("MAX_CANDIDATES_PER_CYCLE", 1000))

THRESHOLDS = {
    "1m": float(os.getenv("THRESH_1M", 7.5)),
    "3m": float(os.getenv("THRESH_3M", 11.5)),
    "1h": float(os.getenv("THRESH_1H", 17.0)),
}

COOLDOWN_SECONDS = int(os.getenv("COOLDOWN_SECONDS", 180))
HEARTBEAT_INTERVAL = int(os.getenv("HEARTBEAT_INTERVAL", 3600))

# -----------------------
# EXCHANGE
# -----------------------
exchange = ccxt.bybit({
    "options": {"defaultType": "swap"}
})

last_prices = {}
last_alert_time = {}

# -----------------------
# TELEGRAM
# -----------------------
def send_telegram(text: str):
    if not TELEGRAM_TOKEN or not CHAT_IDS:
        print("No Telegram configured:", text)
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    for cid in CHAT_IDS:
        try:
            requests.post(url, data={
                "chat_id": cid,
                "text": text,
                "parse_mode": "Markdown",
                "disable_web_page_preview": True
            }, timeout=10)
        except Exception as e:
            print("Telegram error:", e)

# -----------------------
# HELPERS
# -----------------------
def safe_fetch_ohlcv(symbol, tf, limit=120):
    try:
        data = exchange.fetch_ohlcv(symbol, timeframe=tf, limit=limit)
        closes = [c[4] for c in data]
        return closes
    except:
        return None

def get_symbols():
    try:
        m = exchange.load_markets()
        out = []
        for s, info in m.items():
            if info.get("type") == "swap" or s.endswith(":USDT") or "/USDT" in s:
                out.append(s)
        return sorted(set(out))
    except:
        return []

# -----------------------
# FLASK
# -----------------------
app = Flask(__name__)

@app.route("/")
def home():
    return "Crypto Alert Bot — Running"

@app.route("/test")
def test():
    send_telegram("🧪 Test OK")
    return "Test sent", 200

# -----------------------
# MAIN LOOP
# -----------------------
def monitor_loop():
    symbols = get_symbols()
    print(f"Loaded {len(symbols)} derivative symbols from Bybit")

    last_hb = 0
    first = True

    while True:
        for s in symbols[:MAX_CANDIDATES_PER_CYCLE]:
            for tf in FAST_TFS + SLOW_TFS:

                closes = safe_fetch_ohlcv(s, tf)
                if not closes:
                    continue

                price = closes[-1]
                key = f"{s}_{tf}"

                # init
                if key not in last_prices:
                    last_prices[key] = price
                    continue

                prev = last_prices[key]
                variation = ((price - prev) / prev) * 100 if prev else 0

                threshold = THRESHOLDS.get(tf, 999)

                if abs(variation) >= threshold:
                    now = time.time()
                    last_ts = last_alert_time.get(key, 0)

                    # cooldown
                    if now - last_ts >= COOLDOWN_SECONDS:
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
                        last_alert_time[key] = now

                last_prices[key] = price

        # heartbeat
        now = time.time()
        if now - last_hb >= HEARTBEAT_INTERVAL:
            send_telegram("💓 Heartbeat — bot attivo")
            last_hb = now

        time.sleep(LOOP_DELAY)

# -----------------------
# START
# -----------------------
if __name__ == "__main__":
    print("🚀 Starting monitor (clean version)…")
    threading.Thread(target=lambda: app.run(host="0.0.0.0", port=PORT), daemon=True).start()
    threading.Thread(target=monitor_loop, daemon=True).start()

    while True:
        time.sleep(3600)
