#!/usr/bin/env python3
# monitor_bybit_flask.py
# Crypto alert — Bybit monitor (Flask + background)
# - Alert percentuali: 1m, 3m, 1h
# - Wyckoff-like report ogni 4h (max 10 coin) — unica notifica
# - Market Cycle report daily/weekly (una notifica al giorno)
# - Multi-chat Telegram, cooldown per alert/report
# Lingua: Italiano (messaggi)

from __future__ import annotations
import os
import time
import threading
import math
from statistics import mean, pstdev
from datetime import datetime, timezone
import requests
import ccxt
from flask import Flask

# -----------------------
# CONFIG (ENV o default)
# -----------------------
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_IDS = [c.strip() for c in os.getenv("TELEGRAM_CHAT_ID", "").split(",") if c.strip()]
PORT = int(os.getenv("PORT", "10000"))

# timeframes / thresholds
FAST_TFS = ["1m", "3m"]
SLOW_TFS = ["1h"]
LOOP_DELAY = int(os.getenv("LOOP_DELAY", "10"))               # secondi tra i cicli
HEARTBEAT_INTERVAL_SEC = int(os.getenv("HEARTBEAT_INTERVAL_SEC", "3600"))

# soglie (personalizzabili via env)
THRESH_1M = float(os.getenv("THRESH_1M", "7.5"))
THRESH_3M = float(os.getenv("THRESH_3M", "11.5"))
THRESH_1H = float(os.getenv("THRESH_1H", "17.0"))
THRESHOLDS = {"1m": THRESH_1M, "3m": THRESH_3M, "1h": THRESH_1H}

# cooldowns (seconds)
COOLDOWN_PRICE_ALERT = int(os.getenv("COOLDOWN_PRICE_ALERT", str(2 * 60 * 60)))  # 2 ore per chiave, come richiesto
COOLDOWN_REPORT_4H = int(os.getenv("COOLDOWN_REPORT_4H", str(4 * 3600)))
COOLDOWN_REPORT_DAILY = int(os.getenv("COOLDOWN_REPORT_DAILY", str(24 * 3600)))

# report limits
REPORT_TOP_N = int(os.getenv("REPORT_TOP_N", "10"))
REPORT_SCORE_MIN = int(os.getenv("REPORT_SCORE_MIN", "70"))   # soglia 70 come richiesto

# minimal volume/vol spike settings (kept simple)
VOLUME_SPIKE_MULTIPLIER = float(os.getenv("VOLUME_SPIKE_MULTIPLIER", "100.0"))
VOLUME_MIN_USD = float(os.getenv("VOLUME_MIN_USD", "1000000.0"))

# exchange + state
exchange = ccxt.bybit({"options": {"defaultType": "swap"}})
app = Flask(__name__)

last_prices: dict = {}               # key = symbol_tf -> last price seen
last_price_alert_ts: dict = {}       # key = symbol_tf -> ts last alert
last_report_4h_ts = 0
last_report_daily_ts = 0
last_heartbeat_ts = 0

# -----------------------
# UTIL: Telegram
# -----------------------
def send_telegram(text: str):
    """Invia lo stesso messaggio a tutti i CHAT_IDS (Markdown)."""
    if not TELEGRAM_TOKEN or not CHAT_IDS:
        print("❌ Telegram non configurato (TOKEN/CHAT_ID mancanti)")
        print("Messaggio (debug):\n", text)
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    success = True
    for cid in CHAT_IDS:
        try:
            payload = {"chat_id": cid, "text": text, "parse_mode": "Markdown", "disable_web_page_preview": True}
            r = requests.post(url, data=payload, timeout=10)
            if r.status_code != 200:
                print(f"⚠️ Errore Telegram per {cid}: {r.status_code} {r.text}")
                success = False
        except Exception as e:
            print(f"❌ Exception invio Telegram a {cid}: {e}")
            success = False
    return success

