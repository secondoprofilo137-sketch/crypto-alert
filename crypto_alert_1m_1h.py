#!/usr/bin/env python3
# crypto_alert_1m_1h.py (debug + 60s loop + datetime fix)

import os
import time
import json
import math
import requests
import datetime
import threading
import signal
from statistics import mean, pstdev
from flask import Flask
try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

# -------------------------
# CONFIG
# -------------------------
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
GIST_ID = os.getenv("GIST_ID")

PRICE_CHANGE_THRESHOLD = float(os.getenv("PRICE_CHANGE_THRESHOLD", 5.0))
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", 60))  # ⏱️ was 10 → now 60 sec
MAX_RETRIES = int(os.getenv("MAX_RETRIES", 5))
MAX_CANDIDATES_PER_CYCLE = int(os.getenv("MAX_CANDIDATES", 200))

VOLUME_SPIKE_FACTOR = float(os.getenv("VOLUME_SPIKE_FACTOR", 1.5))
RSI_PERIOD = int(os.getenv("RSI_PERIOD", 14))
EMA_PERIOD = int(os.getenv("EMA_PERIOD", 20))
BREAKOUT_LOOKBACK = int(os.getenv("BREAKOUT_LOOKBACK", 15))
BREAKOUT_BUFFER = float(os.getenv("BREAKOUT_BUFFER", 0.002))

HEARTBEAT_ENABLED = os.getenv("HEARTBEAT_ENABLED", "true").lower() in ("1", "true", "yes")
HEARTBEAT_INTERVAL_MIN = int(os.getenv("HEARTBEAT_INTERVAL_MIN", 60))

BYBIT_TICKERS_URL = "https://api.bybit.com/v5/market/tickers?category=linear"
BYBIT_KLINE_URL_TPL = "https://api.bybit.com/v5/market/kline?symbol={symbol}&interval=1&category=linear&limit={limit}"
GIST_API = "https://api.github.com/gists"

# -------------------------
# State
# -------------------------
last_prices, last_alerts, last_volumes, kline_cache = {}, {}, {}, {}
uptime_start = time.time()
_state_lock = threading.Lock()

# -------------------------
# Flask keepalive
# -------------------------
app = Flask(__name__)

@app.route("/")
def home():
    return "✅ Crypto Alert Bot — running"

@app.route("/test")
def test_alert():
    now = datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
    send_telegram_message(f"🧪 TEST ALERT - bot online ({now})")
    return "Test sent", 200

# -------------------------
# Gist persistence
# -------------------------
def load_state_from_gist():
    global last_prices, last_alerts, last_volumes, kline_cache
    if not GITHUB_TOKEN or not GIST_ID:
        print("⚠️ Gist not configured — using in-memory state.")
        return
    try:
        headers = {"Authorization": f"token {GITHUB_TOKEN}"}
        r = requests.get(f"{GIST_API}/{GIST_ID}", headers=headers, timeout=15)
        r.raise_for_status()
        content = r.json().get("files", {}).get("state.json", {}).get("content", "{}")
        data = json.loads(content)
        with _state_lock:
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
        with _state_lock:
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
# Telegram (with debug)
# -------------------------
def send_telegram_message(message: str):
    if not TELEGRAM_TOKEN or not CHAT_ID:
        print("⚠️ Telegram not configured (missing TELEGRAM_TOKEN/CHAT_ID). Message:\n", message)
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": message}
    print(f"📤 Sending Telegram message ({len(message)} chars)...")
    try:
        r = requests.post(url, data=payload, timeout=10)
        print(f"✅ Telegram response {r.status_code}: {r.text[:200]}...")
        if r.status_code != 200:
            print("❌ Telegram send error:", r.status_code, r.text)
    except Exception as e:
        print("⚠️ Telegram exception:", e)

# -------------------------
# (rest of your functions remain identical)
# -------------------------

# The rest of your code (indicators, Bybit, risk, alerts, monitor_loop, heartbeat, signal, main)
# stay *exactly the same* — no logic changed except datetime + CHECK_INTERVAL + Telegram debug.

# -------------------------
# Entrypoint
# -------------------------
if __name__ == "__main__":
    print("🚀 Advanced Crypto Alert Bot starting (60s loop, debug Telegram enabled)")
    load_state_from_gist()
    threading.Thread(target=lambda: app.run(host="0.0.0.0", port=int(os.getenv("PORT", 10000))), daemon=True).start()
    if HEARTBEAT_ENABLED:
        threading.Thread(target=heartbeat_worker, daemon=True).start()
    monitor_loop()
