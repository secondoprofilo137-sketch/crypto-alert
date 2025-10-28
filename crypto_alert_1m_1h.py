import time
import requests
import datetime
import os
import json

# === CONFIGURAZIONE TELEGRAM ===
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

# === CONFIGURAZIONE GIST (per salvataggio remoto) ===
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
GIST_ID = os.getenv("GIST_ID")

# === CONFIGURAZIONE ALLERTA ===
PRICE_CHANGE_THRESHOLD = 5  # variazione percentuale
CHECK_INTERVAL = 60  # ogni 60 secondi
MAX_RETRIES = 5  # numero massimo di retry in caso di errore

BYBIT_URL = "https://api.bybit.com/v5/market/tickers?category=linear"

# === Stato in memoria ===
last_prices = {}
last_alerts = {}


# ==================================================
# 🔹 FUNZIONI DI SUPPORTO GIST
# ==================================================
def load_state_from_gist():
    """Scarica lo stato (last_prices, last_alerts) da GitHub Gist."""
    global last_prices, last_alerts
    if not GITHUB_TOKEN or not GIST_ID:
        print("⚠️ Nessun token GitHub o Gist ID configurato. Uso stato locale in RAM.")
        return

    try:
        url = f"https://api.github.com/gists/{GIST_ID}"
        headers = {"Authorization": f"token {GITHUB_TOKEN}"}
        response = requests.get(url, headers=headers)
        response.raise_for_status()

        gist_data = response.json()
        content = gist_data["files"]["state.json"]["content"]
        data = json.loads(content)

        last_prices = data.get("last_prices", {})
        last_alerts = data.get("last_alerts", {})
        print("✅ Stato caricato da Gist con successo.")

    except Exception as e:
        print("⚠️ Errore nel caricamento da Gist:", e)


def save_state_to_gist():
    """Salva lo stato (last_prices, last_alerts) su GitHub Gist."""
    if not GITHUB_TOKEN or not GIST_ID:
        print("⚠️ Nessun token GitHub o Gist ID configurato. Stato non salvato.")
        return

    try:
        data = {
            "last_prices": last_prices,
            "last_alerts": last_alerts
        }
        payload = {
            "files": {
                "state.json": {"content": json.dumps(data, indent=2)}
            }
        }

        url = f"https://api.github.com/gists/{GIST_ID}"
        headers = {"Authorization": f"token {GITHUB_TOKEN}"}
        response = requests.patch(url, headers=headers, json=payload)
        response.raise_for_status()
        print("💾 Stato salvato su Gist con successo.")

    except Exception as e:
        print("⚠️ Errore nel salvataggio su Gist:", e)


# ==================================================
# 🔹 FUNZIONI PRINCIPALI
# ==================================================
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
    """Controlla variazioni di prezzo su 1m, 2m e 5m"""
    global last_prices, last_alerts

    data = get_bybit_data()
    now = datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%d %H:%M:%S UTC")

    for coin in data:
        symbol = coin.get("symbol")
        price = float(coin.get("lastPrice", 0))
        if not symbol or price == 0:
            continue

        if symbol not in last_prices:
            last_prices[symbol] = {
                "1m": {"price": price, "timestamp": time.time()},
                "2m": {"price": price, "timestamp": time.time()},
                "5m": {"price": price, "timestamp": time.time()}
            }
            continue

        alerts_to_check = [("1m", 60), ("2m", 120), ("5m", 300)]
        for label, period in alerts_to_check:
            prev_price = last_prices[symbol][label]["price"]
            prev_time = last_prices[symbol][label]["timestamp"]

            if time.time() - prev_time >= period:
                change = ((price - prev_price) / prev_price) * 100
                if abs(change) >= PRICE_CHANGE_THRESHOLD:
                    icons = {"1m": "🔥", "2m": "⚡", "5m": "🚀"}
                    icon = icons[label]
                    msg = f"{icon} {symbol} ha variazioni del {change:.2f}% negli ultimi {label}! ({now})"
                    print(msg)
                    send_telegram_message(msg)

                last_prices[symbol][label] = {"price": price, "timestamp": time.time()}

    save_state_to_gist()


# ==================================================
# 🔹 LOOP PRINCIPALE
# ==================================================
if __name__ == "__main__":
    print("🚀 Bot avviato (Bybit Derivatives) – monitoraggio 1m, 2m, 5m attivo...")
    load_state_from_gist()

    while True:
        try:
            check_changes()
        except Exception as e:
            print("⚠️ Errore nel ciclo principale:", e)
        time.sleep(CHECK_INTERVAL)
