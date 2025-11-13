#!/usr/bin/env python3
# monitor_bybit_flask.py
# Bybit monitor — alerts 1m/3m/1h + report 4h/daily/weekly with optional GROQ AI summaries
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
# CONFIG (env or defaults)
# -----------------------
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_IDS = [cid.strip() for cid in os.getenv("TELEGRAM_CHAT_ID", "").split(",") if cid.strip()]
PORT = int(os.getenv("PORT", 10000))

FAST_TFS = os.getenv("FAST_TFS", "1m,3m").split(",")
SLOW_TFS = os.getenv("SLOW_TFS", "1h").split(",")
LOOP_DELAY = int(os.getenv("LOOP_DELAY", 10))
HEARTBEAT_INTERVAL_SEC = int(os.getenv("HEARTBEAT_INTERVAL_SEC", 3600))
MAX_CANDIDATES_PER_CYCLE = int(os.getenv("MAX_CANDIDATES_PER_CYCLE", 1000))
COOLDOWN_SECONDS = int(os.getenv("COOLDOWN_SECONDS", 7200))  # 2 ore default

# thresholds per timeframe (percent)
THRESH_1M = float(os.getenv("THRESH_1M", 7.5))
THRESH_3M = float(os.getenv("THRESH_3M", 11.5))
THRESH_1H = float(os.getenv("THRESH_1H", 17.0))
THRESHOLDS = {"1m": THRESH_1M, "3m": THRESH_3M, "1h": THRESH_1H}

# volume spike (kept but disabled by default unless useful)
VOLUME_SPIKE_MULTIPLIER = float(os.getenv("VOLUME_SPIKE_MULTIPLIER", 100.0))
VOLUME_MIN_USD = float(os.getenv("VOLUME_MIN_USD", 1000000.0))
VOLUME_SPIKE_MIN_VOLUME = float(os.getenv("VOLUME_SPIKE_MIN_VOLUME", 500000.0))
VOLUME_COOLDOWN_SECONDS = int(os.getenv("VOLUME_COOLDOWN_SECONDS", 300))

# Reports
REPORT_4H_INTERVAL_SEC = int(os.getenv("REPORT_4H_INTERVAL_SEC", 4 * 3600))
REPORT_DAILY_INTERVAL_SEC = int(os.getenv("REPORT_DAILY_INTERVAL_SEC", 24 * 3600))
REPORT_WEEKLY_INTERVAL_SEC = int(os.getenv("REPORT_WEEKLY_INTERVAL_SEC", 7 * 24 * 3600))
REPORT_TOP_N = int(os.getenv("REPORT_TOP_N", 10))
REPORT_SCORE_MIN = int(os.getenv("REPORT_SCORE_MIN", 70))  # soglia richiesta per essere inclusi

# AI integration for reports
ENABLE_GROQ_AI = os.getenv("ENABLE_GROQ_AI", "true").lower() in ("1", "true", "yes")
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "").strip()
GROQ_API_URL = os.getenv("GROQ_API_URL", "https://api.groq.ai/v1/generate")  # configurabile

# -----------------------
# Exchange + state
# -----------------------
exchange = ccxt.bybit({"options": {"defaultType": "swap"}})
last_prices: dict = {}
last_alert_time: dict = {}
last_volume_alert_time: dict = {}
app = Flask(__name__)

# -----------------------
# Helpers: Telegram
# -----------------------
def send_telegram(text: str):
    """Invia testo a tutti i chat id (Markdown)."""
    if not TELEGRAM_TOKEN or not CHAT_IDS:
        print("❌ Telegram non configurato (token/chat mancanti). Messaggio di debug:")
        print(text)
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    for cid in CHAT_IDS:
        payload = {
            "chat_id": cid,
            "text": text,
            "disable_web_page_preview": True,
            "parse_mode": "Markdown"
        }
        try:
            r = requests.post(url, data=payload, timeout=10)
            if r.status_code != 200:
                print(f"⚠️ Telegram error {r.status_code} for {cid}: {r.text}")
        except Exception as e:
            print(f"⚠️ Telegram exception for {cid}: {e}")

# -----------------------
# Indicators (light)
# -----------------------
def compute_ema(prices, period):
    if not prices or len(prices) < 2:
        return None
    k = 2 / (period + 1)
    ema = prices[0]
    for p in prices[1:]:
        ema = p * k + ema * (1 - k)
    return ema

