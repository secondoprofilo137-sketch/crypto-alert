#!/usr/bin/env python3
# crypto_alert_1m_1h.py — debug + 60s loop + full analysis logging

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
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", 60))
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
# Telegram (debug)
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
# Technical indicators
# -------------------------
def ema(values, period):
    if len(values) < period: return None
    k = 2 / (period + 1)
    ema_val = values[0]
    for v in values[1:]:
        ema_val = v * k + ema_val * (1 - k)
    return ema_val

def rsi(values, period):
    if len(values) < period + 1: return None
    deltas = [values[i + 1] - values[i] for i in range(len(values) - 1)]
    gains = [d for d in deltas if d > 0]
    losses = [-d for d in deltas if d < 0]
    avg_gain = mean(gains[-period:]) if gains else 0
    avg_loss = mean(losses[-period:]) if losses else 0
    if avg_loss == 0: return 100
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))

# -------------------------
# Bybit data
# -------------------------
def fetch_bybit_klines(symbol, limit=100):
    url = BYBIT_KLINE_URL_TPL.format(symbol=symbol, limit=limit)
    for attempt in range(MAX_RETRIES):
        try:
            r = requests.get(url, timeout=10)
            data = r.json()
            if "result" in data and "list" in data["result"]:
                candles = data["result"]["list"]
                closes = [float(c[4]) for c in candles[::-1]]
                vols = [float(c[5]) for c in candles[::-1]]
                return closes, vols
        except Exception as e:
            print(f"⚠️ Error fetching klines {symbol}: {e}")
        time.sleep(1)
    return [], []

# -------------------------
# Core analysis
# -------------------------
def analyze_symbol(symbol):
    closes, vols = fetch_bybit_klines(symbol, limit=50)
    if not closes or not vols:
        return None

    ema_val = ema(closes, EMA_PERIOD)
    rsi_val = rsi(closes, RSI_PERIOD)
    avg_vol = mean(vols[-BREAKOUT_LOOKBACK:])
    vol_spike = vols[-1] > avg_vol * VOLUME_SPIKE_FACTOR
    recent_high = max(closes[-BREAKOUT_LOOKBACK:])
    breakout = closes[-1] > recent_high * (1 + BREAKOUT_BUFFER)

    print(f"🔍 {symbol}: close={closes[-1]:.4f}, EMA={ema_val:.4f if ema_val else 0}, RSI={rsi_val:.2f if rsi_val else 0}, "
          f"spike={vol_spike}, breakout={breakout}")

    return {
        "symbol": symbol,
        "close": closes[-1],
        "ema": ema_val,
        "rsi": rsi_val,
        "vol_spike": vol_spike,
        "breakout": breakout
    }

# -------------------------
# Monitor loop
# -------------------------
def monitor_loop():
    print("▶️ Monitor loop started")
    while True:
        try:
            tickers = requests.get(BYBIT_TICKERS_URL, timeout=15).json()
            tickers = tickers.get("result", {}).get("list", [])
            print(f"📊 Retrieved {len(tickers)} symbols from Bybit")

            for t in tickers[:MAX_CANDIDATES_PER_CYCLE]:
                symbol = t["symbol"]
                result = analyze_symbol(symbol)
                if not result: continue

                if result["breakout"] or result["vol_spike"]:
                    msg = f"🚨 {symbol} breakout={result['breakout']} spike={result['vol_spike']} RSI={result['rsi']:.2f}"
                    send_telegram_message(msg)
                    print("📈 Alert sent:", msg)
                    time.sleep(1)
            save_state_to_gist()
        except Exception as e:
            print("⚠️ Monitor error:", e)
        time.sleep(CHECK_INTERVAL)

# -------------------------
# Heartbeat
# -------------------------
def heartbeat_worker():
    while True:
        now = datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
        send_telegram_message(f"💓 Heartbeat — bot alive at {now}")
        time.sleep(HEARTBEAT_INTERVAL_MIN * 60)

# -------------------------
# Entrypoint
# -------------------------
if __name__ == "__main__":
    print("🚀 Advanced Crypto Alert Bot starting (60s loop, full logic)")
    load_state_from_gist()
    threading.Thread(target=lambda: app.run(host="0.0.0.0", port=int(os.getenv("PORT", 10000))), daemon=True).start()
    if HEARTBEAT_ENABLED:
        threading.Thread(target=heartbeat_worker, daemon=True).start()
    monitor_loop()
