#!/usr/bin/env python3
# monitor_bybit_flask.py
# Bybit monitor — Flask + multi-chat Telegram + spike + scoring + 4h Wyckoff reports
from __future__ import annotations
import os
import time
import threading
import math
from statistics import mean, pstdev
from datetime import datetime, timezone
import requests
import ccxt
from flask import Flask

# -----------------------
# CONFIG (env or defaults)
# -----------------------
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
# Multiple chat ids separated by comma
CHAT_IDS = [cid.strip() for cid in os.getenv("TELEGRAM_CHAT_ID", "").split(",") if cid.strip()]
PORT = int(os.getenv("PORT", 10000))

FAST_TFS = os.getenv("FAST_TFS", "1m,3m").split(",")
SLOW_TFS = os.getenv("SLOW_TFS", "1h").split(",")
REPORT_TF = os.getenv("REPORT_TF", "4h")  # timeframe used for 4h report (kept as env for flexibility)
LOOP_DELAY = int(os.getenv("LOOP_DELAY", 10))
HEARTBEAT_INTERVAL_SEC = int(os.getenv("HEARTBEAT_INTERVAL_SEC", 3600))
MAX_CANDIDATES_PER_CYCLE = int(os.getenv("MAX_CANDIDATES_PER_CYCLE", 1000))
COOLDOWN_SECONDS = int(os.getenv("COOLDOWN_SECONDS", 180))  # per-symbol+tf cooldown for price alerts

# thresholds per timeframe
THRESH_1M = float(os.getenv("THRESH_1M", 7.5))
THRESH_3M = float(os.getenv("THRESH_3M", 11.5))
THRESH_1H = float(os.getenv("THRESH_1H", 17.0))
THRESHOLDS = {"1m": THRESH_1M, "3m": THRESH_3M, "1h": THRESH_1H}

# volume spike / filtering
VOLUME_SPIKE_MULTIPLIER = float(os.getenv("VOLUME_SPIKE_MULTIPLIER", 100.0))
VOLUME_SPIKE_MIN_VOLUME = float(os.getenv("VOLUME_SPIKE_MIN_VOLUME", 500000.0))  # base asset units
VOLUME_MIN_USD = float(os.getenv("VOLUME_MIN_USD", 1000000.0))  # turnover USD min to trigger
VOLUME_COOLDOWN_SECONDS = int(os.getenv("VOLUME_COOLDOWN_SECONDS", 300))

# report settings (4h): every N seconds, and min score threshold
REPORT_INTERVAL_SEC = int(os.getenv("REPORT_INTERVAL_SEC", 6 * 3600))  # every 6 hours
REPORT_TOP_N = int(os.getenv("REPORT_TOP_N", 10))
REPORT_SCORE_MIN = int(os.getenv("REPORT_SCORE_MIN", 60))

# volatility tiers
VOL_TIER_THRESHOLDS = {"low": 0.002, "medium": 0.006, "high": 0.012}

# exchange + state
exchange = ccxt.bybit({"options": {"defaultType": "swap"}})
last_prices: dict = {}           # key = symbol_tf -> last price stored
last_alert_time: dict = {}       # key = symbol_tf -> ts
last_volume_alert_time: dict = {}  # symbol -> ts
app = Flask(__name__)

# -----------------------
# Telegram helper
# -----------------------
def send_telegram(text: str):
    """Invia a tutti i chat_ids configurati. Se telegram non è settato, stampa in console."""
    if not TELEGRAM_TOKEN or not CHAT_IDS:
        print("❌ Telegram non configurato (TOKEN/CHAT_ID mancanti). Messaggio di debug:")
        print(text)
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    success = True
    for cid in CHAT_IDS:
        payload = {
            "chat_id": cid,
            "text": text,
            "disable_web_page_preview": True,
            "parse_mode": "Markdown"
        }
        try:
            r = requests.post(url, data=payload, timeout=10)
            if r.status_code != 200:
                print(f"⚠️ Errore Telegram {r.status_code} per {cid}: {r.text}")
                success = False
        except Exception as e:
            print(f"❌ Exception sending Telegram to {cid}: {e}")
            success = False
    return success

