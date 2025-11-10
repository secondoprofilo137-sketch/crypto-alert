#!/usr/bin/env python3
# monitor_bybit_flask.py
# 🔥 Bybit Monitor — Alerts 1m/3m/1h + Wyckoff 4h + Wyckoff Daily

import os, time, threading, math
from statistics import mean, pstdev
from datetime import datetime, timezone
import requests, ccxt
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

THRESHOLDS = {"1m": 7.5, "3m": 11.5, "1h": 17.0}

REPORT_TOP_N = int(os.getenv("REPORT_TOP_N", 10))
REPORT_SCORE_MIN = int(os.getenv("REPORT_SCORE_MIN", 70))
REPORT_INTERVAL_SEC = int(os.getenv("REPORT_INTERVAL_SEC", 14400))  # 4h
REPORT_DAILY_INTERVAL_SEC = int(os.getenv("REPORT_DAILY_INTERVAL_SEC", 86400))  # 24h

VOL_TIER_THRESHOLDS = {"low": 0.002, "medium": 0.006, "high": 0.012}
VOLUME_SPIKE_MULTIPLIER = 100.0
VOLUME_MIN_USD = 1_000_000.0

exchange = ccxt.bybit({"options": {"defaultType": "swap"}})
last_prices, last_alert_time = {}, {}
app = Flask(__name__)

# === TELEGRAM ===
def send_telegram(msg: str):
    if not TELEGRAM_TOKEN or not CHAT_IDS:
        print("⚠️ Telegram non configurato.")
        return
    for cid in CHAT_IDS:
        try:
            requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                data={"chat_id": cid, "text": msg, "parse_mode": "Markdown"},
                timeout=10,
            )
        except Exception as e:
            print("❌ Telegram error:", e)

# === INDICATORI ===
def compute_ema(prices, period):
    if len(prices) < 2: return None
    k = 2 / (period + 1)
    ema = prices[0]
    for p in prices[1:]:
        ema = p * k + ema * (1 - k)
    return ema

def compute_rsi(prices, period=14):
    if len(prices) < period + 1: return None
    gains, losses = [], []
    for i in range(1, len(prices)):
        diff = prices[i] - prices[i - 1]
        gains.append(max(diff, 0))
        losses.append(max(-diff, 0))
    ag, al = sum(gains[-period:]) / period, sum(losses[-period:]) / period
    if al == 0: return 100
    rs = ag / al
    return 100 - (100 / (1 + rs))

def compute_macd(prices, fast=12, slow=26, signal=9):
    if len(prices) < slow: return None, None
    ef, es = compute_ema(prices, fast), compute_ema(prices, slow)
    if not ef or not es: return None, None
    macd = ef - es
    series = []
    for i in range(slow - 1, len(prices)):
        ef2, es2 = compute_ema(prices[:i+1], fast), compute_ema(prices[:i+1], slow)
        if ef2 and es2: series.append(ef2 - es2)
    sig = compute_ema(series, signal) if len(series) >= signal else None
    return macd, sig

def compute_volatility(closes):
    if len(closes) < 3: return 0
    lr = [math.log(closes[i]/closes[i-1]) for i in range(1, len(closes)) if closes[i-1] > 0]
    return pstdev(lr) if len(lr) >= 2 else 0

def estimate_vol_tier(closes, vols):
    v = compute_volatility(closes[-30:]) if len(closes) >= 30 else compute_volatility(closes)
    if v < VOL_TIER_THRESHOLDS["low"]: tier = "LOW"
    elif v < VOL_TIER_THRESHOLDS["medium"]: tier = "MEDIUM"
    elif v < VOL_TIER_THRESHOLDS["high"]: tier = "HIGH"
    else: tier = "VERY HIGH"
    base = mean(vols[-6:-1]) if len(vols) >= 6 else mean(vols)
    ratio = vols[-1] / base if base else 1
    return tier, ratio

def breakout_score(closes, lookback=20):
    if len(closes) < lookback + 1: return 0, None
    recent, last = closes[-(lookback+1):-1], closes[-1]
    hi, lo = max(recent), min(recent)
    if last > hi: return 20, f"Breakout sopra max {hi:.6f}"
    if last < lo: return -10, f"Rotto min {lo:.6f}"
    return 0, None

def score_signals(prices, vols, variation):
    score, reasons = 0, []
    e10, e30 = compute_ema(prices, 10), compute_ema(prices, 30)
    if e10 and e30:
        diff = ((e10 - e30) / e30) * 100
        if diff > 0.8: score += 20; reasons.append(f"EMA10>EMA30 ({diff:.2f}%)")
        elif diff < -0.8: score -= 15; reasons.append(f"EMA10<EMA30 ({diff:.2f}%)")
    macd, sig = compute_macd(prices)
    if macd and sig:
        if macd > sig: score += 15; reasons.append("MACD bullish")
        else: score -= 7; reasons.append("MACD bearish")
    rsi = compute_rsi(prices)
    if rsi:
        if rsi < 35: score += 10; reasons.append(f"RSI {rsi:.1f} (oversold)")
        elif rsi > 70: score -= 15; reasons.append(f"RSI {rsi:.1f} (overbought)")
    bs, br = breakout_score(prices)
    score += bs
    if br: reasons.append(br)
    mag = min(25, abs(variation))
    score += mag * 0.6 if variation > 0 else -mag * 0.4
    _, vr = estimate_vol_tier(prices, vols)
    if vr >= VOLUME_SPIKE_MULTIPLIER: score += 25; reasons.append(f"Vol spike {vr:.1f}×")
    elif vr >= 5: score += 5; reasons.append(f"Vol ratio {vr:.1f}×")
    return int((max(-100, min(100, score)) + 100) / 2), reasons

