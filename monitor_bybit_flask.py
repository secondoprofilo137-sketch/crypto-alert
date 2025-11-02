#!/usr/bin/env python3
# Bybit monitor — versione 3.1 con Trend Detector 4H e cooldown 1h

import os
import time
import threading
import math
from statistics import mean, pstdev
from datetime import datetime, timezone
import requests
import ccxt
import numpy as np
from flask import Flask

# === CONFIG ===
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_IDS = [cid.strip() for cid in os.getenv("TELEGRAM_CHAT_ID", "").split(",") if cid.strip()]
PORT = int(os.getenv("PORT", 10000))

FAST_TFS = ["1m", "3m"]
SLOW_TFS = ["1h"]
TREND_TF = "4h"  # Analisi trend
LOOP_DELAY = int(os.getenv("LOOP_DELAY", 5))
HEARTBEAT_INTERVAL_SEC = int(os.getenv("HEARTBEAT_INTERVAL_SEC", 3600))
MAX_CANDIDATES_PER_CYCLE = int(os.getenv("MAX_CANDIDATES_PER_CYCLE", 1000))
COOLDOWN_SECONDS = int(os.getenv("COOLDOWN_SECONDS", 180))
TREND_COOLDOWN = 3600  # 1 ora di cooldown per trend alert

THRESHOLDS = {
    "1m": float(os.getenv("THRESH_1M", 7.5)),
    "3m": float(os.getenv("THRESH_3M", 11.5)),
    "1h": float(os.getenv("THRESH_1H", 17.0)),
}

VOL_TIER_THRESHOLDS = {"low": 0.002, "medium": 0.006, "high": 0.012}
RISK_MAPPING = {
    "low": {"stop_pct": (-0.6, -1.0)},
    "medium": {"stop_pct": (-1.0, -2.0)},
    "high": {"stop_pct": (-2.0, -4.0)},
    "very": {"stop_pct": (-4.0, -8.0)},
}

exchange = ccxt.bybit({"options": {"defaultType": "swap"}})
last_prices = {}
last_alert_time = {}
last_trend_alert = {}
app = Flask(__name__)

# === TELEGRAM ===
def send_telegram(message: str):
    if not TELEGRAM_TOKEN or not CHAT_IDS:
        return
    for chat_id in CHAT_IDS:
        try:
            requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                data={"chat_id": chat_id, "text": message, "parse_mode": "Markdown"},
                timeout=10,
            )
        except Exception as e:
            print("❌ Telegram error:", e)

# === ANALYTICS ===
def compute_volatility_logreturns(closes):
    if len(closes) < 3:
        return 0.0
    try:
        lr = [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes)) if closes[i - 1] > 0]
        return pstdev(lr)
    except Exception:
        return 0.0

def estimate_risk_and_suggestion(closes, volumes):
    vol = compute_volatility_logreturns(closes[-30:]) if len(closes) >= 30 else compute_volatility_logreturns(closes)
    tier = (
        "low" if vol < VOL_TIER_THRESHOLDS["low"]
        else "medium" if vol < VOL_TIER_THRESHOLDS["medium"]
        else "high" if vol < VOL_TIER_THRESHOLDS["high"]
        else "very"
    )
    mapping = RISK_MAPPING[tier]
    vol_ratio = 1.0
    if volumes and len(volumes) >= 6:
        baseline = mean(volumes[-6:-1])
        last_vol = volumes[-1]
        vol_ratio = last_vol / baseline if baseline else 1.0
    return f"📊 Volatilità: {tier.upper()} | Stop: {mapping['stop_pct'][0]:.1f}%→{mapping['stop_pct'][1]:.1f}% | Vol ratio: {vol_ratio:.2f}×"

# === TREND DETECTOR ===
def detect_structure(closes):
    highs, lows = [], []
    for i in range(2, len(closes) - 2):
        if closes[i] > closes[i - 1] and closes[i] > closes[i + 1]:
            highs.append((i, closes[i]))
        if closes[i] < closes[i - 1] and closes[i] < closes[i + 1]:
            lows.append((i, closes[i]))
    if len(lows) >= 2 and lows[-1][1] > lows[-2][1]:
        return "higher_low"
    if len(highs) >= 2 and highs[-1][1] < highs[-2][1]:
        return "lower_high"
    return None

def trendline_break(closes):
    if len(closes) < 20:
        return None
    x = np.arange(len(closes))
    slope, intercept = np.polyfit(x[-10:], closes[-10:], 1)
    predicted = intercept + slope * x[-1]
    current = closes[-1]
    if current > predicted * 1.01:
        return "break_up"
    elif current < predicted * 0.99:
        return "break_down"
    return None