# -----------------------
# Indicators
# -----------------------
def compute_ema(prices, period):
    if not prices or len(prices) < 2:
        return None
    k = 2.0 / (period + 1)
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
    if ema_fast is None or ema_slow is None:
        return None, None
    macd = ema_fast - ema_slow
    # approximate signal line by computing ema of macd over the series
    macd_series = []
    for i in range(slow - 1, len(prices)):
        w = prices[: i + 1]
        ef = compute_ema(w, fast)
        es = compute_ema(w, slow)
        if ef is None or es is None:
            continue
        macd_series.append(ef - es)
    if len(macd_series) < signal:
        return macd, None
    signal_line = compute_ema(macd_series, signal)
    return macd, signal_line

# -----------------------
# Scoring & helpers
# -----------------------
def compute_volatility_logreturns(closes):
    if len(closes) < 3:
        return 0.0
    lr = []
    for i in range(1, len(closes)):
        if closes[i-1] > 0:
            lr.append(math.log(closes[i] / closes[i-1]))
    if len(lr) < 2:
        return 0.0
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
    hi = max(recent)
    lo = min(recent)
    if last > hi:
        return 20, f"Breakout sopra max {hi:.6f}"
    if last < lo:
        return -10, f"Rotto min {lo:.6f}"
    return 0, None

def detect_volume_spike(volumes, multiplier=VOLUME_SPIKE_MULTIPLIER):
    if not volumes or len(volumes) < 6:
        return False, 1.0
    avg_prev = mean(volumes[-6:-1]) if len(volumes) >= 6 else mean(volumes[:-1])
    last = volumes[-1]
    if avg_prev <= 0:
        return False, 1.0
    ratio = last / avg_prev
    return ratio >= multiplier, ratio

def score_signals(prices, volumes, variation, tf):
    score = 0
    reasons = []
    # EMA 10/30
    ema10 = compute_ema(prices, 10)
    ema30 = compute_ema(prices, 30)
    if ema10 and ema30:
        diff_pct = ((ema10 - ema30) / ema30) * 100 if ema30 else 0.0
        if diff_pct > 0.8:
            score += 20
            reasons.append(f"EMA10>EMA30 ({diff_pct:.2f}%)")
        elif diff_pct < -0.8:
            score -= 15
            reasons.append(f"EMA10<EMA30 ({diff_pct:.2f}%)")
    # MACD
    macd, signal = compute_macd(prices)
    if macd is not None and signal is not None:
        if macd > signal:
            score += 15
            reasons.append("MACD bullish")
        else:
            score -= 7
            reasons.append("MACD bearish")
    # RSI
    rsi = compute_rsi(prices, 14)
    if rsi is not None:
        if rsi < 35:
            score += 10
            reasons.append(f"RSI {rsi:.1f} (oversold)")
        elif rsi > 70:
            score -= 15
            reasons.append(f"RSI {rsi:.1f} (overbought)")
    # breakout
    bs, br = breakout_score(prices, lookback=20)
    score += bs
    if br:
        reasons.append(br)
    # variation magnitude weight
    mag = min(25, abs(variation))
    if variation > 0:
        score += mag * 0.6
    else:
        score -= mag * 0.4
    # volume ratio
    _, vol_ratio = estimate_volatility_tier(prices, volumes)
    if vol_ratio and vol_ratio >= VOLUME_SPIKE_MULTIPLIER:
        score += 25
        reasons.append(f"Vol spike {vol_ratio:.1f}×")
    elif vol_ratio and vol_ratio >= 5:
        score += 5
        reasons.append(f"Vol ratio {vol_ratio:.1f}×")
    raw = max(-100, min(100, score))
    normalized = int((raw + 100) / 2)
    return normalized, reasons, {"rsi": rsi, "macd": macd, "signal": signal, "ema10": ema10, "ema30": ema30, "vol_ratio": vol_ratio}

# -----------------------
# Exchange helpers
# -----------------------
def safe_fetch_ohlcv(symbol, timeframe, limit=120):
    try:
        ohlcv = exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
        closes = [c[4] for c in ohlcv]
        vols = [c[5] for c in ohlcv]
        return closes, vols
    except Exception as e:
        print(f"⚠️ fetch error {symbol} {timeframe}: {e}")
        return None, None

