#!/usr/bin/env python3
# crypto_alert_1m_1h.py
# Advanced crypto alert (10s loop) — Bybit linear derivatives
# Persist state on GitHub Gist, keepalive via Flask, Telegram alerts.
# Requires: requests, Flask, python-dotenv (optional for local .env)

import os
import time
import json
import math
import requests
import datetime
import threading
from statistics import mean, pstdev
from flask import Flask
from dotenv import load_dotenv

# -------------------------
# Load .env when running locally (optional)
# -------------------------
load_dotenv()

# -------------------------
# Configuration (from env)
# -------------------------
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
GIST_ID = os.getenv("GIST_ID")

PRICE_CHANGE_THRESHOLD = float(os.getenv("PRICE_CHANGE_THRESHOLD", 5.0))
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", 10))
MAX_RETRIES = int(os.getenv("MAX_RETRIES", 5))
VOLUME_SPIKE_FACTOR = float(os.getenv("VOLUME_SPIKE_FACTOR", 1.5))
RSI_PERIOD = int(os.getenv("RSI_PERIOD", 14))
EMA_PERIOD = int(os.getenv("EMA_PERIOD", 20))
BREAKOUT_LOOKBACK = int(os.getenv("BREAKOUT_LOOKBACK", 15))
BREAKOUT_BUFFER = float(os.getenv("BREAKOUT_BUFFER", 0.002))
MAX_CANDIDATES_PER_CYCLE = int(os.getenv("MAX_CANDIDATES", 200))

BYBIT_TICKERS_URL = "https://api.bybit.com/v5/market/tickers?category=linear"
BYBIT_KLINE_URL_TPL = "https://api.bybit.com/v5/market/kline?symbol={symbol}&interval=1&category=linear&limit=50"

VOL_TIER_THRESHOLDS = {"low": 0.002, "medium": 0.006, "high": 0.012}
RISK_MAPPING = {
    "low":     {"leverage": "x2", "target_pct": (0.8, 1.5),  "stop_pct": (-0.6, -1.0)},
    "medium":  {"leverage": "x3", "target_pct": (1.5, 3.0),  "stop_pct": (-1.0, -2.0)},
    "high":    {"leverage": "x4", "target_pct": (3.0, 6.0),  "stop_pct": (-2.0, -4.0)},
    "very":    {"leverage": "x6", "target_pct": (6.0, 12.0), "stop_pct": (-4.0, -8.0)},
}

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
    try:
        now = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
        send_telegram_message(f"🧪 TEST ALERT - bot online ({now})")
        return "Test sent", 200
    except Exception as e:
        return f"Error: {e}", 500

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
# Indicators
# -------------------------
def compute_rsi(closes, period=14):
    if len(closes) < period + 1:
        return None
    gains, losses = [], []
    for i in range(1, len(closes)):
        change = closes[i] - closes[i-1]
        gains.append(max(change,0))
        losses.append(max(-change,0))
    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))

def compute_ema(closes, period=20):
    if not closes or len(closes) < 2:
        return None
    k = 2 / (period + 1)
    ema = closes[0]
    for price in closes[1:]:
        ema = price * k + (1 - k) * ema
    return ema

def compute_volatility_logreturns(closes):
    if len(closes) < 3:
        return 0.0
    lr = []
    for i in range(1, len(closes)):
        if closes[i-1] > 0:
            lr.append(math.log(closes[i] / closes[i-1]))
    if len(lr) < 2:
        return 0.0
    try:
        return pstdev(lr)
    except Exception:
        return 0.0

# -------------------------
# Bybit helpers
# -------------------------
def get_all_tickers():
    for attempt in range(MAX_RETRIES):
        try:
            r = requests.get(BYBIT_TICKERS_URL, timeout=20)
            if r.status_code == 429:
                time.sleep(5 + attempt * 5)
                continue
            r.raise_for_status()
            return r.json().get("result", {}).get("list", [])
        except Exception as e:
            print("⚠️ get_all_tickers error:", e)
            time.sleep(3)
    return []

def fetch_klines_1m(symbol, limit=50):
    try:
        url = BYBIT_KLINE_URL_TPL.format(symbol=symbol)
        r = requests.get(url, timeout=15)
        r.raise_for_status()
        j = r.json()
        res = j.get("result") or j
        lists = res.get("list") if isinstance(res, dict) else j.get("list")
        if not lists:
            return None
        closes, volumes = [], []
        for entry in lists[-limit:]:
            closes.append(float(entry[4]))
            volumes.append(float(entry[5]))
        return {"closes": closes, "volumes": volumes}
    except Exception as e:
        print("⚠️ fetch_klines_1m error for", symbol, e)
        return None

# -------------------------
# Risk estimation
# -------------------------
def estimate_risk_tier(volatility):
    if volatility < VOL_TIER_THRESHOLDS["low"]:
        return "low"
    elif volatility < VOL_TIER_THRESHOLDS["medium"]:
        return "medium"
    elif volatility < VOL_TIER_THRESHOLDS["high"]:
        return "high"
    else:
        return "very"

# -------------------------
# Monitoring logic
# -------------------------
def check_changes():
    coins = get_all_tickers()
    if not coins:
        print("⚠️ No tickers retrieved.")
        return

    for coin in coins[:MAX_CANDIDATES_PER_CYCLE]:
        symbol = coin.get("symbol")
        price = float(coin.get("lastPrice", 0))
        if not symbol or price <= 0:
            continue

        prev_price = last_prices.get(symbol)
        last_prices[symbol] = price

        if not prev_price:
            continue

        pct_change = ((price - prev_price) / prev_price) * 100
        if abs(pct_change) < PRICE_CHANGE_THRESHOLD:
            continue

        klines = fetch_klines_1m(symbol)
        if not klines:
            continue
        closes, volumes = klines["closes"], klines["volumes"]

        volatility = compute_volatility_logreturns(closes)
        rsi = compute_rsi(closes, RSI_PERIOD)
        ema = compute_ema(closes, EMA_PERIOD)
        risk_tier = estimate_risk_tier(volatility)
        risk_info = RISK_MAPPING[risk_tier]

        direction = "📈 LONG" if pct_change > 0 else "📉 SHORT"
        now = datetime.datetime.utcnow().strftime("%H:%M:%S UTC")

        message = (
            f"⚡ {symbol} {direction}\n"
            f"Δ {pct_change:.2f}% | RSI {rsi:.1f} | EMA {ema:.2f}\n"
            f"Volatility: {volatility:.4f} ({risk_tier})\n"
            f"🎯 Target: {risk_info['target_pct'][0]}–{risk_info['target_pct'][1]}%\n"
            f"⛔ Stop: {risk_info['stop_pct'][0]}–{risk_info['stop_pct'][1]}%\n"
            f"⏰ {now}"
        )

        if symbol not in last_alerts or time.time() - last_alerts[symbol] > 600:
            send_telegram_message(message)
            print(message)
            last_alerts[symbol] = time.time()

    save_state_to_gist()

# -------------------------
# Main monitor thread
# -------------------------
def start_monitoring():
    while True:
        try:
            check_changes()
        except Exception as e:
            print("⚠️ Main loop error:", e)
        time.sleep(CHECK_INTERVAL)

# -------------------------
# Entrypoint
# -------------------------
if __name__ == "__main__":
    print("🚀 Advanced Crypto Alert Bot starting (secure version, env vars)")
    load_state_from_gist()
    threading.Thread(target=start_monitoring, daemon=True).start()
    app.run(host="0.0.0.0", port=10000)
