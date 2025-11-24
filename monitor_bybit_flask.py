#!/usr/bin/env python3
# monitor_bybit_flask.py
# Bybit monitor — ALERT PERCENTUALI + PATTERN SCANNER PRO (DTW + Cosine + Pearson)
from __future__ import annotations
import os
import time
import threading
from datetime import datetime, timezone
import requests
import ccxt
from flask import Flask
import numpy as np
import pandas as pd
from scipy.spatial.distance import cosine
from scipy.stats import pearsonr
from fastdtw import fastdtw
from scipy.spatial.distance import euclidean
import cv2

# -----------------------
# CONFIG
# -----------------------
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

# Pattern scanner config
ENABLE_PATTERN_SCAN = os.getenv("ENABLE_PATTERN_SCAN", "True").lower() in ("1", "true", "yes")
PATTERN_SCAN_INTERVAL = int(os.getenv("PATTERN_SCAN_INTERVAL", 600))  # seconds (default 10 minutes)
PATTERN_IMAGE_PATH = os.getenv("PATTERN_IMAGE_PATH", "/mnt/data/1d39c56b-bbd0-4394-844d-00a9c355df5b.png")
PATTERN_LOOKBACK_CANDLES = int(os.getenv("PATTERN_LOOKBACK_CANDLES", 48))
PATTERN_TOP_N = int(os.getenv("PATTERN_TOP_N", 5))
MAX_SYMBOLS_FOR_PATTERN = int(os.getenv("MAX_SYMBOLS_FOR_PATTERN", 200))  # limit API usage
PATTERN_SCORE_WEIGHTS = {
    "pearson": float(os.getenv("WEIGHT_PEARSON", 0.4)),
    "cosine": float(os.getenv("WEIGHT_COSINE", 0.3)),
    "dtw": float(os.getenv("WEIGHT_DTW", 0.3)),
}

# -----------------------
# EXCHANGE
# -----------------------
exchange = ccxt.bybit({
    "options": {"defaultType": "swap"}
})

last_prices = {}
last_alert_time = {}

# -----------------------
# TELEGRAM
# -----------------------
def send_telegram(text: str):
    if not TELEGRAM_TOKEN or not CHAT_IDS:
        print("No Telegram configured:", text)
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
            print("Telegram error:", e)

# -----------------------
# HELPERS
# -----------------------
def safe_fetch_ohlcv(symbol, tf, limit=120):
    try:
        data = exchange.fetch_ohlcv(symbol, timeframe=tf, limit=limit)
        return data
    except Exception as e:
        # debug print optional
        # print("fetch ohlcv error", symbol, tf, e)
        return None

def get_symbols():
    try:
        m = exchange.load_markets()
        out = []
        for s, info in m.items():
            # keep pairs that end with /USDT or contain :USDT - pick swaps and USDT pairs
            if info.get("type") == "swap" or s.endswith(":USDT") or "/USDT" in s:
                out.append(s)
        # dedupe & sort
        return sorted(set(out))
    except Exception as e:
        print("load_markets error", e)
        return []

# -----------------------
# FLASK
# -----------------------
app = Flask(__name__)

@app.route("/")
def home():
    return "Crypto Alert Bot — Running"

@app.route("/test")
def test():
    send_telegram("🧪 Test OK")
    return "Test sent", 200

# -----------------------
# PATTERN: IMAGE -> SERIES
# -----------------------
def extract_vertical_profile(img_gray):
    """
    Estrae un profilo verticale: per ogni colonna prende la media degli indici dei pixel scuri dal basso.
    """
    h, w = img_gray.shape
    profile = np.zeros(w, dtype=float)
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
        fp = profile[~nans]
        if xp.size >= 2:
            profile[nans] = np.interp(np.flatnonzero(nans), xp, fp)
        else:
            profile[nans] = np.nanmean(profile[~nans]) if xp.size > 0 else 0.0
    return profile

