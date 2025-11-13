#!/usr/bin/env python3
# monitor_bybit_flask.py
# Bybit monitor — percent alerts (1m/3m/1h) + Wyckoff reports + GPT explanations + charts
from __future__ import annotations
import os, time, threading, math, io, tempfile
from statistics import mean, pstdev
from datetime import datetime, timezone
import requests, ccxt
from flask import Flask

# matplotlib for charts
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

# ----------------------- CONFIG -----------------------
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_IDS = [cid.strip() for cid in os.getenv("TELEGRAM_CHAT_ID", "").split(",") if cid.strip()]
PORT = int(os.getenv("PORT", 10000))

FAST_TFS = os.getenv("ANALYSIS_TFS", "1m,3m,1h").split(",")
LOOP_DELAY = int(os.getenv("LOOP_DELAY", 10))

THRESHOLDS = {
    "1m": float(os.getenv("THRESH_1M", 7.5)),
    "3m": float(os.getenv("THRESH_3M", 11.5)),
    "1h": float(os.getenv("THRESH_1H", 17.0)),
}

COOLDOWN_SECONDS = int(os.getenv("COOLDOWN_SECONDS", 7200))

VOLUME_SPIKE_MULTIPLIER = float(os.getenv("VOLUME_SPIKE_MULTIPLIER", 100.0))
VOLUME_SPIKE_MIN_VOLUME = float(os.getenv("VOLUME_SPIKE_MIN_VOLUME", 500000.0))
VOLUME_MIN_USD = float(os.getenv("VOLUME_MIN_USD", 1000000.0))

REPORT_4H_INTERVAL_SEC = int(os.getenv("REPORT_4H_INTERVAL_SEC", 4 * 3600))
REPORT_DAILY_INTERVAL_SEC = int(os.getenv("REPORT_DAILY_INTERVAL_SEC", 24 * 3600))
REPORT_WEEKLY_INTERVAL_SEC = int(os.getenv("REPORT_WEEKLY_INTERVAL_SEC", 7 * 24 * 3600))
REPORT_MAX_ITEMS = int(os.getenv("REPORT_MAX_ITEMS", 10))
REPORT_SCORE_MIN = int(os.getenv("REPORT_SCORE_MIN", 70))

LOOKBACK_CANDLES = int(os.getenv("LOOKBACK_CANDLES", 50))
VOL_ACCUMULATION_RATIO = float(os.getenv("VOL_ACCUMULATION_RATIO", 1.5))

# GPT / OpenAI
ENABLE_GPT = os.getenv("ENABLE_GPT", "false").lower() in ("1","true","yes")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
GPT_MAX_TOKENS = int(os.getenv("GPT_MAX_TOKENS", 250))
GPT_TEMPERATURE = float(os.getenv("GPT_TEMPERATURE", 0.3))

# charts
ENABLE_CHARTS = os.getenv("ENABLE_CHARTS", "true").lower() in ("1","true","yes")
CHART_CANDLES = int(os.getenv("CHART_CANDLES", 200))

VOL_TIER_THRESHOLDS = {"low": 0.002, "medium": 0.006, "high": 0.012}

exchange = ccxt.bybit({"options": {"defaultType": "swap"}})
last_prices = {}
last_alert_time = {}
last_volume_alert_time = {}
app = Flask(__name__)

# ----------------------- TELEGRAM helpers -----------------------
def send_telegram_text(text: str):
    if not TELEGRAM_TOKEN or not CHAT_IDS:
        print("❌ Telegram non configurato (debug):\n", text)
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    for cid in CHAT_IDS:
        payload = {"chat_id": cid, "text": text, "disable_web_page_preview": True, "parse_mode": "Markdown"}
        try:
            r = requests.post(url, data=payload, timeout=15)
            if r.status_code != 200:
                print(f"⚠️ Telegram error {r.status_code} for {cid}: {r.text}")
        except Exception as e:
            print("⚠️ Telegram exception:", e)

