#!/usr/bin/env python3
# monitor_bybit_flask.py
# Bybit: Percent-change monitor + ASCENSORE SAFE (perpetual only)

from __future__ import annotations
import os
import time
import threading
import math
from datetime import datetime, timezone
from dotenv import load_dotenv
from flask import Flask
import requests
import ccxt
import numpy as np

load_dotenv()

# -------------------------
# CONFIG (env-friendly)
# -------------------------
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_IDS = [c.strip() for c in os.getenv("TELEGRAM_CHAT_IDS", "").split(",") if c.strip()]

PORT = int(os.getenv("PORT", "10000"))

# Percent-change monitor
FAST_TFS = os.getenv("FAST_TFS", "1m,3m").split(",")
SLOW_TFS = os.getenv("SLOW_TFS", "1h").split(",")
LOOP_DELAY = int(os.getenv("LOOP_DELAY", "10"))           # seconds between cycles
MAX_CANDIDATES_PER_CYCLE = int(os.getenv("MAX_CANDIDATES_PER_CYCLE", "1000"))
THRESH_1M = float(os.getenv("THRESH_1M", "7.5"))
THRESH_3M = float(os.getenv("THRESH_3M", "11.5"))
THRESH_1H = float(os.getenv("THRESH_1H", "17.0"))
COOLDOWN_SECONDS = int(os.getenv("COOLDOWN_SECONDS", "180"))

# ASCENSORE SAFE params (preset SAFE)
ASC_MIN_SPIKE_PCT = float(os.getenv("ASC_MIN_SPIKE_PCT", "2.0"))      # ±% in 1m
ASC_MIN_VOLUME_USD = float(os.getenv("ASC_MIN_VOLUME_USD", "150000"))# $ volume min in last 1m
ASC_MIN_LIQUIDITY_DEPTH = float(os.getenv("ASC_MIN_LIQUIDITY_DEPTH", "80000")) # $ depth around mid
ASC_DELTA_MIN = float(os.getenv("ASC_DELTA_MIN", "20000"))          # $ buy-sell or sell-buy required
ASC_COOLDOWN = int(os.getenv("ASC_COOLDOWN", "600"))               # seconds cooldown per symbol
ASC_PER_SYMBOL_DELAY = float(os.getenv("ASC_PER_SYMBOL_DELAY", "0.05"))  # polite delay

HEARTBEAT_INTERVAL = int(os.getenv("HEARTBEAT_INTERVAL", "3600"))

# -------------------------
# EXCHANGE
# -------------------------
exchange = ccxt.bybit({
    "enableRateLimit": True,
    "options": {"defaultType": "swap"}
})

