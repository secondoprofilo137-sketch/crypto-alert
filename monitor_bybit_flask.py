#!/usr/bin/env python3

# Crypto Alert Bot — Percent monitor + ASCENSORE SAFE + Heartbeat iniziale

from **future** import annotations
import os
import time
import threading
from datetime import datetime, timezone
from dotenv import load_dotenv
from flask import Flask
import requests
import ccxt
import numpy as np

# -------------------------

# Load environment

# -------------------------

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_IDS = [c.strip() for c in os.getenv("TELEGRAM_CHAT_ID", "").split(",") if c.strip()]

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

# ASCENSORE SAFE parameters

ASC_MIN_SPIKE_PCT = float(os.getenv("ASC_MIN_SPIKE_PCT", "2.0"))
ASC_MIN_VOLUME_USD = float(os.getenv("ASC_MIN_VOLUME_USD", "150000"))
ASC_MIN_LIQUIDITY_DEPTH = float(os.getenv("ASC_MIN_LIQUIDITY_DEPTH", "80000"))
ASC_DELTA_MIN = float(os.getenv("ASC_DELTA_MIN", "20000"))
ASC_COOLDOWN = int(os.getenv("ASC_COOLDOWN", "600"))
ASC_PER_SYMBOL_DELAY = float(os.getenv("ASC_PER_SYMBOL_DELAY", "0.05"))

# -------------------------

# Exchange

# -------------------------

exchange = ccxt.bybit({"enableRateLimit": True, "options": {"defaultType": "swap"}})

# -------------------------

# Telegram helper

# -------------------------

def send_telegram(text: str):
if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_IDS:
print("Telegram non configurato — messaggio:", text)
return
url = f"[https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage](https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage)"
for cid in TELEGRAM_CHAT_IDS:
try:
requests.post(url, data={
"chat_id": cid,
"text": text,
"parse_mode": "Markdown",
"disable_web_page_preview": True
}, timeout=10)
except Exception as e:
print("Errore invio Telegram:", e)

# -------------------------

# Percent-change monitor

# -------------------------

last_prices = {}
last_alert_time = {}
last_hb = 0

TF_THRESH = {"1m": THRESH_1M, "3m": THRESH_3M, "1h": THRESH_1H}

def safe_fetch_ohlcv(symbol: str, timeframe: str, limit: int = 3):
try:
return exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
except Exception:
return None

def get_perpetual_symbols():
try:
markets = exchange.load_markets()
syms = [s for s in markets if s.endswith("/USDT")]
return sorted(syms)
except Exception as e:
print("Errore caricamento simboli:", e)
return []

def percent_monitor_loop():
global last_hb
symbols = get_perpetual_symbols()
print(f"[percent] Loaded {len(symbols)} perpetual symbols")
while True:
for s in symbols[:MAX_CANDIDATES_PER_CYCLE]:
for tf in (FAST_TFS + SLOW_TFS):
ohlcv = safe_fetch_ohlcv(s, tf)
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

```
    now = time.time()
    if now - last_hb >= HEARTBEAT_INTERVAL:
        send_telegram("💓 Heartbeat — bot attivo")
        last_hb = now

    time.sleep(LOOP_DELAY)
```

# -------------------------

# ASCENSORE SAFE monitor

# -------------------------

last_asc_alert_time = {}
def ascensore_safe_loop():
global last_asc_alert_time
symbols = get_perpetual_symbols()
while True:
for s in symbols[:MAX_CANDIDATES_PER_CYCLE]:
# fetch 1m OHLCV
ohlcv = safe_fetch_ohlcv(s, "1m", 2)
if not ohlcv or len(ohlcv) < 2:
continue
close_prev, close_now = ohlcv[-2][4], ohlcv[-1][4]
pct_change = ((close_now - close_prev) / (close_prev + 1e-12)) * 100

```
        # volume USD estimate
        volume = float(ohlcv[-1][5])
        price = float(close_now)
        volume_usd = volume * price

        key = f"ASC_{s}"
        last_ts = last_asc_alert_time.get(key, 0)

        if pct_change >= ASC_MIN_SPIKE_PCT and volume_usd >= ASC_MIN_VOLUME_USD and (time.time() - last_ts) >= ASC_COOLDOWN:
            msg = f"🚀 *ASCENSORE SAFE ALERT* — {s}\nVariazione 1m: {pct_change:+.2f}%\nVolume USD: {volume_usd:,.0f}"
            send_telegram(msg)
            last_asc_alert_time[key] = time.time()
        time.sleep(ASC_PER_SYMBOL_DELAY)
    time.sleep(1)
```

# -------------------------

# Flask web server

# -------------------------

app = Flask(**name**)

@app.route("/")
def home():
return "Crypto Alert Bot — Running"

@app.route("/test")
def test():
send_telegram("🧪 Test OK")
return "Test inviato", 200

# -------------------------

# MAIN

# -------------------------

if **name** == "**main**":
print("🚀 Starting Crypto Alert Bot...")

```
send_telegram("💓 Heartbeat — bot avviato!")

# Percent monitor
threading.Thread(target=percent_monitor_loop, daemon=True).start()

# ASCENSORE SAFE
threading.Thread(target=ascensore_safe_loop, daemon=True).start()

# Flask
threading.Thread(target=lambda: app.run(host="0.0.0.0", port=PORT), daemon=True).start()

# Keep main alive
try:
    while True:
        time.sleep(3600)
except KeyboardInterrupt:
    print("Exiting...")
```
