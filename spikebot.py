#!/usr/bin/env python3
"""
D-SmartTrader — Clean stable version
Features:
 - Fetches active symbols from Deriv and maps requested symbols to available ones
 - Subscribes safely (candles + ticks), fallback to ticks_history if needed
 - SAFE / STRICT toggle via Telegram: "/strict on" and "/strict off"
 - Confidence % per signal (RSI + MA trend + candlestick + volume proxy)
 - TP/SL per signal, tracks wins/losses and win rate
 - Pure Python indicators (no talib), only requires websocket-client and requests
 - Persistence to local JSON file (state)
"""
import os
import json
import time
import threading
import re
from collections import deque, defaultdict

import websocket
import requests

# ---------- CONFIG ----------
DERIV_TOKEN = os.getenv("DERIV_TOKEN")
DERIV_APP_ID = os.getenv("DERIV_APP_ID", "1089")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

WS_URL = f"wss://ws.derivws.com/websockets/v3?app_id={DERIV_APP_ID}"

# Symbols requested by user (your list)
SYMBOLS_REQUESTED = [
    # Volatility
    "R_10", "R_25", "R_50", "R_75", "R_100",
    # Gold (will map tolerant -> frxXAUUSD or XAUUSD as available)
    "XAUUSD",
    # Forex majors
    "frxEURUSD", "frxUSDJPY", "frxGBPUSD", "frxAUDUSD", "frxUSDCAD",
]

# Indicator / signal parameters
RSI_PERIOD = 14
MA_1M = 14
MA_5M = 50
RSI_OB = 70
RSI_OS = 30
VOL_MULTIPLIER = 1.2

# TP/SL (percent)
TP_PCT_BY_GROUP = {"fx": 0.0015, "gold": 0.0020, "vol": 0.0040}
SL_PCT_BY_GROUP = {"fx": 0.0010, "gold": 0.0012, "vol": 0.0025}

# Confidence thresholds
SAFE_MIN_CONF = 60.0
STRICT_MIN_CONF = 85.0

# Persistence
STATE_FILE = "d_smarttrader_state.json"

# ---------- RUNTIME STATE ----------
MODE = {"mode": "safe"}  # "safe" or "strict"
SYMBOL_MAP = {}          # requested -> actual (after mapping with active_symbols)
AVAILABLE_SYMBOLS = set()
SUBSCRIBED = set()
SUBSCRIBE_ATTEMPTS = defaultdict(set)  # actual -> {"candles","ticks","ticks_history"}

# Candle and tick containers
C1 = defaultdict(lambda: deque(maxlen=400))   # 1m candles per actual symbol
C5 = defaultdict(lambda: deque(maxlen=400))   # 5m candles
TICKS_THIS_MIN = defaultdict(int)
LAST_MIN_BUCKET = defaultdict(lambda: None)
LAST_PRICE = {}

# Trade state
OPEN_TRADES = {}   # actual_symbol -> dict(direction, entry, tp, sl, time, confidence)
RESULTS = defaultdict(lambda: {"win": 0, "loss": 0})
LAST_SIGNAL_TS = defaultdict(lambda: 0)

# Misc
LOCK = threading.Lock()
WS_OBJ = None
TG_OFFSET = None

