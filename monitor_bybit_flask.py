#!/usr/bin/env python3
# monitor_bybit_flask.py
# Monitor Bybit — alert percentuali (1m,3m,1h) + Wyckoff report 4h (analisi 1h,4h,daily,weekly)
# Italiano — pronto per Render (Flask + background monitor)

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
# CONFIG (env / defaults)
# -----------------------
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_IDS = [cid.strip() for cid in os.getenv("TELEGRAM_CHAT_ID", "").split(",") if cid.strip()]
PORT = int(os.getenv("PORT", 10000))

# timeframes monitored for immediate alerts
FAST_TFS = os.getenv("FAST_TFS", "1m,3m").split(",")
SLOW_TFS = os.getenv("SLOW_TFS", "1h").split(",")

LOOP_DELAY = int(os.getenv("LOOP_DELAY", 10))                     # seconds between cycles
HEARTBEAT_INTERVAL_SEC = int(os.getenv("HEARTBEAT_INTERVAL_SEC", 3600))
MAX_CANDIDATES_PER_CYCLE = int(os.getenv("MAX_CANDIDATES_PER_CYCLE", 1000))

# thresholds percentuali per timeframe (default come da tuoi input)
THRESH_1M = float(os.getenv("THRESH_1M", 7.5))
THRESH_3M = float(os.getenv("THRESH_3M", 11.5))
THRESH_1H = float(os.getenv("THRESH_1H", 17.0))
THRESHOLDS = {"1m": THRESH_1M, "3m": THRESH_3M, "1h": THRESH_1H}

# Wyckoff report (4h)
REPORT_INTERVAL_SEC = int(os.getenv("REPORT_INTERVAL_SEC", 4 * 3600))  # every 4h
REPORT_TOP_N = int(os.getenv("REPORT_TOP_N", 10))
REPORT_SCORE_MIN = int(os.getenv("REPORT_SCORE_MIN", 70))
REPORT_COOLDOWN_SEC = int(os.getenv("REPORT_COOLDOWN_SEC", 4 * 3600))  # avoid duplicate reports too often

# scoring / volatility params
VOLUME_SPIKE_MULTIPLIER = float(os.getenv("VOLUME_SPIKE_MULTIPLIER", 100.0))
VOL_TIER_THRESHOLDS = {"low": 0.002, "medium": 0.006, "high": 0.012}

# cooldowns
COOLDOWN_SECONDS = int(os.getenv("COOLDOWN_SECONDS", 2 * 3600))  # 2 hours default

# exchange + state
exchange = ccxt.bybit({"options": {"defaultType": "swap"}})
last_prices: dict = {}             # key = symbol_tf -> last price used as reference
last_alert_time: dict = {}         # key -> ts of last alert (price alerts)
last_report_ts = 0
app = Flask(__name__)

# -----------------------
# TELEGRAM helper
# -----------------------
def send_telegram(text: str):
    """Invia a tutti i chat id definiti. Usa Markdown per il bold del simbolo."""
    if not TELEGRAM_TOKEN or not CHAT_IDS:
        print("❌ Telegram non configurato (TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID mancanti). Messaggio:")
        print(text)
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    for cid in CHAT_IDS:
        try:
            payload = {"chat_id": cid, "text": text, "parse_mode": "Markdown", "disable_web_page_preview": True}
            r = requests.post(url, data=payload, timeout=10)
            if r.status_code != 200:
                print(f"⚠️ Errore Telegram per {cid}: {r.status_code} {r.text}")
        except Exception as e:
            print(f"❌ Errore invio Telegram a {cid}: {e}")

# -----------------------
# INDICATORS / SCORING (semplice, leggero)
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
        d = prices[i] - prices[i-1]
        gains.append(max(d, 0))
        losses.append(max(-d, 0))
    avg_g = sum(gains[-period:]) / period
    avg_l = sum(losses[-period:]) / period
    if avg_l == 0:
        return 100.0
    rs = avg_g / avg_l
    return 100 - (100 / (1 + rs))

def compute_macd(prices, fast=12, slow=26, signal=9):
    if len(prices) < slow:
        return None, None
    ef = compute_ema(prices, fast)
    es = compute_ema(prices, slow)
    if ef is None or es is None:
        return None, None
    macd = ef - es
    # approximate signal by computing macd series ema
    macd_series = []
    for i in range(slow - 1, len(prices)):
        ef_i = compute_ema(prices[:i+1], fast)
        es_i = compute_ema(prices[:i+1], slow)
        if ef_i is not None and es_i is not None:
            macd_series.append(ef_i - es_i)
    sig = compute_ema(macd_series, signal) if len(macd_series) >= signal else None
    return macd, sig

