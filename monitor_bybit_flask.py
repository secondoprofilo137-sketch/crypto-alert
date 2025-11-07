#!/usr/bin/env python3
# monitor_bybit_flask.py — Versione 4.1
# Monitor Bybit + Analisi predittiva 4h + Telegram multi-chat + Flask webservice

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
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_IDS = [cid.strip() for cid in os.getenv("TELEGRAM_CHAT_ID", "").split(",") if cid.strip()]
PORT = int(os.getenv("PORT", 10000))

FAST_TFS = ["1m", "3m"]
SLOW_TFS = ["1h"]
ANALYSIS_TF = "4h"
LOOP_DELAY = 10
HEARTBEAT_INTERVAL_SEC = 3600
MAX_CANDIDATES_PER_CYCLE = 1000
COOLDOWN_SECONDS = 180
LONG_ANALYSIS_INTERVAL = 6 * 3600  # ogni 6 ore

THRESHOLDS = {"1m": 7.5, "3m": 11.5, "1h": 17.0}
VOLUME_SPIKE_MULTIPLIER = 100
VOLUME_MIN_USD = 1_000_000

VOL_TIER_THRESHOLDS = {"low": 0.002, "medium": 0.006, "high": 0.012}

exchange = ccxt.bybit({"options": {"defaultType": "swap"}})
last_prices, last_alert_time, last_4h_check = {}, {}, {}
app = Flask(__name__)

# === TELEGRAM ===
def send_telegram(msg: str):
    if not TELEGRAM_TOKEN or not CHAT_IDS:
        print("⚠️ Telegram non configurato correttamente.")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    for cid in CHAT_IDS:
        try:
            requests.post(url, data={"chat_id": cid, "text": msg, "parse_mode": "Markdown"}, timeout=10)
        except Exception as e:
            print(f"❌ Telegram error {cid}: {e}")

# === INDICATORI BASE ===
def ema(values, period):
    if len(values) < period:
        return None
    k = 2 / (period + 1)
    ema_val = values[0]
    for v in values[1:]:
        ema_val = v * k + ema_val * (1 - k)
    return ema_val

def compute_rsi(prices, period=14):
    if len(prices) < period + 1:
        return None
    gains = [max(prices[i] - prices[i - 1], 0) for i in range(1, len(prices))]
    losses = [max(prices[i - 1] - prices[i], 0) for i in range(1, len(prices))]
    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period
    if avg_loss == 0:
        return 100
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))

def compute_volatility_logreturns(closes):
    if len(closes) < 3:
        return 0.0
    try:
        return pstdev([math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes))])
    except Exception:
        return 0.0

def detect_volume_spike(vols):
    if len(vols) < 6:
        return False, 1.0
    avg_prev = mean(vols[-6:-1])
    ratio = vols[-1] / avg_prev if avg_prev else 1.0
    return ratio >= VOLUME_SPIKE_MULTIPLIER, ratio

def breakout_signal(closes, lookback=20):
    if len(closes) < lookback + 1:
        return 0, None
    recent = closes[-(lookback + 1):-1]
    last = closes[-1]
    hi, lo = max(recent), min(recent)
    if last > hi:
        return 20, f"Breakout sopra max {hi:.6f}"
    if last < lo:
        return -15, f"Rotto min {lo:.6f}"
    return 0, "Mercato in compressione — preparati per il breakout"

# === FETCH ===
def safe_fetch(symbol, tf, limit=120):
    try:
        ohlcv = exchange.fetch_ohlcv(symbol, tf, limit=limit)
        closes = [x[4] for x in ohlcv]
        vols = [x[5] for x in ohlcv]
        return closes, vols
    except Exception as e:
        print(f"⚠️ fetch {symbol} ({tf}): {e}")
        return None, None

def get_bybit_symbols():
    try:
        markets = exchange.load_markets()
        return [s for s, m in markets.items() if s.endswith(":USDT")]
    except Exception as e:
        print("⚠️ load_markets:", e)
        return []