def image_to_candle_series(image_path, desired_candles=PATTERN_LOOKBACK_CANDLES):
    img = cv2.imread(image_path)
    if img is None:
        raise FileNotFoundError(f"Immagine non trovata: {image_path}")
    img_gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    h, w = img_gray.shape
    # crop central area heuristically (remove axes margins)
    margin_w = int(w * 0.02)
    margin_h = int(h * 0.12)
    crop = img_gray[margin_h:h - margin_h, margin_w:w - margin_w]
    profile = extract_vertical_profile(crop)
    # compress profile into desired_candles by averaging blocks
    block_size = max(1, int(len(profile) / desired_candles))
    vals = []
    for i in range(0, len(profile), block_size):
        block = profile[i:i+block_size]
        if block.size == 0:
            continue
        vals.append(np.nanmean(block))
    vals = np.array(vals, dtype=float)
    if len(vals) < 8:
        # fallback: use whole image if crop failed
        profile = extract_vertical_profile(img_gray)
        block_size = max(1, int(len(profile) / desired_candles))
        vals = []
        for i in range(0, len(profile), block_size):
            block = profile[i:i+block_size]
            if block.size == 0:
                continue
            vals.append(np.nanmean(block))
        vals = np.array(vals, dtype=float)
    if np.nanstd(vals) == 0 or len(vals) < 6:
        raise RuntimeError("Profilo immagine non valido / troppo corto.")
    # normalize to zscore
    vals = (vals - np.mean(vals)) / (np.std(vals) + 1e-12)
    # convert to series shape consistent with log-return shape used later
    # we will return len = desired_candles - 1 (log returns)
    if len(vals) >= desired_candles:
        vals = vals[-desired_candles:]
    return vals

# -----------------------
# TIME SERIES UTIL
# -----------------------
def series_from_ohlcv(ohlcv, lookback=PATTERN_LOOKBACK_CANDLES):
    closes = np.array([c[4] for c in ohlcv], dtype=float)
    if len(closes) < lookback:
        return None
    closes = closes[-lookback:]
    lr = np.diff(np.log(closes + 1e-12))
    if np.std(lr) == 0:
        return None
    return (lr - np.mean(lr)) / (np.std(lr) + 1e-12)

def ensure_same_length(a, b):
    la, lb = len(a), len(b)
    m = min(la, lb)
    return a[-m:], b[-m:]

# -----------------------
# SIMILARITY METRICS
# -----------------------
def similarity_metrics(ref, other):
    # both arrays same length
    try:
        corr = pearsonr(ref, other)[0]
    except:
        corr = 0.0
    try:
        cos_sim = 1 - cosine(ref, other)
    except:
        cos_sim = 0.0
    try:
        dist, _ = fastdtw(ref, other, dist=euclidean)
        dtw_sim = 1 / (1 + dist)
    except:
        dtw_sim = 0.0
        dist = float("inf")
    return {"pearson": float(corr), "cosine": float(cos_sim), "dtw_sim": float(dtw_sim), "dtw_dist": float(dist)}

def composite_score(mets):
    # map pearson (-1..1) to 0..1
    p = (mets["pearson"] + 1) / 2.0
    c = (mets["cosine"] + 1) / 2.0 if not np.isnan(mets["cosine"]) else 0.0
    d = mets["dtw_sim"]
    w_p = PATTERN_SCORE_WEIGHTS.get("pearson", 0.4)
    w_c = PATTERN_SCORE_WEIGHTS.get("cosine", 0.3)
    w_d = PATTERN_SCORE_WEIGHTS.get("dtw", 0.3)
    return w_p * p + w_c * c + w_d * d

