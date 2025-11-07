# monitor_bybit_flask.py
# Versione 4.1 — Trend + RSI + MACD + Volume spike + breakout + scoring + report 12h
from __future__ import annotations
import os
import time
import threading
import math
import heapq
from statistics import mean, pstdev
from datetime import datetime, timezone
import requests
import ccxt
from flask import Flask

# -----------------------
# CONFIG (env or defaults)
# -----------------------
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_IDS = [cid.strip() for cid in os.getenv("TELEGRAM_CHAT_ID", "").split(",") if cid.strip()]
PORT = int(os.getenv("PORT", 10000))

FAST_TFS = os.getenv("FAST_TFS", "1m,3m").split(",")
SLOW_TFS = os.getenv("SLOW_TFS", "1h,4h").split(",")
LOOP_DELAY = int(os.getenv("LOOP_DELAY", 10))
HEARTBEAT_INTERVAL_SEC = int(os.getenv("HEARTBEAT_INTERVAL_SEC", 3600))
MAX_CANDIDATES_PER_CYCLE = int(os.getenv("MAX_CANDIDATES_PER_CYCLE", 1000))
COOLDOWN_SECONDS = int(os.getenv("COOLDOWN_SECONDS", 180))

# thresholds per timeframe
THRESH_1M = float(os.getenv("THRESH_1M", 7.5))
THRESH_3M = float(os.getenv("THRESH_3M", 11.5))
THRESH_1H = float(os.getenv("THRESH_1H", 17.0))
THRESH_4H = float(os.getenv("THRESH_4H", 12.0))
THRESHOLDS = {"1m": THRESH_1M, "3m": THRESH_3M, "1h": THRESH_1H, "4h": THRESH_4H}

# volume spike / filtering
VOLUME_SPIKE_MULTIPLIER = float(os.getenv("VOLUME_SPIKE_MULTIPLIER", 100.0))
VOLUME_SPIKE_MIN_VOLUME = float(os.getenv("VOLUME_SPIKE_MIN_VOLUME", 500000.0))
VOLUME_MIN_USD = float(os.getenv("VOLUME_MIN_USD", 1000000.0))
VOLUME_COOLDOWN_SECONDS = int(os.getenv("VOLUME_COOLDOWN_SECONDS", 300))

# volatility tiers
VOL_TIER_THRESHOLDS = {"low": 0.002, "medium": 0.006, "high": 0.012}

# report configuration
REPORT_INTERVAL_SEC = int(os.getenv("REPORT_12H_INTERVAL_SEC", 43200))  # 12 ore
TOP_N_REPORT = int(os.getenv("TOP_N_REPORT", 5))

# exchange + state
exchange = ccxt.bybit({"options": {"defaultType": "swap"}})
last_prices: dict = {}
last_alert_time: dict = {}
last_volume_alert_time: dict = {}
last_report_time = 0
report_cache = []
app = Flask(__name__)

# -----------------------
# Helpers: Telegram
# -----------------------
def send_telegram(text: str):
    if not TELEGRAM_TOKEN or not CHAT_IDS:
        print("❌ Telegram non configurato.")
        print(text)
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    for cid in CHAT_IDS:
        payload = {"chat_id": cid, "text": text, "disable_web_page_preview": True, "parse_mode": "Markdown"}
        try:
            r = requests.post(url, data=payload, timeout=10)
            if r.status_code != 200:
                print(f"⚠️ Telegram error {r.status_code}: {r.text}")
        except Exception as e:
            print(f"❌ Telegram send error: {e}")

# -----------------------
# Indicators
# -----------------------
def compute_ema(prices, period):
    if not prices or len(prices) < 2:
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
        diff = prices[i] - prices[i-1]
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
    macd = ema_fast - ema_slow if ema_fast and ema_slow else None
    if macd is None:
        return None, None
    macd_series = []
    for i in range(slow - 1, len(prices)):
        ef = compute_ema(prices[:i+1], fast)
        es = compute_ema(prices[:i+1], slow)
        macd_series.append(ef - es)
    if len(macd_series) < signal:
        return macd, None
    signal_line = compute_ema(macd_series, signal)
    return macd, signal_line

# -----------------------
# Analytics / scoring
# -----------------------
def compute_volatility_logreturns(closes):
    if len(closes) < 3:
        return 0.0
    lr = [math.log(closes[i]/closes[i-1]) for i in range(1, len(closes)) if closes[i-1] > 0]
    return pstdev(lr)

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

def breakout_score(closes, lookback=20):
    if len(closes) < lookback + 1:
        return 0, None
    recent = closes[-(lookback+1):-1]
    last = closes[-1]
    hi, lo = max(recent), min(recent)
    if last > hi:
        return 20, f"Breakout sopra max {hi:.6f}"
    if last < lo:
        return -10, f"Rotto min {lo:.6f}"
    return 0, None

