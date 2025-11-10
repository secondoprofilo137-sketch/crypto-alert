#!/usr/bin/env python3
# monitor_bybit_flask.py
# Alerts percentuali (1m,3m,1h) + grafico PNG inviato su Telegram + spiegazione IA opzionale
from __future__ import annotations
import os
import time
import math
import threading
import tempfile
from statistics import mean, pstdev
from datetime import datetime, timezone
import requests
import ccxt
from flask import Flask
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

# -----------------------
# CONFIG (env or defaults)
# -----------------------
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_IDS = [cid.strip() for cid in os.getenv("TELEGRAM_CHAT_ID", "").split(",") if cid.strip()]
PORT = int(os.getenv("PORT", 10000))

FAST_TFS = os.getenv("FAST_TFS", "1m,3m").split(",")
SLOW_TFS = os.getenv("SLOW_TFS", "1h").split(",")

LOOP_DELAY = int(os.getenv("LOOP_DELAY", 10))
HEARTBEAT_INTERVAL_SEC = int(os.getenv("HEARTBEAT_INTERVAL_SEC", 3600))
MAX_CANDIDATES_PER_CYCLE = int(os.getenv("MAX_CANDIDATES_PER_CYCLE", 1000))

# thresholds per timeframe
THRESH_1M = float(os.getenv("THRESH_1M", 7.5))
THRESH_3M = float(os.getenv("THRESH_3M", 11.5))
THRESH_1H = float(os.getenv("THRESH_1H", 17.0))
THRESHOLDS = {"1m": THRESH_1M, "3m": THRESH_3M, "1h": THRESH_1H}

COOLDOWN_SECONDS = int(os.getenv("COOLDOWN_SECONDS", 7200))  # default 2 hours as requested

# volume spike (kept but not required)
VOLUME_SPIKE_MULTIPLIER = float(os.getenv("VOLUME_SPIKE_MULTIPLIER", 100.0))
VOLUME_MIN_USD = float(os.getenv("VOLUME_MIN_USD", 1000000.0))
VOLUME_SPIKE_MIN_VOLUME = float(os.getenv("VOLUME_SPIKE_MIN_VOLUME", 500000.0))
VOLUME_COOLDOWN_SECONDS = int(os.getenv("VOLUME_COOLDOWN_SECONDS", 300))

# AI explanatory helper (optional)
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")  # placeholder, you can set a model you have access to

# plotting / candles
PLOT_CANDLES = int(os.getenv("PLOT_CANDLES", 200))

# Exchange + state
exchange = ccxt.bybit({"options": {"defaultType": "swap"}})
last_prices = {}
last_alert_time = {}
last_volume_alert_time = {}
app = Flask(__name__)

# -----------------------
# Telegram helpers
# -----------------------
def send_telegram_text(chat_id: str, text: str):
    if not TELEGRAM_TOKEN:
        print("Telegram token not set. Message:", text)
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "Markdown", "disable_web_page_preview": True}
    try:
        r = requests.post(url, data=payload, timeout=10)
        if r.status_code != 200:
            print("Telegram error:", r.status_code, r.text)
    except Exception as e:
        print("Telegram send error:", e)

def send_telegram_photo(chat_id: str, caption: str, photo_path: str):
    if not TELEGRAM_TOKEN:
        print("Telegram token not set. Would send:", caption)
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto"
    try:
        with open(photo_path, "rb") as f:
            files = {"photo": f}
            data = {"chat_id": chat_id, "caption": caption, "parse_mode": "Markdown"}
            r = requests.post(url, files=files, data=data, timeout=20)
            if r.status_code != 200:
                print("Telegram photo error:", r.status_code, r.text)
    except Exception as e:
        print("Telegram photo exception:", e)

def broadcast_text(text: str):
    for cid in CHAT_IDS:
        send_telegram_text(cid, text)

def broadcast_photo(caption: str, photo_path: str):
    for cid in CHAT_IDS:
        send_telegram_photo(cid, caption, photo_path)

# -----------------------
# Indicators simplified
# -----------------------
def compute_ema(prices, period):
    if not prices or len(prices) < 2: return None
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
        d = prices[i] - prices[i-1]
        gains.append(max(d, 0))
        losses.append(max(-d, 0))
    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))

