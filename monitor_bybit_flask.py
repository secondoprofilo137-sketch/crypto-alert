#!/usr/bin/env python3
# monitor_bybit_flask.py
# Monitor Bybit — Flask + alerts 1m/3m/1h + Wyckoff reports 1h/4h + daily/weekly checks
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
CHAT_IDS = [cid.strip() for cid in os.getenv("TELEGRAM_CHAT_ID", "").split(",") if cid.strip()]
PORT = int(os.getenv("PORT", 10000))

FAST_TFS = os.getenv("FAST_TFS", "1m,3m").split(",")
SLOW_TFS = os.getenv("SLOW_TFS", "1h").split(",")
LOOP_DELAY = int(os.getenv("LOOP_DELAY", 10))
HEARTBEAT_INTERVAL_SEC = int(os.getenv("HEARTBEAT_INTERVAL_SEC", 3600))
MAX_CANDIDATES_PER_CYCLE = int(os.getenv("MAX_CANDIDATES_PER_CYCLE", 1000))
COOLDOWN_SECONDS = int(os.getenv("COOLDOWN_SECONDS", 7200))  # default 2h cooldown

# thresholds per timeframe (default match tuo requisito)
THRESH_1M = float(os.getenv("THRESH_1M", 7.5))
THRESH_3M = float(os.getenv("THRESH_3M", 11.5))
THRESH_1H = float(os.getenv("THRESH_1H", 17.0))
THRESHOLDS = {"1m": THRESH_1M, "3m": THRESH_3M, "1h": THRESH_1H}

# volume spike / filtering
VOLUME_SPIKE_MULTIPLIER = float(os.getenv("VOLUME_SPIKE_MULTIPLIER", 100.0))
VOLUME_SPIKE_MIN_VOLUME = float(os.getenv("VOLUME_SPIKE_MIN_VOLUME", 500000.0))
VOLUME_MIN_USD = float(os.getenv("VOLUME_MIN_USD", 1000000.0))
VOLUME_COOLDOWN_SECONDS = int(os.getenv("VOLUME_COOLDOWN_SECONDS", 300))

# Wyckoff / report settings
REPORT_INTERVAL_SEC = int(os.getenv("REPORT_INTERVAL_SEC", 4 * 3600))  # 4h
REPORT_H1_INTERVAL_SEC = int(os.getenv("REPORT_H1_INTERVAL_SEC", 3600))  # 1h
REPORT_TOP_N = int(os.getenv("REPORT_TOP_N", 10))
WYCKOFF_SCORE_THRESHOLD = int(os.getenv("WYCKOFF_SCORE_THRESHOLD", 70))

# volatility tiers
VOL_TIER_THRESHOLDS = {"low": 0.002, "medium": 0.006, "high": 0.012}

# -----------------------
# Exchange + state
# -----------------------
exchange = ccxt.bybit({"options": {"defaultType": "swap"}})
last_prices: dict = {}
last_alert_time: dict = {}
last_volume_alert_time: dict = {}
app = Flask(__name__)

# -----------------------
# Telegram helper
# -----------------------
def send_telegram(text: str):
    if not TELEGRAM_TOKEN or not CHAT_IDS:
        print("❌ Telegram non configurato (token/chat mancanti). Messaggio di debug:")
        print(text)
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    for cid in CHAT_IDS:
        payload = {"chat_id": cid, "text": text, "disable_web_page_preview": True, "parse_mode": "Markdown"}
        try:
            r = requests.post(url, data=payload, timeout=10)
            if r.status_code != 200:
                print(f"⚠️ Telegram error {r.status_code} for {cid}: {r.text}")
        except Exception as e:
            print(f"❌ Exception sending Telegram to {cid}: {e}")

# -----------------------
# Indicators (lightweight)
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
    if ema_fast is None or ema_slow is None:
        return None, None
    macd = ema_fast - ema_slow
    # approximate signal line
    macd_series = []
    for i in range(slow - 1, len(prices)):
        window = prices[: i + 1]
        ef = compute_ema(window, fast)
        es = compute_ema(window, slow)
        if ef is None or es is None:
            continue
        macd_series.append(ef - es)
    if len(macd_series) < signal:
        return macd, None
    signal_line = compute_ema(macd_series, signal)
    return macd, signal_line

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

# -----------------------
# Scoring & breakout
# -----------------------
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

