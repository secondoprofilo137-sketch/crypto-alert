#!/usr/bin/env python3
# monitor_bybit_flask.py
# Versione 5.0 — alert classici + Wyckoff detector (1h/4h/1d/1w)
# Flask keepalive + multi-chat Telegram

from __future__ import annotations
import os
import time
import threading
import math
from statistics import mean, pstdev
from datetime import datetime, timezone
import requests
import ccxt
from flask import Flask

# -----------------------
# CONFIG (env or defaults)
# -----------------------
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_IDS = [cid.strip() for cid in os.getenv("TELEGRAM_CHAT_ID", "").split(",") if cid.strip()]
PORT = int(os.getenv("PORT", 10000))

# === CLASSIC ALERTS ===
FAST_TFS = os.getenv("FAST_TFS", "1m,3m").split(",")
SLOW_TFS = os.getenv("SLOW_TFS", "1h").split(",")
THRESH_1M = float(os.getenv("THRESH_1M", 7.5))
THRESH_3M = float(os.getenv("THRESH_3M", 11.5))
THRESH_1H = float(os.getenv("THRESH_1H", 17.0))
THRESHOLDS = {"1m": THRESH_1M, "3m": THRESH_3M, "1h": THRESH_1H}
COOLDOWN_SECONDS = int(os.getenv("COOLDOWN_SECONDS", 7200))  # 2 ore

# === WYCKOFF ALERTS ===
WYCKOFF_TFS = ["1h", "4h", "1d", "1w"]
WYCKOFF_SCORE_THRESHOLD = int(os.getenv("WYCKOFF_SCORE_THRESHOLD", 70))
LOOKBACK_CANDLES = int(os.getenv("LOOKBACK_CANDLES", 30))
COMPRESSION_RANGE_PCT = float(os.getenv("COMPRESSION_RANGE_PCT", 3.0))
VOL_GROWTH_MIN_RATIO = float(os.getenv("VOL_GROWTH_MIN_RATIO", 1.5))
MIN_USD_VOLUME = float(os.getenv("MIN_USD_VOLUME", 100000))

# === COMMON SETTINGS ===
LOOP_DELAY = int(os.getenv("LOOP_DELAY", 10))
MAX_SYMBOLS = int(os.getenv("MAX_SYMBOLS_PER_CYCLE", 1000))
HEARTBEAT_INTERVAL = int(os.getenv("HEARTBEAT_INTERVAL_SEC", 3600))

exchange = ccxt.bybit({"options": {"defaultType": "swap"}})
app = Flask(__name__)

last_prices = {}
last_alert_time = {}
last_heartbeat = 0

# -----------------------
# Telegram helper
# -----------------------
def send_telegram(msg: str):
    if not TELEGRAM_TOKEN or not CHAT_IDS:
        print(f"⚠️ Telegram non configurato:\n{msg}")
        return
    for cid in CHAT_IDS:
        try:
            requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                data={"chat_id": cid, "text": msg, "parse_mode": "Markdown"},
                timeout=10,
            )
        except Exception as e:
            print("❌ Telegram error:", e)

# -----------------------
# Helper: indicatori
# -----------------------
def volatility_logreturns(closes):
    if len(closes) < 3:
        return 0.0
    lr = [math.log(closes[i] / closes[i-1]) for i in range(1, len(closes)) if closes[i-1] > 0]
    return pstdev(lr) if len(lr) > 2 else 0.0

def range_pct(closes):
    hi, lo = max(closes), min(closes)
    return ((hi - lo) / lo) * 100 if lo > 0 else 0.0

def is_compression(closes):
    if len(closes) < LOOKBACK_CANDLES:
        return False, 0.0
    window = closes[-LOOKBACK_CANDLES:]
    r = range_pct(window)
    half = len(window) // 2
    v1 = volatility_logreturns(window[:half])
    v2 = volatility_logreturns(window[half:])
    return (r <= COMPRESSION_RANGE_PCT) and (v2 < v1), r

def volume_growth_ratio(volumes):
    if len(volumes) < 6:
        return 1.0
    base = mean(volumes[-6:-1])
    return volumes[-1] / base if base > 0 else 1.0

def breakout_check(closes):
    if len(closes) < 21:
        return False
    return closes[-1] > max(closes[-21:-1])

