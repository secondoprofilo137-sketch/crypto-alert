#!/usr/bin/env python3
# monitor_bybit_flask.py
# Bybit derivatives monitor — 1m, 3m, 1h — Flask + background monitor
# Italiano, suggerimenti, emoji, timezone-aware datetimes

import os
import time
import threading
import math
from statistics import mean, pstdev
from datetime import datetime, timezone
import requests
import ccxt
from flask import Flask

# -------------------------
# CONFIG (env or defaults)
# -------------------------
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")  # token bot
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")          # chat id
PORT = int(os.getenv("PORT", 10000))

FAST_TFS = os.getenv("FAST_TFS", "1m,3m").split(",")
SLOW_TFS = os.getenv("SLOW_TFS", "1h").split(",")
FAST_THRESHOLD = float(os.getenv("FAST_THRESHOLD", 5.0))
SLOW_THRESHOLD = float(os.getenv("SLOW_THRESHOLD", 3.0))
LOOP_DELAY = int(os.getenv("LOOP_DELAY", 10))  # sec between cycles
HEARTBEAT_INTERVAL_SEC = int(os.getenv("HEARTBEAT_INTERVAL_SEC", 3600))  # 1h
MAX_CANDIDATES_PER_CYCLE = int(os.getenv("MAX_CANDIDATES_PER_CYCLE", 1000))  # limit per cycle

# risk tiers (very simple mapping)
VOL_TIER_THRESHOLDS = {"low": 0.002, "medium": 0.006, "high": 0.012}
RISK_MAPPING = {
    "low":    {"leverage": "x2", "target_pct": (0.8, 1.5),  "stop_pct": (-0.6, -1.0)},
    "medium": {"leverage": "x3", "target_pct": (1.5, 3.0),  "stop_pct": (-1.0, -2.0)},
    "high":   {"leverage": "x4", "target_pct": (3.0, 6.0),  "stop_pct": (-2.0, -4.0)},
    "very":   {"leverage": "x6", "target_pct": (6.0, 12.0), "stop_pct": (-4.0, -8.0)},
}

# -------------------------
# State
# -------------------------
last_prices = {}   # key = symbol_tf -> last price
last_alerts = {}   # symbol -> {alert_type: ts}
_lock = threading.Lock()

# -------------------------
# Exchange init
# -------------------------
exchange = ccxt.bybit({"options": {"defaultType": "swap"}})

# -------------------------
# Flask (keepalive + endpoints)
# -------------------------
app = Flask(__name__)

@app.route("/")
def home():
    return "✅ Crypto Alert Bot (Bybit derivatives) — running"

@app.route("/test")
def test_alert():
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    send_telegram(f"🧪 TEST ALERT - bot online ({now})")
    return "Test sent", 200

# -------------------------
# Telegram helper
# -------------------------
def send_telegram(text: str):
    if not TELEGRAM_TOKEN or not CHAT_ID:
        print("❌ Telegram non configurato (TELEGRAM_BOT_TOKEN/CHAT_ID mancanti). Messaggio:")
        print(text)
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": text, "disable_web_page_preview": True}
    try:
        r = requests.post(url, data=payload, timeout=10)
        if r.status_code != 200:
            print("❌ Errore Telegram", r.status_code, r.text)
            return False
        return True
    except Exception as e:
        print("❌ Exception Telegram:", e)
        return False

# -------------------------
# Indicators (simple)
# -------------------------
def compute_rsi(closes, period=14):
    if len(closes) < period + 1:
        return None
    gains, losses = [], []
    for i in range(1, len(closes)):
        change = closes[i] - closes[i-1]
        gains.append(max(change, 0))
        losses.append(max(-change, 0))
    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))

def compute_ema(closes, period=20):
    if not closes or len(closes) < 2:
        return None
    k = 2 / (period + 1)
    ema = closes[0]
    for price in closes[1:]:
        ema = price * k + (1 - k) * ema
    return ema

def compute_volatility_logreturns(closes):
    if len(closes) < 3:
        return 0.0
    lr = []
    for i in range(1, len(closes)):
        if closes[i-1] > 0:
            lr.append(math.log(closes[i] / closes[i-1]))
    if len(lr) < 2:
        return 0.0
    try:
        return pstdev(lr)
    except Exception:
        return 0.0

# -------------------------
# Risk / suggestion builder
# -------------------------
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
    mapping = RISK_MAPPING.get(tier, RISK_MAPPING["medium"])
    vol_ratio = 1.0
    if volumes and len(volumes) >= 2:
        baseline = mean(volumes[-6:-1]) if len(volumes) >= 6 else mean(volumes[:-1])
        last_vol = volumes[-1]
        if baseline and baseline > 0:
            vol_ratio = last_vol / baseline
    suggestion = (
        f"Categoria volatilità: {tier.upper()} | Leva suggerita: {mapping['leverage']}\n"
        f"Target indicativi: +{mapping['target_pct'][0]:.1f}% → +{mapping['target_pct'][1]:.1f}% | "
        f"Stop indicativi: {mapping['stop_pct'][0]:.1f}% → {mapping['stop_pct'][1]:.1f}%\n"
        f"Vol ratio: {vol_ratio:.2f}× | Volatility: {vol:.4f}"
    )
    return mapping, suggestion

# -------------------------
# Helpers to fetch klines safely
# -------------------------
def safe_fetch_ohlcv(symbol, timeframe, limit=60):
    try:
        ohlcv = exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
        closes = [c[4] for c in ohlcv]
        volumes = [c[5] if len(c) > 5 else 0.0 for c in ohlcv]
        return closes, volumes
    except Exception as e:
        return None, None