def score_signals(prices, volumes, variation, tf):
    score, reasons = 0, []
    ema10, ema30 = compute_ema(prices, 10), compute_ema(prices, 30)
    direction = None
    if ema10 and ema30:
        diff_pct = ((ema10 - ema30) / ema30) * 100
        if diff_pct > 0.8:
            score += 20
            direction = "📈 rialzista"
            reasons.append(f"EMA10>EMA30 ({diff_pct:.2f}%)")
        elif diff_pct < -0.8:
            score -= 15
            direction = "📉 ribassista"
            reasons.append(f"EMA10<EMA30 ({diff_pct:.2f}%)")
    macd, signal = compute_macd(prices)
    if macd and signal:
        if macd > signal:
            score += 15
            reasons.append("MACD bullish")
        else:
            score -= 7
            reasons.append("MACD bearish")
    rsi = compute_rsi(prices)
    if rsi:
        if rsi < 35:
            score += 10
            reasons.append(f"RSI {rsi:.1f} (oversold)")
        elif rsi > 70:
            score -= 15
            reasons.append(f"RSI {rsi:.1f} (overbought)")
    bs, br = breakout_score(prices)
    score += bs
    if br:
        reasons.append(br)
    mag = min(25, abs(variation))
    if variation > 0:
        score += mag * 0.6
    else:
        score -= mag * 0.4
    _, vol_ratio = estimate_volatility_tier(prices, volumes)
    if vol_ratio >= VOLUME_SPIKE_MULTIPLIER:
        score += 25
        reasons.append(f"Vol spike {vol_ratio:.1f}×")
    elif vol_ratio >= 5:
        score += 5
        reasons.append(f"Vol ratio {vol_ratio:.1f}×")
    raw = max(-100, min(100, score))
    normalized = int((raw + 100) / 2)
    return normalized, reasons, {"rsi": rsi, "ema10": ema10, "ema30": ema30, "vol_ratio": vol_ratio, "direction": direction}

# -----------------------
# Flask endpoints
# -----------------------
@app.route("/")
def home():
    return "✅ Crypto Alert Bot (Bybit) — running"

@app.route("/test")
def test():
    send_telegram("🧪 TEST ALERT — bot online")
    return "Test sent", 200

# -----------------------
# Monitor loop
# -----------------------
def safe_fetch_ohlcv(symbol, tf, limit=120):
    try:
        ohlcv = exchange.fetch_ohlcv(symbol, timeframe=tf, limit=limit)
        closes = [c[4] for c in ohlcv]
        vols = [c[5] for c in ohlcv]
        return closes, vols
    except Exception as e:
        print(f"⚠️ fetch error {symbol} {tf}: {e}")
        return None, None

def get_bybit_derivative_symbols():
    try:
        markets = exchange.load_markets()
        return [s for s, m in markets.items() if m.get("type") == "swap" or s.endswith(":USDT")]
    except Exception as e:
        print("⚠️ load_markets:", e)
        return []

def monitor_loop():
    symbols = get_bybit_derivative_symbols()
    print(f"✅ {len(symbols)} simboli trovati")
    global last_report_time
    last_heartbeat = 0

    while True:
        start_cycle = time.time()
        for sym in symbols[:MAX_CANDIDATES_PER_CYCLE]:
            for tf in ["4h"]:
                closes, vols = safe_fetch_ohlcv(sym, tf, 120)
                if not closes:
                    continue
                price = closes[-1]
                score, reasons, d = score_signals(closes, vols, 0, tf)
                if score >= 60:
                    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
                    msg = (
                        f"🎯 Analisi {tf} — *{sym}*\n"
                        f"🇮🇹 Score: *{score}/100*\n💰 Prezzo: {price:.6f}\n"
                    )
                    if "Breakout" in ";".join(reasons):
                        msg += "🌀 Mercato in compressione — preparati per il breakout\n"
                    if d["direction"]:
                        msg += f"📊 Trend {d['direction']}\n"
                    msg += "▪ " + "; ".join(reasons) + f"\n{now}"
                    send_telegram(msg)
                    report_cache.append((score, sym, price, d["rsi"], d["vol_ratio"], d["direction"]))
                    if len(report_cache) > 200:
                        report_cache[:] = report_cache[-200:]

        # report ogni 12 ore
        if time.time() - last_report_time >= REPORT_INTERVAL_SEC and report_cache:
            top = heapq.nlargest(TOP_N_REPORT, report_cache, key=lambda x: x[0])
            text = "🏆 *Top 5 Coin più promettenti (ultime 12h)*\n"
            for sc, sm, pr, rsi, vr, direction in top:
                text += f"• {sm} — {direction or ''} | Score {sc}/100 | RSI {rsi:.1f} | Vol×{vr:.1f} | {pr:.6f}\n"
            text += "\n🕒 " + datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
            send_telegram(text)
            last_report_time = time.time()

        # heartbeat
        if time.time() - last_heartbeat >= HEARTBEAT_INTERVAL_SEC:
            send_telegram(f"💓 Heartbeat — bot attivo ({len(symbols)} simboli)")
            last_heartbeat = time.time()

        elapsed = time.time() - start_cycle
        if elapsed < LOOP_DELAY:
            time.sleep(LOOP_DELAY - elapsed)

# -----------------------
# Entrypoint
# -----------------------
if __name__ == "__main__":
    print(f"🚀 Avvio monitor Bybit — loop {LOOP_DELAY}s")
    threading.Thread(target=lambda: app.run(host="0.0.0.0", port=PORT), daemon=True).start()
    threading.Thread(target=monitor_loop, daemon=True).start()
    while True:
        time.sleep(3600)
