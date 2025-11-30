#!/usr/bin/env python3
# monitor_bybit_flask.py
# Crypto Alert Bot — Percent monitor + Breakout Detector + Delta Spike + Heartbeat

from __future__ import annotations
import os
import time
import threading
from datetime import datetime, timezone
from dotenv import load_dotenv
from flask import Flask
import requests
import ccxt
import math

# -------------------------
# Load environment
# -------------------------
load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_IDS = [c.strip() for c in os.getenv("TELEGRAM_CHAT_ID", "").split(",") if c.strip()]

PORT = int(os.getenv("PORT", "10000"))

# Percent-change monitor (unchanged thresholds used historically)
FAST_TFS = os.getenv("FAST_TFS", "1m,3m").split(",")
SLOW_TFS = os.getenv("SLOW_TFS", "1h").split(",")
LOOP_DELAY = int(os.getenv("LOOP_DELAY", "10"))
MAX_CANDIDATES_PER_CYCLE = int(os.getenv("MAX_CANDIDATES_PER_CYCLE", "250"))

THRESH_1M = float(os.getenv("THRESH_1M", "7.5"))
THRESH_3M = float(os.getenv("THRESH_3M", "11.5"))
THRESH_1H = float(os.getenv("THRESH_1H", "17.0"))
COOLDOWN_SECONDS = int(os.getenv("COOLDOWN_SECONDS", "180"))
HEARTBEAT_INTERVAL = int(os.getenv("HEARTBEAT_INTERVAL", "3600"))

# -------------------------
# Breakout Detector params (Option 2)
# -------------------------
MA_LOOKBACK = int(os.getenv("MA_LOOKBACK", "20"))            # lookback candles for volume MA
VOLUME_MULTIPLIER = float(os.getenv("VOLUME_MULTIPLIER", "3.2"))  # current vol >= MA * multiplier
SPIKE_PCT_BREAKOUT = float(os.getenv("SPIKE_PCT_BREAKOUT", "1.2")) # % price spike in 1m to consider
BREAKOUT_COOLDOWN = int(os.getenv("BREAKOUT_COOLDOWN", "600"))     # cooldown per symbol (s)

# -------------------------
# Delta Spike params (Option 3)
# -------------------------
DELTA_TRADE_LIMIT = int(os.getenv("DELTA_TRADE_LIMIT", "200"))   # recent trades to fetch
DELTA_RATIO_THRESHOLD = float(os.getenv("DELTA_RATIO_THRESHOLD", "2.0"))  # buy/sell ratio to alert
DELTA_MIN_USD = float(os.getenv("DELTA_MIN_USD", "30000"))      # minimum total USD traded to consider
DELTA_COOLDOWN = int(os.getenv("DELTA_COOLDOWN", "600"))       # cooldown per symbol (s)

# polite delay between per-symbol API calls in breakout/delta loop
PER_SYMBOL_DELAY = float(os.getenv("PER_SYMBOL_DELAY", "0.06"))

# -------------------------
# Exchange
# -------------------------
exchange = ccxt.bybit({"enableRateLimit": True, "options": {"defaultType": "swap"}})