# -----------------------
# Fetch helper
# -----------------------
def safe_fetch(symbol, tf, limit=60):
    try:
        ohlcv = exchange.fetch_ohlcv(symbol, timeframe=tf, limit=limit)
        closes = [c[4] for c in ohlcv]
        vols = [c[5] for c in ohlcv]
        return closes, vols
    except Exception as e:
        print(f"⚠️ Fetch error {symbol} {tf}: {e}")
        return None, None

def get_symbols():
    try:
        markets = exchange.load_markets()
        return [s for s in markets if "USDT" in s and "SWAP" in markets[s]["id"].upper()]
    except Exception as e:
        print("⚠️ load_markets:", e)
        return []

# -----------------------
# Core: Wyckoff analysis
# -----------------------
def wyckoff_signal(symbol):
    data = {}
    for tf in WYCKOFF_TFS:
        closes, vols = safe_fetch(symbol, tf, LOOKBACK_CANDLES + 5)
        if not closes:
            return None
        data[tf] = {"closes": closes, "vols": vols}

    total_score = 0
    for tf, d in data.items():
        closes, vols = d["closes"], d["vols"]
        comp, rng = is_compression(closes)
        volr = volume_growth_ratio(vols)
        br = breakout_check(closes)
        s = 0
        if comp:
            s += 20
        if volr >= VOL_GROWTH_MIN_RATIO:
            s += 20
        if comp and volr >= VOL_GROWTH_MIN_RATIO:
            s += 25
        if br:
            s += 30
        total_score += s
    score = min(100, int(total_score / len(WYCKOFF_TFS)))
    return score

# -----------------------
# Monitor
# -----------------------
def monitor_loop():
    global last_heartbeat
    symbols = get_symbols()
    print(f"✅ Trovati {len(symbols)} simboli derivati")

    while True:
        for symbol in symbols[:MAX_SYMBOLS]:
            now_t = time.time()
            # === CLASSIC ALERTS ===
            for tf in FAST_TFS + SLOW_TFS:
                closes, vols = safe_fetch(symbol, tf, 60)
                if not closes:
                    continue
                price = closes[-1]
                key = f"{symbol}_{tf}"
                if key not in last_prices:
                    last_prices[key] = price
                    continue
                prev = last_prices[key]
                var = ((price - prev) / prev) * 100 if prev else 0
                threshold = THRESHOLDS.get(tf, 5.0)
                if abs(var) >= threshold:
                    last_sent = last_alert_time.get(key, 0)
                    if now_t - last_sent > COOLDOWN_SECONDS:
                        trend = "📈" if var > 0 else "📉"
                        msg = (
                            f"{trend} *{symbol}* — {var:+.2f}% ({tf})\n"
                            f"💰 Prezzo: {price:.6f}\n"
                            f"⏱️ {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}"
                        )
                        send_telegram(msg)
                        last_alert_time[key] = now_t
                last_prices[key] = price

            # === WYCKOFF ALERT ===
            key_w = f"wyckoff_{symbol}"
            last_w = last_alert_time.get(key_w, 0)
            if now_t - last_w > COOLDOWN_SECONDS:
                score = wyckoff_signal(symbol)
                if score and score >= WYCKOFF_SCORE_THRESHOLD:
                    msg = (
                        f"🌀 *WYCKOFF* — **{symbol}**\n"
                        f"🔎 Score: *{score}/100*\n"
                        f"💡 Mercato in compressione, preparati per il breakout.\n"
                        f"[{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}]"
                    )
                    send_telegram(msg)
                    last_alert_time[key_w] = now_t

        # Heartbeat
        if time.time() - last_heartbeat > HEARTBEAT_INTERVAL:
            send_telegram("💓 Heartbeat — bot attivo")
            last_heartbeat = time.time()

        time.sleep(LOOP_DELAY)

# -----------------------
# Flask endpoints
# -----------------------
@app.route("/")
def home():
    return "✅ Monitor Bybit Flask — running"

@app.route("/test")
def test():
    send_telegram("🧪 Test alert dal bot")
    return "ok", 200

# -----------------------
# Entrypoint
# -----------------------
if __name__ == "__main__":
    print("🚀 Avvio monitor Bybit — classic + Wyckoff")
    threading.Thread(target=lambda: app.run(host="0.0.0.0", port=PORT), daemon=True).start()
    threading.Thread(target=monitor_loop, daemon=True).start()
    while True:
        time.sleep(3600)
