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
from scipy.spatial.distance import euclidean  # only for fastdtw distance metric
import cv2

# Load env
load_dotenv()

# -----------------------
# BASIC CONFIG (from .env or defaults)
# -----------------------
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_IDS = [cid.strip() for cid in os.getenv("TELEGRAM_CHAT_ID", "").split(",") if cid.strip()]

PORT = int(os.getenv("PORT", 10000))

FAST_TFS = os.getenv("FAST_TFS", "1m,3m").split(",")
SLOW_TFS = os.getenv("SLOW_TFS", "1h").split(",")
LOOP_DELAY = int(os.getenv("LOOP_DELAY", 10))  # used for percent-alert loop sleep in seconds
MAX_CANDIDATES_PER_CYCLE = int(os.getenv("MAX_CANDIDATES_PER_CYCLE", 1000))

THRESHOLDS = {
    "1m": float(os.getenv("THRESH_1M", 7.5)),
    "3m": float(os.getenv("THRESH_3M", 11.5)),
    "1h": float(os.getenv("THRESH_1H", 17.0)),
}

COOLDOWN_SECONDS = int(os.getenv("COOLDOWN_SECONDS", 180))
HEARTBEAT_INTERVAL = int(os.getenv("HEARTBEAT_INTERVAL", 3600))

# -----------------------
# ASCENSION SCANNER CONFIG
# -----------------------
# run every 1800s by default (30 minutes)
ASC_SCAN_INTERVAL = int(os.getenv("ASC_SCAN_EVERY", 1800))
ASC_TOP_N = int(os.getenv("ASC_TOP_N", 5))
ASC_MAX_SYMBOLS = int(os.getenv("ASC_MAX_SYMBOLS", 200))
ASC_PATTERN_IMAGE = os.getenv("PATTERN_IMAGE_PATH", "/mnt/data/1d39c56b-bbd0-4394-844d-00a9c355df5b.png")
ASC_LOOKBACK_CANDLES = int(os.getenv("PATTERN_LOOKBACK_CANDLES", 48))
# weights for score (pearson, cosine, dtw)
WEIGHT_PEARSON = float(os.getenv("WEIGHT_PEARSON", 0.35))
WEIGHT_COSINE = float(os.getenv("WEIGHT_COSINE", 0.35))
WEIGHT_DTW = float(os.getenv("WEIGHT_DTW", 0.30))

# API polite delay between symbol fetches in asc scan (seconds)
ASC_PER_SYMBOL_DELAY = float(os.getenv("ASC_PER_SYMBOL_DELAY", 0.08))

# -----------------------
# EXCHANGE (Bybit)
# -----------------------
exchange = ccxt.bybit({
    "enableRateLimit": True,
    "options": {"defaultType": "swap"}
})

# -----------------------
# TELEGRAM helper
# -----------------------
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

# -----------------------
# SAFE OHLCV fetcher
# -----------------------
def safe_fetch_ohlcv(symbol: str, timeframe: str, limit: int = 200):
    try:
        return exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
    except Exception:
        return None

# -----------------------
# IMAGE -> SERIES (best-effort)
# -----------------------
def extract_vertical_profile(img_gray: np.ndarray):
    h, w = img_gray.shape
    profile = np.zeros(w, dtype=float)
    # invert bright/dark to get candle pixels (threshold may be tuned)
    _, th = cv2.threshold(img_gray, 200, 255, cv2.THRESH_BINARY_INV)
    for col in range(w):
        col_pixels = np.where(th[:, col] > 0)[0]
        if col_pixels.size == 0:
            profile[col] = np.nan
        else:
            profile[col] = h - np.mean(col_pixels)
    # interpolate nans
    nans = np.isnan(profile)
    if nans.any():
        xp = np.flatnonzero(~nans)
        if xp.size >= 2:
            profile[nans] = np.interp(np.flatnonzero(nans), xp, profile[~nans])
        else:
            profile[nans] = np.nanmean(profile[~nans]) if xp.size > 0 else 0.0
    return profile

