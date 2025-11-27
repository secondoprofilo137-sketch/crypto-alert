#!/usr/bin/env python3

# Crypto Alert Bot completo — Percent monitor + ASCENSORE SAFE + Heartbeat

import os
import time
import threading
from datetime import datetime, timezone
from dotenv import load_dotenv
from flask import Flask
import requests
import ccxt

# -----------------------

# Load env

# -----------------------

load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_IDS = [cid.strip() for cid in os.getenv("TELEGRAM_CHAT_ID", "").split(",") if cid.strip()]
PORT = int(os.getenv("PORT", 10000))

# Percent-change monitor

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

# ASCENSORE SAFE

ASC_MIN_SPIKE_PCT = float(os.getenv("ASC_MIN_SPIKE_PCT", 2.0))
ASC_MIN_VOLUME_USD = float(os.getenv("ASC_MIN_VOLUME_USD", 150000))
ASC_MIN_LIQUIDITY_DEPTH = float(os.getenv("ASC_MIN_LIQUIDITY_DEPTH", 80000))
ASC_DELTA_MIN = float(os.getenv("ASC_DELTA_MIN", 20000))
ASC_COOLDOWN = int(os.getenv("ASC_COOLDOWN", 600))
ASC_PER_SYMBOL_DELAY = float(os.getenv("ASC_PER_SYMBOL_DELAY", 0.05))

# -----------------------

# Exchange

# -----------------------

exchange = ccxt.bybit({"enableRateLimit": True, "options": {"defaultType": "swap"}})

# -----------------------

# Telegram helper

# -----------------------

def send_telegram(text: str):
if not TELEGRAM_TOKEN or not CHAT_IDS:
print("Telegram not configured. Message:\n", text)
return
url = f"[https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage](https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage)"
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

# Utility

# -----------------------

def get_perpetual_symbols():
try:
markets = exchange.load_markets()
return sorted([s for s in markets.keys() if s.endswith("/USDT")])
except Exception as e:
print("Error loading markets:", e)
return []

def safe_fetch_ohlcv(symbol: str, timeframe: str, limit: int = 3):
try:
return exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
except Exception:
return None

def compute_orderbook_depth(symbol: str, pct_window: float = 0.003, levels: int = 10):
try:
ob = exchange.fetch_order_book(symbol, limit=levels)
bids = ob.get("bids", [])
asks = ob.get("asks", [])
if not bids or not asks:
return 0.0
best_bid = bids[0][0]
best_ask = asks[0][0]
mid = (best_bid + best_ask) / 2
low_thr = mid * (1 - pct_window)
high_thr = mid * (1 + pct_window)
depth_buy = sum(price*amt for price, amt in bids if price >= low_thr)
depth_sell = sum(price*amt for price, amt in asks if price <= high_thr)
return depth_buy + depth_sell
except Exception:
return 0.0

def compute_trade_delta(symbol: str, limit: int = 200):
try:
trades = exchange.fetch_trades(symbol, limit=limit)
buy, sell = 0.0, 0.0
for tr in trades:
price = float(tr.get("price", 0.0))
amt = float(tr.get("amount", 0.0))
side = tr.get("side", "").lower()
val = price * amt
if side == "buy":
buy += val
elif side == "sell":
sell += val
return buy, sell
except Exception:
return 0.0, 0.0

# -----------------------

# Percent monitor

# -----------------------

last_prices = {}
last_alert_time = {}
last_hb = 0

