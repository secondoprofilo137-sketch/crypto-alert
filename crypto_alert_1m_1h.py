#!/usr/bin/env python3
# crypto_alert_1m_1h.py
# Full advanced crypto alert — Bybit linear derivatives
# 1m/2m/5m alerts, volume spike, RSI, EMA, breakout, suggestions,
# Gist persistence, Telegram debug, heartbeat, Flask keepalive.
# Use env vars for secrets (TELEGRAM_TOKEN, CHAT_ID, GITHUB_TOKEN, GIST_ID).

import os
import time
import json
import math
import requests
import datetime
import threading
import signal
from statistics import mean, pstdev
from flask import Flask
try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

# -------------------------
# CONFIG (env-driven)
# -------------------------
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
GIST_ID = os.getenv("GIST_ID")

PRICE_CHANGE_THRESHOLD = float(os.getenv("PRICE_CHANGE_THRESHOLD", 5.0))  # percent
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", 60))  # seconds
MAX_RETRIES = int(os.getenv("MAX_RETRIES", 5))
MAX_CANDIDATES_PER_CYCLE = int(os.getenv("MAX_CANDIDATES", 200))

VOLUME_SPIKE_FACTOR = float(os.getenv("VOLUME_SPIKE_FACTOR", 1.5))
RSI_PERIOD = int(os.getenv("RSI_PERIOD", 14))
EMA_PERIOD = int(os.getenv("EMA_PERIOD", 20))
BREAKOUT_LOOKBACK = int(os.getenv("BREAKOUT_LOOKBACK", 15))
BREAKOUT_BUFFER = float(os.getenv("BREAKOUT_BUFFER", 0.002))

HEARTBEAT_ENABLED = os.getenv("HEARTBEAT_ENABLED", "true").lower() in ("1", "true", "yes")
HEARTBEAT_INTERVAL_MIN = int(os.getenv("HEARTBEAT_INTERVAL_MIN", 10))  # minutes

BYBIT_TICKERS_URL = "https://api.bybit.com/v5/market/tickers?category=linear"
BYBIT_KLINE_URL_TPL = "https://api.bybit.com/v5/market/kline?symbol={symbol}&interval=1&category=linear&limit={limit}"
GIST_API = "https://api.github.com/gists"

# -------------------------
# State
# -------------------------
last_prices = {}    # symbol -> {'1m': {'price','ts'}, '2m': {...}, '5m': {...}}
last_alerts = {}    # symbol -> {alert_type: ts}
last_volumes = {}   # symbol -> {'rolling': [...], '1m': {'vol','ts'}}
kline_cache = {}    # symbol -> {'closes': [...], 'volumes': [...]}
uptime_start = time.time()

_state_lock = threading.Lock()

# -------------------------
# Flask keepalive
# -------------------------
app = Flask(__name__)

@app.route("/")
def home():
    now = datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
    return f"✅ Crypto Alert Bot — running ({now})"

@app.route("/test")
def test_alert():
    now = datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
    send_telegram_message(f"🧪 TEST ALERT - bot online ({now})")
    return "Test sent", 200

# -------------------------
# Gist persistence
# -------------------------
def load_state_from_gist():
    global last_prices, last_alerts, last_volumes, kline_cache
    if not GITHUB_TOKEN or not GIST_ID:
        print("⚠️ Gist not configured — using in-memory state.")
        return
    try:
        headers = {"Authorization": f"token {GITHUB_TOKEN}"}
        r = requests.get(f"{GIST_API}/{GIST_ID}", headers=headers, timeout=15)
        r.raise_for_status()
        gist = r.json()
        content = gist.get("files", {}).get("state.json", {}).get("content", "{}")
        data = json.loads(content)
        with _state_lock:
            last_prices = data.get("last_prices", {})
            last_alerts = data.get("last_alerts", {})
            last_volumes = data.get("last_volumes", {})
            kline_cache = data.get("kline_cache", {})
        print("✅ State loaded from Gist.")
    except Exception as e:
        print("⚠️ Failed to load state from Gist:", e)

