#!/usr/bin/env python3
# ✅ Bybit Derivatives Monitor — Flask + Multi-Chat + Volume Spike + Trend Report (4h, 1d)
# Versione 3.0

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
VOLUME_SPIKE_MULTIPLIER = float(os.getenv("VOLUME_SPIKE_MULTIPLIER", 50.0))
VOLUME_MINIMUM_USD = float(os.getenv("VOLUME_MINIMUM_USD", 1000000.0))

# === TREND REPORT CONFIG ===
TREND_TFS = os.getenv("TREND_TFS", "4h,1d").split(",")
TREND_TOP_N = int(os.getenv("TREND_TOP_N", 10))
COINGLASS_API_KEY = os.getenv("COINGLASS_API_KEY", "").strip()

VOL_TIER_THRESHOLDS = {"low": 0.002, "medium": 0.006, "high": 0.012}

exchange = ccxt.bybit({"options": {"defaultType": "swap"}})
last_prices, last_alerts = {}, {}
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
                "parse_mode": "Markdown",
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
def detect_volume_spike(volumes):
    if len(volumes) < 6:
        return False, 1.0
    avg_prev = mean(volumes[-6:-1])
    last_vol = volumes[-1]
    if avg_prev == 0:
        return False, 1.0
    ratio = last_vol / avg_prev
    return ratio >= VOLUME_SPIKE_MULTIPLIER and last_vol >= VOLUME_MINIMUM_USD, ratio

# === TREND REPORT ===
_TF_HOURS = {"4h": 4, "1d": 24}
_last_trend_report = {tf: 0 for tf in TREND_TFS}

def compute_ema_simple(prices, period):
    if not prices or len(prices) < 2:
        return None
    k = 2 / (period + 1)
    ema = prices[0]
    for p in prices[1:]:
        ema = p * k + (1 - k) * ema
    return ema

def detect_breakouts(closes, highs, lows, lookback=12):
    if not closes or len(closes) < 2:
        return False, False, None, None
    max_prev = max(highs[-(lookback + 1):-1]) if len(highs) > 1 else None
    min_prev = min(lows[-(lookback + 1):-1]) if len(lows) > 1 else None
    last_close = closes[-1]
    break_high = max_prev and last_close > max_prev
    break_low = min_prev and last_close < min_prev
    return break_high, break_low, max_prev, min_prev

def build_trend_score(symbol, closes, highs, lows):
    ema10 = compute_ema_simple(closes[-40:], 10) if len(closes) >= 10 else None
    ema30 = compute_ema_simple(closes[-60:], 30) if len(closes) >= 30 else None
    break_high, break_low, _, _ = detect_breakouts(closes, highs, lows)
    vol = compute_volatility_logreturns(closes[-30:])
    score = 0.0
    if ema10 and ema30:
        rel = (ema10 - ema30) / ema30 if ema30 else 0
        score += rel * 10
    if break_high:
        score += 5
    if break_low:
        score -= 5
    score += vol * 100
    if score > 6:
        label = "🟢 RIALZISTA FORTE"
    elif score > 1.5:
        label = "🟢 RIALZISTA"
    elif score > -1.5:
        label = "⚪ LATERALE"
    else:
        label = "🔴 RIBASSISTA"
    return score, label

def generate_and_send_trend_report(tf):
    symbols = get_bybit_symbols()
    results = []
    for symbol in symbols:
        try:
            ohlcv = exchange.fetch_ohlcv(symbol, timeframe=tf, limit=120)
            closes = [c[4] for c in ohlcv]
            highs = [c[2] for c in ohlcv]
            lows = [c[3] for c in ohlcv]
            score, label = build_trend_score(symbol, closes, highs, lows)
            results.append({"symbol": symbol, "score": score, "label": label})
        except Exception:
            continue
    top = sorted(results, key=lambda x: x["score"], reverse=True)[:TREND_TOP_N]
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    lines = [f"📊 TREND REPORT — {tf} — Top {TREND_TOP_N} — {now}\n"]
    for i, item in enumerate(top, start=1):
        lines.append(f"{i}. **{item['symbol']}** — {item['label']} (score {item['score']:.2f})")
    send_telegram("\n".join(lines))

def trend_report_worker():
    while True:
        now_ts = time.time()
        for tf in TREND_TFS:
            hours = _TF_HOURS.get(tf, 4)
            if now_ts - _last_trend_report.get(tf, 0) >= hours * 3600:
                try:
                    print(f"🧭 Generazione report {tf} in corso…")
                    generate_and_send_trend_report(tf)
                except Exception as e:
                    print("⚠️ Errore trend report:", e)
                _last_trend_report[tf] = time.time()
        time.sleep(300)

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

                threshold = FAST_THRESHOLD if tf == "1m" else SLOW_THRESHOLD_FAST if tf == "3m" else SLOW_THRESHOLD
                alert_triggered = abs(variation) >= threshold
                volume_spike = False
                vol_ratio = 1.0
                if tf == "1m":
                    volume_spike, vol_ratio = detect_volume_spike(vols)
                if alert_triggered or volume_spike:
                    vol_tier, volratio = estimate_volatility_tier(closes, vols)
                    trend = "📈 Rialzo" if variation > 0 else "📉 Crollo"
                    emoji = "🔥" if tf in FAST_TFS else "⚡"
                    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
                    text = (
                        f"🔔 ALERT -> {emoji} **{symbol}** — variazione {variation:+.2f}% (ref {tf})\n"
                        f"💰 Prezzo attuale: {price:.6f}\n"
                    )
                    if volume_spike:
                        text += f"🚨 Volume SPIKE rilevato — {vol_ratio:.1f}× sopra la media!\n"
                    text += (
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
    threading.Thread(target=trend_report_worker, daemon=True).start()
    while True:
        time.sleep(3600)