def compute_volatility_logreturns(closes):
    if len(closes) < 3:
        return 0.0
    lr = []
    for i in range(1, len(closes)):
        if closes[i-1] > 0:
            lr.append(math.log(closes[i] / closes[i-1]))
    return pstdev(lr) if len(lr) > 1 else 0.0

def estimate_volatility_tier(closes, volumes):
    v = compute_volatility_logreturns(closes[-30:]) if len(closes) >= 30 else compute_volatility_logreturns(closes)
    if v < VOL_TIER_THRESHOLDS["low"]:
        tier = "LOW"
    elif v < VOL_TIER_THRESHOLDS["medium"]:
        tier = "MEDIUM"
    elif v < VOL_TIER_THRESHOLDS["high"]:
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

# scoring for Wyckoff report (simple composite)
def score_for_report(closes, volumes, variation):
    score = 0
    reasons = []
    ema10 = compute_ema(closes, 10)
    ema30 = compute_ema(closes, 30)
    if ema10 and ema30:
        diff_pct = ((ema10 - ema30) / ema30) * 100 if ema30 else 0
        if diff_pct > 0.8:
            score += 20; reasons.append(f"EMA10>EMA30 ({diff_pct:.2f}%)")
        elif diff_pct < -0.8:
            score -= 10; reasons.append(f"EMA10<EMA30 ({diff_pct:.2f}%)")
    macd, sig = compute_macd(closes)
    if macd is not None and sig is not None:
        if macd > sig:
            score += 15; reasons.append("MACD bullish")
        else:
            score -= 7; reasons.append("MACD bearish")
    rsi = compute_rsi(closes)
    if rsi is not None:
        if rsi < 35:
            score += 10; reasons.append(f"RSI {rsi:.1f} (oversold)")
        elif rsi > 70:
            score -= 10; reasons.append(f"RSI {rsi:.1f} (overbought)")
    # breakout simple
    if len(closes) >= 21:
        recent = closes[-21:-1]
        hi, lo = max(recent), min(recent)
        last = closes[-1]
        if last > hi:
            score += 20; reasons.append("Breakout sopra massimo recente")
        elif last < lo:
            score -= 10; reasons.append("Rotto minimo recente")
    # variation adds weight
    mag = min(25, abs(variation))
    if variation > 0:
        score += mag * 0.6
    else:
        score -= mag * 0.4
    # volatility / volume ratio
    _, volratio = estimate_volatility_tier(closes, volumes)
    if volratio >= VOLUME_SPIKE_MULTIPLIER:
        score += 25; reasons.append(f"Vol spike {volratio:.1f}×")
    elif volratio >= 5:
        score += 5; reasons.append(f"Vol ratio {volratio:.1f}×")
    # normalize -100..100 -> 0..100
    raw = max(-100, min(100, score))
    norm = int((raw + 100) / 2)
    return norm, reasons

# -----------------------
# BYBIT helpers
# -----------------------
def safe_fetch_ohlcv(symbol, timeframe, limit=120):
    try:
        ohlcv = exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
        closes = [c[4] for c in ohlcv]
        vols = [c[5] for c in ohlcv]
        return closes, vols
    except Exception as e:
        # log then return None so caller skips
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
        print("⚠️ load_markets error:", e)
        return []

# -----------------------
# WYCKOFF REPORT (4h) - single notification with top N
# -----------------------
def build_wyckoff_report(symbols):
    out = []
    for s in symbols:
        # analyze multiple TFs: 1h,4h,daily,weekly
        try:
            c1h, v1h = safe_fetch_ohlcv(s, "1h", limit=120)
            c4h, v4h = safe_fetch_ohlcv(s, "4h", limit=120)
            cd, vd = safe_fetch_ohlcv(s, "1d", limit=60)
            cw, vw = safe_fetch_ohlcv(s, "1w", limit=60)
            if not c1h or not c4h:
                continue
            # compute simple combined score (weighted)
            # use latest var on 1h for immediate momentum
            price = c1h[-1]
            prev = c1h[-2] if len(c1h) > 1 else price
            var_1h = ((price - prev) / prev) * 100 if prev else 0.0
            score1, r1 = score_for_report(c1h[-60:], v1h[-60:], var_1h)
            score4, r4 = (score_for_report(c4h[-60:], v4h[-60:], ((c4h[-1] - (c4h[-2] if len(c4h)>1 else c4h[-1]))/ (c4h[-2] if len(c4h)>1 else c4h[-1]))*100) if c4h else (0, []))
            scored = int((score1 * 0.4) + (score4 * 0.4) + ( (score_for_report(cd[-60:], vd[-60:], 0)[0] if cd else 0) * 0.2 ))
            reasons = r1 + (r4 if isinstance(r4, list) else [])
            out.append((s, scored, price, var_1h, reasons))
        except Exception as e:
            # skip symbol on errors
            continue
    # sort by score desc
    out_sorted = sorted(out, key=lambda x: x[1], reverse=True)
    return out_sorted[:REPORT_TOP_N]

