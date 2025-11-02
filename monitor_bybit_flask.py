#!/usr/bin/env python3
# monitor_bybit_flask.py
# Versione "Disciplined Trader" — per Render Web Service
# - Multi-chat Telegram
# - Alert 1m / 3m / 1h con soglie personalizzate
# - Trend detection (EMA10/EMA30) + forza trend + rottura massimi/minimi
# - Report 4h top 10 coin
# - Flask server per uptime ping

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
LOOP_DELAY = int(os.getenv("LOOP_DELAY", 10))
HEARTBEAT_INTERVAL_SEC = int(os.getenv("HEARTBEAT_INTERVAL_SEC", 3600))
MAX_CANDIDATES_PER_CYCLE = int(os.getenv("MAX_CANDIDATES_PER_CYCLE", 1000))
COOLDOWN_SECONDS = int(os.getenv("COOLDOWN_SECONDS", 180))

THRESHOLDS = {
    "1m": float(os.getenv("THRESH_1M", 7.5)),
    "3m": float(os.getenv("THRESH_3M", 11.5)),
    "1h": float(os.getenv("THRESH_1H", 17.0)),
}

REPORT_4H_INTERVAL_SEC = int(os.getenv("REPORT_4H_INTERVAL_SEC", 14400))
TOP_N_REPORT = int(os.getenv("TOP_N_REPORT", 10))

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

app = Flask(__name__)

# === FLASK ===
@app.route("/")
def home():
    return "✅ Crypto Alert Bot — Flask attivo"

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
            print(f"❌ Telegram error: {e}")

# === ANALYTICS ===
def compute_ema(closes, period=10):
    if not closes or len(closes) < 2:
        return None
    k = 2 / (period + 1)
    ema = closes[0]
    for p in closes[1:]:
        ema = p * k + (1 - k) * ema
    return ema

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
        last_vol = volumes[-1]
        if baseline:
            vol_ratio = last_vol / baseline
    return f"📊 Volatilità: {tier.upper()} | Stop: {mapping['stop_pct'][0]:.1f}%→{mapping['stop_pct'][1]:.1f}% | Vol ratio: {vol_ratio:.2f}×"

def compute_trend_strength(closes):
    if not closes or len(closes) < 10:
        return {"direction": "neutral", "strength_pct": 0.0, "breakouts": None}
    fast = compute_ema(closes[-30:], 10)
    slow = compute_ema(closes[-60:], 30)
    if not fast or not slow:
        return {"direction": "neutral", "strength_pct": 0.0, "breakouts": None}
    direction = "bull" if fast > slow else "bear" if fast < slow else "neutral"
    strength_pct = abs((fast - slow) / slow) * 100 if slow else 0.0
    recent_high = max(closes[-30:])
    recent_low = min(closes[-30:])
    current = closes[-1]
    breakout = {
        "breaks_high": current >= recent_high,
        "breaks_low": current <= recent_low,
    }
    return {"direction": direction, "strength_pct": strength_pct, "breakouts": breakout}

# === EXCHANGE ===
def safe_fetch(symbol, tf, limit=120):
    try:
        data = exchange.fetch_ohlcv(symbol, tf, limit=limit)
        closes = [c[4] for c in data]
        vols = [c[5] for c in data]
        return closes, vols
    except Exception:
        return None, None

def get_bybit_symbols():
    try:
        markets = exchange.load_markets()
        return [s for s, m in markets.items() if m.get("type") == "swap" or s.endswith(":USDT")]
    except Exception as e:
        print("⚠️ load_markets error:", e)
        return []

# === REPORT 4H ===
def generate_4h_report(symbols):
    results = []
    for s in symbols:
        closes, vols = safe_fetch(s, "4h", 200)
        if not closes:
            continue
        t = compute_trend_strength(closes)
        score = t["strength_pct"] * (2 if t["direction"] != "neutral" else 1)
        results.append((s, t, score))
    results.sort(key=lambda x: x[2], reverse=True)
    top = results[:TOP_N_REPORT]
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    report = [f"📈 *Report 4H — Top {TOP_N_REPORT} trend coin* ({now})"]
    for s, t, sc in top:
        direction = "RIALZISTA" if t["direction"] == "bull" else "RIBASSISTA" if t["direction"] == "bear" else "LATERALE"
        force = f"{t['strength_pct']:.2f}%"
        bo = t["breakouts"]
        note = " 🔺 rottura massimi" if bo["breaks_high"] else " 🔻 rottura minimi" if bo["breaks_low"] else ""
        report.append(f"*{s}* — {direction} | Forza {force}{note}")
    return "\n".join(report)

# === MONITOR ===
def monitor_loop():
    symbols = get_bybit_symbols()
    print(f"✅ Simboli Bybit derivati trovati: {len(symbols)}")
    last_heartbeat = 0
    last_report = 0

    # inizializzazione (no alert)
    for s in symbols[:MAX_CANDIDATES_PER_CYCLE]:
        for tf in FAST_TFS + SLOW_TFS:
            closes, _ = safe_fetch(s, tf, 2)
            if closes:
                last_prices[f"{s}_{tf}"] = closes[-1]

    while True:
        for s in symbols[:MAX_CANDIDATES_PER_CYCLE]:
            for tf in FAST_TFS + SLOW_TFS:
                closes, vols = safe_fetch(s, tf, 120)
                if not closes:
                    continue
                price = closes[-1]
                key = f"{s}_{tf}"
                prev = last_prices.get(key, price)
                variation = ((price - prev) / prev) * 100 if prev else 0.0
                threshold = THRESHOLDS.get(tf, 5.0)
                now_t = time.time()

                if abs(variation) >= threshold and (now_t - last_alert_time.get(key, 0)) >= COOLDOWN_SECONDS:
                    suggestion = estimate_risk_and_suggestion(closes, vols)
                    tinfo = compute_trend_strength(closes)
                    trend = "📈 Rialzo" if variation > 0 else "📉 Crollo"
                    emoji = "🔥" if tf in FAST_TFS else "⚡"
                    force = f"{tinfo['strength_pct']:.2f}%"
                    note = "🔺 Rottura massimi" if tinfo["breakouts"]["breaks_high"] else "🔻 Rottura minimi" if tinfo["breakouts"]["breaks_low"] else ""
                    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

                    msg = (
                        f"🔔 ALERT -> {emoji} *{s}* — variazione {variation:+.2f}% (ref {tf})\n"
                        f"💰 Prezzo attuale: {price:.6f}\n"
                        f"{trend} — forza trend {force}\n"
                        f"{note}\n"
                        f"{suggestion}\n[{now}]"
                    )
                    send_telegram(msg)
                    last_alert_time[key] = now_t
                last_prices[key] = price

        if time.time() - last_heartbeat >= HEARTBEAT_INTERVAL_SEC:
            now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
            send_telegram(f"💓 Heartbeat — bot attivo ({now}) | Simboli: {len(symbols)}")
            last_heartbeat = time.time()

        if time.time() - last_report >= REPORT_4H_INTERVAL_SEC:
            send_telegram(generate_4h_report(symbols))
            last_report = time.time()

        time.sleep(LOOP_DELAY)

# === MAIN ===
if __name__ == "__main__":
    print(f"🚀 Bybit Monitor — TF {FAST_TFS + SLOW_TFS}, soglie {THRESHOLDS}, loop {LOOP_DELAY}s")
    threading.Thread(target=lambda: app.run(host="0.0.0.0", port=PORT), daemon=True).start()
    threading.Thread(target=monitor_loop, daemon=True).start()
    while True:
        time.sleep(3600)