# -----------------------
# Bybit helpers
# -----------------------
def safe_fetch_ohlcv(symbol, timeframe, limit=200):
    try:
        data = exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
        closes = [c[4] for c in data]
        vols = [c[5] for c in data]
        times = [c[0] for c in data]
        return closes, vols, times
    except Exception as e:
        print(f"fetch error {symbol} {timeframe}: {e}")
        return None, None, None

def get_bybit_derivative_symbols():
    try:
        mk = exchange.load_markets()
        syms = []
        for s, m in mk.items():
            # keep only derivatives/perps or USDT pairs on Bybit
            if m.get("type") == "swap" or s.endswith(":USDT") or "/USDT" in s:
                syms.append(s)
        return sorted(set(syms))
    except Exception as e:
        print("load_markets error:", e)
        return []

# -----------------------
# Chart generator
# -----------------------
def generate_chart_png(symbol: str, timeframe: str, closes, vols, times, path: str):
    # closes, vols, times arrays (same length)
    try:
        plt.close("all")
        fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(10, 6), gridspec_kw={"height_ratios": [3, 1]}, sharex=True)
        x = list(range(len(closes)))
        ax1.plot(x, closes, label="Close", linewidth=1)
        # EMAs
        if len(closes) >= 30:
            ema10 = [compute_ema(closes[:i+1], 10) for i in range(len(closes))]
            ema30 = [compute_ema(closes[:i+1], 30) for i in range(len(closes))]
            ax1.plot(x, ema10, label="EMA10", linewidth=1)
            ax1.plot(x, ema30, label="EMA30", linewidth=1)
        # RSI small subplot overlay
        rsi = compute_rsi(closes, 14)
        ax1.set_title(f"{symbol} {timeframe} — last {len(closes)} candles")
        ax1.legend(loc="upper left", fontsize="small")
        # volume bars
        ax2.bar(x, vols, width=0.8)
        ax2.set_ylabel("Volume")
        ax2.set_xlabel("Candles (recent -> right)")
        # annotate last price
        ax1.annotate(f"Last {closes[-1]:.6f}", xy=(len(closes)-1, closes[-1]), xytext=(len(closes)-1, closes[-1]),
                     textcoords="offset points", xycoords="data")
        plt.tight_layout()
        fig.savefig(path, dpi=150)
    except Exception as e:
        print("Error generating chart:", e)

# -----------------------
# Optional AI explanation (uses OpenAI API if key provided)
# -----------------------
def ai_explanation(symbol: str, timeframe: str, closes, vols, extra_notes=""):
    if not OPENAI_API_KEY:
        return ""  # no AI available
    # Create a light prompt describing latest structure; keep small to avoid heavy costs
    try:
        last = closes[-20:] if len(closes) >= 20 else closes
        last_pct = ((last[-1] - last[0]) / last[0]) * 100 if last and last[0] else 0.0
        prompt = (
            f"Provide a short, clear trading-style explanation (1-3 lines) for {symbol} on timeframe {timeframe}.\n"
            f"Latest change on last window: {last_pct:+.2f}%.\n"
            f"Include risk note and a one-line actionable phrase. Extra: {extra_notes}\n"
            "Keep it concise and in Italian.\n"
        )
        headers = {"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"}
        body = {
            "model": OPENAI_MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 120,
        }
        r = requests.post("https://api.openai.com/v1/chat/completions", json=body, headers=headers, timeout=15)
        if r.status_code == 200:
            j = r.json()
            # extract content
            txt = j["choices"][0]["message"]["content"].strip()
            return txt
        else:
            print("OpenAI error", r.status_code, r.text)
            return ""
    except Exception as e:
        print("AI explanation error:", e)
        return ""

# -----------------------
# Monitor loop (core: percent alerts 1m/3m/1h)
# -----------------------
@app.route("/")
def home():
    return "✅ Crypto Alert Bot — running"

@app.route("/test")
def test():
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    broadcast_text(f"🧪 TEST OK — {now}")
    return "Test sent", 200