# -----------------------
# INDICATORS (semplici, efficienti)
# -----------------------
def compute_ema(prices, period):
    if not prices or len(prices) < 2:
        return None
    k = 2.0 / (period + 1)
    ema = prices[0]
    for p in prices[1:]:
        ema = p * k + ema * (1 - k)
    return ema

def compute_rsi(prices, period=14):
    if len(prices) < period + 1:
        return None
    gains = []
    losses = []
    for i in range(1, len(prices)):
        d = prices[i] - prices[i - 1]
        gains.append(max(d, 0))
        losses.append(max(-d, 0))
    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))

def compute_volatility_logreturns(closes):
    if len(closes) < 3:
        return 0.0
    lr = []
    for i in range(1, len(closes)):
        if closes[i-1] > 0:
            lr.append(math.log(closes[i] / closes[i-1]))
    if len(lr) < 2:
        return 0.0
    return pstdev(lr)

# breakout simple
def breakout_reason(closes, lookback=20):
    if len(closes) < lookback + 1:
        return None
    window = closes[-(lookback+1):-1]
    last = closes[-1]
    hi = max(window)
    lo = min(window)
    if last > hi:
        return f"Breakout sopra massimo {hi:.6f}"
    if last < lo:
        return f"Rotto minimo {lo:.6f}"
    return None

# -----------------------
# BYBIT helpers
# -----------------------
def safe_fetch_ohlcv(symbol, timeframe, limit=60):
    """Ritorna (closes, volumes) o (None, None) su errore."""
    try:
        ohlcv = exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
        closes = [c[4] for c in ohlcv]
        vols = [c[5] for c in ohlcv]
        return closes, vols
    except Exception as e:
        print(f"⚠️ fetch error {symbol} {timeframe}: {e}")
        return None, None

def get_bybit_derivative_symbols():
    try:
        markets = exchange.load_markets()
        syms = []
        for s, m in markets.items():
            if m.get("type") == "swap" or s.endswith(":USDT") or "/USDT" in s:
                syms.append(s)
        return sorted(set(syms))
    except Exception as e:
        print("⚠️ load_markets error:", e)
        return []

# -----------------------
# SCORING (semplice) usato dal report Wyckoff-ish
# -----------------------
def score_for_report(closes, vols, variation):
    """Return score 0..100 (heuristic)"""
    score = 0
    # EMA diff
    ema10 = compute_ema(closes, 10)
    ema30 = compute_ema(closes, 30)
    if ema10 and ema30:
        diff_pct = ((ema10 - ema30) / ema30) * 100 if ema30 else 0
        if diff_pct > 0.8:
            score += 25
        elif diff_pct < -0.8:
            score -= 10
    # breakout
    br = breakout_reason(closes, lookback=20)
    if br and "Breakout sopra" in br:
        score += 20
    # rsi neutral bias
    rsi = compute_rsi(closes, 14)
    if rsi is not None:
        if rsi < 35:
            score += 10
        elif rsi > 70:
            score -= 10
    # volatility & volume ratio
    vol_ratio = 1.0
    if vols and len(vols) >= 6:
        base = mean(vols[-6:-1])
        last = vols[-1]
        if base and base > 0:
            vol_ratio = last / base
            if vol_ratio >= VOLUME_SPIKE_MULTIPLIER:
                score += 25
            elif vol_ratio >= 5:
                score += 5
    # magnitude
    mag = min(25, abs(variation))
    if variation > 0:
        score += mag * 0.6
    else:
        score += mag * 0.3
    # normalize 0..100
    raw = max(-100, min(100, score))
    normalized = int((raw + 100) / 2)
    return normalized

