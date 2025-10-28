def fetch_all_coins():
    print("📦 Carico lista CoinGecko (solo alla prima esecuzione)...")
    results = []
    page = 1
    max_retries = 3  # quante volte ritentare in caso di errore

    while True:
        params = {
            "vs_currency": FIAT,
            "order": "market_cap_desc",
            "per_page": 250,
            "page": page,
            "price_change_percentage": "1h"
        }

        for attempt in range(max_retries):
            try:
                r = requests.get(COINGECKO_URL, params=params, timeout=30)
                if r.status_code == 429:
                    wait_time = 10 + attempt * 5
                    print(f"⏳ Troppi accessi (429), attendo {wait_time}s e ritento...")
                    time.sleep(wait_time)
                    continue
                elif r.status_code != 200:
                    print(f"⚠️ Errore API pagina {page}: {r.status_code}")
                    time.sleep(5)
                    continue

                data = r.json()
                if not data:
                    print("✅ Fine elenco CoinGecko.")
                    save_cache(results)
                    print(f"✅ Cache salvata ({len(results)} monete totali).")
                    return results

                results.extend(data)
                print(f"📄 Pagina {page} scaricata ({len(results)} totali)...")

                # Pausa per non essere bloccati
                time.sleep(6)
                page += 1
                break

            except Exception as e:
                print(f"⚠️ Errore connessione (pagina {page}, tentativo {attempt+1}/{max_retries}): {e}")
                time.sleep(10)

        else:
            # Se tutti i retry falliscono, esci
            print(f"❌ Impossibile scaricare la pagina {page} dopo {max_retries} tentativi.")
            break

    save_cache(results)
    print(f"✅ Cache salvata ({len(results)} monete totali).")
    return results

