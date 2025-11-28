#!/usr/bin/env python3
# monitor_bybit_flask.py
# Complete: Percent-change monitor + ASCENSORE SAFE (preset SAFE) + Heartbeat (init + periodic)
from __future__ import annotations
import os
import time
import threading
from datetime import datetime, timezone
from dotenv import load_dotenv
from flask import Flask
import requests
import ccxt

# -------------------------
# Load environment
# -------------------------
load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
# accept either TELEGRAM_CHAT_ID or TELEGRAM_CHAT_IDS (comma-separated)
_chat_env = os.getenv("TELEGRAM_CHAT_IDS") or os.getenv("TELEGRAM_CHAT_ID") or ""
TELEGRAM_CHAT_IDS = [c.strip() for c in _chat_env.split(",") if c.strip()]

PORT = int(os.getenv("PORT", "10000"))

FAST_TFS = os.getenv("FAST_TFS", "1m,3m").split(",")
SLOW_TFS = os.getenv("SLOW_TFS", "1h").split(",")
LOOP_DELAY = int(os.getenv("LOOP_DELAY", "10"))
MAX_CANDIDATES_PER_CYCLE = int(os.getenv("MAX_CANDIDATES_PER_CYCLE", "1000"))

THRESH_1M = float(os.getenv("THRESH_1M", "7.5"))
THRESH_3M = float(os.getenv("THRESH_3M", "11.5"))
THRESH_1H = float(os.getenv("THRESH_1H", "17.0"))
COOLDOWN_SECONDS = int(os.getenv("COOLDOWN_SECONDS", "180"))
HEARTBEAT_INTERVAL = int(os.getenv("HEARTBEAT_INTERVAL", "3600"))

# ASCENSORE SAFE parameters (preset SAFE)
ASC_MIN_SPIKE_PCT = float(os.getenv("ASC_MIN_SPIKE_PCT", "2.0"))        # % change in 1m
ASC_MIN_VOLUME_USD = float(os.getenv("ASC_MIN_VOLUME_USD", "150000"))  # $ volume in last 1m
ASC_MIN_LIQUIDITY_DEPTH = float(os.getenv("ASC_MIN_LIQUIDITY_DEPTH", "80000"))  # $ depth around mid
ASC_DELTA_MIN = float(os.getenv("ASC_DELTA_MIN", "20000"))            # $ buy-sell or sell-buy required
ASC_COOLDOWN = int(os.getenv("ASC_COOLDOWN", "600"))                  # cooldown seconds per symbol
ASC_PER_SYMBOL_DELAY = float(os.getenv("ASC_PER_SYMBOL_DELAY", "0.05"))

# -------------------------
# Exchange (Bybit)
# -------------------------
exchange = ccxt.bybit({"enableRateLimit": True, "options": {"defaultType": "swap"}})

# -------------------------
# Telegram helper
# -------------------------
def send_telegram(text: str):
    """Send text to configured Telegram chats; prints to stdout if not configured."""
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
            # debug log
            print(f"Telegram -> chat {cid}: {resp.status_code}")
        except Exception as e:
            print("Telegram send error:", e)

# -------------------------
# Utility & fetchers
# -------------------------
def safe_fetch_ohlcv(symbol: str, timeframe: str, limit: int = 3):
    try:
        return exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
    except Exception as e:
        # don't spam logs
        # print(f"fetch_ohlcv error {symbol} {timeframe}: {e}")
        return None

def get_perpetual_symbols():
    try:
        markets = exchange.load_markets()
        syms = [s for s in markets.keys() if s.endswith("/USDT")]
        return sorted(syms)
    except Exception as e:
        print("Error loading markets:", e)
        return []

def compute_orderbook_depth(symbol: str, pct_window: float = 0.003, levels: int = 20):
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
    except Exception:
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
    except Exception:
        return 0.0, 0.0

# -------------------------
# Percent-change monitor
# -------------------------
last_prices: dict = {}
last_alert_time: dict = {}
last_hb = 0.0
TF_THRESH = {"1m": THRESH_1M, "3m": THRESH_3M, "1h": THRESH_1H}