def get_bybit_derivative_symbols():
    try:
        markets = exchange.load_markets()
        syms = []
        for s, m in markets.items():
            if m.get("type") == "swap" or s.endswith(":USDT") or "/USDT" in s:
                syms.append(s)
        return sorted(set(syms))
    except Exception as e:
        print("⚠️ load_markets:", e)
        return []

# -----------------------
# Flask endpoints
# -----------------------
@app.route("/")
def home():
    return "✅ Crypto Alert Bot (Bybit) — running"

@app.route("/test")
def test():
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    send_telegram(f"🧪 TEST ALERT — bot online ({now})")
    return "Test sent", 200

# -----------------------
# 4h Report builder
# -----------------------
def build_and_send_4h_report(symbols):
    """Analizza simboli su timeframe 4h (REPORT_TF) e invia top N con score > REPORT_SCORE_MIN.
       Prefisso 'WYCKOFF' richiesto, e messaggio 'mercato in compressione...' se rilevata compressione."""
    results = []
    for s in symbols:
        closes, vols = safe_fetch_ohlcv(s, REPORT_TF, limit=120)
        if not closes:
            continue
        price = closes[-1]
        # compute score (variation measured vs last stored price if exists)
        key = f"{s}_{REPORT_TF}"
        prev = last_prices.get(key, closes[-2] if len(closes) >= 2 else price)
        try:
            variation = ((price - prev) / prev) * 100 if prev else 0.0
        except Exception:
            variation = 0.0
        score, reasons, details = score_signals(closes, vols, variation, REPORT_TF)
        results.append((s, score, variation, reasons, details, price, closes, vols))
    # sort descending by score, filter > REPORT_SCORE_MIN
    filtered = [r for r in sorted(results, key=lambda x: x[1], reverse=True) if r[1] >= REPORT_SCORE_MIN]
    top = filtered[:REPORT_TOP_N]
    if not top:
        print("🔎 Report 4h: nessun simbolo con score sufficiente.")
        return
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    header = f"WYCKOFF — Report {REPORT_TF} top{REPORT_TOP_N} (score>={REPORT_SCORE_MIN}) — {now}\n"
    lines = [header]
    for (s, score, variation, reasons, details, price, closes, vols) in top:
        vol_tier, volratio = estimate_volatility_tier(closes, vols)
        # detect compression: low volatility & EMA close together
        ema10 = details.get("ema10")
        ema30 = details.get("ema30")
        compression = False
        if ema10 and ema30:
            diffpct = abs((ema10 - ema30) / ema30) * 100 if ema30 else 0.0
            if vol_tier == "LOW" and diffpct < 0.5:
                compression = True
        note = " — mercato in compressione, preparati per il breakout" if compression else ""
        reasons_text = ("; ".join(reasons)) if reasons else ""
        lines.append(f"*{s}* | Score: *{score}/100* | Prezzo: {price:.6f} | Var: {variation:+.2f}%{note}\n▪ {reasons_text}\n")
    message = "\n".join(lines)
    send_telegram(message)

