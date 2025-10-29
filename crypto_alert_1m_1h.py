#!/usr/bin/env python3
# crypto_alert_1m_1h.py
# Advanced crypto alert (10s loop) — Bybit linear derivatives
# Persist state on GitHub Gist, keepalive via Flask, Telegram alerts.

import os
import time
import json
import math
import requests
import datetime
import threading
from statistics import mean, pstdev
from flask import Flask

# -------------------------
# Configuration (secure)
# -------------------------
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID        = os.getenv("CHAT_ID")
GITHUB_TOKEN   = os.getenv("GITHUB_TOKEN")
GIST_ID        = os.getenv("GIST_ID")

PRICE_CHANGE_THRESHOLD = 5.0      # percent
CHECK_INTERVAL = 10               # seconds (fast check)
MAX_RETRIES = 5

VOLUME_SPIKE_FACTOR = 1.5
RSI_PERIOD = 14
EMA_PERIOD = 20
BREAKOUT_LOOKBACK = 15
BREAKOUT_BUFFER = 0.002

BYBIT_TICKERS_URL = "https://api.bybit.com/v5/market/tickers?category=linear"
BYBIT_KLINE_URL_TPL = "https://api.bybit.com/v5/market/kline?symbol={symbol}&interval=1&category=linear&limit=50"

MAX_CANDIDATES_PER_CYCLE = 200

# -------------------------
# State
# -------------------------
last_prices = {}
last_alerts = {}
last_volumes = {}
kline_cache = {}
uptime_start = time.time()

# -------------------------
# Flask keepalive
# -------------------------
app = Flask(__name__)

@app.route("/")
def home():
    return "✅ Crypto Alert Bot (advanced) is running!"

@app.route("/test")
def test_alert():
    now = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
    send_telegram_message(f"🧪 TEST ALERT - bot online ({now})")
    return "Test sent", 200

# -------------------------
# Gist persistence
# -------------------------
GIST_API = "https://api.github.com/gists"

def load_state_from_gist():
    global last_prices, last_alerts, last_volumes, kline_cache
    if not GITHUB_TOKEN or not GIST_ID:
        print("⚠️ Gist not configured — using in-memory state.")
        return
    try:
        headers = {"Authorization": f"token {GITHUB_TOKEN}"}
        r = requests.get(f"{GIST_API}/{GIST_ID}", headers=headers, timeout=15)
        r.raise_for_status()
        gist = r.json()
        content = gist.get("files", {}).get("state.json", {}).get("content", "{}")
        data = json.loads(content)
        last_prices = data.get("last_prices", {})
        last_alerts = data.get("last_alerts", {})
        last_volumes = data.get("last_volumes", {})
        kline_cache = data.get("kline_cache", {})
        print("✅ State loaded from Gist.")
    except Exception as e:
        print("⚠️ Failed to load state from Gist:", e)

def save_state_to_gist():
    if not GITHUB_TOKEN or not GIST_ID:
        return
    try:
        payload = {
            "files": {
                "state.json": {"content": json.dumps({
                    "last_prices": last_prices,
                    "last_alerts": last_alerts,
                    "last_volumes": last_volumes,
                    "kline_cache": kline_cache,
                    "uptime_start": uptime_start
                }, indent=2)}
            }
        }
        headers = {"Authorization": f"token {GITHUB_TOKEN}"}
        r = requests.patch(f"{GIST_API}/{GIST_ID}", headers=headers, json=payload, timeout=15)
        r.raise_for_status()
        print("💾 State saved to Gist.")
    except Exception as e:
        print("⚠️ Failed to save state to Gist:", e)

# -------------------------
# Telegram
# -------------------------
def send_telegram_message(message: str):
    if not TELEGRAM_TOKEN or not CHAT_ID:
        print("⚠️ Telegram not configured (missing TELEGRAM_TOKEN/CHAT_ID).")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": message}
    try:
        r = requests.post(url, data=payload, timeout=10)
        if r.status_code != 200:
            print("❌ Telegram send error:", r.status_code, r.text)
    except Exception as e:
        print("⚠️ Telegram exception:", e)

# -------------------------
# (Indicatori, RSI, EMA, ecc. — invariati)
# -------------------------
# Tutto il resto del codice è identico alla versione che hai già.
# 👇
# Incolla qui tutto il resto del codice senza modificare nulla
# (cioè la parte da “def compute_rsi(...):” fino alla fine)

# -------------------------
# Main loop
# -------------------------
if __name__ == "__main__":
    print("🚀 Advanced Crypto Alert Bot starting (secure version, env vars)")
    load_state_from_gist()
    threading.Thread(target=lambda: app.run(host="0.0.0.0", port=int(os.getenv("PORT", 10000))), daemon=True).start()
    start_monitoring()

