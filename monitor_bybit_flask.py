import os
import time
import threading
from dotenv import load_dotenv
from flask import Flask
import requests
import ccxt
import numpy as np
from fastdtw import fastdtw

load_dotenv()

# ============ CONFIG ============

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_IDS = os.getenv("TELEGRAM_CHAT_ID", "").split(",")

FAST_TFS = os.getenv("FAST_TFS", "1m,3m").split(",")
SLOW_TFS = os.getenv("SLOW_TFS", "1h").split(",")

LOOP_DELAY = int(os.getenv("LOOP_DELAY", 10))  # scan ogni 10 minuti
MAX_CANDIDATES = int(os.getenv("MAX_CANDIDATES_PER_CYCLE", 1000))
COOLDOWN = int(os.getenv("COOLDOWN_SECONDS", 180))
HEARTBEAT_INTERVAL = int(os.getenv("HEARTBEAT_INTERVAL", 3600))

THRESH_1M = float(os.getenv("THRESH_1M", 7.5))
THRESH_3M = float(os.getenv("THRESH_3M", 11.5))
THRESH_1H = float(os.getenv("THRESH_1H", 17.0))

# Pattern detection threshold
PATTERN_CORR_THRESHOLD = 0.92
PATTERN_COS_THRESHOLD = 0.90

# ============ TELEGRAM ============

def send_telegram(msg: str):
    for chat_id in CHAT_IDS:
        if not chat_id.strip():
            continue
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
        requests.post(url, json={"chat_id": chat_id.strip(), "text": msg})


# ============ BYBIT CLIENT ============

exchange = ccxt.bybit({
    "enableRateLimit": True,
})

def fetch_ohlcv(symbol, tf, limit=200):
    try:
        return exchange.fetch_ohlcv(symbol, timeframe=tf, limit=limit)
    except Exception:
        return None


# ============ PERCENT CHANGE ALERTS ============

def compute_percent_change(candles):
    if len(candles) < 2:
        return 0
    return (candles[-1][4] - candles[-2][4]) / candles[-2][4] * 100


# ============ PATTERN SCANNER PRO ============

def normalize(arr):
    arr = np.array(arr)
    return (arr - arr.min()) / (arr.max() - arr.min() + 1e-9)

def similarity_score(arr1, arr2):
    a = normalize(arr1)
    b = normalize(arr2)

    # Pearson correlation
    corr = np.corrcoef(a, b)[0][1]

    # Cosine similarity
    cos = np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b))

    # DTW distance (optional)
    dtw_dist, _ = fastdtw(a, b)

    score = (corr * 0.45) + (cos * 0.45) + (1 / (1 + dtw_dist) * 0.10)
    return score, corr, cos, dtw_dist

def detect_ascending_pattern(candles):
    closes = [c[4] for c in candles]
    pattern = np.linspace(0.1, 1.0, len(closes))  # modello di ascensione

    score, corr, cos, dtw_d = similarity_score(closes, pattern)

    if corr > PATTERN_CORR_THRESHOLD and cos > PATTERN_COS_THRESHOLD:
        return True, score, corr, cos, dtw_d

    return False, score, corr, cos, dtw_d


# ============ MAIN SCANNER LOOP ============

last_alert = {}
last_heartbeat = time.time()

def scanner_loop():
    global last_heartbeat

    while True:
        try:
            markets = exchange.load_markets()
            usdt_pairs = [m for m in markets if m.endswith("USDT")][:MAX_CANDIDATES]

            for symbol in usdt_pairs:

                # ----- 1) Pattern detection (15m) -----
                candles_15 = fetch_ohlcv(symbol, "15m", 80)
                if candles_15:
                    detected, score, corr, cos, dtw_d = detect_ascending_pattern(candles_15)

                    if detected:
                        if symbol not in last_alert or time.time() - last_alert[symbol] > COOLDOWN:
                            msg = (
                                f"📈 *PATTERN ASCENSIONE RILEVATO*\n"
                                f"Coin: {symbol}\n"
                                f"Timeframe: 15m\n"
                                f"Corr: {corr:.3f} | Cos: {cos:.3f} | Score: {score:.3f}\n"
                                f"DTW: {dtw_d:.2f}"
                            )
                            send_telegram(msg)
                            last_alert[symbol] = time.time()

                # ----- 2) Percent-change alerts -----
                for tf in FAST_TFS:
                    candles = fetch_ohlcv(symbol, tf, 3)
                    if not candles:
                        continue
                    change = compute_percent_change(candles)

                    threshold = THRESH_1M if tf == "1m" else THRESH_3M

                    if abs(change) >= threshold:
                        if symbol not in last_alert or time.time() - last_alert[symbol] > COOLDOWN:
                            send_telegram(
                                f"⚡ Movimento su {symbol} ({tf})\n"
                                f"Variazione: {change:.2f}%"
                            )
                            last_alert[symbol] = time.time()

                # Slow TF
                for tf in SLOW_TFS:
                    candles = fetch_ohlcv(symbol, tf, 3)
                    if not candles:
                        continue
                    change = compute_percent_change(candles)

                    if abs(change) >= THRESH_1H:
                        if symbol not in last_alert or time.time() - last_alert[symbol] > COOLDOWN:
                            send_telegram(
                                f"⏳ Movimento su {symbol} ({tf})\n"
                                f"Variazione: {change:.2f}%"
                            )
                            last_alert[symbol] = time.time()

            # Heartbeat
            if time.time() - last_heartbeat > HEARTBEAT_INTERVAL:
                send_telegram("❤️ Bot attivo e funzionante")
                last_heartbeat = time.time()

        except Exception as e:
            send_telegram(f"❗ Errore scanner: {str(e)}")

        time.sleep(LOOP_DELAY * 60)  # SCAN OGNI X MINUTI


# ============ FLASK SERVER ============

app = Flask(__name__)

@app.route("/")
def home():
    return "Monitor Bybit Flask - Running"

# Thread dello scanner
threading.Thread(target=scanner_loop, daemon=True).start()

if __name__ == "__main__":
    port = int(os.getenv("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
