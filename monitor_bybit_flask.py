#!/usr/bin/env python3
# monitor_bybit_flask.py
# Bybit derivatives monitor — 1m, 3m, 1h + volume spike alert — Flask + Telegram multi-chat

import os
import time
import threading
import math
from statistics import mean, pstdev
from datetime import datetime, timezone
import requests
import ccxt
from flask import Flask

# -------------------------
# CONFIG
# -------------------------
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_IDS = [cid.strip() for cid in os.getenv("TELEGRAM_CHAT_ID", "").split(",") if cid.strip()]
PORT = int(os.getenv("PORT", 10000))

FAST_TFS = os.getenv("FAST_TFS", "1m,3m").split(",")
SLOW_TFS = os.getenv("SLOW_TFS", "1h").split(",")
FAST_THRESHOLD = float(os.getenv("FAST_THRESHOLD", 5.0))
SLOW_THRESHOLD = float(os.getenv("SLOW_THRESHOLD", 10.0))
LOOP_DELAY = int(os.getenv("LOOP_DELAY", 10))
HEARTBEAT_INTERVAL_SEC = int(os.getenv("HEARTBEAT_INTERVAL_SEC", 3600))
MAX_CANDIDATES_PER_CYCLE = int(os.getenv("MAX_CANDIDATES_PER_CYCLE", 1000))
VOLUME_SPIKE_RATIO = 2.5  # volume alert if > 2.5× recent avg

# -------------------------
# State
# -------------------------
last_prices = {}
last_alerts = {}
_lock = threading.Lock()

exchange = ccxt.bybit({"options": {"defaultType": "swap"}})
app = Flask(__name__)

# -------------------------
# Flask routes
# -------------------------
@app.route("/")
def home():
    return "✅ Crypto Alert Bot (Bybit) — running"

@app.route("/test")
def test_alert():
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    send_telegram(f"🧪 TEST ALERT — bot online ({now})")
    return "Test sent", 200

# -------------------------
# Telegram
# -------------------------
def send_telegram(text: str):
    if not TELEGRAM_TOKEN or not CHAT_IDS:
        print("❌ Telegram non configurato.")
        print(text)
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    ok = True
    for cid in CHAT_IDS:
        try:
            r = requests.post(url, data={"chat_id": cid, "text": text, "parse_mode": "Markdown", "disable_web_page_preview": True}, timeout=10)
            if r.status_code != 200:
                print(f"❌ Telegram errore ({cid}):", r.text)
                ok = False
        except Exception as e:
            print(f"❌ Telegram eccezione ({cid}):", e)
            ok = False
    return ok

# -------------------------
# Indicatori
# -------------------------
def compute_volatility_logreturns(closes):
    if len(closes) < 3:
        return 0.0
    lr = [math.log(closes[i]/closes[i-1]) for i in range(1, len(closes)) if closes[i-1] > 0]
    return pstdev(lr) if len(lr) > 1 else 0.0

def estimate_risk_and_suggestion(closes, volumes):
    vol = compute_volatility_logreturns(closes[-30:]) if len(closes) >= 30 else compute_volatility_logreturns(closes)
    tier = "VERY" if vol > 0.012 else "HIGH" if vol > 0.006 else "MEDIUM" if vol > 0.002 else "LOW"
    targets = { "LOW": (0.8, 1.5), "MEDIUM": (1.5, 3.0), "HIGH": (3.0, 6.0), "VERY": (6.0, 12.0) }
    stops = { "LOW": (-0.6, -1.0), "MEDIUM": (-1.0, -2.0), "HIGH": (-2.0, -4.0), "VERY": (-4.0, -8.0) }
    t1, t2 = targets[tier]
    s1, s2 = stops[tier]
    vol_ratio = 1.0
    if volumes and len(volumes) >= 6:
        base = mean(volumes[-6:-1])
        if base > 0:
            vol_ratio = volumes[-1] / base
    suggestion = (
        f"🇮🇹 Volatilità: {tier} | Target: +{t1:.1f}%→+{t2:.1f}% | Stop: {s1:.1f}%→{s2:.1f}% | "
        f"Vol ratio: {vol_ratio:.2f}× | Volatility: {vol:.4f}"
    )
    return suggestion

# -------------------------
# Data helpers
# -------------------------
def safe_fetch_ohlcv(symbol, timeframe, limit=60):
    try:
        ohlcv = exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
        closes = [c[4] for c in ohlcv]
        volumes = [c[5] if len(c) > 5 else 0.0 for c in ohlcv]
        return closes, volumes
    except Exception:
        return None, None

def get_bybit_derivative_symbols():
    try:
        markets = exchange.load_markets()
        return [s for s, m in markets.items() if (m.get("type") == "swap" or ":USDT" in s)]
    except Exception as e:
        print("⚠️ Errore load_markets:", e)
        return []