# -----------------------
# FLASK endpoints
# -----------------------
app = Flask(__name__)

@app.route("/")
def home():
    return "✅ Crypto Alert Bot (Bybit) — running"

@app.route("/test")
def test_route():
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    send_telegram(f"🧪 TEST ALERT — bot online ({now})")
    return "Test inviato", 200

# -----------------------
# MAIN monitor loop
# -----------------------
def monitor_loop():
    global last_report_ts
    symbols = get_bybit_derivative_symbols()
    print(f"✅ Simboli derivati trovati: {len(symbols)}")
    first_cycle = True
    last_hb = 0

    while True:
        start = time.time()
        for s in symbols[:MAX_CANDIDATES_PER_CYCLE]:
            # iterate the immediate TFs
            for tf in FAST_TFS + SLOW_TFS:
                closes, vols = safe_fetch_ohlcv(s, tf, limit=120)
                if not closes:
                    continue
                price = closes[-1]
                key = f"{s}_{tf}"

                # Initialize reference price on first observation
                if key not in last_prices:
                    last_prices[key] = price
                    continue

                prev = last_prices.get(key, price)
                try:
                    variation = ((price - prev) / prev) * 100 if prev else 0.0
                except Exception:
                    variation = 0.0

                threshold = THRESHOLDS.get(tf, 5.0)
                if abs(variation) >= threshold:
                    now_ts = time.time()
                    last_ts = last_alert_time.get(key, 0)
                    if now_ts - last_ts >= COOLDOWN_SECONDS:
                        # build and send alert
                        vt, volratio = estimate_volatility_tier(closes, vols)
                        trend = "📈 Rialzo" if variation > 0 else "📉 Crollo"
                        nowtxt = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
                        text = (
                            f"🔔 ALERT -> {'🔥' if tf in FAST_TFS else '⚡'} *{s}* — variazione {variation:+.2f}% (ref {tf})\n"
                            f"💰 Prezzo attuale: {price:.6f}\n"
                            f"{trend} rilevato — attenzione al trend.\n"
                            f"📊 Volatilità: {vt} | Vol ratio: {volratio:.2f}×\n"
                            f"[{nowtxt}]"
                        )
                        send_telegram(text)
                        last_alert_time[key] = now_ts

                # update last price (measure next against this)
                last_prices[key] = price

        # Wyckoff report every REPORT_INTERVAL_SEC: single message with top N
        tnow = time.time()
        if (tnow - last_report_ts) >= REPORT_INTERVAL_SEC:
            # spawn thread to compute report (so it doesn't block)
            def _do_report():
                syms = get_bybit_derivative_symbols()
                report = build_wyckoff_report(syms)
                if report:
                    nowtxt = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
                    lines = [f"🔍 WYCKOFF — Report 4h ({nowtxt}) — top {len(report)} (soglia {REPORT_SCORE_MIN})"]
                    count = 0
                    for s, score, price, var1h, reasons in report:
                        if score < REPORT_SCORE_MIN:
                            continue
                        count += 1
                        reasons_txt = "; ".join(reasons) if reasons else "-"
                        lines.append(f"*{s}* | Score *{score}/100* | Prezzo {price:.6f} | Var 1h {var1h:+.2f}%\n▪ {reasons_txt}")
                        if count >= REPORT_TOP_N:
                            break
                    if count > 0:
                        send_telegram("\n\n".join(lines))
            try:
                threading.Thread(target=_do_report, daemon=True).start()
                last_report_ts = tnow
            except Exception as e:
                print("⚠️ Errore avvio report thread:", e)

        # Heartbeat
        if tnow - last_hb >= HEARTBEAT_INTERVAL_SEC:
            send_telegram(f"💓 Heartbeat — bot attivo ({datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}) | Simboli: {len(symbols)}")
            last_hb = tnow

        # sleep respecting loop delay
        elapsed = time.time() - start
        if elapsed < LOOP_DELAY:
            time.sleep(LOOP_DELAY - elapsed)

# -----------------------
# Entrypoint
# -----------------------
if __name__ == "__main__":
    print(f"🚀 Avvio monitor_bybit_flask — TF {FAST_TFS + SLOW_TFS}, thresholds {THRESHOLDS}, loop {LOOP_DELAY}s")
    # start flask server in background thread (so Render sees the web process)
    threading.Thread(target=lambda: app.run(host="0.0.0.0", port=PORT), daemon=True).start()
    # start monitor loop
    threading.Thread(target=monitor_loop, daemon=True).start()
    # keep main alive
    while True:
        time.sleep(3600)