def send_telegram_photo(image_bytes: bytes, caption: str = ""):
    if not TELEGRAM_TOKEN or not CHAT_IDS:
        print("❌ Telegram not configured (photo debug)")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto"
    for cid in CHAT_IDS:
        try:
            files = {"photo": ("chart.png", image_bytes)}
            data = {"chat_id": cid, "caption": caption, "parse_mode": "Markdown"}
            r = requests.post(url, data=data, files=files, timeout=30)
            if r.status_code != 200:
                print(f"⚠️ Telegram photo error {r.status_code} for {cid}: {r.text}")
        except Exception as e:
            print("⚠️ Telegram photo exception:", e)

# ----------------------- OpenAI helper (simple) -----------------------
def ai_explain(symbol: str, timeframe: str, short_context: str) -> str:
    """Call OpenAI Chat API to produce a short explanation (returns text)."""
    if not ENABLE_GPT or not OPENAI_API_KEY:
        return ""
    try:
        endpoint = "https://api.openai.com/v1/chat/completions"
        headers = {"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"}
        system = "You are a concise crypto market assistant. Produce one short (2-3 lines) human-readable explanation and one actionable sentence about the symbol, timeframe and given metrics. Use Italian."
        user = f"Symbol: {symbol}\nTimeframe: {timeframe}\nContext: {short_context}\nGive a short explanation (Italian)."
        payload = {
            "model": OPENAI_MODEL,
            "messages": [{"role":"system","content":system}, {"role":"user","content":user}],
            "max_tokens": GPT_MAX_TOKENS,
            "temperature": GPT_TEMPERATURE,
            "n": 1
        }
        r = requests.post(endpoint, headers=headers, json=payload, timeout=20)
        if r.status_code != 200:
            print("⚠️ OpenAI error:", r.status_code, r.text)
            return ""
        data = r.json()
        content = data.get("choices",[{}])[0].get("message",{}).get("content","")
        return content.strip()
    except Exception as e:
        print("⚠️ ai_explain error:", e)
        return ""

# ----------------------- INDICATORS -----------------------
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
        diff = prices[i] - prices[i-1]
        gains.append(max(diff, 0)); losses.append(max(-diff, 0))
    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period
    if avg_loss == 0: return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))

def compute_macd(prices, fast=12, slow=26, signal=9):
    if len(prices) < slow:
        return None, None
    ema_fast = compute_ema(prices, fast); ema_slow = compute_ema(prices, slow)
    if ema_fast is None or ema_slow is None:
        return None, None
    macd = ema_fast - ema_slow
    macd_series = []
    for i in range(slow-1, len(prices)):
        ef = compute_ema(prices[:i+1], fast); es = compute_ema(prices[:i+1], slow)
        if ef is None or es is None: continue
        macd_series.append(ef - es)
    signal_line = compute_ema(macd_series, signal) if len(macd_series) >= signal else None
    return macd, signal_line

def compute_volatility_logreturns(closes):
    if len(closes) < 3: return 0.0
    lr = []
    for i in range(1, len(closes)):
        if closes[i-1] > 0:
            lr.append(math.log(closes[i] / closes[i-1]))
    if len(lr) < 2: return 0.0
    return pstdev(lr)

def estimate_volatility_tier(closes, volumes):
    vol = compute_volatility_logreturns(closes[-30:]) if len(closes) >= 30 else compute_volatility_logreturns(closes)
    if vol < VOL_TIER_THRESHOLDS["low"]:
        tier = "LOW"
    elif vol < VOL_TIER_THRESHOLDS["medium"]:
        tier = "MEDIUM"
    elif vol < VOL_TIER_THRESHOLDS["high"]:
        tier = "HIGH"
    else:
        tier = "VERY HIGH"
    vol_ratio = 1.0
    if volumes and len(volumes) >= 6:
        baseline = mean(volumes[-6:-1])
        last_vol = volumes[-1]
        if baseline:
            vol_ratio = last_vol / baseline
    return tier, vol_ratio

def breakout_score(closes, lookback=20):
    if len(closes) < lookback + 1:
        return 0, None
    recent = closes[-(lookback+1):-1]
    last = closes[-1]
    hi = max(recent); lo = min(recent)
    if last > hi:
        return 20, f"Breakout sopra max {hi:.6f}"
    if last < lo:
        return -10, f"Rotto min {lo:.6f}"
    return 0, None