def percent_monitor_loop():
global last_hb
symbols = get_perpetual_symbols()
print(f"[percent] Monitoring {len(symbols)} symbols")
while True:
for s in symbols[:MAX_CANDIDATES_PER_CYCLE]:
for tf in FAST_TFS + SLOW_TFS:
ohlcv = safe_fetch_ohlcv(s, tf)
if not ohlcv or len(ohlcv) < 2:
continue
price = ohlcv[-1][4]
key = f"{s}_{tf}"
if key not in last_prices:
last_prices[key] = price
continue
prev = last_prices[key]
variation = ((price - prev)/(prev+1e-12))*100
threshold = THRESHOLDS.get(tf, 999)
if abs(variation) >= threshold:
nowt = time.time()
last_ts = last_alert_time.get(key, 0)
if nowt - last_ts >= COOLDOWN_SECONDS:
direction = "📈 Rialzo" if variation > 0 else "📉 Ribasso"
msg = (
f"🔔 *ALERT* — {s} ({tf})\n"
f"Variazione: {variation:+.2f}%\n"
f"Prezzo: {price:.6f}\n"
f"{direction}\n"
f"[{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}]"
)
send_telegram(msg)
last_alert_time[key] = nowt
last_prices[key] = price

```
    now = time.time()
    if now - last_hb >= HEARTBEAT_INTERVAL:
        send_telegram("💓 Heartbeat — bot attivo")
        last_hb = now

    time.sleep(LOOP_DELAY)
```

# -----------------------

# ASCENSORE SAFE

# -----------------------

asc_last_alert = {}

def ascensore_safe_check_symbol(symbol: str):
now = time.time()
if symbol in asc_last_alert and now - asc_last_alert[symbol] < ASC_COOLDOWN:
return None
ohlcv = safe_fetch_ohlcv(symbol, "1m", limit=2)
if not ohlcv or len(ohlcv)<2:
return None
prev, last = ohlcv[-2][4], ohlcv[-1][4]
spike_pct = ((last-prev)/prev)*100
volume_usd = ohlcv[-1][5]*last
if abs(spike_pct) < ASC_MIN_SPIKE_PCT or volume_usd < ASC_MIN_VOLUME_USD:
return None
depth = compute_orderbook_depth(symbol, pct_window=0.003, levels=20)
if depth < ASC_MIN_LIQUIDITY_DEPTH:
return None
buy_usd, sell_usd = compute_trade_delta(symbol, limit=200)
delta = buy_usd - sell_usd
if (spike_pct>0 and delta<ASC_DELTA_MIN) or (spike_pct<0 and -delta<ASC_DELTA_MIN):
return None
asc_last_alert[symbol] = now
direction = "LONG" if spike_pct>0 else "SHORT"
msg = (
f"🚨 *ASCENSORE SAFE — SPIKE CONFERMATO*\n"
f"*{symbol}* — {direction}\n"
f"{spike_pct:+.2f}% in 1m\n"
f"• Volume 1m: ${volume_usd:,.0f}\n"
f"• Liquidity ±0.3%: ${depth:,.0f}\n"
f"• Delta (buy-sell): ${delta:,.0f}\n"
f"• Prezzo: {last:.6f}\n"
f"[{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}]"
)
return msg

def ascensore_safe_loop():
symbols = get_perpetual_symbols()
print(f"[ascensore] Monitoring {len(symbols)} symbols")
while True:
for s in symbols:
msg = ascensore_safe_check_symbol(s)
if msg:
print(f"[ascensore] Alert {s}")
send_telegram(msg)
time.sleep(ASC_PER_SYMBOL_DELAY)

# -----------------------

# Flask

# -----------------------

app = Flask(**name**)

@app.route("/")
def home():
return "Crypto Alert Bot — Running"

@app.route("/test")
def test():
send_telegram("🧪 Test OK")
return "Test sent", 200

# -----------------------

# MAIN

# -----------------------

if **name** == "**main**":
print("🚀 Starting Crypto Alert Bot completo...")

```
# Heartbeat iniziale
send_telegram("💓 Heartbeat — bot avviato!")

threading.Thread(target=percent_monitor_loop, daemon=True).start()
threading.Thread(target=ascensore_safe_loop, daemon=True).start()
threading.Thread(target=lambda: app.run(host="0.0.0.0", port=PORT), daemon=True).start()

try:
    while True:
        time.sleep(3600)
except KeyboardInterrupt:
    print("Exiting...")
```