# === EXCHANGE ===
def safe_fetch(symbol, tf, limit=120):
    try:
        data = exchange.fetch_ohlcv(symbol, tf, limit=limit)
        return [x[4] for x in data], [x[5] for x in data]
    except Exception as e:
        print(f"⚠️ fetch {symbol} {tf}: {e}")
        return None, None

def get_symbols():
    try:
        m = exchange.load_markets()
        return [s for s,x in m.items() if x.get("type")=="swap" or s.endswith(":USDT") or "/USDT" in s]
    except Exception as e:
        print("⚠️ load_markets:", e)
        return []

# === WYCKOFF REPORT ===
def build_wyckoff_report(symbols, tf="4h"):
    results = []
    for s in symbols:
        closes, vols = safe_fetch(s, tf)
        if not closes: continue
        price = closes[-1]
        prev = closes[-2] if len(closes) > 1 else price
        var = ((price - prev) / prev) * 100 if prev else 0
        score, reasons = score_signals(closes, vols, var)
        if score >= REPORT_SCORE_MIN:
            vt, _ = estimate_vol_tier(closes, vols)
            e10, e30 = compute_ema(closes, 10), compute_ema(closes, 30)
            diff = abs((e10 - e30) / e30) * 100 if e10 and e30 else 0
            compress = vt == "LOW" and diff < 0.6
            note = " — mercato in compressione, preparati per il breakout" if compress else ""
            reasons_txt = "; ".join(reasons)
            results.append(f"*{s}* | Score *{score}/100* | Prezzo {price:.6f} | Var {var:+.2f}%{note}\n▪ {reasons_txt}")
    return results

# === FLASK ===
@app.route("/")
def home(): return "✅ Crypto Alert Bot — Flask OK"
@app.route("/test")
def test():
    send_telegram("🧪 TEST OK — bot attivo")
    return "Test inviato", 200

# === MONITOR ===
def monitor_loop():
    syms = get_symbols()
    print(f"✅ Trovati {len(syms)} simboli Bybit derivati")
    last_report_4h = 0
    last_report_daily = 0
    first = True

    while True:
        for s in syms[:MAX_CANDIDATES_PER_CYCLE]:
            for tf in FAST_TFS + SLOW_TFS:
                closes, vols = safe_fetch(s, tf, 120)
                if not closes: continue
                price = closes[-1]
                key = f"{s}_{tf}"
                if first:
                    last_prices[key] = price
                    continue
                prev = last_prices.get(key, price)
                var = ((price - prev) / prev) * 100 if prev else 0
                th = THRESHOLDS.get(tf, 5)
                if abs(var) >= th:
                    now = time.time()
                    if now - last_alert_time.get(key, 0) < COOLDOWN_SECONDS:
                        continue
                    score, reasons = score_signals(closes, vols, var)
                    vt, vr = estimate_vol_tier(closes, vols)
                    trend = "📈 Rialzo" if var > 0 else "📉 Crollo"
                    nowtxt = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
                    msg = (
                        f"🔔 ALERT *{s}* ({tf}) {var:+.2f}%\n"
                        f"💰 Prezzo: {price:.6f}\n{trend}\n"
                        f"📊 Volatilità: {vt} | Vol ratio {vr:.2f}×\n"
                        f"🔎 Score {score}/100\n▪ {'; '.join(reasons)}\n[{nowtxt}]"
                    )
                    send_telegram(msg)
                    last_alert_time[key] = now
                last_prices[key] = price
        first = False

        t = time.time()
        if t - last_report_4h >= REPORT_INTERVAL_SEC:
            threading.Thread(
                target=lambda: send_telegram(
                    "📘 WYCKOFF 4h\n" + "\n".join(build_wyckoff_report(syms, "4h")[:REPORT_TOP_N])
                ),
                daemon=True,
            ).start()
            last_report_4h = t

        if t - last_report_daily >= REPORT_DAILY_INTERVAL_SEC:
            threading.Thread(
                target=lambda: send_telegram(
                    "📗 WYCKOFF DAILY\n" + "\n".join(build_wyckoff_report(syms, "1d")[:REPORT_TOP_N])
                ),
                daemon=True,
            ).start()
            last_report_daily = t

        time.sleep(LOOP_DELAY)

# === MAIN ===
if __name__ == "__main__":
    print("🚀 Avvio monitor Bybit — Alerts + Wyckoff Reports")
    threading.Thread(target=lambda: app.run(host="0.0.0.0", port=PORT), daemon=True).start()
    threading.Thread(target=monitor_loop, daemon=True).start()
    while True:
        time.sleep(3600)
