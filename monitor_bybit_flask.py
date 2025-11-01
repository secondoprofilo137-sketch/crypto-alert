#!/usr/bin/env python3
# monitor_bybit_flask.py — Versione stabile (solo alert di prezzo)

import os
import time
import threading
import math
from statistics import mean, pstdev
from datetime import datetime, timezone
import requests
import ccxt
from flask import Flask

# === CONFIG ===
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_IDS = [cid.strip() for cid in os.getenv("TELEGRAM_CHAT_ID", "").split(",") if cid.strip()]
PORT = int(os.getenv("PORT", 10000))

FAST_TFS = os.getenv("FAST_TFS", "1m,3m").split(",")
SLOW_TFS = os.getenv("SLOW_TFS", "1h").split(",")
FAST_THRESHOLD = float(os.getenv("FAST_THRESHOLD", 7.5))
SLOW_THRESHOLD_FAST = float(os.getenv("SLOW_THRESHOLD_FAST", 11.5))
SLOW_THRESHOLD = float(os.getenv("SLOW_THRESHOLD", 17.0))
LOOP_DELAY = int(os.getenv("LOOP_DELAY", 10))
HEARTBEAT_INTERVAL_SEC = int(os.getenv("HEARTBEAT_INTERVAL_SEC", 3600))
MAX_CANDIDATES_PER_CYCLE = int(os.getenv("MAX_CANDIDATES_PER_CYCLE", 1000))

VOL_TIER_THRESHOLDS = {"low": 0.002, "medium": 0.006, "high": 0.012}

exchange = ccxt.bybit({"options": {"defaultType": "swap"}})
last_prices = {}
app = Flask(__name__)

# === FLASK ROUTES ===
@app.route("/")
def home():
    return "✅ Crypto Alert Bot attivo — Flask OK"

@app.route("/test")
def test():
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    send_telegram(f"🧪 TEST OK — {now}")
    return "Test inviato a Telegram", 200

# === TELEGRAM ===
def send_telegram(message: str):
    if not TELEGRAM_TOKEN or not CHAT_IDS:
        print("❌ Telegram non configurato correttamente")
        return
    for chat_id in CHAT_IDS:
        try:
            url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
            payload = {
                "chat_id": chat_id,
                "text": message,
                "disable_web_page_preview": True,
                "parse_mode": "Markdown"
            }
            r = requests.post(url, data=payload, timeout=10)
            if r.status_code != 200:
                print(f"⚠️ Telegram error for {chat_id}: {r.text}")
        except Exception as e:
            print(f"❌ Telegram exception for {chat_id}: {e}")

# === ANALYTICS ===
def compute_volatility_logreturns(closes):
    if len(closes) < 3:
        return 0.0
    try:
        lr = [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes)) if closes[i - 1] > 0]
        return pstdev(lr) if len(lr) >= 2 else 0.0
    except Exception:
        return 0.0

def estimate_volatility_tier(closes, volumes):
    vol = compute_volatility_logreturns(closes[-30:]) if len(closes) >= 30 else compute_volatility_logreturns(closes)
    if vol < VOL_TIER_THRESHOLDS["low"]:
        tier = "LOW"
    elif vol < VOL_TIER_THRESHOLDS["medium"]:
        tier = "MEDIUM"
    elif vol < VOL_TIER_THRESHOLDS["high"]:
        tier = "HIGH"
    else:
        tier = "VERY HIGH"
    vol_ratio = 1.0
    if volumes and len(volumes) >= 6:
        baseline = mean(volumes[-6:-1])
        last_vol = volumes[-1]
        if baseline:
            vol_ratio = last_vol / baseline
    return tier, vol_ratio

# === EXCHANGE HELPERS ===
def safe_fetch(symbol, tf, limit=60):
    try:
        ohlcv = exchange.fetch_ohlcv(symbol, tf, limit=limit)
        closes = [c[4] for c in ohlcv]
        vols = [c[5] for c in ohlcv]
        return closes, vols
    except Exception:
        return None, None

def get_bybit_symbols():
    try:
        return [s for s, m in exchange.load_markets().items() if (m.get("type") == "swap" or s.endswith(":USDT"))]
    except Exception as e:
        print("⚠️ load_markets error:", e)
        return []

# === MONITOR LOOP ===
def monitor_loop():
    symbols = get_bybit_symbols()
    print(f"✅ Simboli derivati Bybit trovati: {len(symbols)}")
    last_heartbeat = 0

    while True:
        for symbol in symbols[:MAX_CANDIDATES_PER_CYCLE]:
            for tf in FAST_TFS + SLOW_TFS:
                closes, vols = safe_fetch(symbol, tf, 60)
                if not closes:
                    continue

                price = closes[-1]
                prev = last_prices.get(f"{symbol}_{tf}", price)
                variation = ((price - prev) / prev) * 100 if prev else 0.0

                if tf == "1m":
                    threshold = FAST_THRESHOLD
                elif tf == "3m":
                    threshold = SLOW_THRESHOLD_FAST
                else:
                    threshold = SLOW_THRESHOLD

                if abs(variation) >= threshold:
                    vol_tier, volratio = estimate_volatility_tier(closes, vols)
                    trend = "📈 Rialzo" if variation > 0 else "📉 Crollo"
                    emoji = "🔥" if tf in FAST_TFS else "⚡"
                    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

                    text = (
                        f"🔔 ALERT -> {emoji} **{symbol}** — variazione {variation:+.2f}% (ref {tf})\n"
                        f"💰 Prezzo attuale: {price:.6f}\n"
                        f"{trend} rilevato — attenzione al trend.\n"
                        f"📊 Volatilità: {vol_tier} | Vol ratio: {volratio:.2f}×\n"
                        f"[{now}]"
                    )

                    send_telegram(text)
                    last_prices[f"{symbol}_{tf}"] = price

        if time.time() - last_heartbeat >= HEARTBEAT_INTERVAL_SEC:
            now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
            send_telegram(f"💓 Heartbeat — bot attivo ({now}) | Simboli: {len(symbols)}")
            last_heartbeat = time.time()

        time.sleep(LOOP_DELAY)

# === MAIN ===
if __name__ == "__main__":
    print(f"🚀 Avvio monitor Bybit — TF {FAST_TFS + SLOW_TFS}, thresholds {FAST_THRESHOLD}/{SLOW_THRESHOLD}, loop {LOOP_DELAY}s")
    threading.Thread(target=lambda: app.run(host="0.0.0.0", port=PORT), daemon=True).start()
    threading.Thread(target=monitor_loop, daemon=True).start()
    while True:
        time.sleep(3600)