def detect_trend_signal(symbol, closes, vols):
    structure = detect_structure(closes)
    break_signal = trendline_break(closes)
    if not vols or len(vols) < 6:
        return None
    volume_ratio = vols[-1] / max(mean(vols[-6:-1]), 1)
    now_t = time.time()
    key = f"{symbol}_trend"

    # filtro doppioni 1h
    if key in last_trend_alert and now_t - last_trend_alert[key] < TREND_COOLDOWN:
        return None

    if structure == "higher_low" and break_signal == "break_up" and volume_ratio > 1.5:
        last_trend_alert[key] = now_t
        return f"📈 **{symbol}** — Trend RIALZISTA POTENZIALE — vol×{volume_ratio:.2f}"
    elif structure == "lower_high" and break_signal == "break_down" and volume_ratio > 1.5:
        last_trend_alert[key] = now_t
        return f"📉 **{symbol}** — Trend RIBASSISTA POTENZIALE — vol×{volume_ratio:.2f}"
    return None

# === EXCHANGE HELPERS ===
def safe_fetch(symbol, tf, limit=60):
    try:
        data = exchange.fetch_ohlcv(symbol, tf, limit=limit)
        return [x[4] for x in data], [x[5] for x in data]
    except Exception as e:
        print(f"⚠️ fetch {symbol} ({tf}):", e)
        return None, None

def get_bybit_symbols():
    try:
        markets = exchange.load_markets()
        return [s for s, m in markets.items() if s.endswith(":USDT")]
    except Exception as e:
        print("⚠️ load_markets:", e)
        return []

@app.route("/")
def home():
    return "✅ Flask up"

@app.route("/test")
def test():
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    send_telegram(f"🧪 TEST OK — {now}")
    return "Telegram test inviato", 200

# === MONITOR LOOP ===
def monitor_loop():
    symbols = get_bybit_symbols()
    print(f"✅ Trovati {len(symbols)} simboli su Bybit")
    last_heartbeat = 0
    initialized = False
    last_trend_check = 0

    while True:
        now_t = time.time()
        trend_check_due = now_t - last_trend_check >= 14400  # ogni 4h

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
                print(f"[{tf}] {symbol}: {variation:+.2f}% (soglia {threshold})")

                if abs(variation) >= threshold:
                    if key in last_alert_time and now_t - last_alert_time[key] < COOLDOWN_SECONDS:
                        continue
                    last_alert_time[key] = now_t

                    suggestion = estimate_risk_and_suggestion(closes, vols)
                    trend = "📈 Rialzo" if variation > 0 else "📉 Crollo"
                    emoji = "🔥" if tf in FAST_TFS else "⚡"
                    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

                    msg = (
                        f"🔔 ALERT -> {emoji} **{symbol}** — variazione {variation:+.2f}% (ref {tf})\n"
                        f"💰 Prezzo attuale: {price:.6f}\n"
                        f"{trend} rilevato — attenzione al trend.\n"
                        f"{suggestion}\n[{now}]"
                    )
                    send_telegram(msg)
                    last_prices[key] = price

            # Analisi trend 4h (ogni 4 ore)
            if trend_check_due:
                closes4h, vols4h = safe_fetch(symbol, TREND_TF, 100)
                if closes4h:
                    trend_msg = detect_trend_signal(symbol, closes4h, vols4h)
                    if trend_msg:
                        send_telegram(f"🔍 {trend_msg}")

        if trend_check_due:
            last_trend_check = now_t

        if not initialized:
            print("✅ Inizializzazione completata. Ora partono i confronti reali.")
            initialized = True

        if time.time() - last_heartbeat >= HEARTBEAT_INTERVAL_SEC:
            now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
            send_telegram(f"💓 Heartbeat — bot attivo ({now}) | Simboli: {len(symbols)}")
            last_heartbeat = time.time()

        time.sleep(LOOP_DELAY)

# === MAIN ===
if __name__ == "__main__":
    print(f"🚀 Avvio monitor Bybit — TF {FAST_TFS + SLOW_TFS}, thresholds {THRESHOLDS}, trend su {TREND_TF}")
    threading.Thread(target=lambda: app.run(host="0.0.0.0", port=PORT), daemon=True).start()
    threading.Thread(target=monitor_loop, daemon=True).start()
    while True:
        time.sleep(3600)