def save_state_to_gist():
    if not GITHUB_TOKEN or not GIST_ID:
        return
    try:
        with _state_lock:
            payload = {
                "files": {
                    "state.json": {"content": json.dumps({
                        "last_prices": last_prices,
                        "last_alerts": last_alerts,
                        "last_volumes": last_volumes,
                        "kline_cache": kline_cache,
                        "uptime_start": uptime_start
                    }, indent=2)}
                }
            }
        headers = {"Authorization": f"token {GITHUB_TOKEN}"}
        r = requests.patch(f"{GIST_API}/{GIST_ID}", headers=headers, json=payload, timeout=15)
        r.raise_for_status()
        print("💾 State saved to Gist.")
    except Exception as e:
        print("⚠️ Failed to save state to Gist:", e)

# -------------------------
# Telegram (debug logging to Render + attempts)
# -------------------------
def send_telegram_message(message: str):
    """Invia messaggio su Telegram e logga esito (debug)."""
    if not TELEGRAM_TOKEN or not CHAT_ID:
        # fallback: print so we can see in logs if missing env
        print("⚠️ Telegram not configured (missing TELEGRAM_TOKEN/CHAT_ID). Message:\n", message)
        return
    now = datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": message, "parse_mode": "HTML"}
    try:
        r = requests.post(url, json=payload, timeout=12)
        # log minimal to console for Render logs
        print(f"[{now}] 📤 Telegram attempt ({len(message)} chars) -> status {r.status_code}")
        # log response text truncated to avoid huge logs
        resp_preview = (r.text[:300] + "...") if len(r.text) > 300 else r.text
        print(f"[{now}] 📬 Telegram resp: {resp_preview}")
        if r.status_code != 200:
            print(f"[{now}] ❌ Telegram send error: {r.status_code} - {r.text}")
    except Exception as e:
        print(f"[{now}] ⚠️ Telegram exception: {e}")

# -------------------------
# Indicators
# -------------------------
def compute_rsi(closes, period=14):
    if len(closes) < period + 1:
        return None
    gains, losses = [], []
    for i in range(1, len(closes)):
        change = closes[i] - closes[i-1]
        gains.append(max(change, 0))
        losses.append(max(-change, 0))
    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))

def compute_ema(closes, period=20):
    if not closes or len(closes) < 2:
        return None
    k = 2 / (period + 1)
    ema = closes[0]
    for price in closes[1:]:
        ema = price * k + (1 - k) * ema
    return ema

def compute_volatility_logreturns(closes):
    if len(closes) < 3:
        return 0.0
    lr = []
    for i in range(1, len(closes)):
        if closes[i-1] > 0:
            lr.append(math.log(closes[i] / closes[i-1]))
    if len(lr) < 2:
        return 0.0
    try:
        return pstdev(lr)
    except Exception:
        return 0.0

# -------------------------
# Bybit helpers
# -------------------------
def get_all_tickers():
    for attempt in range(MAX_RETRIES):
        try:
            r = requests.get(BYBIT_TICKERS_URL, timeout=20)
            if r.status_code == 429:
                wait = 5 + attempt * 5
                print(f"⏳ Bybit rate limit 429, waiting {wait}s (attempt {attempt+1})")
                time.sleep(wait)
                continue
            r.raise_for_status()
            return r.json().get("result", {}).get("list", [])
        except Exception as e:
            print("⚠️ get_all_tickers error:", e)
            time.sleep(3)
    return []

def fetch_klines_1m(symbol, limit=50):
    try:
        url = BYBIT_KLINE_URL_TPL.format(symbol=symbol, limit=limit)
        r = requests.get(url, timeout=15)
        r.raise_for_status()
        j = r.json()
        res = j.get("result") or j
        lists = None
        if isinstance(res, dict):
            lists = res.get("list") or res.get("data") or res.get("candle") or res.get("candles")
        if lists is None:
            lists = j.get("list") or j.get("data")
        if not lists:
            return None
        closes, volumes = [], []
        for entry in lists[-limit:]:
            if isinstance(entry, list) and len(entry) >= 6:
                closes.append(float(entry[4])); volumes.append(float(entry[5]))
            elif isinstance(entry, dict):
                close = entry.get("close") or entry.get("c")
                vol = entry.get("volume") or entry.get("v")
                if close is not None: closes.append(float(close))
                if vol is not None: volumes.append(float(vol))
        return {"closes": closes, "volumes": volumes}
    except Exception as e:
        print("⚠️ fetch_klines_1m error for", symbol, e)
        return None

