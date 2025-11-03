#!/usr/bin/env python3
# ✅ Bybit Monitor con Flask + Volume Spike + Trend Detection
# Versione 3.0 — completa e stabile per Render

import os
import time
import threading
import math
from statistics import mean, pstdev
from datetime import datetime, timezone
import requests
import ccxt
from flask import Flask
import numpy as np

# === CONFIG ===
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_IDS = [cid.strip() for cid in os.getenv("TELEGRAM_CHAT_ID", "").split(",") if cid.strip()]
PORT = int(os.getenv("PORT", 10000))

FAST_TFS = ["1m", "3m"]
SLOW_TFS = ["1h"]
LOOP_DELAY = int(os.getenv("LOOP_DELAY", 5))
HEARTBEAT_INTERVAL_SEC = int(os.getenv("HEARTBEAT_INTERVAL_SEC", 3600))
MAX_CANDIDATES_PER_CYCLE = int(os.getenv("MAX_CANDIDATES_PER_CYCLE", 1000))
COOLDOWN_SECONDS = int(os.getenv("COOLDOWN_SECONDS", 180))
VOLUME_SPIKE_MULTIPLIER = float(os.getenv("VOLUME_SPIKE_MULTIPLIER", 100))
VOLUME_MIN_USD = float(os.getenv("VOLUME_MIN_USD", 1_000_000))

THRESHOLDS = {
    "1m": 7.5,
    "3m": 11.5,
    "1h": 17.0,
}

exchange = ccxt.bybit({"options": {"defaultType": "swap"}})
last_prices, last_alert_time = {}, {}
app = Flask(__name__)

# === TELEGRAM ===
def send_telegram(message: str):
    if not TELEGRAM_TOKEN or not CHAT_IDS:
        print("❌ Telegram non configurato")
        return
    for chat_id in CHAT_IDS:
        try:
            requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                data={
                    "chat_id": chat_id,
                    "text": message,
                    "parse_mode": "Markdown",
                    "disable_web_page_preview": True,
                },
                timeout=10,
            )
        except Exception as e:
            print(f"⚠️ Errore Telegram per {chat_id}: {e}")

# === UTILS ===
def ema(data, period):
    if len(data) < period:
        return mean(data)
    k = 2 / (period + 1)
    ema_val = data[0]
    for price in data[1:]:
        ema_val = price * k + ema_val * (1 - k)
    return ema_val

def compute_volatility_logreturns(closes):
    if len(closes) < 3:
        return 0.0
    lr = [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes)) if closes[i - 1] > 0]
    return pstdev(lr) if len(lr) >= 2 else 0.0

def safe_fetch(symbol, tf, limit=60):
    try:
        data = exchange.fetch_ohlcv(symbol, tf, limit=limit)
        closes = [x[4] for x in data]
        vols = [x[5] for x in data]
        return closes, vols
    except Exception as e:
        print(f"⚠️ fetch error {symbol} ({tf}):", e)
        return None, None

def get_bybit_symbols():
    try:
        markets = exchange.load_markets()
        return [s for s in markets if s.endswith(":USDT")]
    except Exception as e:
        print("⚠️ load_markets:", e)
        return []

# === ANALISI TREND E MASSIMI/MINIMI ===
def detect_trend(closes):
    ema_fast = ema(closes[-30:], 10)
    ema_slow = ema(closes[-30:], 30)
    strength = abs((ema_fast - ema_slow) / ema_slow) * 100
    if ema_fast > ema_slow * 1.002:
        trend = "📈 RIALZISTA"
    elif ema_fast < ema_slow * 0.998:
        trend = "📉 RIBASSISTA"
    else:
        trend = "➖ LATERALE"
    return trend, strength

def detect_breakout(closes):
    recent = closes[-20:]
    last = closes[-1]
    high = max(recent[:-1])
    low = min(recent[:-1])
    if last > high:
        return "🚀 ROTTURA MASSIMI"
    elif last < low:
        return "💥 ROTTURA MINIMI"
    return ""

