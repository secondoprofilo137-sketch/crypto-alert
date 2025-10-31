#!/usr/bin/env python3
# Bybit derivatives monitor — Flask + multi Telegram + trend detection + breakout analysis

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
THRESHOLDS = {"1m": 7.5, "3m": 11.5, "1h": 17.0}

LOOP_DELAY = 10
HEARTBEAT_INTERVAL_SEC = 3600
MAX_CANDIDATES_PER_CYCLE = 1000

VOL_TIER_THRESHOLDS = {"low": 0.002, "medium": 0.006, "high": 0.012}
RISK_MAPPING = {
    "low":    {"stop_pct": (-0.6, -1.0)},
    "medium": {"stop_pct": (-1.0, -2.0)},
    "high":   {"stop_pct": (-2.0, -4.0)},
    "very":   {"stop_pct": (-4.0, -8.0)},
}

exchange = ccxt.bybit({"options": {"defaultType": "swap"}})
last_prices, last_alerts = {}, {}

app = Flask(__name__)

# === FLASK ROUTES ===
@app.route("/")
def home():
    return "✅ Crypto Alert Bot (Bybit) attivo e operativo"

@app.route("/test")
def test():
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    send_telegram(f"🧪 TEST OK — {now}")
    return "Messaggio di test inviato a Telegram", 200

# === TELEGRAM ===
def send_telegram(message: str):
    if not TELEGRAM_TOKEN or not CHAT_IDS:
        print("❌ Telegram non configurato correttamente")
        return
    for chat_id in CHAT_IDS:
        try:
            url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
            payload = {"chat_id": chat_id, "text": message, "parse_mode": "Markdown", "disable_web_page_preview": True}
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
    if volumes and len(volumes) >= 2:
        baseline = mean(volumes[-6:-1]) if len(volumes) >= 6 else mean(volumes[:-1])
        last_vol = volumes[-1]
        if baseline:
            vol_ratio = last_vol / baseline
    return (
        mapping,
        f"📊 Volatilità: {tier.upper()} | Stop: {mapping['stop_pct'][0]:.1f}%→{mapping['stop_pct'][1]:.1f}% | Vol ratio: {vol_ratio:.2f}×"
    )

# === TREND DETECTION ===
def compute_trend_direction(closes, short_period=10, long_period=30):
    """Analizza la direzione e forza del trend basata su EMA10/EMA30."""
    if len(closes) < long_period + 1:
        return "➖ Trend: laterale (dati insufficienti)"
    def ema(values, period):
        k = 2 / (period + 1)
        ema_val = values[0]
        for v in values[1:]:
            ema_val = v * k + (1 - k) * ema_val
        return ema_val
    ema_short = ema(closes[-(short_period + 1):], short_period)
    ema_long = ema(closes[-(long_period + 1):], long_period)
    if not ema_long:
        return "➖ Trend: non disponibile"
    diff_pct = ((ema_short - ema_long) / ema_long) * 100
    strength = abs(diff_pct)
    if strength < 0.3:
        force = "debole"
    elif strength < 1.0:
        force = "moderato"
    elif strength < 2.0:
        force = "forte"
    else:
        force = "molto forte"
    if diff_pct > 0:
        return f"📈 Trend: RIALZISTA ({force})"
    elif diff_pct < 0:
        return f"📉 Trend: RIBASSISTA ({force})"
    else:
        return "➖ Trend: laterale"

def detect_breakout(closes, lookback=30):
    """Rileva la rottura di massimi/minimi recenti."""
    if len(closes) < lookback:
        return "📊 Nessuna rottura (dati insufficienti)"
    current = closes[-1]
    high_recent = max(closes[-lookback:])
    low_recent = min(closes[-lookback:])
    if current >= high_recent * 0.999:
        return "🚀 ROTTURA MASSIMI BREVE TERMINE"
    elif current <= low_recent * 1.001:
        return "⚠️ ROTTURA MINIMI BREVE TERMINE"
    else:
        return "📊 Range intermedio"

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
                threshold = THRESHOLDS.get(tf, 10)
                if abs(variation) >= threshold:
                    kl_closes, kl_vols = safe_fetch(symbol, tf, 60)
                    _, suggestion = estimate_risk_and_suggestion(kl_closes or closes, kl_vols or vols)
                    trend_info = compute_trend_direction(kl_closes or closes)
                    breakout_info = detect_breakout(kl_closes or closes)
                    trend = "📈 Rialzo" if variation > 0 else "📉 Crollo"
                    emoji = "🔥" if tf in FAST_TFS else "⚡"
                    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
                    text = (
                        f"🔔 ALERT -> {emoji} **{symbol}** — variazione {variation:+.2f}% (ref {tf})\n"
                        f"💰 Prezzo attuale: {price:.6f}\n"
                        f"{trend} rilevato — attenzione al trend.\n"
                        f"{trend_info}\n{breakout_info}\n"
                        f"{suggestion}\n[{now}]"
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
    print(f"🚀 Avvio monitor Bybit — thresholds 1m={THRESHOLDS['1m']}%, 3m={THRESHOLDS['3m']}%, 1h={THRESHOLDS['1h']}%")
    threading.Thread(target=lambda: app.run(host="0.0.0.0", port=PORT), daemon=True).start()
    threading.Thread(target=monitor_loop, daemon=True).start()
    while True:
        time.sleep(3600)