def image_to_pattern_series(image_path: str, desired_candles: int = ASC_LOOKBACK_CANDLES) -> np.ndarray:
    img = cv2.imread(image_path)
    if img is None:
        raise FileNotFoundError(f"Pattern image not found: {image_path}")
    img_gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    h, w = img_gray.shape
    margin_w = int(w * 0.02)
    margin_h = int(h * 0.12)
    crop = img_gray[margin_h:h - margin_h, margin_w:w - margin_w]
    profile = extract_vertical_profile(crop)
    # aggregate blocks to map pixel columns -> candles
    block_size = max(1, int(len(profile) / desired_candles))
    vals = []
    for i in range(0, len(profile), block_size):
        block = profile[i:i + block_size]
        if block.size == 0:
            continue
        vals.append(np.nanmean(block))
    vals = np.array(vals, dtype=float)
    # fallback to full image
    if len(vals) < 6:
        profile = extract_vertical_profile(img_gray)
        block_size = max(1, int(len(profile) / desired_candles))
        vals = []
        for i in range(0, len(profile), block_size):
            block = profile[i:i + block_size]
            if block.size == 0:
                continue
            vals.append(np.nanmean(block))
        vals = np.array(vals, dtype=float)
    if np.nanstd(vals) == 0 or len(vals) < 6:
        raise RuntimeError("Extracted pattern series invalid or too short")
    # normalize (zscore)
    vals = (vals - np.mean(vals)) / (np.std(vals) + 1e-12)
    # ensure at most desired_candles
    if len(vals) >= desired_candles:
        vals = vals[-desired_candles:]
    return vals

# -----------------------
# TIME-SERIES UTIL
# -----------------------
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
    la, lb = len(a), len(b)
    m = min(la, lb)
    return a[-m:], b[-m:]

# -----------------------
# SIMILARITY METRICS (numpy only + fastdtw)
# -----------------------
def pearson_numpy(a: np.ndarray, b: np.ndarray):
    # safe Pearson with numpy
    if a.size == 0 or b.size == 0:
        return 0.0
    a_mean = a.mean(); b_mean = b.mean()
    num = ((a - a_mean) * (b - b_mean)).sum()
    den = np.sqrt(((a - a_mean) ** 2).sum() * ((b - b_mean) ** 2).sum())
    return float(num / (den + 1e-12))

def cosine_numpy(a: np.ndarray, b: np.ndarray):
    denom = (np.linalg.norm(a) * np.linalg.norm(b)) + 1e-12
    return float(np.dot(a, b) / denom)

def dtw_similarity(a: np.ndarray, b: np.ndarray):
    dist, _ = fastdtw(a, b, dist=euclidean)
    # convert distance to similarity in 0..1
    return 1.0 / (1.0 + dist), float(dist)

def compute_composite_score(a: np.ndarray, b: np.ndarray):
    p = pearson_numpy(a, b)  # in [-1,1]
    # map p to 0..1
    p_mapped = (p + 1.0) / 2.0
    c = cosine_numpy(a, b)   # in [-1,1] possibly
    c_mapped = (c + 1.0) / 2.0
    d_sim, d_dist = dtw_similarity(a, b)  # 0..1
    # weighted sum
    score = WEIGHT_PEARSON * p_mapped + WEIGHT_COSINE * c_mapped + WEIGHT_DTW * d_sim
    return {
        "score": float(score),
        "pearson": float(p),
        "cosine": float(c),
        "dtw_sim": float(d_sim),
        "dtw_dist": float(d_dist)
    }