# === SCORE / TREND ===
def score_analysis(prices, vols):
    score = 0
    reasons = []

    ema10 = ema(prices, 10)
    ema30 = ema(prices, 30)
    if ema10 and ema30:
        diff = ((ema10 - ema30) / ema30) * 100
        if diff > 0.6:
            score += 20
            reasons.append("Trend rialzista (EMA10>EMA30)")
        elif diff < -0.6:
            score -= 20
            reasons.append("Trend ribassista (EMA10<EMA30)")

    rsi = compute_rsi(prices)
    if rsi:
        if rsi > 70:
            score -= 10
            reasons.append(f"RSI alto ({rsi:.1f})")
        elif rsi < 35:
            score += 10
            reasons.append(f"RSI basso ({rsi:.1f})")

    bscore, breakout_txt = breakout_signal(prices)
    score += bscore
    reasons.append(breakout_txt)

    vol_spike, vol_ratio = detect_volume_spike(vols)
    if vol_spike:
        score += 25
        reasons.append(f"Volume SPIKE {vol_ratio:.1f}×")

    score = max(0, min(100, int(score + 50)))
    return score, reasons

# === FLASK ===
@app.route("/")
def home():
    return "✅ Bybit Bot attivo (Flask OK)"

# === MONITOR ===
def monitor_loop():
    symbols = get_bybit_symbols()
    print(f"✅ Trovati {len(symbols)} simboli su Bybit")
    last_heartbeat = 0

    while True:
        for symbol in symbols[:MAX_CANDIDATES_PER_CYCLE]:
            # ciclo base 1m-3m-1h
            for tf in FAST_TFS + SLOW_TFS:
                closes, vols = safe_fetch(symbol, tf, 60)
                if not closes:
                    continue

                price = closes[-1]
                key = f"{symbol}_{tf}"
                prev = last_prices.get(key, price)
                variation = ((price - prev) / prev) * 100 if prev else 0.0
                last_prices[key] = price

                threshold = THRESHOLDS.get(tf, 5.0)
                if abs(variation) >= threshold:
                    now_t = time.time()
                    if now_t - last_alert_time.get(key, 0) < COOLDOWN_SECONDS:
                        continue
                    last_alert_time[key] = now_t

                    trend = "📈 Rialzo" if variation > 0 else "📉 Crollo"
                    emoji = "🔥" if tf in FAST_TFS else "⚡"
                    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

                    msg = (
                        f"🔔 ALERT {emoji} *{symbol}* — variazione {variation:+.2f}% ({tf})\n"
                        f"💰 Prezzo attuale: {price:.6f}\n"
                        f"{trend} rilevato — attenzione al trend.\n"
                        f"[{now}]"
                    )
                    send_telegram(msg)

            # analisi predittiva 4h
            now_t = time.time()
            if now_t - last_4h_check.get(symbol, 0) >= LONG_ANALYSIS_INTERVAL:
                last_4h_check[symbol] = now_t
                closes, vols = safe_fetch(symbol, ANALYSIS_TF, 120)
                if not closes:
                    continue
                score, reasons = score_analysis(closes, vols)
                if score >= 60:
                    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
                    msg = (
                        f"🧭 *Analisi 4h* — *{symbol}*\n"
                        f"📊 Score: *{score}/100*\n"
                        f"💰 Prezzo: {closes[-1]:.6f}\n"
                    )
                    if score < 75:
                        msg += "🌀 Mercato in compressione — preparati per il breakout\n"
                    else:
                        msg += "✅ Conferma di trend probabile nelle prossime ore\n"
                    msg += "▪ " + "; ".join(reasons) + f"\n[{now}]"
                    send_telegram(msg)

        # Heartbeat
        if time.time() - last_heartbeat >= HEARTBEAT_INTERVAL_SEC:
            now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
            send_telegram(f"💓 Heartbeat — attivo ({now}) | Simboli: {len(symbols)}")
            last_heartbeat = time.time()

        time.sleep(LOOP_DELAY)

# === MAIN ===
if __name__ == "__main__":
    print(f"🚀 Avvio monitor Bybit — TF {FAST_TFS + SLOW_TFS + [ANALYSIS_TF]}")
    threading.Thread(target=lambda: app.run(host="0.0.0.0", port=PORT), daemon=True).start()
    threading.Thread(target=monitor_loop, daemon=True).start()
    while True:
        time.sleep(3600)