def compute_rsi(prices, period=14):
    if len(prices) < period + 1:
        return None
    gains, losses = [], []
    for i in range(1, len(prices)):
        d = prices[i] - prices[i-1]
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
    return pstdev(lr) if len(lr) >= 2 else 0.0

# -----------------------
# Scoring / breakout
# -----------------------
def breakout_score(closes, lookback=20):
    if len(closes) < lookback + 1:
        return 0, None
    recent = closes[-(lookback+1):-1]
    last = closes[-1]
    hi = max(recent)
    lo = min(recent)
    if last > hi:
        return 20, f"Breakout sopra max {hi:.6f}"
    if last < lo:
        return -10, f"Rotto min {lo:.6f}"
    return 0, None

def estimate_volatility_tier(closes, volumes):
    vol = compute_volatility_logreturns(closes[-30:]) if len(closes) >= 30 else compute_volatility_logreturns(closes)
    if vol < 0.002:
        tier = "LOW"
    elif vol < 0.006:
        tier = "MEDIUM"
    elif vol < 0.012:
        tier = "HIGH"
    else:
        tier = "VERY HIGH"
    base = mean(volumes[-6:-1]) if len(volumes) >= 6 else (mean(volumes) if volumes else 1.0)
    ratio = volumes[-1] / base if base else 1.0
    return tier, ratio

def score_signals(prices, volumes, variation):
    score = 0
    reasons = []
    # EMA10 vs EMA30
    ema10 = compute_ema(prices, 10)
    ema30 = compute_ema(prices, 30)
    if ema10 and ema30:
        diff_pct = ((ema10 - ema30) / ema30) * 100 if ema30 else 0
        if diff_pct > 0.8:
            score += 20; reasons.append(f"EMA10>EMA30 ({diff_pct:.2f}%)")
        elif diff_pct < -0.8:
            score -= 15; reasons.append(f"EMA10<EMA30 ({diff_pct:.2f}%)")
    # RSI
    rsi = compute_rsi(prices)
    if rsi is not None:
        if rsi < 35:
            score += 10; reasons.append(f"RSI {rsi:.1f} (oversold)")
        elif rsi > 70:
            score -= 15; reasons.append(f"RSI {rsi:.1f} (overbought)")
    # breakout
    bs, br = breakout_score(prices, lookback=20)
    score += bs
    if br:
        reasons.append(br)
    # variation magnitude weight
    mag = min(25, abs(variation))
    score += mag * 0.6 if variation > 0 else -mag * 0.4
    # volume ratio boost
    _, vol_ratio = estimate_volatility_tier(prices, volumes)
    if vol_ratio >= VOLUME_SPIKE_MULTIPLIER:
        score += 25; reasons.append(f"Vol spike {vol_ratio:.1f}×")
    elif vol_ratio >= 5:
        score += 5; reasons.append(f"Vol ratio {vol_ratio:.1f}×")
    raw = max(-100, min(100, score))
    normalized = int((raw + 100) / 2)  # map -100..100 -> 0..100
    return normalized, reasons

# -----------------------
# Bybit helpers
# -----------------------
def safe_fetch_ohlcv(symbol, timeframe, limit=120):
    try:
        ohlcv = exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
        closes = [c[4] for c in ohlcv]
        vols = [c[5] for c in ohlcv]
        return closes, vols
    except Exception as e:
        print(f"⚠️ fetch {symbol} {timeframe}: {e}")
        return None, None

def get_bybit_derivative_symbols():
    try:
        markets = exchange.load_markets()
        syms = []
        for s, meta in markets.items():
            # prefer explicit type check for derivatives
            if meta.get("type") == "swap" or "/USDT" in s or s.endswith(":USDT"):
                syms.append(s)
        return sorted(set(syms))
    except Exception as e:
        print("⚠️ load_markets:", e)
        return []

# -----------------------
# Volume spike detector
# -----------------------
def detect_volume_spike(volumes, multiplier=VOLUME_SPIKE_MULTIPLIER):
    if not volumes or len(volumes) < 6:
        return False, 1.0
    avg_prev = mean(volumes[-6:-1])
    last = volumes[-1]
    if avg_prev <= 0:
        return False, 1.0
    ratio = last / avg_prev
    return ratio >= multiplier, ratio