# -----------------------
# ASCENSION SCAN (single run) -> returns list of dict results
# -----------------------
def run_ascension_scan_once():
    try:
        # 1) extract reference pattern series from image and convert to log-return-like shape
        ref_vals = image_to_pattern_series(ASC_PATTERN_IMAGE, desired_candles=ASC_LOOKBACK_CANDLES)
        # convert to delta-style (similar to log-return length)
        ref_series = np.diff(ref_vals)
        if np.std(ref_series) == 0:
            print("Ref series flat — abort asc scan")
            return []

        # 2) load markets and choose symbols (USDT pairs)
        markets = exchange.load_markets()
        symbols = [s for s in markets.keys() if s.endswith("/USDT")]
        symbols = symbols[:ASC_MAX_SYMBOLS]

        results = []
        for sym in symbols:
            try:
                ohlcv = safe_fetch_ohlcv(sym, "15m", limit=ASC_LOOKBACK_CANDLES)
                if not ohlcv:
                    continue
                other = series_from_ohlcv(ohlcv, lookback=ASC_LOOKBACK_CANDLES)
                if other is None:
                    continue
                a, b = ensure_same_length(ref_series, other)
                mets = compute_composite_score(a, b)
                # compute also last 15m percent change and last volume
                closes = np.array([c[4] for c in ohlcv], dtype=float)
                vols = np.array([c[5] for c in ohlcv], dtype=float) if len(ohlcv[0]) > 5 else np.array([0.0])
                pct15 = (closes[-1] - closes[-2]) / (closes[-2] + 1e-12) * 100 if len(closes) >= 2 else 0.0
                last_vol = float(vols[-1]) if vols.size > 0 else 0.0
                results.append({
                    "symbol": sym,
                    "score": mets["score"],
                    "pearson": mets["pearson"],
                    "cosine": mets["cosine"],
                    "dtw_sim": mets["dtw_sim"],
                    "dtw_dist": mets["dtw_dist"],
                    "pct15": float(pct15),
                    "vol": last_vol
                })
            except Exception:
                # ignore symbol errors and continue
                pass
            time.sleep(ASC_PER_SYMBOL_DELAY)

        # sort by score desc and return
        results_sorted = sorted(results, key=lambda x: x["score"], reverse=True)
        return results_sorted
    except Exception as e:
        print("Error in run_ascension_scan_once:", e)
        return []

# -----------------------
# ASCENSION SCAN LOOP (every ASC_SCAN_INTERVAL) -> sends 1 Telegram message with top N detailed
# -----------------------
def ascension_scan_loop():
    # small initial delay
    time.sleep(5)
    while True:
        start_t = time.time()
        print(f"[{datetime.now(timezone.utc).isoformat()}] Starting ascension scan...")
        results = run_ascension_scan_once()
        if results:
            top = results[:ASC_TOP_N]
            # build detailed message (format B style)
            nowtxt = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
            lines = [f"📊 *Top {len(top)} Ascensioni (15m)* — {nowtxt}\n"]
            for i, r in enumerate(top, start=1):
                vol_fmt = f"{r['vol']:.0f}" if r['vol'] >= 1000 else f"{r['vol']:.2f}"
                lines.append(
                    f"{i}) *{r['symbol']}*\n"
                    f"   • Score: {r['score']:.3f}\n"
                    f"   • Variazione 15m: {r['pct15']:+.2f}%\n"
                    f"   • Volumi (last 15m): {vol_fmt}\n"
                )
            message = "\n".join(lines)
            send_telegram(message)
            # optional: write CSV for inspection
            try:
                import csv
                with open("asc_top_results.csv", "w", newline="") as fh:
                    w = csv.DictWriter(fh, fieldnames=top[0].keys())
                    w.writeheader()
                    w.writerows(top)
            except Exception:
                pass
            print(f"[{datetime.now(timezone.utc).isoformat()}] Ascension scan finished. Sent top {len(top)}.")
        else:
            print(f"[{datetime.now(timezone.utc).isoformat()}] Ascension scan: no results.")

        elapsed = time.time() - start_t
        to_sleep = max(1, ASC_SCAN_INTERVAL - elapsed)
        print(f"Ascension scanner sleeping {to_sleep:.1f}s until next run.")
        time.sleep(to_sleep)

# -----------------------
# PERCENT-CHANGE MONITOR LOOP (unchanged semantics)
# -----------------------
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
                        msg = (
                            f"🔔 *ALERT* — {s} ({tf})\n"
                            f"Variazione: {variation:+.2f}%\n"
                            f"Prezzo: {price:.6f}\n"
                            f"{direction}\n"
                            f"[{nowtxt}]"
                        )
                        send_telegram(msg)
                        last_alert_time[key] = nowt
                last_prices[key] = price

        # heartbeat
        now = time.time()
        if now - last_hb >= HEARTBEAT_INTERVAL:
            send_telegram("💓 Heartbeat — bot attivo")
            last_hb = now

        time.sleep(LOOP_DELAY)

# -----------------------
# FLASK + THREAD START
# -----------------------
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
    # start flask in thread
    threading.Thread(target=lambda: app.run(host="0.0.0.0", port=PORT), daemon=True).start()
    # start percent-change monitor
    threading.Thread(target=monitor_loop, daemon=True).start()
    # start ascension scanner (every ASC_SCAN_INTERVAL seconds)
    threading.Thread(target=ascension_scan_loop, daemon=True).start()

    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        print("Exiting...")