def score_signals(prices, volumes, variation, tf):
    score = 0
    reasons = []

    ema10 = compute_ema(prices, 10)
    ema30 = compute_ema(prices, 30)
    if ema10 and ema30:
        diff_pct = ((ema10 - ema30) / ema30) * 100 if ema30 else 0
        if diff_pct > 0.8:
            score += 20; reasons.append(f"EMA10>EMA30 ({diff_pct:.2f}%)")
        elif diff_pct < -0.8:
            score -= 15; reasons.append(f"EMA10<EMA30 ({diff_pct:.2f}%)")

    macd, signal = compute_macd(prices)
    if macd is not None and signal is not None:
        if macd > signal:
            score += 15; reasons.append("MACD bullish")
        else:
            score -= 7; reasons.append("MACD bearish")

    rsi = compute_rsi(prices, 14)
    if rsi is not None:
        if rsi < 35:
            score += 10; reasons.append(f"RSI {rsi:.1f} (oversold)")
        elif rsi > 70:
            score -= 15; reasons.append(f"RSI {rsi:.1f} (overbought)")

    bs, br = breakout_score(prices, lookback=20)
    score += bs
    if br:
        reasons.append(br)

    mag = min(25, abs(variation))
    if variation > 0:
        score += mag * 0.6
    else:
        score -= mag * 0.4

    _, vol_ratio = estimate_volatility_tier(prices, volumes)
    if vol_ratio and vol_ratio >= VOLUME_SPIKE_MULTIPLIER:
        score += 25; reasons.append(f"Vol spike {vol_ratio:.1f}×")
    elif vol_ratio and vol_ratio >= 5:
        score += 5; reasons.append(f"Vol ratio {vol_ratio:.1f}×")

    raw = max(-100, min(100, score))
    normalized = int((raw + 100) / 2)
    return normalized, reasons

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

def get_bybit_symbols_derivatives():
    try:
        markets = exchange.load_markets()
        syms = []
        for symbol, meta in markets.items():
            if meta.get("type") == "swap" or symbol.endswith(":USDT") or "/USDT" in symbol:
                syms.append(symbol)
        return sorted(set(syms))
    except Exception as e:
        print("⚠️ load_markets:", e)
        return []

# -----------------------
# Volume spike detector
# -----------------------
def detect_volume_spike(volumes, multiplier=VOLUME_SPIKE_MULTIPLIER):
    if not volumes or len(volumes) < 6:
        return False, 1.0
    avg_prev = mean(volumes[-6:-1])
    last = volumes[-1]
    if avg_prev <= 0:
        return False, 1.0
    ratio = last / avg_prev
    return ratio >= multiplier, ratio

