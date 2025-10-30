#!/usr/bin/env python3
# monitor_bybit_flask.py
# Monitor Bybit Derivatives — Flask + Telegram + Alert con suggerimenti
# Italiano — supporta multi chat_id e consigli sempre presenti

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
# CONFIG (env o default)
# -------------------------
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_IDS = [cid.strip() for cid in os.getenv("TELEGRAM_CHAT_IDS", "").split(",") if cid.strip()]
PORT = int(os.getenv("PORT", 10000))

FAST_TFS = os.getenv("FAST_TFS", "1m,3m").split(",")
SLOW_TFS = os.getenv("SLOW_TFS", "1h").split(",")
FAST_THRESHOLD = float(os.getenv("FAST_THRESHOLD", 5.0))
SLOW_THRESHOLD = float(os.getenv("SLOW_THRESHOLD", 3.0))
LOOP_DELAY = int(os.getenv("LOOP_DELAY", 10))
HEARTBEAT_INTERVAL_SEC = int(os.getenv("HEARTBEAT_INTERVAL_SEC", 3600))
MAX_CANDIDATES_PER_CYCLE = int(os.getenv("MAX_CANDIDATES_PER_CYCLE", 1000))

VOL_TIER_THRESHOLDS = {"low": 0.002, "medium": 0.006, "high": 0.012}
RISK_MAPPING = {
    "low": {"leverage": "x2", "target_pct": (0.8, 1.5), "stop_pct": (-0.6, -1.0)},
    "medium": {"leverage": "x3", "target_pct": (1.5, 3.0), "stop_pct": (-1.0, -2.0)},
    "high": {"leverage": "x4", "target_pct": (3.0, 6.0), "stop_pct": (-2.0, -4.0)},
    "very": {"leverage": "x6", "target_pct": (6.0, 12.0), "stop_pct": (-4.0, -8.0)},
}

# -------------------------
# Stato globale
# -------------------------
last_prices = {}
last_alerts = {}

# -------------------------
# Exchange init
# -------------------------
exchange = ccxt.bybit({"options": {"defaultType": "swap"}})

# -------------------------
# Flask app
# -------------------------
app = Flask(__name__)

@app.route("/")
def home():
    return "✅ Crypto Alert Bot (Bybit derivatives) — attivo e funzionante"

@app.route("/test")
def test_alert():
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    send_telegram(f"🧪 TEST ALERT — bot online ({now})")
    return "Test inviato", 200

# -------------------------
# Telegram invio messaggi
# -------------------------
def send_telegram(text: str):
    if not TELEGRAM_TOKEN or not CHAT_IDS:
        print("❌ Telegram non configurato. Messaggio non inviato.")
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    for chat_id in CHAT_IDS:
        payload = {"chat_id": chat_id, "text": text, "disable_web_page_preview": True}
        try:
            r = requests.post(url, data=payload, timeout=10)
            if r.status_code != 200:
                print(f"❌ Errore Telegram {chat_id}: {r.status_code} {r.text}")
        except Exception as e:
            print(f"❌ Exception Telegram {chat_id}: {e}")
    return True

# -------------------------
# Indicatori tecnici
# -------------------------
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

def estimate_risk_and_suggestion(closes, volumes):
    vol = compute_volatility_logreturns(closes[-30:]) if len(closes) >= 30 else compute_volatility_logreturns(closes)
    if vol < VOL_TIER_THRESHOLDS["low"]:
        tier = "low"
    elif vol < VOL_TIER_THRESHOLDS["medium"]:
        tier = "medium"
    elif vol < VOL_TIER_THRESHOLDS["high"]:
        tier = "high"
    else:
        tier = "very"
    mapping = RISK_MAPPING.get(tier, RISK_MAPPING["medium"])
    vol_ratio = 1.0
    if volumes and len(volumes) >= 2:
        baseline = mean(volumes[-6:-1]) if len(volumes) >= 6 else mean(volumes[:-1])
        last_vol = volumes[-1]
        if baseline and baseline > 0:
            vol_ratio = last_vol / baseline
    suggestion = (
        f"📊 Categoria volatilità: {tier.upper()} | Leva suggerita: {mapping['leverage']}\n"
        f"🎯 Target indicativi: +{mapping['target_pct'][0]:.1f}% → +{mapping['target_pct'][1]:.1f}% | "
        f"🛑 Stop indicativi: {mapping['stop_pct'][0]:.1f}% → {mapping['stop_pct'][1]:.1f}%\n"
        f"📈 Vol ratio: {vol_ratio:.2f}× | Volatility: {vol:.4f}"
    )
    return mapping, suggestion

