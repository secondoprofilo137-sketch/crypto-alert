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
    """Controlla variazioni di