def percent_monitor_loop():
    global last_hb
    symbols = get_perpetual_symbols()
    print(f"[percent] Monitoring {len(symbols)} perpetual symbols")
    # ensure last_hb initialised so periodic heartbeat behaves
    last_hb = time.time()  # local var shadowing avoided by global above being updated below
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

        # Heartbeat periodic (uses module-level last_hb)
        now = time.time()
        if now - globals().get("last_hb", 0.0) >= HEARTBEAT_INTERVAL:
            send_telegram("💓 Heartbeat — bot attivo")
            globals()["last_hb"] = now

        time.sleep(LOOP_DELAY)

# -------------------------
# ASCENSORE SAFE monitor (preset SAFE)
# -------------------------
asc_last_alert: dict = {}

def ascensore_safe_check_symbol(symbol: str):
    """Return alert message or None after applying SAFE filters."""
    try:
        # cooldown
        now = time.time()
        if symbol in asc_last_alert and now - asc_last_alert[symbol] < ASC_COOLDOWN:
            return None

        ohlcv = safe_fetch_ohlcv(symbol, "1m", limit=2)
        if not ohlcv or len(ohlcv) < 2:
            return None

        prev_close = float(ohlcv[-2][4])
        last_close = float(ohlcv[-1][4])
        if prev_close == 0:
            return None

        spike_pct = ((last_close - prev_close) / prev_close) * 100
        volume = float(ohlcv[-1][5])  # base-asset volume
        vol_usd = volume * last_close

        # basic filters
        if abs(spike_pct) < ASC_MIN_SPIKE_PCT:
            return None
        if vol_usd < ASC_MIN_VOLUME_USD:
            return None

        # liquidity depth check
        depth = compute_orderbook_depth(symbol, pct_window=0.003, levels=20)
        if depth < ASC_MIN_LIQUIDITY_DEPTH:
            return None

        # trades delta
        buy_usd, sell_usd = compute_trade_delta(symbol, limit=200)
        delta = buy_usd - sell_usd

        # require delta aligned with spike and above threshold
        if spike_pct > 0:
            if delta < ASC_DELTA_MIN:
                return None
            direction = "LONG"
        else:
            if -delta < ASC_DELTA_MIN:
                return None
            direction = "SHORT"

        # passed filters -> build message and register alert time
        asc_last_alert[symbol] = now
        msg = (
            f"🚨 *ASCENSORE SAFE — SPIKE CONFERMATO*\n"
            f"*{symbol}* — {direction}\n"
            f"{spike_pct:+.2f}% in 1m\n"
            f"• Volume 1m: ${vol_usd:,.0f}\n"
            f"• Liquidity (±0.3%): ${depth:,.0f}\n"
            f"• Delta (buy - sell recent): ${delta:,.0f}\n"
            f"• Prezzo: {last_close:.6f}\n"
            f"[{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}]"
        )
        return msg
    except Exception:
        return None

def ascensore_safe_loop():
    symbols = get_perpetual_symbols()
    print(f"[ascensore] Monitoring {len(symbols)} perpetual symbols (SAFE)")
    while True:
        for s in symbols[:MAX_CANDIDATES_PER_CYCLE]:
            alert_msg = ascensore_safe_check_symbol(s)
            if alert_msg:
                send_telegram(alert_msg)
                print(f"[ascensore] alert -> {s}")
            time.sleep(ASC_PER_SYMBOL_DELAY)
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
    print("🚀 Starting Crypto Alert Bot (percent monitor + ASCENSORE SAFE)...")

    # immediate heartbeat on startup
    send_telegram("💓 Heartbeat — bot avviato!")
    globals()["last_hb"] = time.time()

    # percent monitor thread
    threading.Thread(target=percent_monitor_loop, daemon=True).start()

    # ascensore safe thread
    threading.Thread(target=ascensore_safe_loop, daemon=True).start()

    # flask thread (development server; acceptable on Render as web process)
    threading.Thread(target=lambda: app.run(host="0.0.0.0", port=PORT), daemon=True).start()

    # keep main alive
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        print("Exiting...")