def extract_ticker_volume(coin):
    for k in ("turnover", "volume", "volume_24h", "vol", "quoteVolume", "baseVolume"):
        v = coin.get(k)
        if v is not None:
            try:
                return float(v)
            except:
                pass
    return 0.0

# -------------------------
# Risk & interpretation
# -------------------------
VOL_TIER_THRESHOLDS = {"low": 0.002, "medium": 0.006, "high": 0.012}
RISK_MAPPING = {
    "low":     {"leverage": "x2", "target_pct": (0.8, 1.5),  "stop_pct": (-0.6, -1.0)},
    "medium":  {"leverage": "x3", "target_pct": (1.5, 3.0),  "stop_pct": (-1.0, -2.0)},
    "high":    {"leverage": "x4", "target_pct": (3.0, 6.0),  "stop_pct": (-2.0, -4.0)},
    "very":    {"leverage": "x6", "target_pct": (6.0, 12.0), "stop_pct": (-4.0, -8.0)},
}

def estimate_risk_profile(closes, volumes, last_close, recent_max_pct_diff):
    volatility = compute_volatility_logreturns(closes[-25:]) if len(closes) >= 3 else 0.0
    if volatility < VOL_TIER_THRESHOLDS["low"]:
        tier = "low"
    elif volatility < VOL_TIER_THRESHOLDS["medium"]:
        tier = "medium"
    elif volatility < VOL_TIER_THRESHOLDS["high"]:
        tier = "high"
    else:
        tier = "very"
    baseline = mean(volumes[-6:-1]) if len(volumes) >= 6 else (mean(volumes[:-1]) if len(volumes) > 1 else (volumes[-1] if volumes else 1.0))
    vol_ratio = (volumes[-1] / baseline) if baseline and baseline > 0 else 1.0
    mapping = RISK_MAPPING.get(tier, RISK_MAPPING["medium"])
    return {"tier": tier, "volatility": volatility, "vol_ratio": vol_ratio, "leverage": mapping["leverage"], "target_range_pct": mapping["target_pct"], "stop_range_pct": mapping["stop_pct"], "recent_max_pct_diff": recent_max_pct_diff}

def generate_interpretation_and_suggestion(symbol, change_pct=None, rsi=None, vol_ratio=None, ema_relation=None, breakout=False, risk=None):
    parts = []
    if change_pct is not None:
        if abs(change_pct) >= 7:
            parts.append("movimento molto forte")
        elif abs(change_pct) >= 3:
            parts.append("movimento marcato")
        elif abs(change_pct) >= 1:
            parts.append("leggero movimento")
    if breakout: parts.append("rottura di livello recente")
    if rsi is not None:
        if rsi >= 70: parts.append("RSI alto (possibile ipercomprato)")
        elif rsi <= 30: parts.append("RSI basso (possibile ipervenduto)")
    if vol_ratio is not None:
        if vol_ratio >= 2.0: parts.append(f"volume molto aumentato ({vol_ratio:.1f}×)")
        elif vol_ratio >= 1.5: parts.append(f"volume aumentato ({vol_ratio:.1f}×)")
    if ema_relation == "above": parts.append("prezzo sopra EMA20 (supporto di trend)")
    elif ema_relation == "below": parts.append("prezzo sotto EMA20 (pressione ribassista)")
    interp = "Nessuna evidenza tecnica significativa al momento." if not parts else "; ".join(parts) + "."
    suggestion = ""
    if risk:
        tgt_low, tgt_high = risk["target_range_pct"]
        stop_low, stop_high = risk["stop_range_pct"]
        suggestion = (f"Categoria Volatilità: {risk['tier'].upper()} | Leva suggerita: {risk['leverage']}\n"
                      f"Target indicativi: +{tgt_low:.1f}% → +{tgt_high:.1f}% | Stop indicativi: {stop_low:.1f}% → {stop_high:.1f}%\n"
                      f"Vol ratio: {risk['vol_ratio']:.2f}× | Volatility: {risk['volatility']:.4f}")
    disclaimer = "\n⚠️ Nota: i suggerimenti sono informativi e non costituiscono consulenza finanziaria."
    return interp, suggestion + disclaimer