# -----------------------
# REPORTS
# -----------------------
def build_wyckoff_report(symbols: list[str], timeframe: str):
    """Genera max REPORT_TOP_N coin con score >= REPORT_SCORE_MIN.
       Invio una singola notifica con i top N."""
    items = []
    for s in symbols:
        closes, vols = safe_fetch_ohlcv(s, timeframe, 120)
        if not closes:
            continue
        price = closes[-1]
        prev = closes[-2] if len(closes) > 1 else price
        variation = ((price - prev) / prev) * 100 if prev else 0.0
        score = score_for_report(closes, vols, variation)
        if score >= REPORT_SCORE_MIN:
            reason = breakout_reason(closes, lookback=20) or ""
            items.append((score, s, price, variation, reason))
    # ordina per score desc e prendi top N
    items.sort(reverse=True, key=lambda x: x[0])
    top = items[:REPORT_TOP_N]
    if not top:
        return None
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    lines = [f"WYCKOFF REPORT {timeframe} — {now}"]
    for score, s, price, var, reason in top:
        note = f" — {reason}" if reason else ""
        # messaggio compatto: nome in bold
        lines.append(f"*{s}* | Score *{score}/100* | Prezzo {price:.6f} | Var {var:+.2f}%{note}")
    return "\n".join(lines)

def build_market_cycle_report(symbols: list[str]):
    """Analisi daily/weekly: prende le coin con comportamento migliore su daily/weekly.
       Torna messaggio (o None)."""
    items = []
    for s in symbols:
        # daily closes
        d_closes, d_vols = safe_fetch_ohlcv(s, "1d", 90)
        w_closes, w_vols = safe_fetch_ohlcv(s, "1w", 60)
        if not d_closes or not w_closes:
            continue
        price = d_closes[-1]
        # sample score: combinazione breakout weekly + daily momentum
        weekly_break = breakout_reason(w_closes, lookback=8)
        daily_break = breakout_reason(d_closes, lookback=20)
        score = 0
        if weekly_break and "Breakout sopra" in weekly_break:
            score += 40
        if daily_break and "Breakout sopra" in daily_break:
            score += 40
        # vol trend (recent avg vs older avg)
        if len(d_vols) >= 20:
            recent = mean(d_vols[-5:])
            past = mean(d_vols[-20:-5]) if len(d_vols) >= 20 else recent
            if past and recent / past > 1.5:
                score += 20
        if score >= REPORT_SCORE_MIN:
            items.append((score, s, price))
    items.sort(reverse=True, key=lambda x: x[0])
    top = items[:REPORT_TOP_N]
    if not top:
        return None
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    lines = [f"MARKET CYCLE REPORT (daily/weekly) — {now}"]
    for score, s, price in top:
        lines.append(f"*{s}* | Score *{score}/100* | Prezzo {price:.6f}")
    return "\n".join(lines)

# -----------------------
# MONITOR LOOP (core: alerts 1m/3m/1h)
# -----------------------
@app.route("/")
def home():
    return "✅ Crypto Alert Bot (Bybit) — running"

@app.route("/test")
def test():
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    send_telegram(f"🧪 TEST — bot online ({now})")
    return "Test inviato", 200

