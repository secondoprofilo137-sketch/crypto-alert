#!/usr/bin/env python3
# monitor_bybit_flask.py
# Bybit monitor — ALERT PERCENTUALI + ASCENSIONI (Top 5 ogni 30m, messaggio unico dettagliato)
from __future__ import annotations
import os
import time
import threading
from datetime import datetime, timezone
from dotenv import load_dotenv
from flask import Flask
import requests
import ccxt
import numpy as np
from fastdtw import fastdtw

# Polyfill euclidean for fastdtw
def euclidean(a, b):
    return np.sqrt(np.sum((a - b) ** 2))

# -----------------------
# LOAD ENV
# -----------------------
load_dotenv()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_IDS = [cid.strip() for cid in os.getenv("TELEGRAM_CHAT_ID", "").split(",") if cid.strip()]
PORT = int(os.getenv("PORT", 10000))

FAST_TFS = os.getenv("FAST_TFS", "1m,3m").split(",")
SLOW_TFS = os.getenv("SLOW_TFS", "1h").split(",")
LOOP_DELAY = int(os.getenv("LOOP_DELAY", 10))
MAX_CANDIDATES_PER_CYCLE = int(os.getenv("MAX_CANDIDATES_PER_CYCLE", 1000))

THRESHOLDS = {
    "1m": float(os.getenv("THRESH_1M", 7.5)),
    "3m": float(os.getenv("THRESH_3M", 11.5)),
    "1h": float(os.getenv("THRESH_1H", 17.0)),
}

COOLDOWN_SECONDS = int(os.getenv("COOLDOWN_SECONDS", 180))
HEARTBEAT_INTERVAL = int(os.getenv("HEARTBEAT_INTERVAL", 3600))

# ASCENSION SCANNER CONFIG
ASC_SCAN_INTERVAL = int(os.getenv("ASC_SCAN_EVERY", 1800))
ASC_TOP_N = int(os.getenv("ASC_TOP_N", 5))
ASC_MAX_SYMBOLS = int(os.getenv("ASC_MAX_SYMBOLS", 200))
ASC_LOOKBACK_CANDLES = int(os.getenv("PATTERN_LOOKBACK_CANDLES", 48))
WEIGHT_PEARSON = float(os.getenv("WEIGHT_PEARSON", 0.35))
WEIGHT_COSINE = float(os.getenv("WEIGHT_COSINE", 0.35))
WEIGHT_DTW = float(os.getenv("WEIGHT_DTW", 0.30))
ASC_PER_SYMBOL_DELAY = float(os.getenv("ASC_PER_SYMBOL_DELAY", 0.08))

# EXCHANGE
exchange = ccxt.bybit({"enableRateLimit": True, "options": {"defaultType": "swap"}})

# TELEGRAM
def send_telegram(text: str):
    if not TELEGRAM_TOKEN or not CHAT_IDS:
        print("Telegram not configured. Message would be:\n", text)
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    for cid in CHAT_IDS:
        try:
            requests.post(url, data={
                "chat_id": cid,
                "text": text,
                "parse_mode": "Markdown",
                "disable_web_page_preview": True
            }, timeout=10)
        except Exception as e:
            print("Telegram send error:", e)

# SAFE OHLCV FETCHER
def safe_fetch_ohlcv(symbol: str, timeframe: str, limit: int = 200):
    try:
        return exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
    except Exception:
        return None

# TIME-SERIES UTIL
def series_from_ohlcv(ohlcv, lookback=ASC_LOOKBACK_CANDLES):
    closes = np.array([c[4] for c in ohlcv], dtype=float)
    if len(closes) < lookback:
        return None
    closes = closes[-lookback:]
    lr = np.diff(np.log(closes + 1e-12))
    if np.std(lr) == 0:
        return None
    return (lr - np.mean(lr)) / (np.std(lr) + 1e-12)