# -------------------------
# Core check_changes (1m,2m,5m)
# -------------------------
def check_changes():
    global last_prices, last_volumes, kline_cache, last_alerts
    tickers = get_all_tickers()
    now_str = datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
    if not tickers:
        print(f"[{now_str}] No tickers retrieved.")
        return

    candidates_for_klines = set()

    for coin in tickers:
        symbol = coin.get("symbol")
        if not symbol:
            continue
        # price detection
        last_price_val = 0.0
        for key in ("lastPrice", "last_price", "price"):
            if coin.get(key) is not None:
                try:
                    last_price_val = float(coin.get(key))
                    break
                except:
                    pass
        if last_price_val <= 0:
            continue

        # init
        if symbol not in last_prices:
            last_prices[symbol] = {
                "1m": {"price": last_price_val, "ts": time.time()},
                "2m": {"price": last_price_val, "ts": time.time()},
                "5m": {"price": last_price_val, "ts": time.time()},
            }
            vol = extract_ticker_volume(coin)
            last_volumes.setdefault(symbol, {"1m": {"vol": vol, "ts": time.time()}, "rolling": [vol]})
            continue

        # check windows
        checks = [("1m", 60), ("2m", 120), ("5m", 300)]
        for label, period in checks:
            prev_entry = last_prices[symbol].get(label, {})
            prev_price = prev_entry.get("price", last_price_val)
            prev_ts = prev_entry.get("ts", time.time())
            if time.time() - prev_ts >= period:
                try:
                    change_pct = ((last_price_val - prev_price) / prev_price) * 100.0 if prev_price else 0.0
                except ZeroDivisionError:
                    change_pct = 0.0
                if abs(change_pct) >= PRICE_CHANGE_THRESHOLD:
                    candidates_for_klines.add(symbol)
                    last_alerts.setdefault(symbol, {})
                    last_alerts[symbol][f"price_{label}"] = time.time()
                # update reference
                last_prices[symbol][label] = {"price": last_price_val, "ts": time.time()}

        # rolling vol
        ticker_vol = extract_ticker_volume(coin)
        if ticker_vol:
            lv = last_volumes.setdefault(symbol, {"1m": {"vol": ticker_vol, "ts": time.time()}, "rolling": []})
            rolling = lv["rolling"]
            rolling.append(ticker_vol)
            if len(rolling) > 100: rolling.pop(0)
            baseline = mean(rolling) if rolling else ticker_vol
            if baseline > 0 and ticker_vol / baseline >= VOLUME_SPIKE_FACTOR:
                candidates_for_klines.add(symbol)

    # limit candidates
    candidates = list(candidates_for_klines)[:MAX_CANDIDATES_PER_CYCLE]

    for symbol in candidates:
        try:
            k = fetch_klines_1m(symbol, limit=60)
            if not k: continue
            closes = k.get("closes", [])
            volumes = k.get("volumes", [])
            if not closes: continue
            kline_cache[symbol] = {"closes": closes[-60:], "volumes": volumes[-60:]}

            rsi = compute_rsi(closes[-(RSI_PERIOD+1):], RSI_PERIOD) if len(closes) >= RSI_PERIOD+1 else None
            ema20 = compute_ema(closes[-EMA_PERIOD:], EMA_PERIOD) if len(closes) >= EMA_PERIOD else None
            last_vol = volumes[-1] if volumes else 0.0
            baseline_vol = mean(volumes[-6:-1]) if len(volumes) >= 6 else (mean(volumes[:-1]) if len(volumes) > 1 else last_vol)
            vol_ratio = (last_vol / baseline_vol) if baseline_vol and baseline_vol > 0 else 1.0
            vol_spike = baseline_vol and baseline_vol > 0 and last_vol / baseline_vol >= VOLUME_SPIKE_FACTOR

            lookback = min(BREAKOUT_LOOKBACK, len(closes)-1)
            breakout_flag = False; breakout_type = None; recent_max = None; recent_min = None
            if lookback >= 1:
                recent_max = max(closes[-(lookback+1):-1]); recent_min = min(closes[-(lookback+1):-1])
                last_close = closes[-1]
                if last_close > recent_max * (1 + BREAKOUT_BUFFER):
                    breakout_flag = True; breakout_type = "upper"
                elif last_close < recent_min * (1 - BREAKOUT_BUFFER):
                    breakout_flag = True; breakout_type = "lower"

            ema_relation = None
            if ema20 is not None:
                if closes[-1] > ema20 * 1.002: ema_relation = "above"
                elif closes[-1] < ema20 * 0.998: ema_relation = "below"

            prev_1m_price = last_prices.get(symbol, {}).get("1m", {}).get("price")
            change_pct_1m = None
            if prev_1m_price:
                try:
                    change_pct_1m = ((closes[-1] - prev_1m_price) / prev_1m_price) * 100.0
                except:
                    change_pct_1m = None

            recent_max_pct_diff = None
            if breakout_flag and breakout_type == "upper" and recent_max:
                recent_max_pct_diff = (closes[-1] - recent_max) / recent_max if recent_max else 0.0
            elif breakout_flag and breakout_type == "lower" and recent_min:
                recent_max_pct_diff = (recent_min - closes[-1]) / recent_min if recent_min else 0.0

            risk = estimate_risk_profile(closes, volumes, closes[-1], recent_max_pct_diff)
            interp_text, suggestion_text = generate_interpretation_and_suggestion(
                symbol, change_pct=change_pct_1m, rsi=rsi, vol_ratio=vol_ratio, ema_relation=ema_relation, breakout=breakout_flag, risk=risk
            )

            # choose alerts to send with cooldowns
            alerts_to_send = []
            pa_price_1m = last_alerts.get(symbol, {}).get("price_1m")
            pa_price_2m = last_alerts.get(symbol, {}).get("price_2m")
            recent_threshold = CHECK_INTERVAL * 3
            if pa_price_1m and time.time() - pa_price_1m <= recent_threshold:
                alerts_to_send.append(("price_1m", pa_price_1m)); last_alerts[symbol].pop("price_1m", None)
            if pa_price_2m and time.time() - pa_price_2m <= recent_threshold:
                alerts_to_send.append(("price_2m", pa_price_2m)); last_alerts[symbol].pop("price_2m", None)

            if vol_spike:
                last_t = last_alerts.setdefault(symbol, {}).get("vol_spike")
                if not last_t or time.time() - last_t > (CHECK_INTERVAL * 5):
                    alerts_to_send.append(("vol_spike", time.time())); last_alerts[symbol]["vol_spike"] = time.time()

            if rsi is not None:
                if rsi >= 70:
                    last_t = last_alerts.setdefault(symbol, {}).get("rsi_high")
                    if not last_t or time.time() - last_t > (CHECK_INTERVAL * 10):
                        alerts_to_send.append(("rsi_high", time.time())); last_alerts[symbol]["rsi_high"] = time.time()
                elif rsi <= 30:
                    last_t = last_alerts.setdefault(symbol, {}).get("rsi_low")
                    if not last_t or time.time() - last_t > (CHECK_INTERVAL * 10):
                        alerts_to_send.append(("rsi_low", time.time())); last_alerts[symbol]["rsi_low"] = time.time()

            if breakout_flag:
                last_t = last_alerts.setdefault(symbol, {}).get("breakout")
                if not last_t or time.time() - last_t > (CHECK_INTERVAL * 5):
                    alerts_to_send.append((f"breakout_{breakout_type}", time.time())); last_alerts[symbol]["breakout"] = time.time()

            if ema_relation == "above":
                last_t = last_alerts.setdefault(symbol, {}).get("ema_up")
                if not last_t or time.time() - last_t > (CHECK_INTERVAL * 15):
                    alerts_to_send.append(("ema_up", time.time())); last_alerts[symbol]["ema_up"] = time.time()
            elif ema_relation == "below":
                last_t = last_alerts.setdefault(symbol, {}).get("ema_down")
                if not last_t or time.time() - last_t > (CHECK_INTERVAL * 15):
                    alerts_to_send.append(("ema_down", time.time())); last_alerts[symbol]["ema_down"] = time.time()

            # send alerts
            for alert_type, ts in alerts_to_send:
                lines = []
                if alert_type.startswith("price_"):
                    icon = "🔥" if alert_type == "price_1m" else "⚡"
                    title = f"{icon} {symbol} price alert"
                    if change_pct_1m is not None:
                        title += f" {change_pct_1m:+.2f}% (ult. 1m)"
                    lines.append(title)
                elif alert_type == "vol_spike":
                    lines.append(f"💥 {symbol} VOLUME SPIKE — vol last / avg ≈ {vol_ratio:.2f}×")
                elif alert_type == "rsi_high":
                    lines.append(f"🔴 {symbol} RSI {rsi:.0f} → ipercomprato")
                elif alert_type == "rsi_low":
                    lines.append(f"🟢 {symbol} RSI {rsi:.0f} → ipervenduto")
                elif alert_type.startswith("breakout"):
                    direction = alert_type.split("_")[1] if "_" in alert_type else "upper"
                    emoji = "🚀" if direction == "upper" else "🔻"
                    lines.append(f"{emoji} {symbol} BREAKOUT {direction.upper()} (lookback {BREAKOUT_LOOKBACK}m)")
                elif alert_type == "ema_up":
                    lines.append(f"📈 {symbol} sopra EMA{EMA_PERIOD}")
                elif alert_type == "ema_down":
                    lines.append(f"📉 {symbol} sotto EMA{EMA_PERIOD}")
                else:
                    lines.append(f"ℹ️ {symbol} alert {alert_type}")

                metrics = []
                metrics.append(f"Prezzo: {closes[-1]:.6f}")
                if change_pct_1m is not None:
                    metrics.append(f"Δ1m: {change_pct_1m:+.2f}%")
                if rsi is not None:
                    metrics.append(f"RSI: {rsi:.0f}")
                metrics.append(f"Vol ratio: {vol_ratio:.2f}×")
                if ema20 is not None:
                    metrics.append(f"EMA{EMA_PERIOD}: {ema20:.6f}")
                lines.append(" | ".join(metrics))

                interp_txt, suggestion_txt = generate_interpretation_and_suggestion(
                    symbol, change_pct=change_pct_1m, rsi=rsi, vol_ratio=vol_ratio, ema_relation=ema_relation, breakout=breakout_flag, risk=risk
                )
                lines.append(interp_txt)
                lines.append(suggestion_txt)
                lines.append(f"[{now_str}]")
                message = "\n".join(lines)
                # send and log
                send_telegram_message(message)

            # update rolling volumes
            lv = last_volumes.setdefault(symbol, {"1m": {"vol": last_vol, "ts": time.time()}, "rolling": []})
            lv["rolling"].append(last_vol)
            if len(lv["rolling"]) > 100: lv["rolling"].pop(0)
            lv["1m"] = {"vol": last_vol, "ts": time.time()}

        except Exception as e:
            print("⚠️ Candidate processing error for", symbol, e)

    # persist state
    try:
        save_state_to_gist()
    except Exception:
        pass

