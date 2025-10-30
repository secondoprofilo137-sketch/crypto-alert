#!/usr/bin/env python3
# monitor_bybit_flask_v2.py
# Bybit Derivatives Monitor — Flask + Telegram — con rilevamento spike di volume
# Versione 2 — italiano, emoji, suggerimenti e heartbeat orario

import os
import time
import threading
import math
from statistics import mean, pstdev
from datetime import datetime, timezone
import requests
import ccxt
from flask import Flask

# -------------------------
# CONFIG (env o defaults)
# -------------------------
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_IDS = [cid.strip() for cid in os.getenv("TELEGRAM_CHAT_IDS", "").split(",") if cid.strip()]
PORT = int(os.getenv("PORT", 10000))

FAST_TFS = os.getenv("FAST_TFS", "1m,3m").split(",")
SLOW_TFS = os.getenv("SLOW_TFS", "1h").split(",")
FAST_THRESHOLD = float(os.getenv("FAST_THRESHOLD", 5.0))
SLOW_THRESHOLD = float(os.getenv("SLOW_THRESHOLD", 10.0))  # solo ≥10%
LOOP_DELAY = int(os.getenv("LOOP_DELAY", 10))
HEARTBEAT_INTERVAL_SEC = int(os.getenv("HEARTBEAT_INTERVAL_SEC", 3600))
MAX_CANDIDATES_PER_CYCLE = int(os.getenv("MAX_CANDIDATES_PER_CYCLE", 800))

VOL_TIER_THRESHOLDS = {"low": 0.002, "medium": 0.006, "high": 0.012}
RISK_MAPPING = {
    "low":    {"target_pct": (0.8, 1.5),  "stop_pct": (-0.6, -1.0)},
    "medium": {"target_pct": (1.5, 3.0),  "stop_pct": (-1.0, -2.0)},
    "high":   {"target_pct": (3.0, 6.0),  "stop_pct": (-2.0, -4.0)},
    "very":   {"target_pct": (6.0, 12.0), "stop_pct": (-4.0, -8.0)},
}

# -------------------------
# Stato e inizializzazioni
