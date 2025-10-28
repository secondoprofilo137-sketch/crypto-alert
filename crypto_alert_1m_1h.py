import time
import requests
import datetime
import os
import json

# === CONFIGURAZIONE TELEGRAM ===
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

# === CONFIGURAZIONE ALLERTA ===
PRICE_CHANGE_THRESHOLD = 5   # variazione percentuale per segnalazione
CHECK_INTERVAL = 60          # controllo ogni 60 secondi
MAX_RETRIES = 5              # retry in caso di errore API

# === BYBIT API (derivatives) ===
BYBIT_URL = "https://api.bybit.com/v5/market/tickers?category=linear"

# === FILE DI STATO ===
STATE_FILE = "/data/state.json"

# === MEMORIA ===
last_prices = {}
last_alerts = {}

# === FUNZIONI DI STATO ===
def load_state():
    """Carica stato precedente da file JSON"""
    global last_prices, last_alerts
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f:
                data = json.load(f)
                last_prices = data.get("last_prices", {})
                last_alerts = data.get("last_alerts", {})
                print(f"✅ Stato caricato: {len(last_prices)} prezzi e {len(last_alerts)} alert memorizzati.")
        except Exception as e:
            print(f⚠️ Errore caricamento stato: {e}")

def save_state():
    """Salva stato corrente su file JSON"""
    try:
        with open(STATE_FILE, "w") as f:
            json.dump({"last_prices": last_prices, "last_alerts": last_alerts}, f)
    except Exception as e:
        print(f"⚠️ Errore salvataggio stato: {e}")


# === FUNZIONE TELEGRAM ===
def send_telegram_message(message: str):
    """Invia un messaggio Telegram"""
    if not TELEGRAM_TOKEN or not CHAT_ID:
        print("⚠️ Nessun token/chat Telegram configurato.")
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": message}

    try:
        r = requests.post(url, data=payload, timeout=10)
        if r.status_code != 200:
            print("❌ Errore invio Telegram:", r.text)
    except Exception as e:
        print("⚠️ Errore Telegram:", e)


# === FUNZIONE API BYBIT ===
def get_bybit_data():
    """Recupera tutti i dati delle coppie derivati da Bybit"""
    for attempt in range(MAX_RETRIES):
        try:
            response = requests.get(BYBIT_URL, timeout=15)
            if response.status_code == 429:
                print(f"⏳ Troppi accessi (429), attendo 10s e ritento ({attempt+1}/{MAX_RETRIES})...")
                time.sleep(10)
                continue
            response.raise_for_status()
            return response.json().get("result", {}).get("list", [])
        except Exception as e:
            print(f"⚠️ Errore connessione Bybit ({attempt+1}/{MAX_RETRIES}):", e)
            time.sleep(5)
    return []


# === FUNZIONE PRINCIPALE ===
def check_changes():
    """Controlla variazioni di prezzo su Bybit Derivatives ogni minuto e su 24h"""
    global last_prices, last_alerts

    data = get_bybit_data()
    if not data:
        print("⚠️ Nessun dato ricevuto da Bybit.")
        return

    now = datetime.datetime.now().strftime("%H:%M:%S")
    alerts_sent = 0

    for coin in data:
        try:
            symbol = coin["symbol"]
            last_price = float(coin.get("lastPrice", 0))
            prev_price_24h = float(coin.get("prevPrice24h", 0))
            if last_price == 0 or prev_price_24h == 0:
                continue

            # === 1️⃣ Variazione su 1 minuto ===
            prev_price_1m = last_prices.get(symbol)
            if prev_price_1m:
                change_1m = ((last_price - prev_price_1m) / prev_price_1m) * 100
                if abs(change_1m) >= PRICE_CHANGE_THRESHOLD:
                    if symbol not in last_alerts or abs((last_price - last_alerts[symbol]) / last_alerts[symbol]) > 0.01:
                        direction = "📈 AUMENTO" if change_1m > 0 else "📉 CALO"
                        msg = (
                            f"{direction} {symbol} (1m)\n"
                            f"Variazione: {change_1m:.2f}%\n"
                            f"Prezzo attuale: {last_price:.4f} USDT"
                        )
                        send_telegram_message(msg)
                        last_alerts[symbol] = last_price
                        alerts_sent += 1

            # === 2️⃣ Variazione su 24h ===
            change_24h = ((last_price - prev_price_24h) / prev_price_24h) * 100
            if abs(change_24h) >= PRICE_CHANGE_THRESHOLD:
                if symbol not in last_alerts or abs((last_price - last_alerts[symbol]) / last_alerts[symbol]) > 0.01:
                    direction = "📈 AUMENTO" if change_24h > 0 else "📉 CALO"
                    msg = (
                        f"{direction} {symbol} (24h)\n"
                        f"Variazione: {change_24h:.2f}%\n"
                        f"Prezzo attuale: {last_price:.4f} USDT"
                    )
                    send_telegram_message(msg)
                    last_alerts[symbol] = last_price
                    alerts_sent += 1

            last_prices[symbol] = last_price

        except Exception as e:
            print(f"⚠️ Errore su {coin.get('symbol')}: {e}")

    # Salva lo stato dopo ogni ciclo
    save_state()
    print(f"✅ [{now}] Controllo completato - {alerts_sent} nuovi alert inviati.")


# === LOOP PRINCIPALE ===
if __name__ == "__main__":
    print("🚀 Bot avviato e in esecuzione continua...")
    load_state()
    while True:
        try:
            check_changes()
        except Exception as e:
            print("⚠️ Errore nel ciclo principale:", e)
        time.sleep(CHECK_INTERVAL)