def ensure_same_length(a: np.ndarray, b: np.ndarray):
    m = min(len(a), len(b))
    return a[-m:], b[-m:]

# SIMILARITY METRICS
def pearson_numpy(a: np.ndarray, b: np.ndarray):
    if a.size == 0 or b.size == 0:
        return 0.0
    a_mean, b_mean = a.mean(), b.mean()
    num = ((a - a_mean) * (b - b_mean)).sum()
    den = np.sqrt(((a - a_mean) ** 2).sum() * ((b - b_mean) ** 2).sum())
    return float(num / (den + 1e-12))

def cosine_numpy(a: np.ndarray, b: np.ndarray):
    denom = (np.linalg.norm(a) * np.linalg.norm(b)) + 1e-12
    return float(np.dot(a, b) / denom)

def dtw_similarity(a: np.ndarray, b: np.ndarray):
    dist, _ = fastdtw(a, b, dist=euclidean)
    return 1.0 / (1.0 + dist), float(dist)

def compute_composite_score(a: np.ndarray, b: np.ndarray):
    p_mapped = (pearson_numpy(a, b) + 1.0) / 2.0
    c_mapped = (cosine_numpy(a, b) + 1.0) / 2.0
    d_sim, d_dist = dtw_similarity(a, b)
    score = WEIGHT_PEARSON * p_mapped + WEIGHT_COSINE * c_mapped + WEIGHT_DTW * d_sim
    return {"score": float(score), "pearson": float(p_mapped), "cosine": float(c_mapped), "dtw_sim": float(d_sim), "dtw_dist": float(d_dist)}

# -----------------------
# REFERENCE SERIES (senza immagine)
# -----------------------
def get_reference_series(symbol="BTC/USDT", timeframe="15m", lookback=ASC_LOOKBACK_CANDLES):
    ohlcv = safe_fetch_ohlcv(symbol, timeframe, limit=lookback+1)
    if not ohlcv:
        return None
    closes = np.array([c[4] for c in ohlcv], dtype=float)
    lr = np.diff(np.log(closes + 1e-12))
    if np.std(lr) == 0:
        return None
    return (lr - np.mean(lr)) / (np.std(lr) + 1e-12)

# ASCENSION SCAN
def run_ascension_scan_once():
    try:
        ref_series = get_reference_series("BTC/USDT", "15m", ASC_LOOKBACK_CANDLES)
        if ref_series is None:
            print("Cannot get reference series — abort asc scan")
            return []
        markets = exchange.load_markets()
        symbols = [s for s in markets.keys() if s.endswith("/USDT")][:ASC_MAX_SYMBOLS]
        results = []
        for sym in symbols:
            try:
                ohlcv = safe_fetch_ohlcv(sym, "15m", limit=ASC_LOOKBACK_CANDLES)
                if not ohlcv:
                    continue
                other = series_from_ohlcv(ohlcv, ASC_LOOKBACK_CANDLES)
                if other is None:
                    continue
                a, b = ensure_same_length(ref_series, other)
                mets = compute_composite_score(a, b)
                closes = np.array([c[4] for c in ohlcv], dtype=float)
                vols = np.array([c[5] for c in ohlcv], dtype=float) if len(ohlcv[0]) > 5 else np.array([0.0])
                pct15 = (closes[-1] - closes[-2]) / (closes[-2] + 1e-12) * 100 if len(closes) >= 2 else 0.0
                last_vol = float(vols[-1]) if vols.size > 0 else 0.0
                results.append({"symbol": sym, "score": mets["score"], "pearson": mets["pearson"],
                                "cosine": mets["cosine"], "dtw_sim": mets["dtw_sim"], "dtw_dist": mets["dtw_dist"],
                                "pct15": float(pct15), "vol": last_vol})
            except Exception:
                pass
            time.sleep(ASC_PER_SYMBOL_DELAY)
        return sorted(results, key=lambda x: x["score"], reverse=True)
    except Exception as e:
        print("Error in run_ascension_scan_once:", e)
        return []