# -------------------------
# Monitor loop
# -------------------------
def monitor_loop():
    global last_prices, last_alerts
    symbols = get_bybit_derivative_symbols()
    print(f"✅ Simboli derivati trovati: {len(symbols)}")
    last_heartbeat = 0

    while True:
        start = time.time()

        for symbol in symbols[:MAX_CANDIDATES_PER_CYCLE]:
            # ---- FAST TFS ----
            for tf in FAST_TFS:
                closes, vols = safe_fetch_ohlcv(symbol, tf, limit=60)
                if not closes: continue
                price = closes[-1]
                key = f"{symbol}_{tf}"
                prev = last_prices.get(key, price)
                variation = ((price - prev) / prev) * 100 if prev else 0
                last_prices[key] = price

                # prezzo alert
                if abs(variation) >= FAST_THRESHOLD:
                    cooldown = last_alerts.setdefault(symbol, {}).get(f"fast_{tf}", 0)
                    if time.time() - cooldown > LOOP_DELAY * 3:
                        suggestion = estimate_risk_and_suggestion(closes, vols)
                        trend = "Rialzo" if variation > 0 else "Crollo"
                        emoji = "🔥"
                        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
                        text = (
                            f"🔔 ALERT -> {emoji} {symbol} — variazione {variation:+.2f}% (ref {tf})\n"
                            f"💰 Prezzo attuale: {price:.6f}\n"
                            f"📉 {trend} rilevato — attenzione al trend.\n"
                            f"{suggestion}\n[{now}]"
                        )
                        print(text)
                        send_telegram(text)
                        last_alerts[symbol][f"fast_{tf}"] = time.time()

                # volume spike alert (solo 1m)
                if tf == "1m" and vols and len(vols) >= 11:
                    avg_vol = mean(vols[-11:-1])
                    if avg_vol > 0 and vols[-1] / avg_vol >= VOLUME_SPIKE_RATIO:
                        cooldown = last_alerts.setdefault(symbol, {}).get("vol_spike", 0)
                        if time.time() - cooldown > 120:  # 2 min cooldown
                            ratio = vols[-1] / avg_vol
                            now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
                            text = (
                                f"🛗 *VOLUME SPIKE su {symbol}*\n"
                                f"📊 Volume attuale: `{vols[-1]:,.2f}` — Media 10m: `{avg_vol:,.2f}` *(×{ratio:.2f})*\n"
                                f"⚡ Possibile movimento imminente!\n[{now}]"
                            )
                            print(text)
                            send_telegram(text)
                            last_alerts[symbol]["vol_spike"] = time.time()

            # ---- SLOW TFS (1h) ----
            for tf in SLOW_TFS:
                closes, vols = safe_fetch_ohlcv(symbol, tf, limit=120)
                if not closes: continue
                price = closes[-1]
                key = f"{symbol}_{tf}"
                prev = last_prices.get(key, price)
                variation = ((price - prev) / prev) * 100 if prev else 0
                last_prices[key] = price

                if abs(variation) >= SLOW_THRESHOLD:
                    cooldown = last_alerts.setdefault(symbol, {}).get(f"slow_{tf}", 0)
                    if time.time() - cooldown > HEARTBEAT_INTERVAL_SEC / 2:
                        suggestion = estimate_risk_and_suggestion(closes, vols)
                        trend = "Rialzo" if variation > 0 else "Crollo"
                        emoji = "⚡"
                        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
                        text = (
                            f"🔔 ALERT -> {emoji} {symbol} — variazione {variation:+.2f}% (ref {tf})\n"
                            f"💰 Prezzo attuale: {price:.6f}\n"
                            f"📈 {trend} rilevato — attenzione al trend.\n"
                            f"{suggestion}\n[{now}]"
                        )
                        print(text)
                        send_telegram(text)
                        last_alerts[symbol][f"slow_{tf}"] = time.time()

        # heartbeat
        if time.time() - last_heartbeat >= HEARTBEAT_INTERVAL_SEC:
            now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
            hb = f"💓 Heartbeat — attivo ({now}) | Simboli: {len(symbols)}"
            print(hb)
            send_telegram(hb)
            last_heartbeat = time.time()

        elapsed = time.time() - start
        print(f"[{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}] Ciclo completato — {len(symbols)} simboli — {elapsed:.1f}s")
        time.sleep(LOOP_DELAY)

# -------------------------
# Entrypoint
# -------------------------
if __name__ == "__main__":
    print(f"🚀 Avvio monitor Bybit — timeframes {FAST_TFS}+{SLOW_TFS}, thresholds {FAST_THRESHOLD}/{SLOW_THRESHOLD}, loop {LOOP_DELAY}s")
    threading.Thread(target=lambda: app.run(host="0.0.0.0", port=PORT), daemon=True).start()
    threading.Thread(target=monitor_loop, daemon=True).start()
    while True:
        time.sleep(3600)