# -----------------------
# PATTERN SCAN WORKER
# -----------------------
def run_pattern_scan_once():
    try:
        # extract reference series from image (log-return style)
        ref_vals = image_to_candle_series(PATTERN_IMAGE_PATH, desired_candles=PATTERN_LOOKBACK_CANDLES)
        # convert to log-return shape (len-1) to match series_from_ohlcv
        ref_series = np.diff(ref_vals)
        if np.std(ref_series) == 0:
            print("Ref series flat, aborting pattern scan.")
            return None

        # load markets and pick symbols
        markets = exchange.load_markets()
        symbols = [s for s in markets.keys() if s.endswith("/USDT")]
        # limit number to avoid hitting rate limits
        symbols = symbols[:MAX_SYMBOLS_FOR_PATTERN]

        results = []
        for sym in symbols:
            try:
                ohlcv = safe_fetch_ohlcv(sym, "15m", limit=PATTERN_LOOKBACK_CANDLES)
                if not ohlcv:
                    continue
                other = series_from_ohlcv(ohlcv)
                if other is None:
                    continue
                a, b = ensure_same_length(ref_series, other)
                mets = similarity_metrics(a, b)
                score = composite_score(mets)
                results.append({
                    "symbol": sym,
                    "pearson": mets["pearson"],
                    "cosine": mets["cosine"],
                    "dtw_sim": mets["dtw_sim"],
                    "dtw_dist": mets["dtw_dist"],
                    "score": score
                })
            except Exception:
                # ignore single symbol errors
                pass
            # polite sleep to avoid tight loops (ccxt rate limit)
            time.sleep(0.08)

        if not results:
            return None

        df = pd.DataFrame(results).sort_values("score", ascending=False).reset_index(drop=True)
        top = df.head(PATTERN_TOP_N)
        # format message
        nowtxt = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        msg_lines = [f"🔎 *Pattern Scan* — top {len(top)} matches ({nowtxt})"]
        for idx, r in top.iterrows():
            msg_lines.append(
                f"{idx+1}. `{r['symbol']}` — score: {r['score']:.3f} (p={r['pearson']:.3f} cos={r['cosine']:.3f} dtw={r['dtw_sim']:.3f})"
            )
        message = "\n".join(msg_lines)
        # send to telegram + print
        send_telegram(message)
        print("Pattern scan finished. Top matches:")
        print(top.head(PATTERN_TOP_N).to_string(index=False))
        # save csv for later inspection
        try:
            df.to_csv("pattern_scan_results.csv", index=False)
        except Exception:
            pass
        return df
    except Exception as e:
        print("Pattern scan error:", e)
        return None

def pattern_scan_loop():
    # run initial delay to let monitor loop populate data if needed
    time.sleep(5)
    while ENABLE_PATTERN_SCAN:
        start = time.time()
        print("Starting pattern scan (PRO)...")
        try:
            run_pattern_scan_once()
        except Exception as e:
            print("Pattern scan top-level error:", e)
        elapsed = time.time() - start
        # sleep until next scheduled run
        to_sleep = max(5, PATTERN_SCAN_INTERVAL - elapsed)
        print(f"Pattern scan sleeping {to_sleep:.1f}s until next run.")
        time.sleep(to_sleep)

# -----------------------
# MAIN ALERT LOOP
# -----------------------
def monitor_loop():
    symbols = get_symbols()
    print(f"Loaded {len(symbols)} derivative symbols from Bybit")

    last_hb = 0
    first = True

    while True:
        for s in symbols[:MAX_CANDIDATES_PER_CYCLE]:
            for tf in FAST_TFS + SLOW_TFS:

                data = safe_fetch_ohlcv(s, tf)
                if not data:
                    continue

                price = data[-1][4]
                key = f"{s}_{tf}"

                # init
                if key not in last_prices:
                    last_prices[key] = price
                    continue

                prev = last_prices[key]
                variation = ((price - prev) / prev) * 100 if prev else 0

                threshold = THRESHOLDS.get(tf, 999)

                if abs(variation) >= threshold:
                    now = time.time()
                    last_ts = last_alert_time.get(key, 0)

                    # cooldown
                    if now - last_ts >= COOLDOWN_SECONDS:
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
                        last_alert_time[key] = now

                last_prices[key] = price

        # heartbeat
        now = time.time()
        if now - last_hb >= HEARTBEAT_INTERVAL:
            send_telegram("💓 Heartbeat — bot attivo")
            last_hb = now

        time.sleep(LOOP_DELAY)

# -----------------------
# START
# -----------------------
if __name__ == "__main__":
    print("🚀 Starting monitor (with Pattern Scanner PRO)…")
    # start flask
    threading.Thread(target=lambda: app.run(host="0.0.0.0", port=PORT), daemon=True).start()
    # start monitor loop
    threading.Thread(target=monitor_loop, daemon=True).start()
    # start pattern scanner thread if enabled
    if ENABLE_PATTERN_SCAN:
        threading.Thread(target=pattern_scan_loop, daemon=True).start()

    # keep main alive
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        print("Exiting...")