# -----------------------
# Wyckoff report builder
# -----------------------
def wyckoff_report(symbols, tf, tag):
    results = []
    for s in symbols:
        closes, vols = safe_fetch_ohlcv(s, tf, 120)
        if not closes:
            continue
        price = closes[-1]
        prev = closes[-2] if len(closes) > 1 else price
        var = ((price - prev) / prev) * 100 if prev else 0.0
        score, reasons = score_signals(closes, vols, var, tf)
        if score >= WYCKOFF_SCORE_THRESHOLD:
            vt, vr = estimate_volatility_tier(closes, vols)
            e10 = compute_ema(closes, 10); e30 = compute_ema(closes, 30)
            diffpct = abs((e10 - e30) / e30) * 100 if e10 and e30 else 0
            compress = (vt == "LOW" and diffpct < 0.5)
            note = " — **mercato in compressione, preparati per il breakout** 🔼" if compress else ""
            reasons_text = "; ".join(reasons) if reasons else "-"
            results.append((score, f"*{s}* | Score *{score}/100* | Prezzo {price:.6f} | Var {var:+.2f}%{note}\n▪ {reasons_text}"))

    if results:
        results.sort(reverse=True, key=lambda x: x[0])
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        body = "\n\n".join([r[1] for r in results[:REPORT_TOP_N]])
        msg = f"WYCKOFF — Report {tag} ({now})\n\n{body}"
        send_telegram(msg)

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
# Monitor loop
# -----------------------
def monitor_loop():
    symbols = get_bybit_symbols_derivatives()
    print(f"✅ Simboli derivati Bybit trovati: {len(symbols)}")
    last_hb = 0
    last_r4 = 0
    last_r1 = 0
    first_run = True

    while True:
        cycle_start = time.time()
        for symbol in symbols[:MAX_CANDIDATES_PER_CYCLE]:
            for tf in FAST_TFS + SLOW_TFS:
                closes, vols = safe_fetch_ohlcv(symbol, tf, 120)
                if not closes:
                    continue

                price = closes[-1]
                key = f"{symbol}_{tf}"

                # initialization: store price and skip alerting on first pass for each key
                if key not in last_prices:
                    last_prices[key] = price
                    continue

                prev = last_prices[key]
                try:
                    variation = ((price - prev) / prev) * 100 if prev else 0.0
                except Exception:
                    variation = 0.0

                threshold = THRESHOLDS.get(tf, 5.0)

                # volume spike detection (use only where useful, e.g. 1m)
                vol_spike, vol_ratio = False, 1.0
                if len(vols) >= 6:
                    vol_spike, vol_ratio = detect_volume_spike(vols, multiplier=VOLUME_SPIKE_MULTIPLIER)

                # approximate USD turnover for last candle (works for symbol/USDT)
                usd_volume = 0.0
                try:
                    if symbol.endswith(":USDT") or "/USDT" in symbol:
                        usd_volume = price * vols[-1]
                except Exception:
                    usd_volume = 0.0

                alert_price_move = abs(variation) >= threshold
                alert_volume = vol_spike and (usd_volume >= VOLUME_MIN_USD or vols[-1] >= VOLUME_SPIKE_MIN_VOLUME)

                now_t = time.time()
                # volume cooldown
                if alert_volume:
                    last_vol_ts = last_volume_alert_time.get(symbol, 0)
                    if now_t - last_vol_ts < VOLUME_COOLDOWN_SECONDS:
                        alert_volume = False

                if alert_price_move or alert_volume:
                    last_ts = last_alert_time.get(key, 0)
                    if now_t - last_ts < COOLDOWN_SECONDS:
                        # skip because of cooldown
                        pass
                    else:
                        score, reasons = score_signals(closes, vols, variation, tf)
                        vt, volratio = estimate_volatility_tier(closes, vols)
                        trend = "📈 Rialzo" if variation > 0 else "📉 Crollo"
                        now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

                        # build message (coin bold)
                        text = (
                            f"🔔 ALERT -> {'🔥' if tf in FAST_TFS else '⚡'} *{symbol}* — variazione {variation:+.2f}% (ref {tf})\n"
                            f"💰 Prezzo attuale: {price:.6f}\n"
                        )

                        if alert_volume:
                            text += f"🚨 *Volume SPIKE* — {vol_ratio:.1f}× sopra la media | Volume candle: {vols[-1]:.0f} ({usd_volume:.0f} USD)\n"

                        text += (
                            f"{trend} rilevato — attenzione al trend.\n"
                            f"📊 Volatilità: {vt} | Vol ratio: {volratio:.2f}×\n"
                            f"🔎 Score predittivo: *{score}/100*\n"
                        )
                        if reasons:
                            text += "▪ " + "; ".join(reasons) + "\n"
                        text += f"[{now_str}]"

                        send_telegram(text)
                        last_alert_time[key] = now_t
                        if alert_volume:
                            last_volume_alert_time[symbol] = now_t

                # update last price
                last_prices[key] = price

        # heartbeat
        tnow = time.time()
        if tnow - last_hb >= HEARTBEAT_INTERVAL_SEC:
            now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
            send_telegram(f"💓 Heartbeat — bot attivo ({now_str}) | Simboli monitorati: {len(symbols)}")
            last_hb = tnow

        # Wyckoff reports
        if tnow - last_r4 >= REPORT_INTERVAL_SEC:
            threading.Thread(target=wyckoff_report, args=(symbols, "4h", "4h"), daemon=True).start()
            last_r4 = tnow
        if tnow - last_r1 >= REPORT_H1_INTERVAL_SEC:
            threading.Thread(target=wyckoff_report, args=(symbols, "1h", "1h"), daemon=True).start()
            last_r1 = tnow

        first_run = False
        # sleep to respect loop delay (account for cycle execution time)
        elapsed = time.time() - cycle_start
        if elapsed < LOOP_DELAY:
            time.sleep(LOOP_DELAY - elapsed)

# -----------------------
# Entrypoint
# -----------------------
if __name__ == "__main__":
    print(f"🚀 Avvio monitor Bybit — TF {FAST_TFS + SLOW_TFS}, thresholds {THRESHOLDS}, loop {LOOP_DELAY}s")
    # start flask for keepalive
    threading.Thread(target=lambda: app.run(host="0.0.0.0", port=PORT), daemon=True).start()
    # start monitor background thread
    threading.Thread(target=monitor_loop, daemon=True).start()
    # block main
    while True:
        time.sleep(3600)
