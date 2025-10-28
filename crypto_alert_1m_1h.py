import time
import requests
import datetime
import os
import sys

# === CONFIGURAZIONE TELEGRAM ===
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

# === CONFIGURAZIONE ALLERTA ===
PRICE_CHANGE_THRESHOLD = 5  # variazione percentuale
CHECK_INTERVAL = 60  # ogni 60 secondi
MAX_RETRIES = 5  # per sicurezza, in caso di errore temporaneo

# === BYBIT API (derivatives) ===
BYBIT_URL = "https://api.bybit.com/v5/market/tickers?category=linear"


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


def check_changes():
    """Controlla variazioni di prezzo su Bybit"""
    data = get_bybit_data()
    if not data:
        print("⚠️ Nessun dato ricevuto da Bybit.")
        return

    now = datetime.datetime.now().strftime("%H:%M:%S")
    print(f"🔍 Controllo alle {now} - {len(data)} coppie trovate")

    for coin in data:
        try:
            last_price = float(coin.get("lastPrice", 0))
            prev_price = float(coin.get("prevPrice24h", 0))
            if prev_price == 0:
                continue

            change_percent = ((last_price - prev_price) / prev_price) * 100

            if abs(change_percent) >= PRICE_CHANGE_THRESHOLD:
                message = (
                    f"🚨 {coin['symbol']} ha variazione del {change_percent:.2f}% nelle ultime 24h\n"
                    f"Prezzo attuale: {last_price}\n"
                    f"Prezzo 24h fa: {prev_price}"
                )
                print(message)
                send_telegram_message(message)

        except Exception as e:
            print(f"Errore elaborando {coin.get('symbol')}: {e}")


if __name__ == "__main__":
    print("🚀 Bot avviato e in esecuzione continua...")
    while True:
        try:
            check_changes()  # funzione principale che controlla le variazioni
        except Exception as e:
            print("⚠️ Errore nel ciclo principale:", e)
        time.sleep(CHECK_INTERVAL)
