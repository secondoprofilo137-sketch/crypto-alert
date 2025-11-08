#!/usr/bin/env python3
# monitor_bybit_flask.py
# Versione with Early Momentum Detector (trade-based spike early alerts)

import os
import time
import threading
import math
from statistics import mean, pstdev
from datetime import datetime, timezone
import requests
import ccxt
from flask import Flask

# === CONFIG (ENV or defaults) ===
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_IDS = [cid.strip() for cid in os.getenv("TELEGRAM_CHAT_ID", "").split(",") if cid.strip()]
PORT = int(os.getenv("PORT", 10000))

FAST_TFS = os.getenv("FAST_TFS", "1m,3m").split(",")
SLOW_TFS = os.getenv("SLOW_TFS", "1h").split(",")
LOOP_DELAY = int(os.getenv("LOOP_DELAY", 10))
HEARTBEAT_INTERVAL_SEC = int(os.getenv("HEARTBEAT_INTERVAL_SEC", 3600))
MAX_CANDIDATES_PER_CYCLE = int(os.getenv("MAX_CANDIDATES_PER_CYCLE", 1000))
COOLDOWN_SECONDS = int(os.getenv("COOLDOWN_SECONDS", 180))

# thresholds per timeframe
THRESH_1M = float(os.getenv("THRESH_1M", 7.5))
THRESH_3M = float(os.getenv("THRESH_3M", 11.5))
THRESH_1H = float(os.getenv("THRESH_1H", 17.0))
THRESHOLDS = {"1m": THRESH_1M, "3m": THRESH_3M, "1h": THRESH_1H}

# Volume spike / early detector config
EARLY_SPIKE_MODE = os.getenv("EARLY_SPIKE_MODE", "true").lower() in ("1", "true", "yes")
EARLY_WINDOW_SEC = int(os.getenv("EARLY_WINDOW_SEC", 15))               # window to measure last trades (seconds)
EARLY_BASE_WINDOWS = int(os.getenv("EARLY_BASE_WINDOWS", 6))            # how many previous windows to average
EARLY_MULTIPLIER = float(os.getenv("EARLY_MULTIPLIER", 50.0))           # multiplier threshold (e.g. 50x)
EARLY_MIN_USD = float(os.getenv("EARLY_MIN_USD", 50000.0))              # min USD turnover in last window
EARLY_COOLDOWN_SECONDS = int(os.getenv("EARLY_COOLDOWN_SECONDS", 60))   # per-symbol early alerts cooldown

# volume/candle spike filters (kept for standard alerts)
VOLUME_SPIKE_MULTIPLIER = float(os.getenv("VOLUME_SPIKE_MULTIPLIER", 100.0))
VOLUME_SPIKE_MIN_VOLUME = float(os.getenv("VOLUME_SPIKE_MIN_VOLUME", 500000.0))
VOLUME_MIN_USD = float(os.getenv("VOLUME_MIN_USD", 1000000.0))
VOLUME_COOLDOWN_SECONDS = int(os.getenv("VOLUME_COOLDOWN_SECONDS", 300))

# volatility tiers
VOL_TIER_THRESHOLDS = {"low": 0.002, "medium": 0.006, "high": 0.012}

# exchange + app
exchange = ccxt.bybit({"options": {"defaultType": "swap"}})
app = Flask(__name__)

# runtime state
last_prices = {}
last_alert_time = {}
last_volume_alert_time = {}
last_early_alert_time = {}

# === TELEGRAM ===
def send_telegram(text: str):
    if not TELEGRAM_TOKEN or not CHAT_IDS:
        print("❌ Telegram not configured; debug message:\n", text)
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

# === INDICATORS & HELPERS ===
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

def score_signals(prices, volumes, variation, tf):
    score = 0
    reasons = []
    ema10 = compute_ema(prices, 10)
    ema30 = compute_ema(prices, 30)
    if ema10 and ema30:
        diff_pct = ((ema10 - ema30) / ema30) * 100 if ema30 else 0.0
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
    return normalized, reasons, {"rsi": rsi, "macd": macd, "signal": signal, "ema10": ema10, "ema30": ema30, "vol_ratio": vol_ratio}

# === EXCHANGE HELPERS ===
def safe_fetch_ohlcv(symbol, timeframe, limit=60):
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
            if m.get("type") == "swap" or s.endswith(":USDT"):
                syms.append(s)
        return sorted(set(syms))
    except Exception as e:
        print("⚠️ load_markets:", e)
        return []

# === EARLY SPIKE DETECTOR (trades) ===
def fetch_recent_trades(symbol, limit=500):
    try:
        trades = exchange.fetch_trades(symbol, limit=limit)
        return trades
    except Exception as e:
        # don't spam logs
        # print(f"⚠️ fetch_trades {symbol}: {e}")
        return []