# -------------------------
# Telegram helper
# -------------------------
def send_telegram(text: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_IDS:
        print("Telegram not configured — would send:\n", text)
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    for cid in TELEGRAM_CHAT_IDS:
        try:
            resp = requests.post(url, data={
                "chat_id": cid,
                "text": text,
                "parse_mode": "Markdown",
                "disable_web_page_preview": True
            }, timeout=10)
            # optional debug print
            print(f"Telegram -> chat {cid}: {resp.status_code}")
        except Exception as e:
            print("Telegram send error:", e)

# -------------------------
# Safe fetchers
# -------------------------
def safe_fetch_ohlcv(symbol: str, timeframe: str, limit: int = 3):
    try:
        return exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
    except Exception as e:
        # suppress noisy logs
        # print("ohlcv error", symbol, timeframe, e)
        return None

def safe_fetch_trades(symbol: str, limit: int = 200):
    try:
        return exchange.fetch_trades(symbol, limit=limit)
    except Exception:
        return []

def safe_fetch_ticker(symbol: str):
    try:
        return exchange.fetch_ticker(symbol)
    except Exception:
        return None

def safe_load_markets():
    try:
        return exchange.load_markets()
    except Exception as e:
        print("load_markets error:", e)
        return {}

# -------------------------
# Symbols filtering: ONLY Bybit USDT perpetual swaps
# -------------------------
def get_perpetual_symbols():
    try:
        markets = safe_load_markets()
        syms = []
        for s, m in markets.items():
            # ccxt market object may have 'type' or 'contract' flags; ensure swap/perpetual and linear USDT
            mtype = m.get("type") or m.get("contractType") or m.get("contract") or ""
            linear = m.get("linear", None)
            # fallback: prefer symbols that end with /USDT and have 'swap' type
            if s.endswith("/USDT") and (mtype == "swap" or m.get("contractType") == "PERPETUAL" or linear is True):
                syms.append(s)
        return sorted(list(set(syms)))
    except Exception as e:
        print("get_perpetual_symbols error:", e)
        return []

# -------------------------
# Percent-change monitor (unchanged)
# -------------------------
last_prices = {}
last_alert_time = {}
last_hb = 0.0
TF_THRESH = {"1m": THRESH_1M, "3m": THRESH_3M, "1h": THRESH_1H}

def percent_monitor_loop():
    global last_hb
    symbols = get_perpetual_symbols()
    print(f"[percent] Monitoring {len(symbols)} perpetual symbols")
    # immediate startup heartbeat
    send_telegram("💓 Heartbeat — bot avviato!")
    last_hb = time.time()

    while True:
        for s in symbols[:MAX_CANDIDATES_PER_CYCLE]:
            for tf in (FAST_TFS + SLOW_TFS):
                ohlcv = safe_fetch_ohlcv(s, tf, limit=3)
                if not ohlcv or len(ohlcv) < 2:
                    continue
                price = float(ohlcv[-1][4])
                key = f"{s}_{tf}"
                if key not in last_prices:
                    last_prices[key] = price
                    continue
                prev = last_prices[key]
                variation = ((price - prev) / (prev + 1e-12)) * 100
                threshold = TF_THRESH.get(tf, 999)
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

        # heartbeat periodic
        now = time.time()
        if now - last_hb >= HEARTBEAT_INTERVAL:
            send_telegram("💓 Heartbeat — bot attivo")
            last_hb = now

        time.sleep(LOOP_DELAY)

# -------------------------
# Breakout Detector (Option 2) + Delta Spike (Option 3) combined loop
# -------------------------
last_breakout_alert = {}
last_delta_alert = {}

def compute_volume_ma_usd(symbol: str, lookback: int = MA_LOOKBACK):
    """Return (ma_volume_usd, current_volume_usd, current_close) or (None, None, None)"""
    ohlcv = safe_fetch_ohlcv(symbol, "1m", limit=lookback + 1)
    if not ohlcv or len(ohlcv) < lookback + 1:
        return None, None, None
    closes = [float(c[4]) for c in ohlcv[-(lookback + 1):]]
    vols = [float(c[5]) for c in ohlcv[-(lookback + 1):]]
    # last element corresponds to current minute
    current_vol = vols[-1]
    current_close = closes[-1]
    # compute MA over previous lookback candles excluding current
    prev_vols = vols[:-1]
    if len(prev_vols) == 0:
        return None, None, None
    ma_vol = sum(prev_vols) / len(prev_vols)
    # convert to USD
    ma_vol_usd = ma_vol * (sum(closes[:-1]) / len(closes[:-1])) if len(closes[:-1])>0 else ma_vol * current_close
    current_vol_usd = current_vol * current_close
    return ma_vol_usd, current_vol_usd, current_close

def compute_trades_delta_usd(symbol: str, limit: int = DELTA_TRADE_LIMIT):
    trades = safe_fetch_trades(symbol, limit=limit)
    if not trades:
        return 0.0, 0.0
    buy_usd = 0.0
    sell_usd = 0.0
    for tr in trades:
        price = float(tr.get("price", 0.0))
        amount = float(tr.get("amount", 0.0))
        side = (tr.get("side") or "").lower()
        val = price * amount
        if side == "buy":
            buy_usd += val
        elif side == "sell":
            sell_usd += val
    return buy_usd, sell_usd

def breakout_and_delta_loop():
    symbols = get_perpetual_symbols()
    print(f"[breakout_delta] Monitoring {len(symbols)} perpetual symbols for Breakout & Delta")
    while True:
        for s in symbols[:MAX_CANDIDATES_PER_CYCLE]:
            try:
                # ---- Breakout Detector ----
                ma_vol_usd, cur_vol_usd, cur_close = compute_volume_ma_usd(s, lookback=MA_LOOKBACK)
                if ma_vol_usd is not None and cur_vol_usd is not None and cur_close is not None:
                    # relative spike in price 1m
                    ohlcv = safe_fetch_ohlcv(s, "1m", limit=2)
                    if ohlcv and len(ohlcv) >= 2:
                        prev_close = float(ohlcv[-2][4])
                        last_close = float(ohlcv[-1][4])
                        spike_pct = ((last_close - prev_close) / (prev_close + 1e-12)) * 100
                        # check volume multiplier and spike threshold
                        if cur_vol_usd >= ma_vol_usd * VOLUME_MULTIPLIER and abs(spike_pct) >= SPIKE_PCT_BREAKOUT:
                            nowt = time.time()
                            last_ts = last_breakout_alert.get(s, 0)
                            if nowt - last_ts >= BREAKOUT_COOLDOWN:
                                direction = "LONG" if spike_pct > 0 else "SHORT"
                                msg = (
                                    f"⚡ *BREAKOUT DETECTOR* — {s}\n"
                                    f"Spike 1m: {spike_pct:+.2f}%\n"
                                    f"Volume 1m: ${cur_vol_usd:,.0f} (MA{MA_LOOKBACK}: ${ma_vol_usd:,.0f})\n"
                                    f"Direzione: {direction}\n"
                                    f"[{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}]"
                                )
                                send_telegram(msg)
                                last_breakout_alert[s] = nowt

                # ---- Delta Spike Detector ----
                buy_usd, sell_usd = compute_trades_delta_usd(s, limit=DELTA_TRADE_LIMIT)
                total_usd = buy_usd + sell_usd
                if total_usd >= DELTA_MIN_USD:
                    # compute ratio defensively
                    ratio = (buy_usd / (sell_usd + 1e-12)) if sell_usd > 0 else (math.inf if buy_usd>0 else 1.0)
                    nowt = time.time()
                    last_ts = last_delta_alert.get(s, 0)
                    if (ratio >= DELTA_RATIO_THRESHOLD or (1/ratio) >= DELTA_RATIO_THRESHOLD) and (nowt - last_ts >= DELTA_COOLDOWN):
                        # direction by delta
                        if ratio >= DELTA_RATIO_THRESHOLD:
                            dir_str = "BUY PRESSURE"
                        else:
                            dir_str = "SELL PRESSURE"
                        msg = (
                            f"📊 *DELTA SPIKE* — {s}\n"
                            f"Buy USD (recent): ${buy_usd:,.0f}\n"
                            f"Sell USD (recent): ${sell_usd:,.0f}\n"
                            f"Ratio: {ratio:.2f}\n"
                            f"Signal: {dir_str}\n"
                            f"[{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}]"
                        )
                        send_telegram(msg)
                        last_delta_alert[s] = nowt

            except Exception as e:
                # protect loop from exceptions
                print(f"[breakout_delta] error for {s}: {e}")
            # polite delay to respect rate limits
            time.sleep(PER_SYMBOL_DELAY)
        # short sleep before next pass
        time.sleep(1)

# -------------------------
# Flask app (health + test)
# -------------------------
app = Flask(__name__)

@app.route("/")
def home():
    return "Crypto Alert Bot — Running"

@app.route("/test")
def test():
    send_telegram("🧪 Test OK")
    return "Test sent", 200

# -------------------------
# Main start
# -------------------------
if __name__ == "__main__":
    print("🚀 Starting Crypto Alert Bot (percent monitor + breakout & delta)...")
    # start percent monitor
    threading.Thread(target=percent_monitor_loop, daemon=True).start()
    # start breakout+delta loop
    threading.Thread(target=breakout_and_delta_loop, daemon=True).start()
    # start flask (web) — runs in main thread on Render, but we start it as blocking here
    app.run(host="0.0.0.0", port=PORT)
