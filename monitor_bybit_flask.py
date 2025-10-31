#!/usr/bin/env python3
# monitor_bybit_flask.py
# Bybit Derivatives Monitor — Flask + Telegram — versione stabile senza spike

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
# CONFIGURAZIONE BASE
# -------------------------
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_IDS = [cid.strip() for cid in os.getenv("TELEGRAM_CHAT_IDS", "").split(",") if cid.strip()]
PORT = int(os.getenv("PORT", 10000))

FAST_TFS = os.getenv("FAST_TFS", "1m,3m").split(",")
SLOW_TFS = os.getenv("SLOW_TFS", "1h").split(",")
FAST_THRESHOLD = float(os.getenv("FAST_THRESHOLD", 5.0))
SLOW_THRESHOLD = float(os.getenv("SLOW_THRESHOLD", 10.0))  # >= 10%
LOOP_DELAY = int(os.getenv("LOOP_DELAY", 10))
HEARTBEAT_INTERVAL_SEC = int(os.getenv("HEARTBEAT_INTERVAL_SEC", 3600))
MAX_CANDIDATES_PER_CYCLE = int(os.getenv("MAX_CANDIDATES_PER_CYCLE", 800))

VOL_TIER_THRESHOLDS = {"low": 0.002, "medium": 0.006, "high": 0.012}
RISK_MAPPING = {
    "low":    {"target_pct": (0.8, 1.5),  "stop_pct": (-0.6, -1.0)},
    "medium": {"target_pct": (1.5, 3.0),  "stop_pct": (-1.0, -2.0)},
    "high":   {"target_pct": (3.0, 6.0),  "stop_pct": (-2.0, -4.0)},
    "very":   {"target_pct": (6.0, 12.0), "stop_pct": (-4.0, -8.0)},
}

# -------------------------
# Stato
# -------------------------
last_prices = {}
last_alerts = {}
_lock = threading.Lock()

exchange = ccxt.bybit({"options": {"defaultType": "swap"}})
app = Flask(__name__)

@app.route("/")
def home():
    return "✅ Crypto Alert Bot (Bybit derivatives) — running"

@app.route("/test")
def test_alert():
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    send_telegram(f"🧪 TEST ALERT - bot online ({now})")
    return "Test sent", 200

# -------------------------
# Telegram helper
# -------------------------
def send_telegram(text: str):
    if not TELEGRAM_TOKEN or not CHAT_IDS:
        print("❌ Telegram non configurato o nessuna CHAT_ID.")
        print(text)
        return
    for chat_id in CHAT_IDS:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        payload = {"chat_id": chat_id, "text": text, "parse_mode": "Markdown", "disable_web_page_preview": True}
        try:
            r = requests.post(url, data=payload, timeout=10)
            if r.status_code != 200:
                print(f"❌ Errore Telegram {chat_id}:", r.text)
        except Exception as e:
            print("❌ Exception Telegram:", e)

# -------------------------
# Indicatori base
# -------------------------
def compute_volatility_logreturns(closes):
    if len(closes) < 3:
        return 0.0
    lr = []
    for i in range(1, len(closes)):
        if closes[i-1] > 0:
            lr.append(math.log(closes[i] / closes[i-1]))
    return pstdev(lr) if len(lr) >= 2 else 0.0

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
    mapping = RISK_MAPPING[tier]
    vol_ratio = 1.0
    if volumes and len(volumes) >= 6:
        baseline = mean(volumes[-6:-1])
        if baseline > 0:
            vol_ratio = volumes[-1] / baseline
    suggestion = (
        f"Categoria volatilità: {tier.upper()}\n"
        f"🎯 Target: +{mapping['target_pct'][0]:.1f}% → +{mapping['target_pct'][1]:.1f}%\n"
        f"🛑 Stop: {mapping['stop_pct'][0]:.1f}% → {mapping['stop_pct'][1]:.1f}%\n"
        f"📊 Rapporto volume: {vol_ratio:.2f}× | Volatilità: {vol:.4f}"
    )
    return mapping, suggestion