# -------------------------
# Helpers
# -------------------------
def safe_fetch_ohlcv(symbol, timeframe, limit=60):
    try:
        ohlcv = exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
        closes = [c[4] for c in ohlcv]
        volumes = [c[5] if len(c) > 5 else 0.0 for c in ohlcv]
        return closes, volumes
    except Exception as e:
        return None, None

def get_bybit_derivative_symbols():
    try:
        markets = exchange.load_markets()
        syms = []
        for symbol, meta in markets.items():
            if meta.get("type") == "swap" or symbol.endswith(":USDT") or ":USDT" in symbol:
                syms.append(symbol)
        return list(sorted(set(syms)))
    except Exception as e:
        print("⚠️ Errore load_markets:", e)
        return []

# -------------------------
# Monitor principale
# -------------------------
def monitor_loop():
    symbols = get_bybit_derivative_symbols()
    print(f"✅ Simboli derivati Bybit trovati: {len(symbols)}")
    last_heartbeat = 0

    while True:
        start = time.time()

        for symbol in symbols[:MAX_CANDIDATES_PER_CYCLE]:
            # --- FAST ALERTS (1m, 3m)
            for tf in FAST_TFS:
                closes, volumes = safe_fetch_ohlcv(symbol, tf, limit=2)
                if not closes:
                    continue
                price = closes[-1]
                key = f"{symbol}_{tf}"
                prev = last_prices.get(key, price)
                variation = ((price - prev) / prev) * 100 if prev else 0.0

                if abs(variation) >= FAST_THRESHOLD:
                    cooldown = last_alerts.setdefault(symbol, {}).get(f"fast_{tf}", 0)
                    if time.time() - cooldown > max(60, LOOP_DELAY * 3):
                        kl_closes, kl_vols = safe_fetch_ohlcv(symbol, tf, limit=60)
                        mapping, suggestion = estimate_risk_and_suggestion(kl_closes or closes, kl_vols or volumes)
                        trend = "rialzo" if variation > 0 else "crollo"
                        emoji = "🔥"
                        msg = (
                            f"🔔 ALERT -> {emoji} {symbol} — variazione {variation:+.2f}% (ref {tf})\n"
                            f"💰 Prezzo attuale: {price:.6f}\n"
                            f"📉 Movimento di {trend} — attenzione al trend.\n"
                            f"{suggestion}\n"
                            f"[{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}]"
                        )
                        print(msg)
                        send_telegram(msg)
                        last_alerts[symbol][f"fast_{tf}"] = time.time()

                last_prices[key] = price

            # --- SLOW ALERTS (1h)
            for tf in SLOW_TFS:
                closes, volumes = safe_fetch_ohlcv(symbol, tf, limit=2)
                if not closes:
                    continue
                price = closes[-1]
                key = f"{symbol}_{tf}"
                prev = last_prices.get(key, price)
                variation = ((price - prev) / prev) * 100 if prev else 0.0

                if abs(variation) >= SLOW_THRESHOLD:
                    cooldown = last_alerts.setdefault(symbol, {}).get(f"slow_{tf}", 0)
                    if time.time() - cooldown > max(1800, HEARTBEAT_INTERVAL_SEC / 2):
                        kl_closes, kl_vols = safe_fetch_ohlcv(symbol, tf, limit=120)
                        mapping, suggestion = estimate_risk_and_suggestion(kl_closes or closes, kl_vols or volumes)
                        msg = (
                            f"🔔 ALERT -> ⚡ {symbol} — variazione {variation:+.2f}% (ref {tf})\n"
                            f"💰 Prezzo attuale: {price:.6f}\n"
                            f"📈 Movimento medio-lungo rilevato.\n"
                            f"{suggestion}\n"
                            f"[{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}]"
                        )
                        print(msg)
                        send_telegram(msg)
                        last_alerts[symbol][f"slow_{tf}"] = time.time()

                last_prices[key] = price

        # --- HEARTBEAT orario
        if time.time() - last_heartbeat >= HEARTBEAT_INTERVAL_SEC:
            now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
            hb = f"💓 Heartbeat — bot attivo ({now}) | Simboli monitorati: {len(symbols)}"
            print(hb)
            send_telegram(hb)
            last_heartbeat = time.time()

        elapsed = time.time() - start
        print(f"[{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}] Ciclo completato — simboli: {len(symbols)} — tempo {elapsed:.1f}s")
        time.sleep(LOOP_DELAY)

# -------------------------
# Avvio
# -------------------------
if __name__ == "__main__":
    print(f"🚀 Avvio monitor Bybit — timeframes {FAST_TFS + SLOW_TFS}, thresholds {FAST_THRESHOLD}/{SLOW_THRESHOLD}, loop {LOOP_DELAY}s")
    threading.Thread(target=lambda: app.run(host="0.0.0.0", port=PORT), daemon=True).start()
    threading.Thread(target=monitor_loop, daemon=True).start()
    while True:
        time.sleep(60)