def ascension_scan_loop():
    time.sleep(5)
    while True:
        start_t = time.time()
        print(f"[{datetime.now(timezone.utc).isoformat()}] Starting ascension scan...")
        results = run_ascension_scan_once()
        if results:
            top = results[:ASC_TOP_N]
            nowtxt = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
            lines = [f"📊 *Top {len(top)} Ascensioni (15m)* — {nowtxt}\n"]
            for i, r in enumerate(top, start=1):
                vol_fmt = f"{r['vol']:.0f}" if r['vol'] >= 1000 else f"{r['vol']:.2f}"
                lines.append(f"{i}) *{r['symbol']}*\n   • Score: {r['score']:.3f}\n   • Variazione 15m: {r['pct15']:+.2f}%\n   • Volumi (last 15m): {vol_fmt}\n")
            send_telegram("\n".join(lines))
            print(f"[{datetime.now(timezone.utc).isoformat()}] Ascension scan finished. Sent top {len(top)}.")
        else:
            print(f"[{datetime.now(timezone.utc).isoformat()}] Ascension scan: no results.")
        elapsed = time.time() - start_t
        to_sleep = max(1, ASC_SCAN_INTERVAL - elapsed)
        time.sleep(to_sleep)

# PERCENT-CHANGE MONITOR LOOP
last_prices = {}
last_alert_time = {}
last_hb = 0

def monitor_loop():
    global last_hb
    symbols = []
    try:
        symbols = exchange.load_markets().keys()
        symbols = [s for s in symbols if s.endswith("/USDT")]
        symbols = sorted(symbols)
        print(f"Loaded {len(symbols)} symbols for percent alerts")
    except Exception as e:
        print("Error loading markets in monitor_loop:", e)
    while True:
        for s in list(symbols)[:MAX_CANDIDATES_PER_CYCLE]:
            for tf in (FAST_TFS + SLOW_TFS):
                ohlcv = safe_fetch_ohlcv(s, tf, limit=3)
                if not ohlcv:
                    continue
                price = ohlcv[-1][4]
                key = f"{s}_{tf}"
                if key not in last_prices:
                    last_prices[key] = price
                    continue
                prev = last_prices[key]
                variation = ((price - prev) / (prev + 1e-12)) * 100
                threshold = THRESHOLDS.get(tf, 999)
                if abs(variation) >= threshold:
                    nowt = time.time()
                    last_ts = last_alert_time.get(key, 0)
                    if nowt - last_ts >= COOLDOWN_SECONDS:
                        direction = "📈 Rialzo" if variation > 0 else "📉 Ribasso"
                        nowtxt = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
                        msg = f"🔔 *ALERT* — {s} ({tf})\nVariazione: {variation:+.2f}%\nPrezzo: {price:.6f}\n{direction}\n[{nowtxt}]"
                        send_telegram(msg)
                        last_alert_time[key] = nowt
                last_prices[key] = price
        now = time.time()
        if now - last_hb >= HEARTBEAT_INTERVAL:
            send_telegram("💓 Heartbeat — bot attivo")
            last_hb = now
        time.sleep(LOOP_DELAY)

# FLASK + THREAD START
app = Flask(__name__)
@app.route("/")
def home():
    return "Crypto Alert Bot — Running"

@app.route("/test")
def test():
    send_telegram("🧪 Test OK")
    return "Test sent", 200

if __name__ == "__main__":
    print("🚀 Starting monitor_bybit_flask with Ascension scanner (Top 5 every 30m)...")
    threading.Thread(target=lambda: app.run(host="0.0.0.0", port=PORT), daemon=True).start()
    threading.Thread(target=monitor_loop, daemon=True).start()
    threading.Thread(target=ascension_scan_loop, daemon=True).start()
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        print("Exiting...")