def compute_trade_window_volumes(trades, now_ms, window_sec=15, base_windows=6):
    """
    Partition trades into windows of window_sec seconds ending at now.
    Return last_window_usd, baseline_avg_usd (avg of previous base_windows windows).
    trades: list of ccxt trade dicts with 'timestamp', 'price', 'amount'
    """
    if not trades:
        return 0.0, 0.0, []
    window_ms = window_sec * 1000
    # compute per-window USD volumes for last (base_windows + 1) windows
    volumes = []
    start = now_ms - (window_ms * (base_windows + 1))
    # initialize bins
    for w in range(base_windows + 1):
        volumes.append(0.0)
    for t in trades:
        ts = t.get("timestamp") or 0
        if ts < start:
            continue
        idx = int((ts - start) // window_ms)
        if 0 <= idx < len(volumes):
            price = t.get("price") or 0.0
            amount = t.get("amount") or 0.0
            usd = price * amount
            volumes[idx] += usd
    # last window is last element
    last_window_usd = volumes[-1]
    prev_windows = volumes[:-1]
    # compute baseline average over previous base_windows (if enough)
    baseline = 0.0
    cnt = 0
    for v in prev_windows[-base_windows:]:
        baseline += v
        cnt += 1
    baseline_avg = (baseline / cnt) if cnt > 0 else 0.0
    return last_window_usd, baseline_avg, volumes

def detect_early_spike_by_trades(symbol):
    """
    Return (is_spike: bool, ratio: float, last_window_usd: float)
    """
    trades = fetch_recent_trades(symbol, limit=500)
    if not trades:
        return False, 1.0, 0.0
    now_ms = int(time.time() * 1000)
    last_usd, baseline_avg, volumes = compute_trade_window_volumes(
        trades, now_ms, window_sec=EARLY_WINDOW_SEC, base_windows=EARLY_BASE_WINDOWS
    )
    if baseline_avg <= 0:
        return False, 1.0, last_usd
    ratio = last_usd / baseline_avg
    is_spike = (ratio >= EARLY_MULTIPLIER) and (last_usd >= EARLY_MIN_USD)
    return is_spike, ratio, last_usd

# === FLASK endpoints ===
@app.route("/")
def home():
    return "✅ Crypto Alert Bot (Bybit) — running"

@app.route("/test")
def test():
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    send_telegram(f"🧪 TEST ALERT — bot online ({now})")
    return "Test sent", 200

# === MONITOR LOOP ===
def monitor_loop():
    symbols = get_bybit_derivative_symbols()
    print(f"✅ Simboli derivati trovati: {len(symbols)}")
    last_heartbeat = 0
    initialized = False

    while True:
        start_cycle = time.time()
        # iterate symbols (limit to candidate cap)
        for symbol in symbols[:MAX_CANDIDATES_PER_CYCLE]:
            # check fast + slow timeframes
            for tf in FAST_TFS + SLOW_TFS:
                closes, vols = safe_fetch_ohlcv(symbol, tf, 120)
                if not closes:
                    continue
                price = closes[-1]
                key = f"{symbol}_{tf}"

                # initialize on first run
                if key not in last_prices:
                    last_prices[key] = price
                    continue

                prev = last_prices[key]
                try:
                    variation = ((price - prev) / prev) * 100 if prev else 0.0
                except Exception:
                    variation = 0.0

                threshold = THRESHOLDS.get(tf, 5.0)

                # EARLY SPIKE CHECK (trade-based) -- only if enabled and tf is 1m and symbol looks like USDT pair
                early_alert = False
                early_ratio = 1.0
                early_usd = 0.0
                if EARLY_SPIKE_MODE and tf == "1m" and (symbol.endswith(":USDT") or "/USDT" in symbol):
                    try:
                        # rate-limit: do not spam fetch_trades too often; honored by per-symbol cooldown
                        last_early_ts = last_early_alert_time.get(symbol, 0)
                        if time.time() - last_early_ts >= EARLY_COOLDOWN_SECONDS:
                            spike, ratio, usd_last = detect_early_spike_by_trades(symbol)
                            if spike:
                                early_alert = True
                                early_ratio = ratio
                                early_usd = usd_last
                    except Exception as e:
                        # ignore early detector errors
                        print(f"⚠️ early detector error {symbol}: {e}")

                # detect volume spike on candle-level
                vol_spike, vol_ratio = False, 1.0
                if vols and len(vols) >= 6:
                    avg_prev = mean(vols[-6:-1])
                    last_vol = vols[-1]
                    if avg_prev and avg_prev > 0:
                        vol_ratio = last_vol / avg_prev
                        vol_spike = vol_ratio >= VOLUME_SPIKE_MULTIPLIER and (last_vol >= VOLUME_SPIKE_MIN_VOLUME)

                # compute USD volume for candle last
                usd_volume = 0.0
                try:
                    if symbol.endswith(":USDT") or "/USDT" in symbol:
                        usd_volume = price * (vols[-1] if vols else 0.0)
                except Exception:
                    usd_volume = 0.0

                alert_price_move = abs(variation) >= threshold
                alert_volume = vol_spike and (usd_volume >= VOLUME_MIN_USD or (vols and vols[-1] >= VOLUME_SPIKE_MIN_VOLUME))
                # enforce volume cooldown
                now_t = time.time()
                if alert_volume:
                    last_vol_ts = last_volume_alert_time.get(symbol, 0)
                    if now_t - last_vol_ts < VOLUME_COOLDOWN_SECONDS:
                        alert_volume = False

                # If early_alert triggered, send early notification (with its own cooldown)
                if early_alert:
                    last_early_ts = last_early_alert_time.get(symbol, 0)
                    if now_t - last_early_ts >= EARLY_COOLDOWN_SECONDS:
                        # Verify additional optional condition: small price movement already? we can warn anyway
                        trend = "📈 Rialzo in formazione" if variation >= 0 else "📉 Crollo in formazione"
                        emoji = "⚡️"
                        tsnow = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
                        txt = (
                            f"🚨 EARLY SPIKE -> {emoji} *{symbol}* — momentum in formazione (ref {tf})\n"
                            f"💰 Prezzo attuale: {price:.6f}\n"
                            f"🔊 Volume recent (ultimi {EARLY_WINDOW_SEC}s): {early_usd:.0f} USD — {early_ratio:.1f}× baseline\n"
                            f"{trend} — possibile pump, controlla orderflow / book.\n"
                            f"[{tsnow}]"
                        )
                        send_telegram(txt)
                        last_early_alert_time[symbol] = now_t
                        # do not proceed to duplicate price alert this cycle for same key (optional)
                        # continue

                # price / candle alerts (existing logic)
                if alert_price_move or alert_volume:
                    last_ts = last_alert_time.get(key, 0)
                    if now_t - last_ts >= COOLDOWN_SECONDS:
                        score, reasons, details = score_signals(closes, vols, variation, tf)
                        vol_tier, volratio = estimate_volatility_tier(closes, vols)
                        trend = "📈 Rialzo" if variation > 0 else "📉 Crollo"
                        emoji = "🔥" if tf in FAST_TFS else "⚡"
                        tsnow = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

                        text = (
                            f"🔔 ALERT -> {emoji} *{symbol}* — variazione {variation:+.2f}% (ref {tf})\n"
                            f"💰 Prezzo attuale: {price:.6f}\n"
                        )
                        if alert_volume:
                            text += f"🚨 *Volume SPIKE* — {vol_ratio:.1f}× sopra la media | Volume candle: {vols[-1]:.0f} ({usd_volume:.0f} USD)\n"
                        text += (
                            f"{trend} rilevato — attenzione al trend.\n"
                            f"📊 Volatilità: {vol_tier} | Vol ratio: {volratio:.2f}×\n"
                            f"🔎 Score predittivo: *{score}/100*\n"
                        )
                        if reasons:
                            text += "▪ " + "; ".join(reasons) + "\n"
                        text += f"[{tsnow}]"
                        send_telegram(text)
                        last_alert_time[key] = now_t
                        if alert_volume:
                            last_volume_alert_time[symbol] = now_t

                # always update last_prices for next comparison
                last_prices[key] = price

        # heartbeat
        if time.time() - last_heartbeat >= HEARTBEAT_INTERVAL_SEC:
            now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
            send_telegram(f"💓 Heartbeat — bot attivo ({now}) | Simboli: {len(symbols)}")
            last_heartbeat = time.time()

        # sleep remaining of cycle
        elapsed = time.time() - start_cycle
        if elapsed < LOOP_DELAY:
            time.sleep(LOOP_DELAY - elapsed)

# === ENTRYPOINT ===
if __name__ == "__main__":
    print(f"🚀 Avvio monitor Bybit — TF {FAST_TFS + SLOW_TFS}, thresholds {THRESHOLDS}, loop {LOOP_DELAY}s, EARLY_SPIKE={EARLY_SPIKE_MODE}")
    threading.Thread(target=lambda: app.run(host="0.0.0.0", port=PORT), daemon=True).start()
    threading.Thread(target=monitor_loop, daemon=True).start()
    while True:
        time.sleep(3600)
