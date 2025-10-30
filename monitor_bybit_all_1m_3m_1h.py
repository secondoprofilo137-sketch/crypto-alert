import ccxt
import time
import requests
from datetime import datetime, timedelta, timezone
import os

# === CONFIGURAZIONE BASE ===
API_URL = "https://api.telegram.org/bot{}/sendMessage"
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# === PARAMETRI DI ANALISI ===
TIMEFRAMES_FAST = ["1m", "3m"]  # Analisi veloce
TIMEFRAMES_SLOW = ["1h"]        # Analisi più lenta
FAST_THRESHOLD = 5.0            # Variazione percentuale per alert rapido
SLOW_THRESHOLD = 3.0            # Per 1h
LOOP_DELAY = 10                 # Secondi tra i cicli veloci
HEARTBEAT_INTERVAL = 360        # Ogni 360 cicli (~1 ora)

exchange = ccxt.bybit({"options": {"defaultType": "swap"}})
previous_prices = {}
cycle_count = 0


def send_telegram(message):
    """Invia un messaggio Telegram."""
    if not BOT_TOKEN or not CHAT_ID:
        print("❌ Errore Telegram: Token o Chat ID mancanti")
        return
    try:
        url = API_URL.format(BOT_TOKEN)
        requests.post(url, data={"chat_id": CHAT_ID, "text": message, "parse_mode": "Markdown"})
    except Exception as e:
        print(f"❌ Errore Telegram: {e}")


def fetch_tickers():
    """Scarica tutti i ticker USDT dal mercato derivati Bybit."""
    markets = exchange.load_markets()
    return [s for s in markets if s.endswith("/USDT:USDT")]


def fetch_price(symbol, timeframe):
    """Scarica l’ultimo prezzo per simbolo/timeframe."""
    try:
        ohlcv = exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=2)
        return ohlcv[-1][4]
    except Exception as e:
        print(f"Errore {symbol} ({timeframe}): {e}")
        return None


def analyze_variation(symbol, timeframe, threshold):
    """Calcola la variazione di prezzo e genera eventuale alert."""
    global previous_prices
    price = fetch_price(symbol, timeframe)
    if price is None:
        return

    key = f"{symbol}_{timeframe}"
    old_price = previous_prices.get(key, price)
    variation = ((price - old_price) / old_price) * 100

    # Se variazione supera soglia -> alert
    if abs(variation) >= threshold:
        direction = "📈" if variation > 0 else "📉"
        emoji = "🔥" if timeframe in ["1m", "3m"] else "⚡"
        trend = "rialzo" if variation > 0 else "crollo"
        msg = (
            f"🔔 ALERT -> {emoji} *{symbol}* — variazione {variation:+.2f}% (ref {timeframe})\n"
            f"Prezzo attuale: {price:.6f}\n"
            f"{direction} Movimento di {trend} — attenzione al trend di mercato.\n"
            f"[{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}]"
        )
        print(msg)
        send_telegram(msg)

    previous_prices[key] = price


def send_heartbeat():
    """Invia messaggio orario di stato."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    msg = (
        f"🟢 Bot attivo e operativo\n"
        f"Ultimo heartbeat: {now}\n"
        f"Simboli analizzati: {len(all_symbols)}\n"
        f"Timeframes monitorati: {', '.join(TIMEFRAMES_FAST + TIMEFRAMES_SLOW)}"
    )
    print(msg)
    send_telegram(msg)


# === LOOP PRINCIPALE ===
if __name__ == "__main__":
    print("🚀 Bybit Derivatives Monitor — 1m, 3m, 1h — Avvio in corso…")
    all_symbols = fetch_tickers()
    print(f"✅ Simboli trovati: {len(all_symbols)}")

    while True:
        cycle_count += 1
        for symbol in all_symbols:
            for tf in TIMEFRAMES_FAST:
                analyze_variation(symbol, tf, FAST_THRESHOLD)

            # Esegui l'analisi 1h ogni ora
            if cycle_count % HEARTBEAT_INTERVAL == 0:
                for tf in TIMEFRAMES_SLOW:
                    analyze_variation(symbol, tf, SLOW_THRESHOLD)

        # Heartbeat orario
        if cycle_count % HEARTBEAT_INTERVAL == 0:
            send_heartbeat()

        print(f"[{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}] Ciclo completato — simboli: {len(all_symbols)}")
        time.sleep(LOOP_DELAY)