def monitor_loop():
    global last_report_4h_ts, last_report_daily_ts, last_heartbeat_ts
    symbols = get_bybit_derivative_symbols()
    print(f"✅ Simboli derivati trovati: {len(symbols)}")
    initialized = False

    while True:
        cycle_start = time.time()
        for s in symbols[:1000]:   # limit per ciclo per non saturare richieste (configurabile)
            for tf in FAST_TFS + SLOW_TFS:
                closes, vols = safe_fetch_ohlcv(s, tf, 60)
                if not closes:
                    continue
                price = closes[-1]
                key = f"{s}_{tf}"

                # se primo ciclo inizializziamo il prezzo e non segnaliamo
                if key not in last_prices:
                    last_prices[key] = price
                    continue

                prev = last_prices.get(key, price)
                try:
                    variation = ((price - prev) / prev) * 100 if prev else 0.0
                except Exception:
                    variation = 0.0

                threshold = THRESHOLDS.get(tf, 5.0)
                send_alert = abs(variation) >= threshold

                # checkpoint volume spike (semplice)
                vol_ratio = 1.0
                if vols and len(vols) >= 6:
                    base = mean(vols[-6:-1])
                    last_vol = vols[-1]
                    if base and base > 0:
                        vol_ratio = last_vol / base

                # approx USD volume if USDT pair
                usd_vol = 0.0
                try:
                    if "/USDT" in s or s.endswith(":USDT"):
                        usd_vol = price * (vols[-1] if vols else 0)
                except Exception:
                    usd_vol = 0.0

                # volume alert rules: only if vol_ratio big AND usd_vol above threshold
                volume_trigger = vol_ratio >= VOLUME_SPIKE_MULTIPLIER and usd_vol >= VOLUME_MIN_USD

                now_ts = time.time()
                if send_alert or volume_trigger:
                    # cooldown per key
                    last_ts = last_price_alert_ts.get(key, 0)
                    if now_ts - last_ts < COOLDOWN_PRICE_ALERT:
                        # skip due to cooldown
                        pass
                    else:
                        # build message
                        trend = "📈 Rialzo" if variation > 0 else "📉 Crollo"
                        reason = breakout_reason(closes, lookback=20) or ""
                        vol_info = f" | Vol ratio {vol_ratio:.1f}×" if vol_ratio != 1.0 else ""
                        usd_info = f" | Vol candle USD {usd_vol:.0f}" if usd_vol else ""
                        nowtxt = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

                        # nome in bold
                        msg = (
                            f"🔔 ALERT -> {'🔥' if tf in FAST_TFS else '⚡'} *{s}* — variazione {variation:+.2f}% (ref {tf})\n"
                            f"💰 Prezzo attuale: {price:.6f}\n"
                            f"{trend} rilevato.\n"
                        )
                        if volume_trigger:
                            msg += f"🚨 *Volume SPIKE* rilevato — {vol_ratio:.1f}× sopra la media{usd_info}\n"
                        if reason:
                            msg += f"🔎 {reason}\n"
                        msg += f"📊 Volatilità appross.: (vol ratio {vol_ratio:.2f}){vol_info}\n"
                        msg += f"[{nowtxt}]"

                        send_telegram(msg)
                        last_price_alert_ts[key] = now_ts

                # aggiorna sempre last price (misure future vs ultimo)
                last_prices[key] = price

        initialized = True

        # 4H Wyckoff-like report (una sola notifica ogni COOLDOWN_REPORT_4H)
        tnow = time.time()
        if tnow - last_report_4h_ts >= COOLDOWN_REPORT_4H:
            threading.Thread(target=lambda: send_wyckoff_report_thread(symbols, "4h"), daemon=True).start()
            last_report_4h_ts = tnow

        # daily market cycle report (una volta al giorno)
        if tnow - last_report_daily_ts >= COOLDOWN_REPORT_DAILY:
            threading.Thread(target=lambda: send_market_cycle_report_thread(symbols), daemon=True).start()
            last_report_daily_ts = tnow

        # heartbeat
        if tnow - last_heartbeat_ts >= HEARTBEAT_INTERVAL_SEC:
            send_telegram(f"💓 Heartbeat — bot attivo ({datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}) | Simboli monitorati: {len(symbols)}")
            last_heartbeat_ts = tnow

        # pacing
        elapsed = time.time() - cycle_start
        if elapsed < LOOP_DELAY:
            time.sleep(LOOP_DELAY - elapsed)

# wrapper threads for reports (so monitor loop not blocked)
def send_wyckoff_report_thread(symbols, timeframe):
    msg = build_wyckoff_report(symbols, timeframe)
    if msg:
        send_telegram(msg)

def send_market_cycle_report_thread(symbols):
    msg = build_market_cycle_report(symbols)
    if msg:
        send_telegram(msg)

# -----------------------
# Entrypoint
# -----------------------
if __name__ == "__main__":
    print("🚀 Avvio monitor Bybit — alert 1m/3m/1h + Wyckoff 4h + MarketCycle daily")
    # start flask in background (Render expects a web service)
    threading.Thread(target=lambda: app.run(host="0.0.0.0", port=PORT), daemon=True).start()
    # start monitor
    threading.Thread(target=monitor_loop, daemon=True).start()
    # keep main alive
    while True:
        time.sleep(3600)