def detect_volume_spike(volumes, multiplier=VOLUME_SPIKE_MULTIPLIER):
    if not volumes or len(volumes) < 6:
        return False, 1.0
    avg_prev = mean(volumes[-6:-1])
    last = volumes[-1]
    if avg_prev <= 0:
        return False, 1.0
    ratio = last / avg_prev
    return ratio >= multiplier, ratio

def score_signals(prices, volumes, variation, tf):
    score = 0
    reasons = []
    ema10 = compute_ema(prices, 10); ema30 = compute_ema(prices, 30)
    if ema10 and ema30:
        diff_pct = ((ema10 - ema30) / ema30) * 100 if ema30 else 0.0
        if diff_pct > 0.8:
            score += 20; reasons.append(f"EMA10>EMA30 ({diff_pct:.2f}%)")
        elif diff_pct < -0.8:
            score -= 15; reasons.append(f"EMA10<EMA30 ({diff_pct:.2f}%)")
    macd, signal = compute_macd(prices)
    if macd is not None and signal is not None:
        if macd > signal:
            score += 15; reasons.append("MACD bullish")
        else:
            score -= 7; reasons.append("MACD bearish")
    rsi = compute_rsi(prices, 14)
    if rsi is not None:
        if rsi < 35:
            score += 10; reasons.append(f"RSI {rsi:.1f} (oversold)")
        elif rsi > 70:
            score -= 15; reasons.append(f"RSI {rsi:.1f} (overbought)")
    bs, br = breakout_score(prices, lookback=20)
    score += bs
    if br:
        reasons.append(br)
    mag = min(25, abs(variation))
    if variation > 0:
        score += mag * 0.6
    else:
        score -= mag * 0.4
    _, vol_ratio = estimate_volatility_tier(prices, volumes)
    if vol_ratio and vol_ratio >= VOLUME_SPIKE_MULTIPLIER:
        score += 25; reasons.append(f"Vol spike {vol_ratio:.1f}×")
    elif vol_ratio and vol_ratio >= 5:
        score += 5; reasons.append(f"Vol ratio {vol_ratio:.1f}×")
    raw = max(-100, min(100, score))
    normalized = int((raw + 100) / 2)
    return normalized, reasons

# ----------------------- Bybit helpers -----------------------
def safe_fetch_ohlcv(symbol, timeframe, limit=120):
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
        print("⚠️ load_markets:", e)
        return []

# ----------------------- Chart generation -----------------------
def make_chart_png(symbol: str, timeframe: str, closes, vols):
    try:
        candles = len(closes)
        fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(6,5), gridspec_kw={'height_ratios':[3,1]})
        x = list(range(candles))
        ax1.plot(x, closes, label="Close")
        ema10 = [compute_ema(closes[:i+1], 10) if i+1>=10 else None for i in range(candles)]
        ema30 = [compute_ema(closes[:i+1], 30) if i+1>=30 else None for i in range(candles)]
        ax1.plot(x, [v if v is not None else float('nan') for v in ema10], label="EMA10")
        ax1.plot(x, [v if v is not None else float('nan') for v in ema30], label="EMA30")
        ax1.set_title(f"{symbol} {timeframe} — last {candles} candles")
        ax1.legend(fontsize="small")
        ax1.grid(True, alpha=0.3)

        ax2.bar(x, vols)
        ax2.set_ylabel("Volume")
        ax2.grid(True, alpha=0.3)

        plt.tight_layout()
        buf = io.BytesIO()
        plt.savefig(buf, format="png", dpi=100)
        plt.close(fig)
        buf.seek(0)
        return buf.read()
    except Exception as e:
        print("⚠️ make_chart_png error:", e)
        return None