# -------------------------
# Symbol filtering: derivatives (swap)
# -------------------------
def get_bybit_derivative_symbols():
    try:
        markets = exchange.load_markets()
        syms = []
        for symbol, meta in markets.items():
            # prefer explicit type check, fallback to symbol suffix check
            mtype = meta.get("type") or meta.get("spot", None)
            if meta.get("info", {}) and meta.get("info", {}).get("category"):
                # some exchanges include category
                if str(meta["info"].get("category")).lower() in ("linear", "swap", "derivative"):
                    syms.append(symbol)
                    continue
            # fallback: type == 'swap' or symbol endswith ':USDT' etc.
            if meta.get("type") == "swap" or symbol.endswith(":USDT") or ":USDT" in symbol:
                syms.append(symbol)
        return list(sorted(set(syms)))
    except Exception as e:
        print("⚠️ Errore load_markets:", e)
        return []

# -------------------------
# Monitor loop (background)
# -------------------------
def monitor_loop():
    global last_prices, last_alerts
    symbols = get_bybit_derivative_symbols()
    print(f"✅ Simboli derivati Bybit trovati: {len(symbols)}")
    # last heartbeat
    last_heartbeat = 0
    while True:
        start = time.time()
        processed = 0
        # iterate symbols but optionally limit per cycle
        for symbol in symbols[:MAX_CANDIDATES_PER_CYCLE]:
            processed += 1
            # fast timeframes
            for tf in FAST_TFS:
                # fetch last candle price
                closes, volumes = safe_fetch_ohlcv(symbol, tf, limit=2)
                if not closes:
                    continue
                price = closes[-1]
                key = f"{symbol}_{tf}"
                prev = last_prices.get(key, price)
                try:
                    variation = ((price - prev) / prev) * 100 if prev else 0.0
                except Exception:
                    variation = 0.0
                if abs(variation) >= FAST_THRESHOLD:
                    # cooldown per symbol+type
                    cooldown = last_alerts.setdefault(symbol, {}).get(f"fast_{tf}", 0)
                    if time.time() - cooldown > max(60, LOOP_DELAY * 3):
                        # get richer data for suggestion
                        kl_closes, kl_vols = safe_fetch_ohlcv(symbol, tf, limit=60)
                        mapping, suggestion = estimate_risk_and_suggestion(kl_closes or closes, kl_vols or volumes)
                        trend = "rialzo" if variation > 0 else "crollo"
                        emoji = "🔥"
                        header = f"🔔 ALERT -> {emoji} {symbol} — variazione {variation:+.2f}% (ref {tf})"
                        metrics = f"Prezzo attuale: {price:.6f}"
                        interp = f"📊 { 'Rialzo' if variation>0 else 'Crollo' } rilevato — attenzione al trend."
                        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
                        text = f"{header}\n{metrics}\n{interp}\n{suggestion}\n[{now}]"
                        print(text)
                        send_telegram(text)
                        last_alerts[symbol][f"fast_{tf}"] = time.time()
                # update last price
                last_prices[f"{symbol}_{tf}"] = price

            # slow timeframes (1h) run less often — compute but use a different cooldown
            for tf in SLOW_TFS:
                if time.time() % HEARTBEAT_INTERVAL_SEC > LOOP_DELAY:  # heuristic: spread 1h runs
                    pass
                closes, volumes = safe_fetch_ohlcv(symbol, tf, limit=2)
                if not closes:
                    continue
                price = closes[-1]
                key = f"{symbol}_{tf}"
                prev = last_prices.get(key, price)
                try:
                    variation = ((price - prev) / prev) * 100 if prev else 0.0
                except Exception:
                    variation = 0.0
                if abs(variation) >= SLOW_THRESHOLD:
                    cooldown = last_alerts.setdefault(symbol, {}).get(f"slow_{tf}", 0)
                    if time.time() - cooldown > max(1800, HEARTBEAT_INTERVAL_SEC/2):
                        kl_closes, kl_vols = safe_fetch_ohlcv(symbol, tf, limit=120)
                        mapping, suggestion = estimate_risk_and_suggestion(kl_closes or closes, kl_vols or volumes)
                        emoji = "⚡"
                        header = f"🔔 ALERT -> {emoji} {symbol} — variazione {variation:+.2f}% (ref {tf})"
                        metrics = f"Prezzo attuale: {price:.6f}"
                        interp = f"📈 Movimento medio-lungo rilevato."
                        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
                        text = f"{header}\n{metrics}\n{interp}\n{suggestion}\n[{now}]"
                        print(text)
                        send_telegram(text)
                        last_alerts[symbol][f"slow_{tf}"] = time.time()
                last_prices[f"{symbol}_{tf}"] = price

        # heartbeat (invio orario)
        if time.time() - last_heartbeat >= HEARTBEAT_INTERVAL_SEC:
            now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
            hb = f"💓 Heartbeat — bot attivo ({now}) | Simboli monitorati: {len(symbols)}"
            print(hb)
            send_telegram(hb)
            last_heartbeat = time.time()

        elapsed = time.time() - start
        print(f"[{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}] Ciclo completato — simboli analizzati: {len(symbols)} — tempo {elapsed:.1f}s")
        time.sleep(LOOP_DELAY)

# -------------------------
# Entrypoint
# -------------------------
if __name__ == "__main__":
    print(f"🚀 Bybit Derivatives monitor — timeframes {FAST_TFS} + {SLOW_TFS} — threshold fast {FAST_THRESHOLD}% / slow {SLOW_THRESHOLD}% — loop {LOOP_DELAY}s")
    # start Flask app in a thread
    threading.Thread(target=lambda: app.run(host="0.0.0.0", port=PORT), daemon=True).start()
    # start monitor in background
    monitor_thread = threading.Thread(target=monitor_loop, daemon=True)
    monitor_thread.start()
    # block main thread while background runs
    monitor_thread.join()