def monitor_loop():
    symbols = get_bybit_derivative_symbols()
    print(f"Trovati simboli: {len(symbols)}")
    initialized = False
    last_heartbeat = 0

    while True:
        start = time.time()
        for symbol in symbols[:MAX_CANDIDATES_PER_CYCLE]:
            for tf in FAST_TFS + SLOW_TFS:
                closes, vols, times = safe_fetch_ohlcv(symbol, tf, limit=PLOT_CANDLES)
                if not closes:
                    continue
                price = closes[-1]
                key = f"{symbol}_{tf}"

                # initialize (skip first comparison)
                if not initialized:
                    last_prices[key] = price
                    continue

                prev = last_prices.get(key, price)
                try:
                    variation = ((price - prev) / prev) * 100 if prev else 0.0
                except Exception:
                    variation = 0.0

                threshold = THRESHOLDS.get(tf, 5.0)

                # volume spike detection (optional)
                vol_spike = False
                vol_ratio = 1.0
                if vols and len(vols) >= 6:
                    baseline = mean(vols[-6:-1]) if mean(vols[-6:-1]) else 0.0
                    last_vol = vols[-1]
                    if baseline:
                        vol_ratio = last_vol / baseline
                        vol_spike = vol_ratio >= VOLUME_SPIKE_MULTIPLIER and last_vol >= VOLUME_SPIKE_MIN_VOLUME

                alert_price = abs(variation) >= threshold
                alert_volume = vol_spike

                now_ts = time.time()
                # cooldown per key
                if key in last_alert_time and now_ts - last_alert_time[key] < COOLDOWN_SECONDS:
                    # skip due to cooldown
                    pass
                else:
                    if alert_price or alert_volume:
                        # prepare message + chart + (optional) AI explanation
                        trend = "📈 Rialzo" if variation > 0 else "📉 Crollo"
                        ts_txt = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
                        base_msg = (f"🔔 ALERT {symbol} ({tf}) {variation:+.2f}%\n"
                                    f"💰 Prezzo: {price:.6f}\n"
                                    f"{trend}\n")

                        # generate small score details (RSI) to include
                        rsi = compute_rsi(closes)
                        if rsi is not None:
                            base_msg += f"📉 RSI: {rsi:.1f}\n"

                        base_msg += f"[{ts_txt}]"

                        # generate chart file
                        try:
                            tmp = tempfile.NamedTemporaryFile(prefix="chart_", suffix=".png", delete=False)
                            tmp_path = tmp.name
                            tmp.close()
                            generate_chart_png(symbol, tf, closes[-PLOT_CANDLES:], vols[-PLOT_CANDLES:], times[-PLOT_CANDLES:], tmp_path)
                        except Exception as e:
                            print("chart gen error:", e)
                            tmp_path = None

                        # optional AI explanation
                        ai_text = ai_explanation(symbol, tf, closes, vols, extra_notes="short")
                        caption = base_msg
                        if ai_text:
                            caption = caption + "\n\n" + ai_text

                        # send to chats
                        if tmp_path:
                            broadcast_photo(caption, tmp_path)
                            try:
                                os.remove(tmp_path)
                            except Exception:
                                pass
                        else:
                            broadcast_text(caption)

                        # update cooldown
                        last_alert_time[key] = now_ts

                # always update last price
                last_prices[key] = price

        initialized = True

        # heartbeat
        if time.time() - last_heartbeat >= HEARTBEAT_INTERVAL_SEC:
            nowtxt = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
            broadcast_text(f"💓 Heartbeat — bot attivo ({nowtxt}) | Simboli: {len(symbols)}")
            last_heartbeat = time.time()

        elapsed = time.time() - start
        if elapsed < LOOP_DELAY:
            time.sleep(LOOP_DELAY - elapsed)

# -----------------------
# Entrypoint
# -----------------------
if __name__ == "__main__":
    print("🚀 Avvio monitor Bybit — percent alerts 1m/3m/1h + chart + optional AI")
    # start flask (web service)
    threading.Thread(target=lambda: app.run(host="0.0.0.0", port=PORT), daemon=True).start()
    # start monitor
    threading.Thread(target=monitor_loop, daemon=True).start()
    # block main
    while True:
        time.sleep(3600)