# ---------- UTIL FUNCTIONS ----------
def send_telegram(text: str):
    """Send message to configured Telegram chat (if configured)."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("[TG] not configured:", text[:120])
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            data={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "Markdown"}
        )
    except Exception as e:
        print("[TG] send error:", e)

def norm(s: str) -> str:
    return re.sub(r"[^0-9a-z]", "", (s or "").lower())

def best_match(requested: str, available: set) -> str:
    """Tolerant mapping: preferred exact, fallback to substring match on normalized forms."""
    if requested in available:
        return requested
    rq = norm(requested)
    if not rq:
        return None
    for a in available:
        if norm(a) == rq or rq in norm(a) or norm(a) in rq:
            return a
    return None

def load_state():
    global MODE, SYMBOL_MAP, OPEN_TRADES, RESULTS, LAST_SIGNAL_TS
    try:
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE, "r") as f:
                d = json.load(f)
                MODE["mode"] = d.get("mode", MODE["mode"])
                SYMBOL_MAP = d.get("symbol_map", SYMBOL_MAP)
                OPEN_TRADES.update(d.get("open_trades", {}))
                RESULTS.update(d.get("results", {}))
                for k,v in d.get("last_signal_ts", {}).items():
                    LAST_SIGNAL_TS[k] = v
    except Exception as e:
        print("load_state error:", e)

def save_state():
    try:
        with open(STATE_FILE, "w") as f:
            json.dump({
                "mode": MODE["mode"],
                "symbol_map": SYMBOL_MAP,
                "open_trades": OPEN_TRADES,
                "results": RESULTS,
                "last_signal_ts": dict(LAST_SIGNAL_TS)
            }, f, indent=2)
    except Exception as e:
        print("save_state error:", e)

def group_of_symbol(sym: str) -> str:
    su = sym.upper()
    if "XAU" in su or "GOLD" in su:
        return "gold"
    if su.startswith("FRX") or su.startswith("frx"):
        return "fx"
    return "vol"

# ---------- INDICATORS (pure Python) ----------
def sma(values, n):
    if len(values) < n:
        return None
    return sum(values[-n:]) / n

def rsi_calc(values, n=14):
    if len(values) < n + 1:
        return None
    gains = losses = 0.0
    for i in range(-n, 0):
        d = values[i] - values[i-1]
        if d >= 0:
            gains += d
        else:
            losses += -d
    if losses == 0:
        return 100.0
    rs = gains / losses
    return 100 - (100 / (1 + rs))

def detect_engulfing(candles):
    if len(candles) < 2:
        return None
    a, b = candles[-2], candles[-1]
    # bullish engulfing
    if a["close"] < a["open"] and b["close"] > b["open"] and b["close"] > a["open"] and b["open"] < a["close"]:
        return "bullish"
    # bearish engulfing
    if a["close"] > a["open"] and b["close"] < b["open"] and b["open"] > a["close"] and b["close"] < a["open"]:
        return "bearish"
    return None

# ---------- CONFIDENCE & DECISION ----------
def compute_confidence(actual_sym: str):
    # needs recent C1 and C5
    c1 = list(C1[actual_sym])
    c5 = list(C5[actual_sym])
    if len(c1) < max(RSI_PERIOD + 1, MA_1M) or len(c5) < MA_5M:
        return None
    closes1 = [c["close"] for c in c1]
    closes5 = [c["close"] for c in c5]
    r = rsi_calc(closes1, RSI_PERIOD)
    ma1 = sma(closes1, MA_1M)
    ma5 = sma(closes5, MA_5M)
    patt = detect_engulfing(c1[-2:]) if len(c1) >= 2 else None

    # volume proxy
    last_vol = c1[-1].get("volume", 0) or TICKS_THIS_MIN.get(actual_sym, 0)
    vols = [c.get("volume", 0) for c in c1[-(RSI_PERIOD+5):] if c.get("volume", 0)]
    avg_vol = (sum(vols) / len(vols)) if vols else None
    vol_ok = (avg_vol is not None and last_vol > avg_vol * VOL_MULTIPLIER) or (TICKS_THIS_MIN.get(actual_sym, 0) >= 3)

    score = 0.0
    dir_hint = None
    if r is None or ma1 is None or ma5 is None:
        return None
    # RSI 30 points
    if r <= RSI_OS:
        score += min(30, (RSI_OS - r) * 1.0)
        dir_hint = "BUY"
    elif r >= RSI_OB:
        score += min(30, (r - RSI_OB) * 1.0)
        dir_hint = "SELL"
    # Trend 35
    trend_ok = (dir_hint == "BUY" and ma1 > ma5) or (dir_hint == "SELL" and ma1 < ma5)
    if trend_ok:
        score += 35
    else:
        score -= 5
    # Pattern 20
    if (dir_hint == "BUY" and patt == "bullish") or (dir_hint == "SELL" and patt == "bearish"):
        score += 20
    # Volume 15
    if vol_ok:
        score += 15
    score = max(0.0, min(100.0, score))
    return {
        "score": round(score, 1),
        "dir": dir_hint,
        "rsi": round(r, 2),
        "ma1": round(ma1, 6),
        "ma5": round(ma5, 6),
        "pattern": patt or "none",
        "vol_ok": bool(vol_ok),
        "avg_vol": round(avg_vol, 2) if avg_vol else None
    }

def should_open_trade(actual_sym: str, price: float, now_ts: int):
    info = compute_confidence(actual_sym)
    if not info or not info["dir"]:
        return None
    min_conf = STRICT_MIN_CONF if MODE["mode"] == "strict" else SAFE_MIN_CONF
    if info["score"] < min_conf:
        return None
    # anti-spam
    if now_ts - LAST_SIGNAL_TS.get(actual_sym, 0) < 30:
        return None
    if actual_sym in OPEN_TRADES:
        return None
    direction = info["dir"]
    grp = group_of_symbol(actual_sym)
    tp_pct = TP_PCT_BY_GROUP.get(grp, 0.004)
    sl_pct = SL_PCT_BY_GROUP.get(grp, 0.0025)
    if direction == "BUY":
        tp = price * (1 + tp_pct)
        sl = price * (1 - sl_pct)
    else:
        tp = price * (1 - tp_pct)
        sl = price * (1 + sl_pct)
    return {"direction": direction, "tp": tp, "sl": sl, "info": info}

# ---------- DERIV WS / SUBSCRIPTION HELPERS ----------
def try_subscribe(ws, actual):
    """Try candles + ticks subscribe (primary)."""
    if "candles" not in SUBSCRIBE_ATTEMPTS[actual]:
        SUBSCRIBE_ATTEMPTS[actual].add("candles")
        try:
            ws.send(json.dumps({"candles": actual, "granularity": 60, "subscribe": 1}))
            time.sleep(0.05)
            ws.send(json.dumps({"candles": actual, "granularity": 300, "subscribe": 1}))
        except Exception as e:
            send_telegram(f"subscribe (candles) send error for {actual}: {e}")
    if "ticks" not in SUBSCRIBE_ATTEMPTS[actual]:
        SUBSCRIBE_ATTEMPTS[actual].add("ticks")
        try:
            ws.send(json.dumps({"ticks": actual, "subscribe": 1}))
        except Exception as e:
            send_telegram(f"subscribe (ticks) send error for {actual}: {e}")

def fallback_ticks_history(ws, actual):
    """Fallback to ticks_history style:'candles' subscribe if primary fails."""
    if "ticks_history" in SUBSCRIBE_ATTEMPTS[actual]:
        return
    SUBSCRIBE_ATTEMPTS[actual].add("ticks_history")
    try:
        ws.send(json.dumps({
            "ticks_history": actual,
            "style": "candles",
            "granularity": 60,
            "count": 120,
            "end": "latest",
            "subscribe": 1
        }))
        time.sleep(0.05)
        ws.send(json.dumps({
            "ticks_history": actual,
            "style": "candles",
            "granularity": 300,
            "count": 120,
            "end": "latest",
            "subscribe": 1
        }))
    except Exception as e:
        send_telegram(f"fallback ticks_history error for {actual}: {e}")

def handle_error_request(ws, msg):
    echo = msg.get("echo_req", {}) or {}
    sym = echo.get("candles") or echo.get("ticks") or echo.get("ticks_history") or None
    if not sym:
        return
    # If candles failed -> fallback ticks_history
    if "candles" in echo:
        if "ticks_history" not in SUBSCRIBE_ATTEMPTS[sym]:
            fallback_ticks_history(ws, sym)
        else:
            send_telegram(f"❌ {sym} unsupported for candles on this account. Skipping.")
    elif "ticks" in echo:
        if "ticks_history" not in SUBSCRIBE_ATTEMPTS[sym]:
            fallback_ticks_history(ws, sym)
        else:
            send_telegram(f"❌ {sym} ticks unsupported on this account. Skipping.")
    else:
        send_telegram(f"❌ Unrecognized error for {sym}: {msg.get('error')}")

# ---------- WS CALLBACKS ----------
def ws_on_open(ws):
    try:
        ws.send(json.dumps({"authorize": DERIV_TOKEN}))
    except Exception as e:
        send_telegram(f"Auth send error: {e}")

def ws_on_message(ws, raw):
    global AVAILABLE_SYMBOLS, SYMBOL_MAP, SUBSCRIBED, WS_OBJ
    WS_OBJ = ws
    try:
        msg = json.loads(raw)
    except Exception:
        return

    # error handler
    if "error" in msg:
        handle_error_request(ws, msg)
        return

    mtype = msg.get("msg_type")

    if mtype == "authorize":
        send_telegram(f"✅ D-SmartTrader authorized. Fetching active symbols...")
        try:
            ws.send(json.dumps({"active_symbols": "brief", "product_type": "basic"}))
        except Exception as e:
            send_telegram(f"active_symbols request failed: {e}")
        return

    if mtype == "active_symbols" and isinstance(msg.get("active_symbols"), list):
        # build available symbol set
        AVAILABLE_SYMBOLS = set()
        for it in msg["active_symbols"]:
            if isinstance(it, dict):
                sym = it.get("symbol")
                if sym:
                    AVAILABLE_SYMBOLS.add(sym)
        # map requested -> available
        found = []
        not_found = []
        for req in SYMBOLS_REQUESTED:
            m = best_match(req, AVAILABLE_SYMBOLS)
            if m:
                SYMBOL_MAP[req] = m
                found.append((req, m))
            else:
                not_found.append(req)
        lines = ["🔎 Active symbols retrieved."]
        if found:
            lines.append("✅ Found / mapped:")
            for r, m in found:
                lines.append(f"  • {r} -> {m}")
        if not_found:
            lines.append("❌ Not available on this account:")
            for n in not_found:
                lines.append(f"  • {n}")
        send_telegram("\n".join(lines))
        # subscribe to mapped
        for req, actual in SYMBOL_MAP.items():
            if actual in SUBSCRIBED:
                continue
            try_subscribe(ws, actual)
            SUBSCRIBED.add(actual)
            time.sleep(0.05)
        return

    # initial candles list (candles)
    if mtype == "candles" and isinstance(msg.get("candles"), list):
        echo = msg.get("echo_req", {}) or {}
        actual = echo.get("candles") or echo.get("ticks_history") or msg.get("symbol")
        gran = int(echo.get("granularity", 60))
        container = C1 if gran == 60 else C5
        for c in msg["candles"]:
            try:
                candle = {
                    "open": float(c["open"]), "high": float(c["high"]),
                    "low": float(c["low"]), "close": float(c["close"]),
                    "volume": float(c.get("volume", 0)),
                    "epoch": int(c.get("epoch", time.time()))
                }
            except Exception:
                continue
            container[actual].append(candle)
        return

    # streaming ohlc
    if mtype == "ohlc" and "ohlc" in msg:
        echo = msg.get("echo_req", {}) or {}
        gran = int(echo.get("granularity", 60))
        actual = echo.get("candles") or msg.get("symbol")
        o = msg["ohlc"]
        candle = {
            "open": float(o["open"]), "high": float(o["high"]),
            "low": float(o["low"]), "close": float(o["close"]),
            "volume": float(o.get("volume", 0)),
            "epoch": int(o.get("open_time", time.time()))
        }
        if gran == 300:
            C5[actual].append(candle)
        else:
            C1[actual].append(candle)
            # new minute -> reset tick proxy
            TICKS_THIS_MIN[actual] = 0
        return

    # tick streaming
    if mtype == "tick" and "tick" in msg:
        t = msg["tick"]
        actual = t.get("symbol")
        price = float(t.get("quote"))
        epoch = int(t.get("epoch", time.time()))
        LAST_PRICE[actual] = price
        mb = epoch // 60
        if LAST_MIN_BUCKET[actual] != mb:
            TICKS_THIS_MIN[actual] = 0
            LAST_MIN_BUCKET[actual] = mb
        TICKS_THIS_MIN[actual] += 1

        # check open trades TP/SL
        if actual in OPEN_TRADES:
            sig = OPEN_TRADES[actual]
            d = sig["direction"]
            tp = sig["tp"]; sl = sig["sl"]
            if d == "BUY" and price >= tp:
                RESULTS[actual]["win"] += 1
                send_telegram(f"✅ `{actual}` BUY TP hit @ `{price}` — WinRate {round(win_rate(actual),2)}%")
                with LOCK:
                    del OPEN_TRADES[actual]
                    save_state()
            elif d == "BUY" and price <= sl:
                RESULTS[actual]["loss"] += 1
                send_telegram(f"❌ `{actual}` BUY SL hit @ `{price}` — WinRate {round(win_rate(actual),2)}%")
                with LOCK:
                    del OPEN_TRADES[actual]
                    save_state()
            elif d == "SELL" and price <= tp:
                RESULTS[actual]["win"] += 1
                send_telegram(f"✅ `{actual}` SELL TP hit @ `{price}` — WinRate {round(win_rate(actual),2)}%")
                with LOCK:
                    del OPEN_TRADES[actual]
                    save_state()
            elif d == "SELL" and price >= sl:
                RESULTS[actual]["loss"] += 1
                send_telegram(f"❌ `{actual}` SELL SL hit @ `{price}` — WinRate {round(win_rate(actual),2)}%")
                with LOCK:
                    del OPEN_TRADES[actual]
                    save_state()

        # only evaluate signals for actual symbols mapped from user's requested list
        if actual not in SYMBOL_MAP.values():
            return

        # build confidence and maybe open
        now_ts = int(time.time())
        decision = should_open_trade(actual, price, now_ts)
        if decision:
            with LOCK:
                OPEN_TRADES[actual] = {
                    "direction": decision["direction"],
                    "entry": price,
                    "tp": decision["tp"],
                    "sl": decision["sl"],
                    "time": now_ts,
                    "confidence": decision["info"]["score"],
                    "info": decision["info"]
                }
                LAST_SIGNAL_TS[actual] = now_ts
                save_state()
            send_telegram(
                "📡 *D-SmartTrader Signal*\n"
                f"Pair: `{actual}`\n"
                f"Direction: *{decision['direction']}*\n"
                f"Entry: `{round(price,6)}`\n"
                f"TP: `{round(decision['tp'],6)}` | SL: `{round(decision['sl'],6)}`\n"
                f"RSI: `{decision['info']['rsi']}` | MA(1m/5m): `{decision['info']['ma1']}` / `{decision['info']['ma5']}`\n"
                f"Pattern: `{decision['info']['pattern']}` | Vol spike: `{decision['info']['vol_ok']}`\n"
                f"Confidence: *{decision['info']['score']}%* | Mode: `{MODE['mode'].upper()}`\n"
                f"WinRate: *{round(win_rate(actual),2)}%*"
            )
        return

def ws_on_error(ws, err):
    send_telegram(f"⚠️ WebSocket error: {err}")

def ws_on_close(ws, code=None, reason=None):
    send_telegram("🔌 D-SmartTrader disconnected. Reconnecting...")

# ---------- TELEGRAM POLLER (commands) ----------
def telegram_poller():
    global TG_OFFSET
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("[TG] Not configured, poller disabled.")
        return
    send_telegram(f"🤖 D-SmartTrader is starting ({MODE['mode'].upper()} mode).")
    while True:
        try:
            params = {"timeout": 30}
            if TG_OFFSET:
                params["offset"] = TG_OFFSET + 1
            resp = requests.get(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates", params=params, timeout=40).json()
            if not resp.get("ok"):
                time.sleep(2)
                continue
            for upd in resp.get("result", []):
                TG_OFFSET = upd["update_id"]
                msg = upd.get("message") or {}
                chat = str(msg.get("chat", {}).get("id", ""))
                if chat != str(TELEGRAM_CHAT_ID):
                    continue
                text = (msg.get("text") or "").strip().lower()
                if text in ("/strict on", "/strict"):
                    MODE["mode"] = "strict"
                    save_state()
                    send_telegram("🔒 STRICT mode ON — only highest-confidence signals.")
                elif text in ("/strict off", "/safe"):
                    MODE["mode"] = "safe"
                    save_state()
                    send_telegram("🟢 SAFE mode ON — more signals, still filtered.")
                elif text == "/status":
                    lines = [f"Mode: {MODE['mode'].upper()}"]
                    for req, actual in SYMBOL_MAP.items():
                        r = RESULTS.get(actual, {"win": 0, "loss": 0})
                        lines.append(f"{req} -> {actual}  {r['win']}W/{r['loss']}L  WR {round(win_rate(actual),1)}%")
                    if OPEN_TRADES:
                        lines.append("Open trades:")
                        for s, sig in OPEN_TRADES.items():
                            lines.append(f"{s} {sig['direction']} @ {round(sig['entry_price'], 2)} (RSI_OB = 70)")

# TP/SL groups -- tune per your instruments
TP_SL = { "fx": {"tp": 0.0015, "sl": 0.0010},
    "gold": {"tp": 0.0020, "sl": 0.0012},
    "vol": {"tp": 0.0040, "sl": 0.0025},}

# Confidence thresholds
SAFE_MIN_CONF = 60
STRICT_MIN_CONF = 85

STATE_FILE = "d_smarttrader_state.json"

# ---------------- RUNTIME STATE ----------------
mode_lock = threading.Lock()
MODE = {"mode": "safe"}   # "safe" or "strict" stored here and persisted

# mapping requested -> actual symbol on Deriv (populated after active_symbols)
symbol_map = {}           # e.g. "XAUUSD" -> "frxXAUUSD"
available_symbols = set()
subscribed = set()
subscribe_attempts = defaultdict(set)   # symbol -> set of attempted request types: {"candles","ticks_history","ticks"}

# market data
C1 = defaultdict(lambda: deque(maxlen=400))   # 1m candles per symbol (dicts)
C5 = defaultdict(lambda: deque(maxlen=400))   # 5m candles per symbol
TICKS_MIN = defaultdict(int)                  # tick count per current minute for symbol
LAST_MIN_BUCKET = defaultdict(lambda: None)
LAST_PRICE = {}

# trade state
OPEN = {}    # actual_symbol -> {direction, entry, tp, sl, time, confidence}
RESULTS = defaultdict(lambda: {"win": 0, "loss": 0})
LAST_SIGNAL_TS = defaultdict(lambda: 0)

# internal helpers
lock = threading.Lock()
tg_last_update = 0

# ---------------- UTIL ----------------
def send_telegram(text):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("[TG] disabled (missing env)")
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            data={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "Markdown"}
        )
    except Exception as e:
        print("[TG] send error:", e)

def norm(s):
    return re.sub(r'[^0-9a-z]', '', (s or "").lower())

def best_match(req, avail_set):
    rq = norm(req)
    if not rq:
        return None
    # exact match preferred
    if req in avail_set:
        return req
    for a in avail_set:
        an = norm(a)
        if rq == an or rq in an or an in rq:
            return a
    return None

def save_state():
    try:
        with open(STATE_FILE, "w") as f:
            json.dump({
                "mode": MODE["mode"],
                "symbol_map": symbol_map,
                "open": OPEN,
                "results": RESULTS,
                "last_signal": dict(LAST_SIGNAL_TS)
            }, f, indent=2)
    except Exception as e:
        print("save state err:", e)

def load_state():
    try:
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE, "r") as f:
                d = json.load(f)
                MODE["mode"] = d.get("mode", MODE["mode"])
                for k,v in d.get("open", {}).items():
                    OPEN[k] = v
                for k,v in d.get("results", {}).items():
                    RESULTS[k] = v
                for k,v in d.get("last_signal", {}).items():
                    LAST_SIGNAL_TS[k] = v
    except Exception as e:
        print("load state err:", e)

def group(sym):
    # simple grouping for TP/SL percent
    if sym.upper().startswith("FRXXAU") or sym.upper().endswith("XAUUSD") or "XAU" in sym.upper():
        return "gold"
    if sym.upper().startswith("FRX") or sym.lower().startswith("frx"):
        return "fx"
    return "vol"

def win_rate(sym):
    r = RESULTS.get(sym, {"win":0,"loss":0})
    tot = r["win"] + r["loss"]
    return 0.0 if tot == 0 else (r["win"] / tot) * 100.0

# ---------------- INDICATORS (pure python) ----------------
def sma(values, n):
    if len(values) < n: return None
    return sum(values[-n:]) / n

def rsi_calc(values, period=14):
    if len(values) < period + 1: return None
    gains = losses = 0.0
    for i in range(-period, 0):
        diff = values[i] - values[i-1]
        if diff >= 0: gains += diff
        else: losses += -diff
    if losses == 0:
        return 100.0
    rs = gains / losses
    return 100 - (100 / (1 + rs))

def engulfing(candles):
    if len(candles) < 2: return None
    a, b = candles[-2], candles[-1]
    if a["close"] < a["open"] and b["close"] > b["open"] and b["close"] > a["open"] and b["open"] < a["close"]:
        return "bullish"
    if a["close"] > a["open"] and b["close"] < b["open"] and b["open"] > a["close"] and b["close"] < a["open"]:
        return "bearish"
    return None

# ---------------- CONFIDENCE & DECISION ----------------
def compute_confidence(sym):
    c1 = list(C1[sym]); c5 = list(C5[sym])
    if len(c1) < max(RSI_PERIOD+1, MA_FAST_1M) or len(c5) < MA_SLOW_5M:
        return None
    closes1 = [c["close"] for c in c1]
    closes5 = [c["close"] for c in c5]
    r = rsi_calc(closes1, RSI_PERIOD)
    ma1 = sma(closes1, MA_FAST_1M)
    ma5 = sma(closes5, MA_SLOW_5M)
    patt = engulfing(c1[-2:]) if len(c1)>=2 else None
    # volume proxy
    last_vol = c1[-1].get("volume", 0) or TICKS_MIN.get(sym, 0)
    vols = [c.get("volume", 0) for c in c1[-(RSI_PERIOD+5):] if c.get("volume", 0)]
    avg_vol = (sum(vols)/len(vols)) if vols else None
    vol_ok = (avg_vol and last_vol > avg_vol * VOL_MULT) or (TICKS_MIN.get(sym,0) >= 3)

    score = 0.0
    dir_hint = None
    if r is None or ma1 is None or ma5 is None:
        return None
    # RSI: up to 30
    if r <= RSI_OS:
        score += min(30, (RSI_OS - r) * 1.0); dir_hint = "BUY"
    elif r >= RSI_OB:
        score += min(30, (r - RSI_OB) * 1.0); dir_hint = "SELL"
    # Trend: 35
    trend_ok = (dir_hint=="BUY" and ma1>ma5) or (dir_hint=="SELL" and ma1<ma5)
    if trend_ok: score += 35
    else: score -= 5
    # Pattern: 20
    if (dir_hint=="BUY" and patt=="bullish") or (dir_hint=="SELL" and patt=="bearish"):
        score += 20
    # Volume: 15
    if vol_ok: score += 15

    conf = max(0.0, min(100.0, score))
    return {"r": round(r,2), "ma1": round(ma1,6), "ma5": round(ma5,6), "pattern": patt, "vol_ok": bool(vol_ok), "avg_vol": round(avg_vol,2) if avg_vol else None, "score": round(conf,1), "dir": dir_hint}

def should_open(sym, price, now_ts):
    info = compute_confidence(sym)
    if not info or not info["dir"]:
        return None
    min_conf = STRICT_MIN_CONF if MODE["mode"] == "strict" else SAFE_MIN_CONF
    if info["score"] < min_conf:
        return None
    # avoid spam / flip within 30s
    if now_ts - LAST_SIGNAL_TS.get(sym, 0) < 30:
        return None
    if sym in OPEN:
        return None
    # direction check again
    d = info["dir"]
    # compute TP/SL
    g = group(sym)
    tp_pct = TP_SL[g]["tp"]; sl_pct = TP_SL[g]["sl"]
    if d == "BUY":
        tp = price * (1 + tp_pct); sl = price * (1 - sl_pct)
    else:
        tp = price * (1 - tp_pct); sl = price * (1 + sl_pct)
    return {"direction": d, "tp": tp, "sl": sl, "info": info}

# ---------------- DERIV WS interaction ----------------
# We'll attempt to subscribe with "candles" + "ticks". If server sends error referencing that request,
# we'll attempt fallback "ticks_history" subscribe for candles (style:"candles") once.

ws_obj = None
pending_active_request = False

def try_subscribe(ws, actual_sym):
    """Try primary subscription (candles streaming + ticks). Don't retry if we've attempted before."""
    if "candles" not in subscribe_attempts[actual_sym]:
        subscribe_attempts[actual_sym].add("candles")
        try:
            ws.send(json.dumps({"candles": actual_sym, "granularity": 60, "subscribe": 1}))
            time.sleep(0.05)
            ws.send(json.dumps({"candles": actual_sym, "granularity": 300, "subscribe": 1}))
            time.sleep(0.05)
        except Exception as e:
            send_telegram(f"Subscribe (candles) send error for {actual_sym}: {e}")
    if "ticks" not in subscribe_attempts[actual_sym]:
        subscribe_attempts[actual_sym].add("ticks")
        try:
            ws.send(json.dumps({"ticks": actual_sym, "subscribe": 1}))
        except Exception as e:
            send_telegram(f"Subscribe (ticks) send error for {actual_sym}: {e}")

def fallback_subscribe_ticks_history(ws, actual_sym):
    """Fallback: use ticks_history style:'candles' subscribe (if supported)."""
    if "ticks_history" in subscribe_attempts[actual_sym]:
        return
    subscribe_attempts[actual_sym].add("ticks_history")
    try:
        ws.send(json.dumps({
            "ticks_history": actual_sym,
            "style": "candles",
            "granularity": 60,
            "count": 120,
            "end": "latest",
            "subscribe": 1
        }))
        time.sleep(0.05)
        ws.send(json.dumps({
            "ticks_history": actual_sym,
            "style": "candles",
            "granularity": 300,
            "count": 120,
            "end": "latest",
            "subscribe": 1
        }))
    except Exception as e:
        send_telegram(f"Fallback subscribe error for {actual_sym}: {e}")

def handle_error_message(ws, msg):
    # msg includes 'error' and often 'echo_req' that shows what failed
    echo = msg.get("echo_req", {})
    # determine symbol attempted
    sym = echo.get("candles") or echo.get("ticks") or echo.get("ticks_history") or echo.get("active_symbols") or None
    if not sym:
        # nothing to fallback
        return
    # If candles request failed, try fallback ticks_history once
    if "candles" in echo and "candles" in subscribe_attempts.get(sym, set()):
        # already tried candles, fallback
        if "ticks_history" not in subscribe_attempts[sym]:
            fallback_subscribe_ticks_history(ws, sym)
        else:
            # already tried fallback -> mark as unavailable
            send_telegram(f"❌ {sym} appears unsupported for candle stream on this account. Skipping.")
    elif "candles" in echo:
        # haven't tried candles yet — try subscribe primary
        try_subscribe(ws, sym)
    elif "ticks" in echo:
        # ticks failed — try fallback to ticks_history for candles (but ticks_history may not give live ticks)
        if "ticks_history" not in subscribe_attempts[sym]:
            fallback_subscribe_ticks_history(ws, sym)
        else:
            send_telegram(f"❌ {sym} ticks subscription unsupported. Skipping ticks for {sym}.")
    else:
        # generic error
        send_telegram(f"❌ Error for {sym}: {msg.get('error')}")

# ---------------- WS callbacks ----------------
def ws_on_open(ws):
    try:
        ws.send(json.dumps({"authorize": DERIV_TOKEN}))
    except Exception as e:
        send_telegram(f"Auth send error: {e}")

def ws_on_message(ws, raw):
    global pending_active_request, available_symbols, symbol_map, subscribed, ws_obj
    ws_obj = ws
    try:
        msg = json.loads(raw)
    except Exception:
        return

    # handle error messages first (and attempt fallback)
    if "error" in msg:
        # Avoid spamming telegram: print locally too
        echo = msg.get("echo_req", {})
        sym = echo.get("candles") or echo.get("ticks") or echo.get("ticks_history") or None
        em = msg["error"].get("message") if isinstance(msg["error"], dict) else str(msg["error"])
        print(f"[Deriv error] {sym} -> {em}")
        handle_error_message(ws, msg)
        return

    mtype = msg.get("msg_type")

    # authorized -> request active_symbols
    if mtype == "authorize":
        send_telegram("✅ D-SmartTrader authorized with Deriv. Requesting active symbols...")
        try:
            ws.send(json.dumps({"active_symbols": "brief", "product_type": "basic"}))
            pending_active_request = True
        except Exception as e:
            send_telegram(f"active_symbols request failed: {e}")
        return

    # active_symbols response
    if mtype == "active_symbols" and isinstance(msg.get("active_symbols"), list):
        pending_active_request = False
        available_symbols = set()
        for it in msg["active_symbols"]:
            sym = it.get("symbol")
            if sym:
                available_symbols.add(sym)
        # map requested -> available tolerant
        found = []; not_found = []
        for r in REQUESTED:
            m = best_match(r, available_symbols)
            if m:
                symbol_map[r] = m
                found.append((r,m))
            else:
                not_found.append(r)
        # report mapping
        lines = ["🔎 Active symbols retrieved."]
        if found:
            lines.append("✅ Found / mapped:")
            for r,m in found: lines.append(f"  • {r} -> {m}")
        if not_found:
            lines.append("❌ Not available on this account:")
            for r in not_found: lines.append(f"  • {r}")
        send_telegram("\n".join(lines))
        # subscribe to mapped symbols
        for r, actual in symbol_map.items():
            if actual in subscribed: continue
            try_subscribe(ws, actual)
            subscribed.add(actual)
            time.sleep(0.05)
        return

    # candle history (array)
    if mtype == "candles" and isinstance(msg.get("candles"), list):
        # msg.echo_req may include 'candles' key with symbol or 'ticks_history'
        echo = msg.get("echo_req", {})
        actual = echo.get("candles") or echo.get("ticks_history") or msg.get("symbol")
        gran = int(echo.get("granularity", 60))
        target = C1 if gran == 60 else C5
        for c in msg["candles"]:
            candle = {
                "open": float(c["open"]), "high": float(c["high"]), "low": float(c["low"]),
                "close": float(c["close"]), "volume": float(c.get("volume", 0)),
                "epoch": int(c.get("epoch", time.time()))
            }
            target[actual].append(candle)
        return

    # streaming OHLC
    if mtype == "ohlc" and "ohlc" in msg:
        echo = msg.get("echo_req", {})
        gran = int(echo.get("granularity", 60))
        actual = echo.get("candles") or msg.get("symbol")
        o = msg["ohlc"]
        candle = {
            "open": float(o["open"]), "high": float(o["high"]), "low": float(o["low"]),
            "close": float(o["close"]), "volume": float(o.get("volume", 0)),
            "epoch": int(o.get("open_time", time.time()))
        }
        if gran == 300:
            C5[actual].append(candle)
        else:
            C1[actual].append(candle)
            # reset tick proxy on new minute
            TICKS_MIN[actual] = 0
        return

    # tick stream
    if mtype == "tick" and "tick" in msg:
        t = msg["tick"]
        actual = t.get("symbol")
        price = float(t.get("quote"))
        epoch = int(t.get("epoch", time.time()))
        LAST_PRICE[actual] = price
        # minute bucket
        mb = epoch // 60
        if LAST_MIN_BUCKET[actual] != mb:
            TICKS_MIN[actual] = 0
            LAST_MIN_BUCKET[actual] = mb
        TICKS_MIN[actual] += 1

        # check open trades for TP/SL
        if actual in OPEN:
            s = OPEN[actual]
            dirn = s["direction"]; tp = s["tp"]; sl = s["sl"]
            if dirn == "BUY" and price >= tp:
                RESULTS[actual]["win"] += 1
                send_telegram(f"✅ `{actual}` BUY TP hit @ `{price}` | WR: {round(win_rate(actual),2)}%")
                with lock: del OPEN[actual]; save_state_wrapper()
            elif dirn == "BUY" and price <= sl:
                RESULTS[actual]["loss"] += 1
                send_telegram(f"❌ `{actual}` BUY SL hit @ `{price}` | WR: {round(win_rate(actual),2)}%")
                with lock: del OPEN[actual]; save_state_wrapper()
            elif dirn == "SELL" and price <= tp:
                RESULTS[actual]["win"] += 1
                send_telegram(f"✅ `{actual}` SELL TP hit @ `{price}` | WR: {round(win_rate(actual),2)}%")
                with lock: del OPEN[actual]; save_state_wrapper()
            elif dirn == "SELL" and price >= sl:
                RESULTS[actual]["loss"] += 1
                send_telegram(f"❌ `{actual}` SELL SL hit @ `{price}` | WR: {round(win_rate(actual),2)}%")
                with lock: del OPEN[actual]; save_state_wrapper()

        # evaluate potential new signal (only for mapped requested symbols)
        # ensure actual is in symbol_map.values()
        if actual not in symbol_map.values():
            return
        now_ts = int(time.time())
        decision = should_open(actual, price, now_ts)
        if decision:
            with lock:
                OPEN[actual] = {
                    "direction": decision["direction"],
                    "entry": price,
                    "tp": decision["tp"],
                    "sl": decision["sl"],
                    "time": now_ts,
                    "confidence": decision["info"]["score"],
                    "info": decision["info"]
                }
                LAST_SIGNAL_TS[actual] = now_ts
                save_state_wrapper()
            send_telegram(
                f"📡 *D-SmartTrader Signal*\n"
                f"Pair: `{actual}`\n"
                f"Direction: *{decision['direction']}*\n"
                f"Entry: `{round(price,6)}`\n"
                f"TP: `{round(decision['tp'],6)}` | SL: `{round(decision['sl'],6)}`\n"
                f"RSI: `{decision['info']['r']}` | MA(1m/5m): `{decision['info']['ma1']}` / `{decision['info']['ma5']}`\n"
                f"Pattern: `{decision['info']['pattern']}` | Vol spike: `{decision['info']['vol_ok']}`\n"
                f"Confidence: *{decision['info']['score']}%* | Mode: `{MODE['mode'].upper()}`\n"
                f"WinRate: *{round(win_rate(actual),2)}%*"
            )
        return

def ws_on_error(ws, err):
    print("[WS] error:", err)
    send_telegram(f"⚠️ WebSocket error: {err}")

def ws_on_close(ws, a=None, b=None):
    send_telegram("🔌 D-SmartTrader disconnected. Reconnecting...")

# ---------------- Telegram command poller ----------------
def save_state_wrapper():
    try:
        save_state()
    except Exception:
        pass

def telegram_poller():
    global tg_last_update
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("[TG] missing config; poller disabled")
        return
    send_telegram(f"🤖 D-SmartTrader starting ({MODE['mode'].upper()} mode).")
    while True:
        try:
            resp = requests.get(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates",
                params={"timeout": 30, "offset": tg_last_update + 1}
            ).json()
            if not resp.get("ok"):
                time.sleep(2); continue
            for upd in resp.get("result", []):
                tg_last_update = upd["update_id"]
                msg = upd.get("message", {})
                txt = (msg.get("text") or "").strip().lower()
                cid = str(msg.get("chat", {}).get("id", ""))
                if cid != str(TELEGRAM_CHAT_ID):
                    continue
                if txt in ("/strict on", "/strict_off"):
    RSI_OB = 70
RSI_OS = 30
TP_PCT = 0.004
SL_PCT = 0.0025
SAFE_MIN_CONF = 60
STRICT_MIN_CONF = 85

# state files (optional on Railway)
STATE_FILE = "d_smarttrader_state.json"

# ---------- runtime state ----------
authorized = False
strict_mode = False
stop_flag = False

# containers
active_symbol_list = []             # populated from Deriv active_symbols response
available_symbol_set = set()        # set of actual symbol codes from exchange
symbol_map = {}                     # mapping: requested -> actual_symbol (after matching)
subscribed_symbols = set()

candles_1m = defaultdict(lambda: deque(maxlen=300))
candles_5m = defaultdict(lambda: deque(maxlen=300))
tick_count = defaultdict(int)
last_min_bucket = defaultdict(lambda: 0)

open_signal = {}
wl = defaultdict(lambda: {"win": 0, "loss": 0})
last_signal_ts = defaultdict(lambda: 0)
lock = threading.Lock()

# ---------- helpers ----------
def send_telegram(text):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("[TG] Not configured")
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            data={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "Markdown"}
        )
    except Exception as e:
        print("[TG] send error:", e)

def norm(s):
    return re.sub(r'[^0-9a-z]', '', (s or "").lower())

def best_match(requested, available_list):
    """
    tolerant matching: returns first available symbol where
    normalized requested is substring of normalized available or vice versa.
    This matches frxXAUUSD <-> XAUUSD etc.
    """
    rq = norm(requested)
    if not rq:
        return None
    # exact match fast
    for a in available_list:
        if a == requested:
            return a
    for a in available_list:
        an = norm(a)
        if rq == an or rq in an or an in rq:
            return a
    return None

def persist_state():
    try:
        with open(STATE_FILE, "w") as f:
            json.dump({"wl": wl, "open_signal": open_signal}, f)
    except Exception as e:
        print("persist_state err:", e)

def load_state():
    try:
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE, "r") as f:
                d = json.load(f)
                for k,v in d.get("wl", {}).items():
                    wl[k] = v
                for k,v in d.get("open_signal", {}).items():
                    open_signal[k] = v
    except Exception as e:
        print("load_state err:", e)

# ---------- indicator helpers (same as before) ----------
def sma(vals, n):
    if len(vals) < n:
        return None
    return sum(vals[-n:]) / n

def rsi_calc(vals, n=14):
    if len(vals) < n + 1:
        return None
    gains = losses = 0.0
    for i in range(-n, 0):
        diff = vals[i] - vals[i-1]
        if diff >= 0:
            gains += diff
        else:
            losses += -diff
    if losses == 0:
        return 100.0
    rs = gains / losses
    return 100 - (100 / (1 + rs))

def engulfing(last2):
    if len(last2) < 2:
        return None
    a, b = last2[-2], last2[-1]
    if a["close"] < a["open"] and b["close"] > b["open"] and b["close"] > a["open"] and b["open"] < a["close"]:
        return "bullish"
    if a["close"] > a["open"] and b["close"] < b["open"] and b["open"] > a["close"] and b["close"] < a["open"]:
        return "bearish"
    return None

# simplified scoring same idea as before
def build_confidence(sym):
    c1 = list(candles_1m[sym])
    c5 = list(candles_5m[sym])
    if len(c1) < max(RSI_PERIOD+1, MA_FAST_1M) or len(c5) < MA_SLOW_5M:
        return None
    closes1 = [c["close"] for c in c1]
    closes5 = [c["close"] for c in c5]
    r = rsi_calc(closes1, RSI_PERIOD)
    fast = sma(closes1, MA_FAST_1M)
    slow = sma(closes5, MA_SLOW_5M)
    patt = engulfing(c1[-2:]) if len(c1)>=2 else None
    if r is None or fast is None or slow is None:
        return None
    score = 0
    dir_hint = None
    if r <= RSI_OS:
        score += min(30, (RSI_OS - r) * 1.0)
        dir_hint = "BUY"
    elif r >= RSI_OB:
        score += min(30, (r - RSI_OB) * 1.0)
        dir_hint = "SELL"
    tr = "bull" if fast > slow else "bear"
    if (dir_hint == "BUY" and tr == "bull") or (dir_hint == "SELL" and tr == "bear"):
        score += 35
    else:
        score -= 10
    if patt == "bullish" and dir_hint == "BUY":
        score += 20
    elif patt == "bearish" and dir_hint == "SELL":
        score += 20
    # quick vol check using tick_count (fallback)
    vol_ok = tick_count[sym] >= 3
    if vol_ok:
        score += 15
    score = max(0, min(100, score))
    return {"r": r, "fast": fast, "slow": slow, "pattern": patt, "score": score, "dir": dir_hint, "vol_ok": vol_ok}

def winrate(sym):
    d = wl.get(sym, {"win":0,"loss":0})
    tot = d["win"] + d["loss"]
    return (d["win"]/tot*100) if tot>0 else 0.0

# ---------- WS handlers (with active_symbols) ----------
pending_active_request = False

def ws_on_open(ws):
    try:
        ws.send(json.dumps({"authorize": DERIV_TOKEN}))
    except Exception as e:
        send_telegram(f"Authorize send error: {e}")

def ws_on_message(ws, raw):
    global authorized, active_symbol_list, available_symbol_set, symbol_map, pending_active_request, subscribed_symbols
    try:
        msg = json.loads(raw)
    except Exception:
        return

    # handle errors quickly
    if "error" in msg:
        err = msg["error"].get("message") if isinstance(msg["error"], dict) else str(msg["error"])
        # echo_req might show which request caused it
        echo = msg.get("echo_req", {})
        sym = echo.get("ticks") or echo.get("candles") or echo.get("active_symbols") or "?"
        send_telegram(f"❌ {sym} Error: {err}")
        return

    mtype = msg.get("msg_type")

    # authorize response
    if mtype == "authorize":
        authorized = True
        send_telegram("✅ D-SmartTrader authorized with Deriv. Fetching active symbols…")
        # request active symbols (brief)
        try:
            # ask for brief list of active symbols for product_type basic (this is the recommended flow)
            ws.send(json.dumps({"active_symbols": "brief", "product_type": "basic"}))
            pending_active_request = True
        except Exception as e:
            send_telegram(f"Active symbols request failed: {e}")
        return

    # active_symbols response (list)
    if mtype == "active_symbols" and isinstance(msg.get("active_symbols"), list):
        active_symbol_list = msg["active_symbols"]
        available_symbol_set = set()
        for item in active_symbol_list:
            # item typically has {symbol: "frxEURUSD", display_name: "...", market: "...", ...}
            sym = item.get("symbol") or item.get("market") or None
            if sym:
                available_symbol_set.add(sym)
        # build tolerant mapping for requested symbols
        found = []
        not_found = []
        for req in REQUESTED_SYMBOLS:
            match = best_match(req, available_symbol_set)
            if match:
                symbol_map[req] = match
                found.append((req, match))
            else:
                not_found.append(req)
        # report results
        msg_lines = ["🔎 Active symbols retrieved."]
        if found:
            msg_lines.append("✅ Found / mapped:")
            for r, m in found:
                msg_lines.append(f"  • {r} -> {m}")
        if not_found:
            msg_lines.append("❌ Not available on this account / not found:")
            for nf in not_found:
                msg_lines.append(f"  • {nf}")
        send_telegram("\n".join(msg_lines))
        pending_active_request = False

        # now subscribe only to mapped symbols
        for req, actual in symbol_map.items():
            if actual in subscribed_symbols:
                continue
            try:
                # subscribe candles 1m, candles 5m, ticks
                ws.send(json.dumps({"candles": actual, "granularity": 60, "subscribe": 1}))
                ws.send(json.dumps({"candles": actual, "granularity": 300, "subscribe": 1}))
                ws.send(json.dumps({"ticks": actual, "subscribe": 1}))
                subscribed_symbols.add(actual)
                time.sleep(0.1)
            except Exception as e:
                send_telegram(f"Subscribe error for {actual}: {e}")
        return

    # candles initial list (some responses use msg_type 'candles' with 'candles' array)
    if mtype == "candles" and isinstance(msg.get("candles"), list):
        sym = msg.get("echo_req", {}).get("candles") or msg.get("symbol")
        gran = msg.get("echo_req", {}).get("granularity", 60)
        out = candles_1m if int(gran)==60 else candles_5m
        for c in msg["candles"]:
            out[sym].append({
                "open": float(c["open"]), "high": float(c["high"]),
                "low": float(c["low"]), "close": float(c["close"]),
                "volume": float(c.get("volume", 0)), "epoch": int(c.get("epoch", time.time()))
            })
        return

    # streaming ohlc
    if mtype == "ohlc" and "ohlc" in msg:
        sym = msg.get("echo_req", {}).get("candles") or msg.get("symbol")
        gran = int(msg.get("echo_req", {}).get("granularity", 60))
        o = msg["ohlc"]
        candle = {
            "open": float(o["open"]), "high": float(o["high"]),
            "low": float(o["low"]), "close": float(o["close"]),
            "volume": float(o.get("volume", 0)), "epoch": int(o.get("epoch", time.time()))
        }
        if gran == 300:
            candles_5m[sym].append(candle)
        else:
            candles_1m[sym].append(candle)
        return

    # tick stream
    if mtype == "tick" and "tick" in msg:
        sym = msg["tick"]["symbol"]
        price = float(msg["tick"]["quote"])
        epoch = int(msg["tick"].get("epoch", time.time()))
        # minute bucket
        mb = epoch // 60
        if mb != last_min_bucket[sym]:
            tick_count[sym] = 0
            last_min_bucket[sym] = mb
        tick_count[sym] += 1

        # check open TP/SL
        if sym in open_signal:
            # check price vs TP/SL
            sig = open_signal[sym]
            dirn = sig["direction"]
            if dirn == "BUY" and price >= sig["tp"]:
                wl[sym]["win"] += 1
                send_telegram(f"✅ `{sym}` BUY TP hit @ `{price}` — WinRate {round(winrate(sym),2)}%")
                del open_signal[sym]; persist_state()
            elif dirn == "BUY" and price <= sig["sl"]:
                wl[sym]["loss"] += 1
                send_telegram(f"❌ `{sym}` BUY SL hit @ `{price}` — WinRate {round(winrate(sym),2)}%")
                del open_signal[sym]; persist_state()
            elif dirn == "SELL" and price <= sig["tp"]:
                wl[sym]["win"] += 1
                send_telegram(f"✅ `{sym}` SELL TP hit @ `{price}` — WinRate {round(winrate(sym),2)}%")
                del open_signal[sym]; persist_state()
            elif dirn == "SELL" and price >= sig["sl"]:
                wl[sym]["loss"] += 1
                send_telegram(f"❌ `{sym}` SELL SL hit @ `{price}` — WinRate {round(winrate(sym),2)}%")
                del open_signal[sym]; persist_state()

        # attempt to evaluate new signal only for symbols mapped from user's requested list
        # find requested key for this 'sym' if any
        requested_key = None
        for rk, act in symbol_map.items():
            if act == sym:
                requested_key = rk
                break
        if not requested_key:
            return

        # evaluation (score and open if passes)
        info = build_confidence(sym)
        if not info:
            return
        conf = info["score"]
        direction = None
        if info["r"] <= RSI_OS and info["fast"] > info["slow"]:
            direction = "BUY"
        elif info["r"] >= RSI_OB and info["fast"] < info["slow"]:
            direction = "SELL"

        min_conf = STRICT_MIN_CONF if strict_mode else SAFE_MIN_CONF
        now_ts = int(time.time())
        if direction and conf >= min_conf and (now_ts - last_signal_ts[sym] > 30) and sym not in open_signal:
            # compute tp/sl
            if direction == "BUY":
                tp = price * (1 + TP_PCT); sl = price * (1 - SL_PCT)
            else:
                tp = price * (1 - TP_PCT); sl = price * (1 + SL_PCT)
            with lock:
                open_signal[sym] = {"direction": direction, "entry": price, "tp": tp, "sl": sl, "time": now_ts, "confidence": conf}
                last_signal_ts[sym] = now_ts
                persist_state()
            send_telegram(
                f"📡 *D-SmartTrader* Signal\nPair: `{sym}`\nDirection: *{direction}*\nEntry: `{round(price,6)}`\nTP: `{round(tp,6)}` | SL: `{round(sl,6)}`\nConfidence: *{int(conf)}%*  WinRate: {round(winrate(sym),2)}%\nMode: {'STRICT' if strict_mode else 'SAFE'}"
            )

# ---------- WS error/close ----------
def ws_on_error(ws, err):
    send_telegram(f"⚠️ WebSocket error: {err}")

def ws_on_close(ws, a=None, b=None):
    send_telegram("🔌 D-SmartTrader disconnected (will reconnect).")

def ws_runner():
    while not stop_flag:
        try:
            ws = websocket.WebSocketApp(
                WS_URL,
                on_open=ws_on_open,
                on_message=ws_on_message,
                on_error=ws_on_error,
                on_close=ws_on_close
            )
            ws.run_forever(ping_interval=20, ping_timeout=10)
        except Exception as e:
            send_telegram(f"WS loop exception: {e}")
        time.sleep(2)

# ---------- Telegram poller ----------
tg_last = 0
def telegram_poller():
    global tg_last, strict_mode
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("[TG] Not configured; poller disabled")
        return
    send_telegram("🤖 D-SmartTrader (fixed) starting — fetching active symbols…")
    while not stop_flag:
        try:
            res = requests.get(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates",
                params={"offset": tg_last+1, "timeout": 20}
            ).json()
            if not res.get("ok"):
                time.sleep(2); continue
            for upd in res.get("result", []):
                tg_last = upd["update_id"]
                msg = upd.get("message") or {}
                cid = str(msg.get("chat", {}).get("id", ""))
                if cid != str(TELEGRAM_CHAT_ID):
                    continue
                txt = (msg.get("text") or "").strip().lower()
                if txt == "/strict on":
                    strict_mode = True; send_telegram("🔒 STRICT mode ON")
                elif txt == "/strict off":
                    strict_mode = False; send_telegram("🟢 SAFE mode ON")
                elif txt == "/status":
                    lines = [f"Mode: {'STRICT' if strict_mode else 'SAFE'}"]
                    for req, act in symbol_map.items():
                        lines.append(f"{req} -> {act}  W/L: {wl[act]['win']}/{wl[act]['loss']}  WR: {round(winrate(act),1)}%")
                    if open_signal:
                        lines.append("Open:")
                        for s, sig in open_signal.items():
                            lines.append(f"{s} {sig['direction']} @ {round(sig['entry'],6)} TP {round(sig['tp'],6)} SL {round(sig['sl'],6)} Conf {sig.get('confidence',0)}%")
                    send_telegram("\n".join(lines))
                elif txt == "/symbols":
                    send_telegram("Requested symbols:\n" + ", ".join(REQUESTED_SYMBOLS))
        except Exception as e:
            print("[TG] poll err:", e)
            time.sleep(2)

# ---------- main ----------
def main():
    missing = []
    if not DERIV_TOKEN: missing.append("DERIV_TOKEN")
    if not TELEGRAM_TOKEN: missing.append("TELEGRAM_BOT_TOKEN")
    if not TELEGRAM_CHAT_ID: missing.append("TELEGRAM_CHAT_ID")
    if missing:
        print("Missing env:", missing)
        raise SystemExit(1)

    load_state()
    t1 = threading.Thread(target=ws_runner, daemon=True)
    t2 = threading.Thread(target=telegram_poller, daemon=True)
    t1.start(); t2.start()
    try:
        while True:
            persist_state()
            time.sleep(30)
    except KeyboardInterrupt:
        print("Stopping")
        persist_state()

if __name__ == "__main__":
    main()

# Indicator settings
RSI_PERIOD       = 14
MA_FAST_1M       = 14        # 1m MA
MA_SLOW_5M       = 50        # 5m MA for trend
RSI_OB           = 70
RSI_OS           = 30

# Risk targets (percent of price)
TP_PCT           = 0.004     # 0.4%
SL_PCT           = 0.0025    # 0.25%

# Confidence gates
CONFIRM_VOL_MULT = 1.2       # Volume/tick activity threshold vs avg
SAFE_MIN_CONF    = 60        # in SAFE mode, only send if >= this confidence
STRICT_MIN_CONF  = 85        # in STRICT mode, only send if >= this confidence

# State / persistence
STATE_FILE       = "d_smarttrader_state.json"
WS_URL           = f"wss://ws.derivws.com/websockets/v3?app_id={DERIV_APP_ID}"

# ========= RUNTIME STATE =========
authorized         = False
strict_mode        = False   # SAFE by default; toggle via /strict on|off
stop_flag          = False

# deques of recent candles per symbol (1m & 5m)
candles_1m = {s: deque(maxlen=300) for s in SYMBOLS}  # dicts: {open,high,low,close,volume,epoch}
candles_5m = {s: deque(maxlen=300) for s in SYMBOLS}
# simple per-minute "volume" proxy (ticks counted within the minute)
tick_count_this_min = defaultdict(int)
last_minute_epoch   = defaultdict(lambda: 0)

# open trade per symbol (only one at a time to avoid flip-flopping)
open_signal = {}   # sym -> dict(direction, entry, tp, sl, t_open)

# win/loss counters
wl = defaultdict(lambda: {"win": 0, "loss": 0})

# per-symbol last signal timestamp (anti-spam)
last_signal_ts = defaultdict(lambda: 0)

# Telegram polling
tg_last_update_id = 0

lock = threading.Lock()

# ========= HELPERS =========
def send_telegram(text):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("[TG] Not configured")
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            data={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "Markdown"}
        )
    except Exception as e:
        print("[TG] send error:", e)

def load_state():
    try:
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE, "r") as f:
                data = json.load(f)
                # restore wl and open_signal
                if "wl" in data:
                    for k,v in data["wl"].items():
                        wl[k] = v
                if "open_signal" in data:
                    for k,v in data["open_signal"].items():
                        open_signal[k] = v
    except Exception as e:
        print("load_state error:", e)

def persist_state():
    try:
        with open(STATE_FILE, "w") as f:
            json.dump({"wl": wl, "open_signal": open_signal}, f, indent=2)
    except Exception as e:
        print("persist_state error:", e)

def winrate(sym):
    w = wl[sym]["win"]
    l = wl[sym]["loss"]
    tot = w + l
    return (w / tot * 100) if tot > 0 else 0.0

# ========= INDICATORS =========
def sma(vals, n):
    if len(vals) < n:
        return None
    return sum(vals[-n:]) / n

def rsi(vals, n=14):
    if len(vals) < n + 1:
        return None
    gains = 0.0
    losses = 0.0
    for i in range(-n, 0):
        diff = vals[i] - vals[i - 1]
        if diff >= 0:
            gains += diff
        else:
            losses += -diff
    if losses == 0:
        return 100.0
    rs = gains / losses
    return 100 - (100 / (1 + rs))

def engulfing(last2):
    if len(last2) < 2:
        return None
    a, b = last2[-2], last2[-1]
    # bullish engulfing
    if a["close"] < a["open"] and b["close"] > b["open"] and b["close"] > a["open"] and b["open"] < a["close"]:
        return "bullish"
    # bearish engulfing
    if a["close"] > a["open"] and b["close"] < b["open"] and b["open"] > a["close"] and b["close"] < a["open"]:
        return "bearish"
    return None

def minute_bucket(epoch):
    return int(epoch // 60)

# ========= SIGNAL ENGINE =========
def trend_ok(sym):
    c1 = candles_1m[sym]
    c5 = candles_5m[sym]
    if len(c1) < max(RSI_PERIOD + 1, MA_FAST_1M) or len(c5) < MA_SLOW_5M:
        return None
    closes1 = [c["close"] for c in c1]
    closes5 = [c["close"] for c in c5]
    fast = sma(closes1, MA_FAST_1M)
    slow = sma(closes5, MA_SLOW_5M)
    if fast is None or slow is None:
        return None
    if fast > slow:
        return "bull"
    elif fast < slow:
        return "bear"
    else:
        return None

def volume_ok(sym):
    c1 = candles_1m[sym]
    if len(c1) < RSI_PERIOD + 5:
        return False
    last = c1[-1]
    # prefer candle volume if present, else tick proxy
    last_vol = last.get("volume", 0) or tick_count_this_min[sym]
    vols = []
    for c in c1[-(RSI_PERIOD+5):]:
        v = c.get("volume", 0)
        if v:
            vols.append(v)
    if vols:
        avg = sum(vols)/len(vols)
        return last_vol > (avg * CONFIRM_VOL_MULT)
    else:
        # fallback: require at least 3 ticks in current minute
        return tick_count_this_min[sym] >= 3

def build_confidence(sym, price):
    """
    0..100 score based on: RSI distance, trend alignment, engulfing pattern, volume spike.
    """
    c1 = candles_1m[sym]
    c5 = candles_5m[sym]
    if len(c1) < max(RSI_PERIOD + 1, MA_FAST_1M) or len(c5) < MA_SLOW_5M:
        return None, None, None, None, 0

    closes1 = [c["close"] for c in c1]
    closes5 = [c["close"] for c in c5]
    r = rsi(closes1, RSI_PERIOD)
    fast = sma(closes1, MA_FAST_1M)
    slow = sma(closes5, MA_SLOW_5M)
    patt = engulfing(c1[-2:])
    vol_ok = volume_ok(sym)

    dir_hint = None
    score = 0

    if r is None or fast is None or slow is None:
        return None, None, None, None, 0

    # RSI component
    if r <= RSI_OS:
        score += min(30, (RSI_OS - r) * 1.0)  # deeper oversold = more score
        dir_hint = "BUY"
    elif r >= RSI_OB:
        score += min(30, (r - RSI_OB) * 1.0)
        dir_hint = "SELL"

    # Trend component (must align)
    tr = "bull" if fast > slow else "bear"
    if (dir_hint == "BUY" and tr == "bull") or (dir_hint == "SELL" and tr == "bear"):
        score += 35
    else:
        score -= 10  # misaligned trend

    # Candlestick pattern
    if patt == "bullish" and dir_hint == "BUY":
        score += 20
    elif patt == "bearish" and dir_hint == "SELL":
        score += 20

    # Volume spike
    if vol_ok:
        score += 15

    # clamp
    score = max(0, min(100, score))

    return r, fast, slow, patt, score

def maybe_signal(sym, price, now_ts):
    global strict_mode
    r, fast, slow, patt, conf = build_confidence(sym, price)
    if r is None:
        return

    # Gate by mode
    min_conf = STRICT_MIN_CONF if strict_mode else SAFE_MIN_CONF
    if conf < min_conf:
        return

    # anti-spam / duplicate flip
    if now_ts - last_signal_ts[sym] < 30:
        return
    if sym in open_signal:
        # don't open another while one is open
        return

    # Decide direction from RSI/trend/pattern outcome
    direction = "BUY" if r <= RSI_OS and fast > slow else "SELL" if r >= RSI_OB and fast < slow else None
    if direction is None:
        return

    # compute TP/SL
    if direction == "BUY":
        tp = price * (1 + TP_PCT)
        sl = price * (1 - SL_PCT)
    else:
        tp = price * (1 - TP_PCT)
        sl = price * (1 + SL_PCT)

    with lock:
        open_signal[sym] = {
            "direction": direction,
            "entry": price,
            "tp": tp,
            "sl": sl,
            "t_open": now_ts,
            "confidence": conf,
            "rsi": r,
            "ma_fast": fast,
            "ma_slow": slow,
            "pattern": patt
        }
        last_signal_ts[sym] = now_ts
        persist_state()

    # Alert
    msg = (
        f"📡 *D-SmartTrader* Signal\n"
        f"Pair: `{sym}`\n"
        f"Direction: *{direction}*\n"
        f"Entry: `{round(price, 6)}`\n"
        f"TP: `{round(tp, 6)}`  |  SL: `{round(sl, 6)}`\n"
        f"RSI: {round(r,2)} | MA(1m): {round(fast,6)} vs MA(5m): {round(slow,6)}\n"
        f"Pattern: {patt or 'None'}\n"
        f"Confidence: *{int(conf)}%*\n"
        f"WinRate: {round(winrate(sym),2)}%\n"
        f"Mode: {'STRICT' if strict_mode else 'SAFE'}"
    )
    send_telegram(msg)

def check_tp_sl(sym, price):
    if sym not in open_signal:
        return
    sig = open_signal[sym]
    direction = sig["direction"]
    tp, sl = sig["tp"], sig["sl"]

    hit = None
    if direction == "BUY":
        if price >= tp:
            hit = "TP"
        elif price <= sl:
            hit = "SL"
    else:
        if price <= tp:
            hit = "TP"
        elif price >= sl:
            hit = "SL"

    if hit:
        with lock:
            if hit == "TP":
                wl[sym]["win"] += 1
                outcome = "✅ TP hit"
            else:
                wl[sym]["loss"] += 1
                outcome = "❌ SL hit"
            del open_signal[sym]
            persist_state()

        send_telegram(
            f"{outcome} on `{sym}` @ `{round(price,6)}` — WinRate now {round(winrate(sym),2)}%"
        )

# ========= TELEGRAM COMMANDS (polling) =========
def telegram_poller():
    global tg_last_update_id, strict_mode
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("[TG] Poller disabled (no token/chat)")
        return
    send_telegram(f"🤖 D-SmartTrader is starting ({'STRICT' if strict_mode else 'SAFE'} mode)…")
    while not stop_flag:
        try:
            resp = requests.get(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates",
                params={"timeout": 25, "offset": tg_last_update_id + 1}
            ).json()
            if not resp.get("ok"):
                time.sleep(2)
                continue
            for upd in resp.get("result", []):
                tg_last_update_id = upd["update_id"]
                msg = upd.get("message") or {}
                chat_id = str(msg.get("chat", {}).get("id", ""))
                if chat_id != str(TELEGRAM_CHAT_ID):
                    continue
                text = (msg.get("text") or "").strip().lower()

                if text == "/strict on":
                    strict_mode = True
                    send_telegram("🔒 STRICT mode *ON* (very selective, highest confidence).")
                elif text == "/strict off":
                    strict_mode = False
                    send_telegram("🟢 SAFE mode *ON* (selective but more signals).")
                elif text == "/status":
                    lines = [f"Mode: {'STRICT' if strict_mode else 'SAFE'}"]
                    for s in SYMBOLS:
                        lines.append(f"{s}: {wl[s]['win']}W/{wl[s]['loss']}L  WinRate {round(winrate(s),1)}%")
                    if open_signal:
                        lines.append("Open trades:")
                        for s, sig in open_signal.items():
                            lines.append(f"• {s} {sig['direction']} @ {round(sig['entry'],6)}  TP {round(sig['tp'],6)}  SL {round(sig['sl'],6)}  Conf {sig['confidence']}%")
                    send_telegram("\n".join(lines))
                elif text == "/symbols":
                    send_telegram("Tracking:\n" + ", ".join(SYMBOLS))
        except Exception as e:
            print("[TG] poll error:", e)
            time.sleep(3)

# ========= DERIV WS =========
def ws_on_open(ws):
    try:
        ws.send(json.dumps({"authorize": DERIV_TOKEN}))
    except Exception as e:
        send_telegram(f"Authorize send error: {e}")

def ws_on_message(ws, raw):
    global authorized
    try:
        msg = json.loads(raw)
    except Exception:
        return

    # handle generic errors
    if "error" in msg:
        em = msg["error"].get("message") if isinstance(msg["error"], dict) else str(msg["error"])
        # try to include symbol if present
        sym = msg.get("echo_req", {}).get("ticks") or msg.get("echo_req", {}).get("candles") or "?"
        send_telegram(f"❌ {sym} Error: {em}")
        return

    mtype = msg.get("msg_type")

    # authorize
    if mtype == "authorize":
        authorized = True
        send_telegram("✅ D-SmartTrader authorized with Deriv. Subscribing feeds…")
        # subscribe after authorize
        for s in SYMBOLS:
            try:
                # 1m candles
                ws.send(json.dumps({"candles": s, "granularity": 60, "subscribe": 1}))
                # 5m candles
                ws.send(json.dumps({"candles": s, "granularity": 300, "subscribe": 1}))
                # ticks (for price + TP/SL checks)
                ws.send(json.dumps({"ticks": s, "subscribe": 1}))
            except Exception as e:
                send_telegram(f"Sub error {s}: {e}")
        return

    # initial candles (list)
    if mtype == "candles" and isinstance(msg.get("candles"), list):
        sym = msg.get("echo_req", {}).get("candles")
        gran = msg.get("echo_req", {}).get("granularity", 60)
        out = candles_1m if gran == 60 else candles_5m
        for c in msg["candles"]:
            out[sym].append({
                "open": float(c["open"]),
                "high": float(c["high"]),
                "low": float(c["low"]),
                "close": float(c["close"]),
                "volume": float(c.get("volume", 0)),
                "epoch": int(c.get("epoch", time.time()))
            })
        return

    # streaming ohlc (single)
    if mtype == "ohlc" and "ohlc" in msg:
        sym = msg.get("echo_req", {}).get("candles") or msg.get("symbol")
        gran = msg.get("echo_req", {}).get("granularity", 60)
        o = msg["ohlc"]
        candle = {
            "open": float(o["open"]),
            "high": float(o["high"]),
            "low": float(o["low"]),
            "close": float(o["close"]),
            "volume": float(o.get("volume", 0)),
            "epoch": int(o.get("epoch", time.time()))
        }
        if gran == 300:
            candles_5m[sym].append(candle)
        else:
            candles_1m[sym].append(candle)
        return

    # tick stream
    if mtype == "tick" and "tick" in msg:
        sym = msg["tick"]["symbol"]
        price = float(msg["tick"]["quote"])
        epoch = int(msg["tick"].get("epoch", time.time()))
        # track per-minute ticks (volume proxy)
        mb = minute_bucket(epoch)
        if mb != last_minute_epoch[sym]:
            # new minute -> reset
            tick_count_this_min[sym] = 0
            last_minute_epoch[sym] = mb
        tick_count_this_min[sym] += 1

        # TP/SL checks
        check_tp_sl(sym, price)

        # Only try signal evaluation when we have some candle context
        if len(candles_1m[sym]) >= max(RSI_PERIOD + 2, MA_FAST_1M) and len(candles_5m[sym]) >= MA_SLOW_5M:
            maybe_signal(sym, price, epoch)
        return

def ws_on_error(ws, err):
    send_telegram(f"⚠️ WebSocket error: {err}")

def ws_on_close(ws, a=None, b=None):
    send_telegram("🔌 D-SmartTrader disconnected. Reconnecting…")

def ws_thread():
    while not stop_flag:
        try:
            ws = websocket.WebSocketApp(
                WS_URL,
                on_open=ws_on_open,
                on_message=ws_on_message,
                on_error=ws_on_error,
                on_close=ws_on_close
            )
            ws.run_forever(ping_interval=20, ping_timeout=10)
        except Exception as e:
            send_telegram(f"WS loop error: {e}")
        time.sleep(3)

# ========= MAIN =========
def main():
    missing = []
    if not DERIV_TOKEN: missing.append("DERIV_TOKEN")
    if not TELEGRAM_TOKEN: missing.append("TELEGRAM_BOT_TOKEN")
    if not TELEGRAM_CHAT_ID: missing.append("TELEGRAM_CHAT_ID")
    if missing:
        print("Missing env:", missing)
        raise SystemExit(1)

    load_state()

    # start Telegram poller
    t_tg = threading.Thread(target=telegram_poller, daemon=True)
    t_tg.start()

    # start WS
    t_ws = threading.Thread(target=ws_thread, daemon=True)
    t_ws.start()

    # keep process alive for Railway
    while True:
        persist_state()
        time.sleep(30)

if __name__ == "__main__":
    main()
