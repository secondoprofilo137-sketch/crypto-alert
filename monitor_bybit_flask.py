import time
import threading
import requests
import ccxt
from flask import Flask
from dotenv import load_dotenv
import os

load_dotenv()

# -------------------------------------------------------------
# 🔥 CONFIG
# -------------------------------------------------------------
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_IDS = os.getenv("TELEGRAM_CHAT_IDS", "").split(",")
PERCENT_CHANGE_THRESHOLD = float(os.getenv("PERCENT_CHANGE_THRESHOLD", "3"))
TIMEFRAME = os.getenv("TIMEFRAME", "1m")
LOOKBACK_MINUTES = int(os.getenv("LOOKBACK_MINUTES", "3"))
LOOP_DELAY = int(os.getenv("LOOP_DELAY", "10"))

# ASCENSORE SAFE MODE
SAFE_MIN_SPIKE_PCT = 2.0
SAFE_MIN_VOLUME_USD = 150000
SAFE_MIN_LIQUIDITY_DEPTH = 80000
SAFE_DELTA_MIN = 20000
SAFE_COOLDOWN = 600
last_alert = {}

BYBIT_URL = "https://api.bybit.com"

app = Flask(__name__)

exchange = ccxt.bybit({"enableRateLimit": True})

# -------------------------------------------------------------
# 🔥 TELEGRAM
# -------------------------------------------------------------
def send_telegram(text: str):
    for chat_id in TELEGRAM_CHAT_IDS:
        chat_id = chat_id.strip()
        if not chat_id:
            continue
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        try:
            requests.post(url, data={"chat_id": chat_id, "text": text})
        except Exception as e:
            print(f"Telegram error → {e}")

# -------------------------------------------------------------
# 🔥 FUNZIONI ASCENSORE SAFE
# -------------------------------------------------------------
def get_klines(symbol):
    r = requests.get(f"{BYBIT_URL}/v5/market/kline", params={
        "category":"linear",
        "symbol":symbol,
        "interval":"1",
        "limit":2
    })
    return r.json()

def get_orderbook(symbol):
    r = requests.get(f"{BYBIT_URL}/v5/market/orderbook", params={
        "category":"linear",
        "symbol":symbol,
        "limit":50
    })
    return r.json()

def get_tickers():
    r = requests.get(f"{BYBIT_URL}/v5/market/tickers", params={"category":"linear"})
    return r.json()

def get_delta(symbol):
    r = requests.get(f"{BYBIT_URL}/v5/market/trade", params={
        "category":"linear",
        "symbol":symbol,
        "limit":100
    })
    data = r.json()["result"]["list"]
    buy = sum(float(t["price"])*float(t["size"]) for t in data if t["side"]=="Buy")
    sell = sum(float(t["price"])*float(t["size"]) for t in data if t["side"]=="Sell")
    return buy, sell

def get_liquidity(orderbook):
    bids = orderbook["result"]["b"]
    asks = orderbook["result"]["a"]
    best_bid = float(bids[0][0])
    best_ask = float(asks[0][0])
    mid = (best_bid + best_ask) / 2
    depth_buy = sum(float(p[0])*float(p[1]) for p in bids if (float(p[0]) >= mid*0.997))
    depth_sell = sum(float(p[0])*float(p[1]) for p in asks if (float(p[0]) <= mid*1.003))
    return depth_buy + depth_sell

def safe_filter(symbol):
    try:
        kl = get_klines(symbol)
        k = kl["result"]["list"]

        prev = float(k[1]["close"])
        last = float(k[0]["close"])
        volume = float(k[0]["volume"]) * last
        spike = ((last-prev)/prev)*100

        if abs(spike) < SAFE_MIN_SPIKE_PCT:
            return None
        if volume < SAFE_MIN_VOLUME_USD:
            return None

        ob = get_orderbook(symbol)
        depth = get_liquidity(ob)
        if depth < SAFE_MIN_LIQUIDITY_DEPTH:
            return None

        buy, sell = get_delta(symbol)
        delta = buy - sell

        if spike > 0 and delta < SAFE_DELTA_MIN:
            return None
        if spike < 0 and -delta < SAFE_DELTA_MIN:
            return None

        now = time.time()
        if symbol in last_alert and now - last_alert[symbol] < SAFE_COOLDOWN:
            return None
        last_alert[symbol] = now

        direction = "LONG" if spike > 0 else "SHORT"
        return f"""
🚀 ASCENSORE SAFE MODE
Coppia: {symbol}
Direzione: {direction}
Spike: {spike:.2f}% in 60s
Volume: ${volume:,.0f}
Delta: {delta:,.0f}
Liquidità (depth 0.3%): ${depth:,.0f}
"""
    except:
        return None

def run_ascensore_safe():
    print("ASCENSORE SAFE MODE AVVIATO...")
    while True:
        try:
            tickers = get_tickers()["result"]["list"]
            for t in tickers:
                sym = t["symbol"]
                res = safe_filter(sym)
                if res:
                    print(res)
                    send_telegram(res)
            time.sleep(3)
        except Exception as e:
            print("Errore ASCENSORE:", e)
            time.sleep(5)

# -------------------------------------------------------------
# 🔥 FUNZIONI VARIAZIONE %
# -------------------------------------------------------------
def get_perpetual_symbols():
    markets = exchange.load_markets()
    return [m["symbol"] for s,m in markets.items() if m.get("type")=="swap" and m.get("settle")=="USDT"]

def safe_fetch(symbol):
    try:
        data = exchange.fetch_ohlcv(symbol, TIMEFRAME, limit=LOOKBACK_MINUTES+1)
        return data
    except Exception as e:
        print(f"Fetch error {symbol}: {e}")
        return None

def compute_change_percent(ohlcv):
    prices = [c[4] for c in ohlcv]
    old = prices[0]
    new = prices[-1]
    return (new-old)/old*100

def price_monitor_loop():
    print("🚀 Starting Price % Monitor…")
    send_telegram("📡 *Bot Price% avviato*")

    symbols = get_perpetual_symbols()
    print(f"Trovati {len(symbols)} perpetual.")

    while True:
        try:
            for symbol in symbols:
                ohlcv = safe_fetch(symbol)
                if not ohlcv or len(ohlcv)<2:
                    continue
                change = compute_change_percent(ohlcv)
                if abs(change) >= PERCENT_CHANGE_THRESHOLD:
                    direction = "🚀 UP" if change>0 else "📉 DOWN"
                    msg = f"{direction} {symbol} → {change:.2f}% in {LOOKBACK_MINUTES}m"
                    print(msg)
                    send_telegram(msg)
            time.sleep(LOOP_DELAY)
        except Exception as e:
            print(f"Loop error → {e}")
            time.sleep(5)

# -------------------------------------------------------------
# 🔥 FLASK
# -------------------------------------------------------------
@app.route("/")
def home():
    return "Crypto Price% & Ascensore SAFE Monitor running OK."

def start_background_threads():
    t1 = threading.Thread(target=price_monitor_loop, daemon=True)
    t2 = threading.Thread(target=run_ascensore_safe, daemon=True)
    t1.start()
    t2.start()

if __name__ == "__main__":
    start_background_threads()
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 10000)))