# ----------------------- Wyckoff heuristic -----------------------
def wyckoff_phase_from_series(closes, vols):
    if not closes or not vols:
        return "Unknown"
    vt, vr = estimate_volatility_tier(closes, vols)
    recent = closes[-LOOKBACK_CANDLES:] if len(closes) >= LOOKBACK_CANDLES else closes
    hi = max(recent); lo = min(recent)
    last = closes[-1]
    ema10 = compute_ema(closes, 10); ema30 = compute_ema(closes, 30)
    range_pct = (hi - lo) / lo if lo else 0.0

    if vt in ("LOW","MEDIUM") and range_pct < 0.06:
        baseline = mean(vols[-(LOOKBACK_CANDLES*2//3):-1]) if len(vols) >= 10 else mean(vols)
        last_vol = vols[-1]
        if baseline and last_vol / baseline >= 1.0 and last >= lo and last <= hi:
            return "Accumulation"
    if last > hi and vr >= VOL_ACCUMULATION_RATIO:
        return "Markup"
    if last >= 0.9 * hi and vt in ("HIGH","VERY HIGH"):
        return "Distribution"
    if ema10 and ema30 and ema10 < ema30 and last < (lo * 1.02):
        return "Markdown"
    return "Range"

# ----------------------- Report builder (with GPT + chart) -----------------------
def build_and_send_wyckoff_report(symbols, tf_label, tf):
    items = []
    for s in symbols:
        closes, vols = safe_fetch_ohlcv(s, tf, limit=max(CHART_CANDLES, LOOKBACK_CANDLES, 120))
        if not closes:
            continue
        price = closes[-1]
        prev = closes[-2] if len(closes) > 1 else price
        var = ((price - prev) / prev) * 100 if prev else 0.0
        score, reasons = score_signals(closes, vols, var, tf)
        if score < REPORT_SCORE_MIN:
            continue
        phase = wyckoff_phase_from_series(closes, vols)
        vt, vr = estimate_volatility_tier(closes, vols)
        items.append({
            "symbol": s, "score": score, "price": price, "var": var,
            "phase": phase, "reasons": reasons, "vt": vt, "vr": vr,
            "closes": closes, "vols": vols
        })
    items = sorted(items, key=lambda x: x["score"], reverse=True)[:REPORT_MAX_ITEMS]
    if not items:
        print("No items for report", tf_label)
        return

    # market sentiment (optional)
    sentiment = ""
    if ENABLE_GPT:
        # use a cheap local market summary if GPT disabled for sentiment; here we call a simple function
        try:
            btc_closes, _ = safe_fetch_ohlcv("BTC/USDT", "1h", limit=25)
            eth_closes, _ = safe_fetch_ohlcv("ETH/USDT", "1h", limit=25)
            if btc_closes and eth_closes:
                btc_ch = (btc_closes[-1] - btc_closes[0]) / btc_closes[0] * 100
                eth_ch = (eth_closes[-1] - eth_closes[0]) / eth_closes[0] * 100
                sentiment = f"*Market*: BTC Δ24h {btc_ch:+.2f}% | ETH Δ24h {eth_ch:+.2f}%\n"
        except Exception:
            sentiment = ""

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    header = f"WYCKOFF REPORT {tf_label} — {now}\n{sentiment}\n"

    # send a single bulk text summary first
    lines = [header]
    for it in items:
        lines.append(f"{it['symbol']} | Score {it['score']}/100 | Prezzo {it['price']:.6f} | Var {it['var']:+.2f}%\n ▣ Fase: {it['phase']} | Vol ratio {it['vr']:.1f}× | Vol Tier {it['vt']}\n ▣ Motivi: {'; '.join(it['reasons']) if it['reasons'] else '—'}\n")
    send_telegram_text("\n".join(lines))

    # For each top item, optionally send chart + GPT explanation (one photo + caption)
    for it in items:
        caption = (f"{it['symbol']} | Score {it['score']}/100 | {it['phase']} | Var {it['var']:+.2f}%\n")
        # short context for GPT
        ctx = f"Score {it['score']}/100. Phase {it['phase']}. Var {it['var']:+.2f}%. Reasons: {'; '.join(it['reasons']) if it['reasons'] else 'none'}. Vol ratio {it['vr']:.2f}×."
        ai_text = ""
        if ENABLE_GPT and OPENAI_API_KEY:
            ai_text = ai_explain(it['symbol'], tf, ctx)
            if ai_text:
                caption += "\n" + ai_text
        # chart
        if ENABLE_CHARTS:
            png = make_chart_png(it['symbol'], tf, it['closes'][-CHART_CANDLES:], it['vols'][-CHART_CANDLES:])
            if png:
                # send photo with caption
                send_telegram_photo(png, caption)
                continue
        # if no chart, send text caption
        send_telegram_text(caption)

# ----------------------- Flask endpoints -----------------------
@app.route("/")
def home():
    return "✅ Crypto Alert Bot (Bybit) — running"

@app.route("/test")
def test():
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    send_telegram_text(f"🧪 TEST ALERT — bot online ({now})")
    return "Test sent", 200

# ----------------------- Monitor loop -----------------------
def monitor_loop():
    symbols = get_bybit_derivative_symbols()
    print(f"✅ Simboli derivati trovati: {len(symbols)}")
    last_hb = 0
    last_r4 = 0
    last_daily = 0
    last_weekly = 0
    first = True

    while True:
        start_cycle = time.time()
        for s in symbols[:min(len(symbols), 2000)]:
            for tf in FAST_TFS:
                closes, vols = safe_fetch_ohlcv(s, tf, limit=120)
                if not closes:
                    continue
                price = closes[-1]
                key = f"{s}_{tf}"
                if first or key not in last_prices:
                    last_prices[key] = price
                    continue
                prev = last_prices.get(key, price)
                variation = ((price - prev) / prev) * 100 if prev else 0.0
                threshold = THRESHOLDS.get(tf, 5.0)
                vol_spike, vol_ratio = detect_volume_spike(vols)
                usd_volume = 0.0
                try:
                    if "/USDT" in s or s.endswith(":USDT"):
                        usd_volume = price * vols[-1]
                except Exception:
                    usd_volume = 0.0
                alert_price_move = abs(variation) >= threshold
                alert_volume = vol_spike and (usd_volume >= VOLUME_MIN_USD or vols[-1] >= VOLUME_SPIKE_MIN_VOLUME)
                now_ts = time.time()
                if alert_price_move or alert_volume:
                    last_ts = last_alert_time.get(key, 0)
                    if now_ts - last_ts >= COOLDOWN_SECONDS:
                        score, reasons = score_signals(closes, vols, variation, tf)
                        vt, vr = estimate_volatility_tier(closes, vols)
                        trend = "📈 Rialzo" if variation > 0 else "📉 Crollo"
                        nowtxt = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
                        text = (f"🔔 ALERT -> *{s}* ({tf}) — variazione {variation:+.2f}%\n"
                                f"💰 Prezzo: {price:.6f}\n"
                                f"{trend}\n"
                                f"🔎 Score: *{score}/100*\n")
                        if alert_volume:
                            text += f"🚨 Volume spike {vol_ratio:.1f}× — volume candle {vols[-1]:.0f} (~{usd_volume:.0f} USD)\n"
                        if reasons:
                            text += "▪ " + "; ".join(reasons) + "\n"
                        text += f"[{nowtxt}]"
                        send_telegram_text(text)
                        last_alert_time[key] = now_ts
                        if alert_volume:
                            last_volume_alert_time[s] = now_ts
                last_prices[key] = price
        first = False

        t = time.time()
        if t - last_hb >= 3600:
            nowtxt = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
            send_telegram_text(f"💓 Heartbeat — bot attivo ({nowtxt}) | Simboli monitorati: {len(symbols)}")
            last_hb = t

        if t - last_r4 >= REPORT_4H_INTERVAL_SEC:
            threading.Thread(target=build_and_send_wyckoff_report, args=(symbols, "4h", "4h"), daemon=True).start()
            last_r4 = t
        if t - last_daily >= REPORT_DAILY_INTERVAL_SEC:
            threading.Thread(target=build_and_send_wyckoff_report, args=(symbols, "DAILY", "1d"), daemon=True).start()
            last_daily = t
        if t - last_weekly >= REPORT_WEEKLY_INTERVAL_SEC:
            threading.Thread(target=build_and_send_wyckoff_report, args=(symbols, "WEEKLY", "1w"), daemon=True).start()
            last_weekly = t

        elapsed = time.time() - start_cycle
        if elapsed < LOOP_DELAY:
            time.sleep(LOOP_DELAY - elapsed)

# ----------------------- Entrypoint -----------------------
if __name__ == "__main__":
    print("🚀 Avvio monitor Bybit — percent alerts 1m/3m/1h + Wyckoff+GPT+charts")
    threading.Thread(target=lambda: app.run(host="0.0.0.0", port=PORT), daemon=True).start()
    threading.Thread(target=monitor_loop, daemon=True).start()
    while True:
        time.sleep(3600)