# -----------------------
# AI helper (for reports only)
# -----------------------
def generate_ai_summary(entries: list[str], timeframe: str) -> str:
    """
    entries: list of textual lines (score reasons, price, var)
    timeframe: '4h'/'daily'/'weekly'
    """
    if not ENABLE_GROQ_AI or not GROQ_API_KEY:
        # fallback: short heuristic summary
        topk = min(5, len(entries))
        summary = f"Mini-analisi ({timeframe}): top {topk} coin selezionate dal report. Concentrati su breakout e compressioni."
        return summary
    # build prompt
    prompt = (
        f"Sei un analista crypto breve. Ricevi {len(entries)} righe con coin, score e motivazioni.\n"
        f"Fornisci per il timeframe {timeframe} una mini-analisi (2-3 righe) e 1 breve 'fase operativa' (es. 'accumula', 'attendi rottura', 'vendi parzialmente').\n\n"
        "Dati:\n" + "\n".join(entries) + "\n\nRisposta:\n"
    )
    try:
        headers = {"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"}
        payload = {"prompt": prompt, "max_tokens": 220}
        r = requests.post(GROQ_API_URL, json=payload, headers=headers, timeout=12)
        if r.status_code == 200:
            data = r.json()
            # best-effort: accept text field or choices[0].text
            text = data.get("text") or (data.get("choices") and data["choices"][0].get("text")) or str(data)
            return text.strip()
        else:
            print("⚠️ GROQ API error:", r.status_code, r.text)
            return "Mini-analisi non disponibile (errore AI)."
    except Exception as e:
        print("⚠️ GROQ request failed:", e)
        return "Mini-analisi non disponibile (eccezione AI)."

# -----------------------
# Reports (wyckoff-like)
# -----------------------
def build_report_for_timeframe(symbols: list[str], tf: str, tag: str):
    results = []
    for s in symbols:
        closes, vols = safe_fetch_ohlcv(s, tf, 120)
        if not closes:
            continue
        price = closes[-1]
        prev = closes[-2] if len(closes) > 1 else price
        variation = ((price - prev) / prev) * 100 if prev else 0.0
        score, reasons = score_signals(closes, vols, variation)
        if score >= REPORT_SCORE_MIN:
            vt, vr = estimate_volatility_tier(closes, vols)
            note = ""
            # simple compression heuristic
            e10 = compute_ema(closes, 10)
            e30 = compute_ema(closes, 30)
            if e10 and e30:
                diffpct = abs((e10 - e30) / e30) * 100 if e30 else 0
                if vt == "LOW" and diffpct < 0.5:
                    note = " — mercato in compressione, preparati per il breakout"
            reasons_text = "; ".join(reasons) if reasons else "-"
            line = f"*{s}* | Score *{score}/100* | Prezzo {price:.6f} | Var {variation:+.2f}%{note} ▪ {reasons_text}"
            results.append((score, line))
    # sort by score desc and take top N
    results.sort(key=lambda x: x[0], reverse=True)
    top_lines = [r[1] for r in results[:REPORT_TOP_N]]
    if not top_lines:
        return None
    # AI summary for report
    ai_summary = generate_ai_summary(top_lines, tf)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    header = f"WYCKOFF REPORT {tag} — top {min(len(top_lines), REPORT_TOP_N)} (score ≥ {REPORT_SCORE_MIN}) — {now}\n"
    body = "\n".join(top_lines)
    message = header + body + "\n\n" + "*Mini-analisi AI:*\n" + ai_summary
    return message

# -----------------------
# Flask endpoints
# -----------------------
@app.route("/")
def home():
    return "✅ Crypto Alert Bot (Bybit) — running"

@app.route("/test")
def test():
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    send_telegram(f"🧪 TEST ALERT — bot online ({now})")
    return "Test sent", 200

