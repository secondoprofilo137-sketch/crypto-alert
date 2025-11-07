#!/usr/bin/env python3
# monitor_bybit_flask.py
# Versione 4.1 — Trend + RSI + MACD + Volume spike + Breakout + Scoring + RSI Filter (4h)
# Flask + multi-chat Telegram + cooldown

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
LOOP_DELAY = int(os.getenv("LOOP_DELAY", 10))
HEARTBEAT_INTERVAL_SEC = int(os.getenv("HEARTBEAT_INTERVAL_SEC", 3600))
MAX_CANDIDATES_PER_CYCLE = int(os.getenv("MAX_CANDIDATES_PER_CYCLE", 1000))
COOLDOWN_SECONDS = int(os.getenv("COOLDOWN_SECONDS", 180))

THRESHOLDS = {
    "1m": float(os.getenv("THRESH_1M", 7.5)),
    "3m": float(os.getenv("THRESH_3M", 11.5)),
    "1h": float(os.getenv("THRESH_1H", 17.0)),
}

VOLUME_SPIKE_MULTIPLIER = float(os.getenv("VOLUME_SPIKE_MULTIPLIER", 100.0))
VOLUME_MIN_USD = float(os.getenv("VOLUME_MIN_USD", 1000000.0))
VOLUME_COOLDOWN_SECONDS = int(os.getenv("VOLUME_COOLDOWN_SECONDS", 300))

REPORT_INTERVAL_HOURS = 6
REPORT_MIN_SCORE = 75  # soglia minima
REPORT_RSI_MIN = 35
REPORT_RSI_MAX = 70

VOL_TIER_THRESHOLDS = {"low": 0.002, "medium": 0.006, "high": 0.012}

exchange = ccxt.bybit({"options": {"defaultType": "swap"}})
last_prices = {}
last_alert_time = {}
last_volume_alert_time = {}
app = Flask(__name__)

# === TELEGRAM ===
def send_telegram(text: str):
    if not TELEGRAM_TOKEN or not CHAT_IDS:
        print("⚠️ Telegram non configurato")
        return
    for cid in CHAT_IDS:
        try:
            requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                data={"chat_id": cid, "text": text, "parse_mode": "Markdown"},
                timeout=10,
            )
        except Exception as e:
            print(f"❌ Telegram error: {e}")

# === INDICATORI ===
def compute_ema(prices, period):
    if not prices or len(prices) < period:
        return None
    k = 2 / (period + 1)
    ema = prices[0]
    for p in prices[1:]:
        ema = p * k + ema * (1 - k)
    return ema

def compute_rsi(prices, period=14):
    if len(prices) < period + 1:
        return None
    gains, losses = [], []
    for i in range(1, len(prices)):
        diff = prices[i] - prices[i - 1]
        gains.append(max(diff, 0))
        losses.append(max(-diff, 0))
    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))

def compute_macd(prices, fast=12, slow=26, signal=9):
    if len(prices) < slow:
        return None, None
    ema_fast = compute_ema(prices, fast)
    ema_slow = compute_ema(prices, slow)
    macd = ema_fast - ema_slow if ema_fast and ema_slow else 0
    signal_line = compute_ema([macd], signal)
    return macd, signal_line

# === ANALYTICS ===
def compute_volatility_logreturns(closes):
    if len(closes) < 3:
        return 0.0
    lr = [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes)) if closes[i - 1] > 0]
    return pstdev(lr) if len(lr) > 1 else 0.0

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
        if baseline > 0:
            vol_ratio = volumes[-1] / baseline
    return tier, vol_ratio

def detect_volume_spike(volumes, multiplier=VOLUME_SPIKE_MULTIPLIER):
    if len(volumes) < 6:
        return False, 1.0
    avg_prev = mean(volumes[-6:-1])
    if avg_prev <= 0:
        return False, 1.0
    ratio = volumes[-1] / avg_prev
    return ratio >= multiplier, ratio