# -------------------------
# Monitor loop (resilient)
# -------------------------
def monitor_loop():
    backoff = 1
    while True:
        try:
            check_changes()
            backoff = 1
            time.sleep(CHECK_INTERVAL)
        except Exception as e:
            print("⚠️ Unhandled error in monitor loop:", e)
            try:
                last_alerts["_last_error"] = {"ts": time.time(), "err": str(e)}
                save_state_to_gist()
            except:
                pass
            time.sleep(min(60, backoff))
            backoff = min(300, backoff * 2)

# -------------------------
# Heartbeat worker
# -------------------------
def heartbeat_worker():
    while True:
        if not HEARTBEAT_ENABLED:
            return
        try:
            uptime_min = round((time.time() - uptime_start) / 60, 1)
            now = datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
            send_telegram_message(f"💓 Heartbeat — Bot attivo da {uptime_min} min ({now})")
        except Exception as e:
            print("⚠️ Heartbeat error:", e)
        time.sleep(max(1, HEARTBEAT_INTERVAL_MIN) * 60)

# -------------------------
# Graceful shutdown
# -------------------------
def handle_sigterm(signum, frame):
    print("🔔 SIGTERM received, saving state and exiting...")
    try:
        save_state_to_gist()
    except:
        pass
    os._exit(0)

signal.signal(signal.SIGTERM, handle_sigterm)
signal.signal(signal.SIGINT, handle_sigterm)

# -------------------------
# Entrypoint
# -------------------------
if __name__ == "__main__":
    print("🚀 Advanced Crypto Alert Bot starting (60s loop, full logic)")
    load_state_from_gist()
    # start Flask keepalive
    threading.Thread(target=lambda: app.run(host="0.0.0.0", port=int(os.getenv("PORT", 10000))), daemon=True).start()
    # start heartbeat
    if HEARTBEAT_ENABLED:
        threading.Thread(target=heartbeat_worker, daemon=True).start()
    # start monitor loop
    monitor_loop()