# -----------------------
# Monitor loop
# -----------------------
def monitor_loop():
    symbols = get_bybit_derivative_symbols()
    print(f"✅ Simboli derivati trovati: {len(symbols)}")
    last_heartbeat = 0
    last_report_ts = 0

    # Ensure we initialize last_prices for stable measurement: store the latest price for each symbol/tf first loop
    first_pass = True

    while True:
        cycle_start = time.time()
        for symbol in symbols[:MAX_CANDIDATES_PER_CYCLE]:
            for tf in FAST_TFS + SLOW_TFS:
                closes, vols = safe_fetch_ohlcv(symbol, tf, 120)
                if not closes:
                    continue
                price = closes[-1]
                key = f"{symbol}_{tf}"

                # initialize on first pass -> skip alerting first time to avoid spam
                if first_pass:
                    last_prices[key] = price
                    continue

                prev = last_prices.get(key, price)
                try:
                    variation = ((price - prev) / prev) * 100 if prev else 0.0
                except Exception:
                    variation = 0.0

                threshold = THRESHOLDS.get(tf, 5.0)

                # detect volume spike (1m only by default; adjustable)
                vol_spike, vol_ratio = detect_volume_spike(vols, multiplier=VOLUME_SPIKE_MULTIPLIER)
                # approx USD volume if symbol quote is USDT
                usd_volume = 0.0
                try:
                    if symbol.endswith(":USDT") or "/USDT" in symbol:
                        usd_volume = price * vols[-1]
                except Exception:
                    usd_volume = 0.0

                alert_price_move = abs(variation) >= threshold
                alert_volume = vol_spike and (usd_volume >= VOLUME_MIN_USD or vols[-1] >= VOLUME_SPIKE_MIN_VOLUME)

                now_t = time.time()
                # enforce cooldown for volume alerts per symbol
                if alert_volume:
                    last_vol_ts = last_volume_alert_time.get(symbol, 0)
                    if now_t - last_vol_ts < VOLUME_COOLDOWN_SECONDS:
                        alert_volume = False

                # enforce cooldown per key for price alerts
                if alert_price_move:
                    last_key_ts = last_alert_time.get(key, 0)
                    if now_t - last_key_ts < COOLDOWN_SECONDS:
                        alert_price_move = False

                if alert_price_move or alert_volume:
                    # build enriched message: score + reasons
                    score, reasons, details = score_signals(closes, vols, variation, tf)
                    vol_tier, volratio = estimate_volatility_tier(closes, vols)
                    trend = "📈 Rialzo" if variation > 0 else "📉 Crollo"
                    emoji = "🔥" if tf in FAST_TFS else "⚡"
                    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

                    # construct message; coin in bold as requested
                    text = (
                        f"🔔 ALERT -> {emoji} *{symbol}* — variazione {variation:+.2f}% (ref {tf})\n"
                        f"💰 Prezzo attuale: {price:.6f}\n"
                    )
                    if alert_volume:
                        text += f"🚨 *Volume SPIKE* — {vol_ratio:.1f}× sopra media | Volume candle: {vols[-1]:.0f} ({usd_volume:.0f} USD)\n"
                    text += (
                        f"{trend} rilevato — attenzione al trend.\n"
                        f"📊 Volatilità: {vol_tier} | Vol ratio: {volratio:.2f}×\n"
                        f"🔎 Score predittivo: *{score}/100*\n"
                    )
                    if reasons:
                        text += "▪ " + "; ".join(reasons) + "\n"
                    # If this alert is from 4h report triggers, we will prefix WYCKOFF in the report sending path (report builder).
                    text += f"[{now}]"

                    send_telegram(text)
                    # update cooldown/state
                    last_alert_time[key] = now_t
                    if alert_volume:
                        last_volume_alert_time[symbol] = now_t

                # always update last price after processing
                last_prices[key] = price

        first_pass = False

        # heartbeat
        if time.time() - last_heartbeat >= HEARTBEAT_INTERVAL_SEC:
            now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
            send_telegram(f"💓 Heartbeat — bot attivo ({now}) | Simboli monitorati: {len(symbols)}")
            last_heartbeat = time.time()

        # report every REPORT_INTERVAL_SEC (4h analysis)
        if time.time() - last_report_ts >= REPORT_INTERVAL_SEC:
            # run report in separate thread to avoid blocking
            threading.Thread(target=build_and_send_4h_report, args=(symbols,), daemon=True).start()
            last_report_ts = time.time()

        elapsed = time.time() - cycle_start
        if elapsed < LOOP_DELAY:
            time.sleep(LOOP_DELAY - elapsed)

# -----------------------
# Entrypoint
# -----------------------
if __name__ == "__main__":
    print(f"🚀 Avvio monitor Bybit — TF {FAST_TFS + SLOW_TFS} + report {REPORT_TF}, thresholds {THRESHOLDS}, loop {LOOP_DELAY}s")
    # start flask (keepalive) in background thread
    threading.Thread(target=lambda: app.run(host="0.0.0.0", port=PORT), daemon=True).start()
    # start monitor loop
    threading.Thread(target=monitor_loop, daemon=True).start()
    # keep main thread alive
    while True:
        time.sleep(3600)