# === SCORING ===
def score_signals(closes, vols, variation, tf):
    score = 0
    ema10 = compute_ema(closes, 10)
    ema30 = compute_ema(closes, 30)
    if ema10 and ema30:
        diff = (ema10 - ema30) / ema30 * 100
        if diff > 1: score += 20
        elif diff < -1: score -= 10
    macd, signal = compute_macd(closes)
    if macd and signal:
        if macd > signal: score += 10
        else: score -= 5
    rsi = compute_rsi(closes)
    if rsi:
        if rsi < 35: score += 10
        elif rsi > 70: score -= 10
    _, vol_ratio = estimate_volatility_tier(closes, vols)
    if vol_ratio >= VOLUME_SPIKE_MULTIPLIER:
        score += 25
    elif vol_ratio > 5:
        score += 5
    return min(100, max(0, score)), {"rsi": rsi}

# === EXCHANGE ===
def safe_fetch_ohlcv(symbol, timeframe, limit=120):
    try:
        ohlcv = exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
        closes = [c[4] for c in ohlcv]
        vols = [c[5] for c in ohlcv]
        return closes, vols
    except Exception:
        return None, None

def get_symbols():
    try:
        mkts = exchange.load_markets()
        return [s for s in mkts.keys() if s.endswith(":USDT")]
    except Exception:
        return []

# === FLASK ROUTES ===
@app.route("/")
def home():
    return "✅ Bybit Flask bot attivo"

@app.route("/test")
def test():
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    send_telegram(f"🧪 TEST OK — {now}")
    return "OK", 200

# === MONITOR LOOP ===
def monitor_loop():
    symbols = get_symbols()
    print(f"✅ Simboli trovati: {len(symbols)}")
    last_heartbeat = 0
    last_report_time = 0

    while True:
        # ---- PRICE ALERT ----
        for symbol in symbols[:MAX_CANDIDATES_PER_CYCLE]:
            for tf in FAST_TFS + SLOW_TFS:
                closes, vols = safe_fetch_ohlcv(symbol, tf)
                if not closes:
                    continue
                price = closes[-1]
                key = f"{symbol}_{tf}"
                if key not in last_prices:
                    last_prices[key] = price
                    continue
                variation = ((price - last_prices[key]) / last_prices[key]) * 100
                if abs(variation) >= THRESHOLDS.get(tf, 5):
                    score, data = score_signals(closes, vols, variation, tf)
                    rsi = data["rsi"]
                    trend = "📈 Rialzo" if variation > 0 else "📉 Crollo"
                    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
                    text = f"🔔 ALERT {symbol} {tf}\n{trend} {variation:+.2f}% | RSI {rsi:.1f} | Score {score}\n[{now}]"
                    send_telegram(text)
                last_prices[key] = price

        # ---- 4H TREND REPORT ----
        if time.time() - last_report_time >= REPORT_INTERVAL_HOURS * 3600:
            trend_candidates = []
            for sym in symbols[:MAX_CANDIDATES_PER_CYCLE]:
                closes, vols = safe_fetch_ohlcv(sym, "4h")
                if not closes:
                    continue
                score, data = score_signals(closes, vols, 0, "4h")
                rsi = data["rsi"]
                if not (rsi and REPORT_RSI_MIN <= rsi <= REPORT_RSI_MAX):
                    continue  # filtro RSI 35-70
                if score >= REPORT_MIN_SCORE:
                    trend_candidates.append((sym, score, rsi))
            if trend_candidates:
                trend_candidates.sort(key=lambda x: x[1], reverse=True)
                msg = "📊 *REPORT 4H — Coin in compressione*\n"
                for sym, score, rsi in trend_candidates[:10]:
                    msg += f"• *{sym}*: Score {score} | RSI {rsi:.1f}\n"
                msg += "\n💡 Mercato in compressione — preparati per un breakout."
                send_telegram(msg)
            last_report_time = time.time()

        # Heartbeat
        if time.time() - last_heartbeat >= HEARTBEAT_INTERVAL_SEC:
            now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
            send_telegram(f"💓 Heartbeat — attivo ({now}) | Simboli: {len(symbols)}")
            last_heartbeat = time.time()

        time.sleep(LOOP_DELAY)

# === MAIN ===
if __name__ == "__main__":
    print("🚀 Avvio monitor con filtro RSI 35–70 per 4H report")
    threading.Thread(target=lambda: app.run(host="0.0.0.0", port=PORT), daemon=True).start()
    threading.Thread(target=monitor_loop, daemon=True).start()
    while True:
        time.sleep(3600)