# -------------------------
# TELEGRAM
# -------------------------
def send_telegram(text: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_IDS:
        # print to logs if not configured
        print("Telegram not configured — would send:\n", text)
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    for cid in TELEGRAM_CHAT_IDS:
        try:
            requests.post(url, data={
                "chat_id": cid,
                "text": text,
                "parse_mode": "Markdown",
                "disable_web_page_preview": True
            }, timeout=10)
        except Exception as e:
            print("Telegram send error:", e)

# -------------------------
# UTIL: symbols (perpetual only)
# -------------------------
def get_perpetual_symbols():
    try:
        markets = exchange.load_markets()
        syms = []
        for s, m in markets.items():
            # ccxt bybit market object keys vary; use type == 'swap' and settle == 'USDT' if present
            t = m.get("type") or m.get("contract") or ""
            settle = m.get("settle") or m.get("settlement") or m.get("settle_currency") or ""
            # safe check: symbol endswith /USDT is common
            if m.get("symbol", "").endswith("/USDT") and (m.get("type") == "swap" or m.get("future") is None):
                syms.append(m["symbol"])
        syms = sorted(list(set(syms)))
        return syms
    except Exception as e:
        print("Error loading markets:", e)
        return []

# -------------------------
# SAFE OHLCV fetcher
# -------------------------
def safe_fetch_ohlcv(symbol: str, timeframe: str, limit: int = 200):
    try:
        return exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
    except Exception as e:
        # don't crash on single fetch
        # print(f"fetch_ohlcv error {symbol} {timeframe}: {e}")
        return None

# -------------------------
# PERCENT-CHANGE MONITOR (lightweight)
# -------------------------
last_prices = {}
last_alert_time = {}
last_hb = 0

TF_THRESH = {
    "1m": THRESH_1M,
    "3m": THRESH_3M,
    "1h": THRESH_1H
}

def percent_monitor_loop():
    global last_hb
    symbols = get_perpetual_symbols()
    print(f"[percent] Loaded {len(symbols)} perpetual symbols")
    while True:
        for s in list(symbols)[:MAX_CANDIDATES_PER_CYCLE]:
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

        # heartbeat
        now = time.time()
        if now - last_hb >= HEARTBEAT_INTERVAL:
            send_telegram("💓 Heartbeat — bot attivo")
            last_hb = now

        time.sleep(LOOP_DELAY)

# -------------------------
# ASCENSORE SAFE MODULE
# -------------------------
asc_last_alert = {}  # per-symbol cooldown

def compute_spike_and_volume_usd(symbol: str):
    """Return (spike_pct, volume_usd, last_close, prev_close) or None on error"""
    ohlcv = safe_fetch_ohlcv(symbol, "1m", limit=2)
    if not ohlcv or len(ohlcv) < 2:
        return None
    prev = float(ohlcv[-2][4])
    last = float(ohlcv[-1][4])
    volume = float(ohlcv[-1][5])  # volume in base asset
    vol_usd = volume * last
    if prev == 0:
        return None
    spike = ((last - prev) / prev) * 100
    return spike, vol_usd, last, prev

def compute_orderbook_depth(symbol: str, pct_window: float = 0.003, levels: int = 10):
    """Sum price*amount for bids within mid*(1-pct_window) and asks within mid*(1+pct_window)"""
    try:
        ob = exchange.fetch_order_book(symbol, limit=levels)
        bids = ob.get("bids", [])
        asks = ob.get("asks", [])
        if not bids or not asks:
            return 0.0
        best_bid = bids[0][0]
        best_ask = asks[0][0]
        mid = (best_bid + best_ask) / 2.0
        low_thr = mid * (1 - pct_window)
        high_thr = mid * (1 + pct_window)
        depth_buy = sum((price * amount) for price, amount in bids if price >= low_thr)
        depth_sell = sum((price * amount) for price, amount in asks if price <= high_thr)
        return float(depth_buy + depth_sell)
    except Exception as e:
        # print("orderbook error", e)
        return 0.0

def compute_trade_delta(symbol: str, limit: int = 200):
    """Fetch recent trades and return (buy_usd, sell_usd)"""
    try:
        trades = exchange.fetch_trades(symbol, limit=limit)
        buy = 0.0
        sell = 0.0
        for tr in trades:
            price = float(tr.get("price", 0.0))
            amount = float(tr.get("amount", 0.0))
            side = tr.get("side", "").lower()
            val = price * amount
            if side == "buy":
                buy += val
            elif side == "sell":
                sell += val
        return buy, sell
    except Exception as e:
        # print("fetch_trades error", e)
        return 0.0, 0.0

def ascensore_safe_check_symbol(symbol: str):
    """Return message string if alert should be emitted, else None"""
    try:
        # cooldown per symbol
        now = time.time()
        if symbol in asc_last_alert and now - asc_last_alert[symbol] < ASC_COOLDOWN:
            return None

        # 1) spike + volume
        sv = compute_spike_and_volume_usd(symbol)
        if not sv:
            return None
        spike_pct, vol_usd, last_price, prev_price = sv

        if abs(spike_pct) < ASC_MIN_SPIKE_PCT:
            return None
        if vol_usd < ASC_MIN_VOLUME_USD:
            return None

        # 2) liquidity depth check
        depth = compute_orderbook_depth(symbol, pct_window=0.003, levels=20)
        if depth < ASC_MIN_LIQUIDITY_DEPTH:
            return None

        # 3) trades delta (aggressive orders)
        buy_usd, sell_usd = compute_trade_delta(symbol, limit=200)
        delta = buy_usd - sell_usd

        # require delta direction aligned with spike, with threshold
        if spike_pct > 0:
            if delta < ASC_DELTA_MIN:
                return None
            direction = "LONG"
        else:
            if -delta < ASC_DELTA_MIN:
                return None
            direction = "SHORT"

        # passed all filters -> alert
        asc_last_alert[symbol] = now

        # details
        vol_fmt = f"${vol_usd:,.0f}"
        depth_fmt = f"${depth:,.0f}"
        delta_fmt = f"${delta:,.0f}"
        spike_str = f"{spike_pct:+.2f}% in 1m"

        msg = (
            f"🚨 *ASCENSORE SAFE — SPIKE CONFERMATO*\n"
            f"*{symbol}* — {direction}\n"
            f"{spike_str}\n\n"
            f"• Volume 1m: {vol_fmt}\n"
            f"• Liquidity (±0.3%): {depth_fmt}\n"
            f"• Delta (buy - sell recent): {delta_fmt}\n"
            f"• Prezzo: {last_price:.6f}\n"
            f"[{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}]"
        )
        return msg
    except Exception as e:
        # print("asc check error", e)
        return None

def ascensore_safe_loop():
    print("ASCENSORE SAFE loop started...")
    symbols = get_perpetual_symbols()
    print(f"[ascensore] monitoring {len(symbols)} perpetual symbols")
    while True:
        try:
            for sym in symbols:
                res = ascensore_safe_check_symbol(sym)
                if res:
                    print("[ascensore] alert:", sym)
                    send_telegram(res)
                time.sleep(ASC_PER_SYMBOL_DELAY)
        except Exception as e:
            print("[ascensore] loop error:", e)
            time.sleep(2)

# -------------------------
# FLASK + THREAD START
# -------------------------
app = Flask(__name__)

@app.route("/")
def home():
    return "Crypto Alert Bot — Running"

@app.route("/test")
def test():
    send_telegram("🧪 Test OK")
    return "Test sent", 200

def start_threads():
    threading.Thread(target=percent_monitor_loop, daemon=True).start()
    threading.Thread(target=ascensore_safe_loop, daemon=True).start()

if __name__ == "__main__":
    print("🚀 Starting Crypto Alert Bot (percent monitor + ASCENSORE SAFE)...")
    start_threads()
    # start flask (development server is acceptable on Render with web process)
    app.run(host="0.0.0.0", port=PORT)