# -----------------------
# Monitor loop (alerts only: 1m/3m/1h)
# -----------------------
def monitor_loop():
    symbols = get_bybit_derivative_symbols()
    print(f"✅ Simboli derivatives trovati: {len(symbols)}")
    last_hb = 0
    last_r4 = 0
    last_daily = 0
    last_weekly = 0
    first = True

    while True:
        cycle_start = time.time()
        for s in symbols[:MAX_CANDIDATES_PER_CYCLE]:
            for tf in FAST_TFS + SLOW_TFS:
                closes, vols = safe_fetch_ohlcv(s, tf, 120)
                if not closes:
                    continue
                price = closes[-1]
                key = f"{s}_{tf}"
                if first:
                    last_prices[key] = price
                    continue
                prev = last_prices.get(key, price)
                try:
                    variation = ((price - prev) / prev) * 100 if prev else 0.0
                except Exception:
                    variation = 0.0
                threshold = THRESHOLDS.get(tf, 5.0)
                alert_price_move = abs(variation) >= threshold

                # volume spike evaluation (optional)
                vol_spike, vol_ratio = detect_volume_spike(vols, multiplier=VOLUME_SPIKE_MULTIPLIER)
                usd_vol = 0.0
                if "/USDT" in s or s.endswith(":USDT"):
                    try:
                        usd_vol = price * vols[-1]
                    except Exception:
                        usd_vol = 0.0
                alert_volume = vol_spike and (usd_vol >= VOLUME_MIN_USD or vols[-1] >= VOLUME_SPIKE_MIN_VOLUME)

                now_t = time.time()
                if alert_volume:
                    last_vol_ts = last_volume_alert_time.get(s, 0)
                    if now_t - last_vol_ts < VOLUME_COOLDOWN_SECONDS:
                        alert_volume = False

                # combine alerts: price move OR volume spike => send
                if alert_price_move or alert_volume:
                    # cooldown per symbol+tf
                    last_ts = last_alert_time.get(key, 0)
                    if now_t - last_ts < COOLDOWN_SECONDS:
                        # skip due cooldown
                        pass
                    else:
                        # build message
                        score, reasons = score_signals(closes, vols, variation)
                        vt, vr = estimate_volatility_tier(closes, vols)
                        trend = "📈 Rialzo" if variation > 0 else "📉 Crollo"
                        nowtxt = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
                        text = (
                            f"🔔 ALERT -> {'🔥' if tf in FAST_TFS else '⚡'} *{s}* — variazione {variation:+.2f}% (ref {tf})\n"
                            f"💰 Prezzo attuale: {price:.6f}\n"
                        )
                        if alert_volume:
                            text += f"🚨 *Volume SPIKE* — {vol_ratio:.1f}× sopra la media | Volume candle: {vols[-1]:.0f} (~{usd_vol:.0f} USD)\n"
                        text += (
                            f"{trend} rilevato — attenzione al trend.\n"
                            f"📊 Volatilità: {vt} | Vol ratio: {vr:.2f}×\n"
                            f"🔎 Score: *{score}/100*\n"
                        )
                        if reasons:
                            text += "▪ " + "; ".join(reasons) + "\n"
                        text += f"[{nowtxt}]"
                        send_telegram(text)
                        last_alert_time[key] = now_t
                        if alert_volume:
                            last_volume_alert_time[s] = now_t
                # update last price always
                last_prices[key] = price
        first = False

        # Heartbeat
        tnow = time.time()
        if tnow - last_hb >= HEARTBEAT_INTERVAL_SEC:
            send_telegram(f"💓 Heartbeat — bot attivo ({datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}) | Simboli: {len(symbols)}")
            last_hb = tnow

        # Reports: 4h, daily, weekly (run as separate threads)
        if tnow - last_r4 >= REPORT_4H_INTERVAL_SEC:
            threading.Thread(target=lambda: send_report_thread(symbols, "4h", "4H"), daemon=True).start()
            last_r4 = tnow
        if tnow - last_daily >= REPORT_DAILY_INTERVAL_SEC:
            threading.Thread(target=lambda: send_report_thread(symbols, "1d", "DAILY"), daemon=True).start()
            last_daily = tnow
        if tnow - last_weekly >= REPORT_WEEKLY_INTERVAL_SEC:
            threading.Thread(target=lambda: send_report_thread(symbols, "1w", "WEEKLY"), daemon=True).start()
            last_weekly = tnow

        # sleep respecting loop delay
        elapsed = time.time() - cycle_start
        if elapsed < LOOP_DELAY:
            time.sleep(LOOP_DELAY - elapsed)

def send_report_thread(symbols, timeframe, tag):
    msg = build_report_for_timeframe(symbols, timeframe, tag)
    if msg:
        # prefix Wyckoff word as requested
        send_telegram(f"Wyckoff —\n{msg}")

# -----------------------
# Entrypoint
# -----------------------
if __name__ == "__main__":
    print(f"🚀 Avvio monitor Bybit — TF {FAST_TFS + SLOW_TFS}, thresholds {THRESHOLDS}, loop {LOOP_DELAY}s")
    # run flask (keepalive) in background
    threading.Thread(target=lambda: app.run(host="0.0.0.0", port=PORT), daemon=True).start()
    # run monitor
    threading.Thread(target=monitor_loop, daemon=True).start()
    # keep alive
    while True:
        time.sleep(3600)