# === DETECTOR VOLUME SPIKE ===
def detect_volume_spike(volumes, closes):
    if len(volumes) < 6:
        return False, 1.0, 0
    avg_prev = mean(volumes[-6:-1])
    last_vol = volumes[-1]
    price = closes[-1]
    usd_volume = last_vol * price
    if avg_prev == 0:
        return False, 1.0, usd_volume
    ratio = last_vol / avg_prev
    spike = ratio >= VOLUME_SPIKE_MULTIPLIER and usd_volume >= VOLUME_MIN_USD
    return spike, ratio, usd_volume

# === MAIN MONITOR ===
def monitor_loop():
    symbols = get_bybit_symbols()
    print(f"✅ Simboli derivati trovati: {len(symbols)}")
    last_heartbeat = 0
    initialized = False

    while True:
        for symbol in symbols[:MAX_CANDIDATES_PER_CYCLE]:
            for tf in FAST_TFS + SLOW_TFS:
                closes, vols = safe_fetch(symbol, tf, 60)
                if not closes:
                    continue
                price = closes[-1]
                key = f"{symbol}_{tf}"

                if key not in last_prices:
                    last_prices[key] = price
                    continue

                variation = ((price - last_prices[key]) / last_prices[key]) * 100
                threshold = THRESHOLDS[tf]
                alert_trigger = abs(variation) >= threshold

                # Controllo spike volume 1m
                spike_alert = False
                vol_ratio = 1.0
                usd_volume = 0
                if tf == "1m":
                    spike_alert, vol_ratio, usd_volume = detect_volume_spike(vols, closes)

                # Unifica trigger
                if alert_trigger or spike_alert:
                    now_t = time.time()
                    if key in last_alert_time and now_t - last_alert_time[key] < COOLDOWN_SECONDS:
                        continue
                    last_alert_time[key] = now_t

                    trend, strength = detect_trend(closes)
                    breakout = detect_breakout(closes)
                    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
                    emoji = "🔥" if tf in FAST_TFS else "⚡"
                    direction = "📈 Rialzo" if variation > 0 else "📉 Crollo"

                    msg = (
                        f"🔔 ALERT -> {emoji} **{symbol}** — variazione {variation:+.2f}% (ref {tf})\n"
                        f"💰 Prezzo: {price:.6f}\n"
                    )
                    if spike_alert:
                        msg += f"🚨 *Volume SPIKE {vol_ratio:.0f}×* — {usd_volume:,.0f} USD\n"
                    if breakout:
                        msg += f"{breakout}\n"
                    msg += (
                        f"{direction} | {trend} ({strength:.2f}% forza)\n"
                        f"[{now}]"
                    )
                    send_telegram(msg)
                    last_prices[key] = price

        if not initialized:
            print("✅ Avvio completato — ora iniziano gli alert reali.")
            initialized = True

        if time.time() - last_heartbeat >= HEARTBEAT_INTERVAL_SEC:
            send_telegram(f"💓 Heartbeat — bot attivo ({datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')})")
            last_heartbeat = time.time()

        time.sleep(LOOP_DELAY)

# === FLASK SERVER ===
@app.route("/")
def home():
    return "✅ Crypto Alert Bot attivo — Flask OK"

@app.route("/test")
def test():
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    send_telegram(f"🧪 TEST OK — {now}")
    return "Test Telegram inviato", 200

# === MAIN ===
if __name__ == "__main__":
    print(f"🚀 Avvio monitor Bybit — TF {FAST_TFS + SLOW_TFS}, soglie {THRESHOLDS}, spike ≥{VOLUME_SPIKE_MULTIPLIER}×")
    threading.Thread(target=lambda: app.run(host="0.0.0.0", port=PORT), daemon=True).start()
    threading.Thread(target=monitor_loop, daemon=True).start()
    while True:
        time.sleep(3600)
