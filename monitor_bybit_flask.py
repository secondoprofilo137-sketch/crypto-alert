import ccxt
import time
import requests
import numpy as np
from datetime import datetime, timezone
from flask import Flask
from dotenv import load_dotenv
import os

load_dotenv()

# === CONFIG ===
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_IDS = os.getenv("TELEGRAM_CHAT_ID", "").split(",")
PORT = int(os.getenv("PORT", 10000))

FAST_TFS = os.getenv("FAST_TFS", "1m,3m").split(",")
SLOW_TFS = os.getenv("SLOW_TFS", "1h").split(",")

THRESH_1M = float(os.getenv("THRESH_1M", 7.5))
THRESH_3M = float(os.getenv("THRESH_3M", 11.5))
THRESH_1H = float(os.getenv("THRESH_1H", 17.0))

COOLDOWN_SECONDS = int(os.getenv("COOLDOWN_SECONDS", 7200))
HEARTBEAT_INTERVAL = int(os.getenv("HEARTBEAT_INTERVAL_SEC", 3600))

# === SETUP ===
app = Flask(__name__)
exchange = ccxt.bybit({'enableRateLimit': True})
markets = exchange.load_markets()

print(f"✅ Mercati caricati: {len(markets)} simboli trovati")
symbols = [s for s in markets if '/USDT' in s and ':USDT' not in s]
print(f"🔍 Simboli monitorati: {len(symbols)} — esempio: {symbols[:5]}")

last_alert_times = {}
last_heartbeat = 0

# === FUNZIONI TELEGRAM ===
def send_telegram_message(msg: str):
    for chat_id in CHAT_IDS:
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
        try:
            requests.post(url, data={"chat_id": chat_id.strip(), "text": msg})
        except Exception as e:
            print(f"❌ Errore Telegram ({chat_id}): {e}")

# === FUNZIONI ANALISI ===
def get_price_change(symbol, tf, lookback=2):
    try:
        candles = exchange.fetch_ohlcv(symbol, timeframe=tf, limit=lookback)
        if len(candles) < 2:
            return None
        old_close = candles[-2][4]
        new_close = candles[-1][4]
        return ((new_close - old_close) / old_close) * 100
    except Exception as e:
        print(f"⚠️ Errore fetch {symbol} {tf}: {e}")
        return None

def can_send_alert(symbol):
    last = last_alert_times.get(symbol, 0)
    return (time.time() - last) > COOLDOWN_SECONDS

# === LOOP PRINCIPALE ===
def main_loop():
    global last_heartbeat
    print("🚀 Avvio monitoraggio variazioni percentuali + Wyckoff (disattivato report top10)")

    while True:
        now = datetime.now(timezone.utc)

        # 💗 HEARTBEAT
        if time.time() - last_heartbeat >= HEARTBEAT_INTERVAL:
            msg = f"💗 Heartbeat — bot attivo ({now.strftime('%Y-%m-%d %H:%M:%S UTC')}) | Simboli: {len(symbols)}"
            send_telegram_message(msg)
            last_heartbeat = time.time()

        for symbol in symbols:
            try:
                pct_1m = get_price_change(symbol, "1m")
                pct_3m = get_price_change(symbol, "3m")
                pct_1h = get_price_change(symbol, "1h")

                if pct_1m is None or pct_3m is None or pct_1h is None:
                    continue

                # DEBUG log
                print(f"[DEBUG] {symbol} → Δ1m: {pct_1m:.2f}% | Δ3m: {pct_3m:.2f}% | Δ1h: {pct_1h:.2f}%")

                # === ALERT 1M ===
                if abs(pct_1m) >= THRESH_1M and can_send_alert(symbol):
                    direction = "Rialzo" if pct_1m > 0 else "Crollo"
                    msg = (
                        f"🔔 ALERT {symbol} 1m\n"
                        f"📈 {direction} {pct_1m:+.2f}%\n"
                        f"🕒 {now.strftime('%Y-%m-%d %H:%M:%S UTC')}"
                    )
                    send_telegram_message(msg)
                    print(f"🚨 Alert generato per {symbol} — timeframe 1m")
                    last_alert_times[symbol] = time.time()

                # === ALERT 3M ===
                if abs(pct_3m) >= THRESH_3M and can_send_alert(symbol):
                    direction = "Rialzo" if pct_3m > 0 else "Crollo"
                    msg = (
                        f"🔔 ALERT {symbol} 3m\n"
                        f"📈 {direction} {pct_3m:+.2f}%\n"
                        f"🕒 {now.strftime('%Y-%m-%d %H:%M:%S UTC')}"
                    )
                    send_telegram_message(msg)
                    print(f"🚨 Alert generato per {symbol} — timeframe 3m")
                    last_alert_times[symbol] = time.time()

                # === ALERT 1H ===
                if abs(pct_1h) >= THRESH_1H and can_send_alert(symbol):
                    direction = "Rialzo" if pct_1h > 0 else "Crollo"
                    msg = (
                        f"🔔 ALERT {symbol} 1h\n"
                        f"📈 {direction} {pct_1h:+.2f}%\n"
                        f"🕒 {now.strftime('%Y-%m-%d %H:%M:%S UTC')}"
                    )
                    send_telegram_message(msg)
                    print(f"🚨 Alert generato per {symbol} — timeframe 1h")
                    last_alert_times[symbol] = time.time()

            except Exception as e:
                print(f"❌ Errore generale su {symbol}: {e}")

        time.sleep(10)

# === FLASK ===
@app.route("/")
def home():
    return "✅ Crypto monitor attivo"

if __name__ == "__main__":
    main_loop()