# -------------------------
# Funzioni di supporto
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
        syms = [s for s in markets if ":USDT" in s or s.endswith(":USDT")]
        return sorted(set(syms))
    except Exception as e:
        print("⚠️ Errore load_markets:", e)
        return []

# -------------------------
# Monitor loop
# -------------------------
def monitor_loop():
    global last_prices, last_alerts
    symbols = get_bybit_derivative_symbols()
    print(f"✅ Simboli derivati Bybit trovati: {len(symbols)}")
    last_heartbeat = 0

    while True:
        start = time.time()

        for symbol in symbols[:MAX_CANDIDATES_PER_CYCLE]:
            # === Fast timeframe (1m, 3m)
            for tf in FAST_TFS:
                closes, _ = safe_fetch_ohlcv(symbol, tf, limit=2)
                if not closes:
                    continue
                price = closes[-1]
                key = f"{symbol}_{tf}"
                prev = last_prices.get(key, price)
                variation = ((price - prev) / prev) * 100 if prev else 0.0
                if abs(variation) >= FAST_THRESHOLD:
                    cooldown = last_alerts.setdefault(symbol, {}).get(f"fast_{tf}", 0)
                    if time.time() - cooldown > LOOP_DELAY * 3:
                        kl_closes, kl_vols = safe_fetch_ohlcv(symbol, tf, limit=60)
                        _, suggestion = estimate_risk_and_suggestion(kl_closes or closes, kl_vols)
                        emoji = "🔥" if variation > 0 else "❄️"
                        trend = "rialzo" if variation > 0 else "crollo"
                        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
                        text = (
                            f"🔔 ALERT {emoji} *{symbol}* — variazione {variation:+.2f}% (ref {tf})\n"
                            f"Prezzo attuale: {price:.6f}\n"
                            f"📊 Movimento di {trend} — attenzione al trend.\n"
                            f"{suggestion}\n[{now}]"
                        )
                        print(text)
                        send_telegram(text)
                        last_alerts[symbol][f"fast_{tf}"] = time.time()
                last_prices[key] = price

            # === Slow timeframe (1h, >=10%)
            for tf in SLOW_TFS:
                closes, _ = safe_fetch_ohlcv(symbol, tf, limit=2)
                if not closes:
                    continue
                price = closes[-1]
                key = f"{symbol}_{tf}"
                prev = last_prices.get(key, price)
                variation = ((price - prev) / prev) * 100 if prev else 0.0
                if abs(variation) >= SLOW_THRESHOLD:
                    cooldown = last_alerts.setdefault(symbol, {}).get(f"slow_{tf}", 0)
                    if time.time() - cooldown > HEARTBEAT_INTERVAL_SEC / 2:
                        kl_closes, kl_vols = safe_fetch_ohlcv(symbol, tf, limit=120)
                        _, suggestion = estimate_risk_and_suggestion(kl_closes or closes, kl_vols)
                        emoji = "⚡" if variation > 0 else "🌩️"
                        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
                        text = (
                            f"🔔 ALERT {emoji} *{symbol}* — variazione {variation:+.2f}% (ref {tf})\n"
                            f"Prezzo attuale: {price:.6f}\n"
                            f"📈 Movimento medio-lungo rilevato.\n"
                            f"{suggestion}\n[{now}]"
                        )
                        print(text)
                        send_telegram(text)
                        last_alerts[symbol][f"slow_{tf}"] = time.time()
                last_prices[key] = price

        # === Heartbeat
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
    print(f"🚀 monitor Bybit — timeframes {FAST_TFS + SLOW_TFS}, thresholds {FAST_THRESHOLD}/{SLOW_THRESHOLD}, loop {LOOP_DELAY}s")
    threading.Thread(target=lambda: app.run(host="0.0.0.0", port=PORT), daemon=True).start()
    monitor_thread = threading.Thread(target=monitor_loop, daemon=True)
    monitor_thread.start()
    monitor_thread.join()
