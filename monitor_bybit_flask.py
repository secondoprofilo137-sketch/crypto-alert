#!/usr/bin/env python3
# monitor_bybit_flask.py
# ✅ Versione 2.4 — Volume SPIKE ≥ 50× e volume USD ≥ 1M

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

FAST_TFS = ["1m", "3m"]
SLOW_TFS = ["1h"]
FAST_THRESHOLD = 8.0
SLOW_THRESHOLD_FAST = 12.0
SLOW_THRESHOLD = 20.0
VOLUME_SPIKE_MULTIPLIER = 50.0
VOLUME_MIN_USD = 1_000_000.0  # soglia minima in USD
COOLDOWN_SECONDS = 180  # 3 minuti
LOOP_DELAY = 10
HEARTBEAT_INTERVAL_SEC = 3600
MAX_CANDIDATES_PER_CYCLE = 1000

VOL_TIER_THRESHOLDS = {"low": 0.002, "medium": 0.006, "high": 0.012}

exchange = ccxt.bybit({"options": {"defaultType": "swap"}})
last_prices, last_alerts = {}, {}
app = Flask(__name__)

# === FLASK ===
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

# === VOLUME SPIKE DETECTOR ===
def detect_volume_spike(symbol, closes, volumes):
    """
    Rileva un volume spike solo se:
    - volume > media * VOLUME_SPIKE_MULTIPLIER
    - volume * prezzo >= 1 milione USD
    """
    if len(volumes) < 6 or len(closes) < 6:
        return False, 1.0, 0.0
    avg_prev = mean(volumes[-6:-1])
    last_vol = volumes[-1]
    last_price = closes[-1]
    usd_volume = last_vol * last_price
    if avg_prev == 0:
        return False, 1.0, usd_volume
    ratio = last_vol / avg_prev
    spike = ratio >= VOLUME_SPIKE_MULTIPLIER and usd_volume >= VOLUME_MIN_USD
    return spike, ratio, usd_volume

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

                # Threshold dinamica
                if tf == "1m":
                    threshold = FAST_THRESHOLD
                elif tf == "3m":
                    threshold = SLOW_THRESHOLD_FAST
                else:
                    threshold = SLOW_THRESHOLD

                alert_triggered = abs(variation) >= threshold
                volume_spike, vol_ratio, usd_vol = detect_volume_spike(symbol, closes, vols) if tf == "1m" else (False, 1.0, 0.0)

                # Cooldown
                key = f"{symbol}_{tf}"
                now_ts = time.time()
                if key in last_alerts and now_ts - last_alerts[key] < COOLDOWN_SECONDS:
                    continue

                if alert_triggered or volume_spike:
                    vol_tier, volratio = estimate_volatility_tier(closes, vols)
                    trend = "📈 Rialzo" if variation > 0 else "📉 Crollo"
                    emoji = "🔥" if tf in FAST_TFS else "⚡"
                    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

                    text = (
                        f"🔔 ALERT -> {emoji} **{symbol}** — variazione {variation:+.2f}% (ref {tf})\n"
                        f"💰 Prezzo attuale: {price:.6f}\n"
                    )

                    if volume_spike:
                        text += f"🚨 **VOLUME SPIKE** — {vol_ratio:.1f}× sopra la media | 💵 Volume: {usd_vol/1_000_000:.2f}M USD\n"

                    text += (
                        f"{trend} rilevato — attenzione al trend.\n"
                        f"📊 Volatilità: {vol_tier} | Vol ratio: {volratio:.2f}×\n"
                        f"[{now_str}]"
                    )

                    send_telegram(text)
                    last_alerts[key] = now_ts
                    last_prices[key] = price

        # Heartbeat
        if time.time() - last_heartbeat >= HEARTBEAT_INTERVAL_SEC:
            now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
            send_telegram(f"💓 Heartbeat — bot attivo ({now}) | Simboli monitorati: {len(symbols)}")
            last_heartbeat = time.time()

        time.sleep(LOOP_DELAY)

# === MAIN ===
if __name__ == "__main__":
    print(f"🚀 Avvio monitor Bybit — TF {FAST_TFS + SLOW_TFS}, thresholds {FAST_THRESHOLD}/{SLOW_THRESHOLD}, loop {LOOP_DELAY}s")
    threading.Thread(target=lambda: app.run(host="0.0.0.0", port=PORT), daemon=True).start()
    threading.Thread(target=monitor_loop, daemon=True).start()
    while True:
        time.sleep(3600) 
