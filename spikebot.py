#!/usr/bin/env python3
# d_smarttrader.py
# D-SmartTrader: strict-confirmation signal bot (RSI + MA trend + Candlestick + Volume)
# Requires: websocket-client, requests
# Set environment variables: DERIV_TOKEN, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, optional DERIV_APP_ID

import os
import json
import time
import math
import threading
from collections import deque, defaultdict
import websocket
import requests

# ---------------- CONFIG ----------------
DERIV_TOKEN = os.getenv("DERIV_TOKEN")             # set on host
DERIV_APP_ID = os.getenv("DERIV_APP_ID", "1089")   # default public app id
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# Symbols to monitor (no Boom/Crash)
SYMBOLS = ["R_75", "R_100", "frxEURUSD", "frxUSDJPY"]

# Indicators / thresholds
RSI_PERIOD = 14                 # RSI lookback used for signal
MA_SHORT = 14                   # MA on 1m
MA_LONG = 50                    # MA on 5m (trend filter)
RSI_OVERSOLD = 30
RSI_OVERBOUGHT = 70
VOLUME_MULTIPLIER = 1.2         # last volume must be > avg_volume * VOLUME_MULTIPLIER
TP_PCT = 0.004                  # 0.4% take profit by default (adjust to your instrument)
SL_PCT = 0.0025                 # 0.25% stop loss by default
PERSIST_FILE = "d_smarttrader_state.json"

# WebSocket URL
WS_URL = f"wss://ws.derivws.com/websockets/v3?app_id={DERIV_APP_ID}"

# ---------------- UTIL ----------------
def send_telegram(text):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram not configured.")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": text})
    except Exception as e:
        print("Telegram send error:", e)

def persist_state(state):
    try:
        with open(PERSIST_FILE, "w") as f:
            json.dump(state, f, indent=2)
    except Exception as e:
        print("Persist error:", e)

def load_state():
    try:
        if os.path.exists(PERSIST_FILE):
            with open(PERSIST_FILE, "r") as f:
                return json.load(f)
    except Exception as e:
        print("Load state err:", e)
    return {"results": {}, "open_signals": {}}

# ---------------- INDICATORS (pure python) ----------------
def compute_sma(values, period):
    if len(values) < period:
        return None
    return sum(values[-period:]) / period

def compute_rsi(values, period=14):
    if len(values) < period + 1:
        return None
    gains = 0.0
    losses = 0.0
    for i in range(-period, 0):
        diff = values[i] - values[i-1]
        if diff >= 0:
            gains += diff
        else:
            losses += -diff
    avg_gain = gains / period
    avg_loss = losses / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    rsi = 100 - (100 / (1 + rs))
    return rsi

def detect_engulfing(candles):
    # candles: list of dicts with open, high, low, close (floats). We want last two completed candles.
    if len(candles) < 2:
        return None
    prev = candles[-2]
    last = candles[-1]
    # bullish engulfing: previous bearish, last bullish and last body engulfs previous body
    prev_body = abs(prev["close"] - prev["open"])
    last_body = abs(last["close"] - last["open"])
    if prev["close"] < prev["open"] and last["close"] > last["open"] and last["close"] > prev["open"] and last["open"] < prev["close"]:
        return "bullish"
    if prev["close"] > prev["open"] and last["close"] < last["open"] and last["open"] > prev["close"] and last["close"] < prev["open"]:
        return "bearish"
    return None

# ---------------- CORE BOT LOGIC ----------------
state = load_state()
results = state.get("results", {})          # per-symbol win/loss totals
open_signals = state.get("open_signals", {})  # symbol -> signal details

# runtime containers
candles_1m = {s: deque(maxlen=200) for s in SYMBOLS}    # store dicts {"open","high","low","close","volume","epoch"}
candles_5m = {s: deque(maxlen=200) for s in SYMBOLS}
tick_counts_1m = {s: 0 for s in SYMBOLS}                # simple volume proxy: tick counts per candle
last_candle_epoch_1m = {s: None for s in SYMBOLS}
last_candle_epoch_5m = {s: None for s in SYMBOLS}

lock = threading.Lock()

def should_send_strict_signal(symbol):
    """
    Strict confirmation:
    - RSI on 1m oversold/overbought
    - MA short on 1m in same direction as MA long on 5m (trend confirmation)
    - Engulfing candle on 1m matches direction
    - Volume strong (using 'volume' field if present, else tick count)
    """
    c1 = list(candles_1m[symbol])
    c5 = list(candles_5m[symbol])
    if len(c1) < max(RSI_PERIOD+1, MA_SHORT) or len(c5) < MA_LONG:
        return None

    closes_1m = [c["close"] for c in c1]
    closes_5m = [c["close"] for c in c5]

    rsi_val = compute_rsi(closes_1m, RSI_PERIOD)
    ma1 = compute_sma(closes_1m, MA_SHORT)
    ma5 = compute_sma(closes_5m, MA_LONG)

    if rsi_val is None or ma1 is None or ma5 is None:
        return None

    # trend: require 1m MA direction to match 5m trend
    trend = "bull" if ma1 > ma5 else "bear"

    # candle pattern on last closed 1m candle
    pattern = detect_engulfing(c1[-2:])  # last two completed -> ensure using actual structures

    # volume check
    last_vol = c1[-1].get("volume") or tick_counts_1m[symbol]
    avg_vol = None
    vol_list = [c.get("volume") or 0 for c in c1[-(RSI_PERIOD+5):]]
    if any(v for v in vol_list):
        nonzero = [v for v in vol_list if v]
        avg_vol = sum(nonzero)/len(nonzero) if nonzero else None

    vol_ok = False
    if avg_vol and last_vol:
        vol_ok = last_vol > (avg_vol * VOLUME_MULTIPLIER)
    else:
        # fallback: check tick_counts: require at least some ticks
        vol_ok = tick_counts_1m[symbol] >= 3

    # Strict rules:
    # BUY: rsi < oversold, trend bull, pattern bullish, volume strong
    if rsi_val < RSI_OVERSOLD and trend == "bull" and pattern == "bullish" and vol_ok:
        return {"signal":"BUY","rsi":rsi_val,"ma1":ma1,"ma5":ma5,"pattern":pattern}
    # SELL: rsi > overbought, trend bear, pattern bearish, volume strong
    if rsi_val > RSI_OVERBOUGHT and trend == "bear" and pattern == "bearish" and vol_ok:
        return {"signal":"SELL","rsi":rsi_val,"ma1":ma1,"ma5":ma5,"pattern":pattern}
    return None

def open_signal(symbol, direction, entry):
    # compute TP/SL levels
    if direction == "BUY":
        tp = entry * (1 + TP_PCT)
        sl = entry * (1 - SL_PCT)
    else:
        tp = entry * (1 - TP_PCT)
        sl = entry * (1 + SL_PCT)
    now = int(time.time())
    signal = {"symbol": symbol, "direction": direction, "entry": entry, "tp": tp, "sl": sl, "time": now}
    with lock:
        open_signals[symbol] = signal
        persist_state({"results": results, "open_signals": open_signals})
    # Send Telegram alert
    send_telegram(f"📡 *D-SmartTrader* Signal\nPair: `{symbol}`\nDirection: *{direction}*\nEntry: `{entry}`\nTP: `{round(tp,6)}` | SL: `{round(sl,6)}`\nRSI: {round(signal.get('rsi',0),2)}\nPattern: {signal.get('pattern','N/A')}\nWinRate: {round(win_rate(symbol),2)}%")
    return signal

def win_rate(symbol):
    data = results.get(symbol, {"win":0,"loss":0})
    total = data["win"] + data["loss"]
    if total == 0:
        return 0.0
    return (data["win"] / total) * 100

def check_open_signals(symbol, price):
    """Check if open signal for symbol hit TP/SL and update results"""
    with lock:
        sig = open_signals.get(symbol)
        if not sig:
            return
        direction = sig["direction"]
        tp = sig["tp"]
        sl = sig["sl"]
        if direction == "BUY":
            if price >= tp:
                # win
                results.setdefault(symbol, {"win":0,"loss":0})
                results[symbol]["win"] += 1
                send_telegram(f"✅ `{symbol}` BUY hit TP at `{price}`. WinRate: {round(win_rate(symbol),2)}%")
                del open_signals[symbol]
                persist_state({"results": results, "open_signals": open_signals})
            elif price <= sl:
                results.setdefault(symbol, {"win":0,"loss":0})
                results[symbol]["loss"] += 1
                send_telegram(f"❌ `{symbol}` BUY hit SL at `{price}`. WinRate: {round(win_rate(symbol),2)}%")
                del open_signals[symbol]
                persist_state({"results": results, "open_signals": open_signals})
        else:  # SELL
            if price <= tp:
                results.setdefault(symbol, {"win":0,"loss":0})
                results[symbol]["win"] += 1
                send_telegram(f"✅ `{symbol}` SELL hit TP at `{price}`. WinRate: {round(win_rate(symbol),2)}%")
                del open_signals[symbol]
                persist_state({"results": results, "open_signals": open_signals})
            elif price >= sl:
                results.setdefault(symbol, {"win":0,"loss":0})
                results[symbol]["loss"] += 1
                send_telegram(f"❌ `{symbol}` SELL hit SL at `{price}`. WinRate: {round(win_rate(symbol),2)}%")
                del open_signals[symbol]
                persist_state({"results": results, "open_signals": open_signals})

# ---------------- WebSocket handling ----------------
def authorize_and_subscribe(ws, symbol):
    # Called after authorize success
    # Subscribe to 1m candles and 5m candles for trend check + ticks (for quicker price)
    try:
        # candles 1m
        ws.send(json.dumps({
            "candles": symbol,
            "subscribe": 1,
            "granularity": 60
        }))
        # candles 5m
        ws.send(json.dumps({
            "candles": symbol,
            "subscribe": 1,
            "granularity": 300
        }))
        # ticks (subscribe to ticks too)
        ws.send(json.dumps({
            "ticks": symbol
        }))
    except Exception as e:
        print("subscribe error:", e)

def handle_message(msg, symbol):
    """
    msg is the parsed JSON message from Deriv.
    handle 'authorize' responses, 'candles', and 'tick' payloads.
    """
    # handle authorize response
    if isinstance(msg, dict) and msg.get("msg_type") == "authorize":
        # authorized; server responded
        send_telegram(f"✅ D-SmartTrader ({symbol}) authorized with Deriv.")
        return

    # handle error
    if "error" in msg:
        err = msg["error"].get("message") if isinstance(msg["error"], dict) else str(msg["error"])
        send_telegram(f"❌ {symbol} Error: {err}")
        return

    # candles responses structure: msg may contain "candles" (initial) or "candles" inside other keys.
    # We'll detect if 'candles' is present as key
    if "candles" in msg and isinstance(msg["candles"], list):
        # initial candles list
        for c in msg["candles"]:
            # Deriv candle fields might be strings; ensure floats
            candle = {
                "open": float(c["open"]),
                "high": float(c["high"]),
                "low": float(c["low"]),
                "close": float(c["close"]),
                "volume": float(c.get("volume", 0)),
                "epoch": int(c.get("epoch", time.time()))
            }
            # depending on granularity in message if present - but we don't always get it; assume 1m when subscribed first
            # we will push into both if appropriate later
            # For safety we append to 1m; during subscribe we will also get granularity-specific messages in practice
            candles_1m[symbol].append(candle)
        return

    # If message contains 'ohlc' or 'tick' structure
    if "ohlc" in msg:
        o = msg["ohlc"]
        candle = {
            "open": float(o["open"]),
            "high": float(o["high"]),
            "low": float(o["low"]),
            "close": float(o["close"]),
            "volume": float(o.get("volume", 0)),
            "epoch": int(o.get("epoch", time.time()))
        }
        # Determine granularity by 'granularity' field if available
        gran = msg.get("granularity") or msg.get("ohlc", {}).get("granularity")
        if gran == 300:
            candles_5m[symbol].append(candle)
            last_candle_epoch_5m[symbol] = candle["epoch"]
        else:
            candles_1m[symbol].append(candle)
            last_candle_epoch_1m[symbol] = candle["epoch"]
            # reset tick count for new 1m candle
            tick_counts_1m[symbol] = 0
        return

    if "tick" in msg:
        t = msg["tick"]
        # tick quote may be string; convert
        price = float(t["quote"])
        epoch = int(t.get("epoch", time.time()))
        # Append small structure to 1m candles list as partial candle if not fully provided
        # We'll increment tick count to approximate volume if candles lack volume
        tick_counts_1m[symbol] += 1
        # Check open signals for TP/SL
        check_open_signals(symbol, price)
        # We only evaluate signal when we have fresh closed 1m candle; but since we may not always get 'closed' candle event,
        # Evaluate using latest 1m candles when enough data present
        candidate = should_send_strict_signal(symbol)
        if candidate:
            # Make sure we do not have an open signal opposite direction already
            with lock:
                existing = open_signals.get(symbol)
            if existing:
                # If existing signal direction same as candidate, ignore (already open).
                if existing["direction"] == candidate["signal"]:
                    return
                else:
                    # Opposite direction open -> do NOT flip; skip
                    return
            # open strict signal
            # entry price use current tick price
            open_signal(symbol, candidate["signal"], price)
        return

# ---------------- Worker per symbol ----------------
def run_symbol(symbol):
    while True:
        try:
            ws = websocket.WebSocketApp(
                WS_URL,
                on_open=lambda ws: ws.send(json.dumps({"authorize": DERIV_TOKEN})),
                on_message=lambda ws, msg: handle_message(json.loads(msg), symbol),
                on_error=lambda ws, err: send_telegram(f"⚠️ WebSocket Error ({symbol}): {err}"),
                on_close=lambda ws: send_telegram(f"🔌 D-SmartTrader ({symbol}) disconnected.")
            )
            # We need to open in blocking mode: run_forever
            send_telegram(f"🔁 Starting connection for {symbol} ...")
            ws.run_forever()
        except Exception as e:
            send_telegram(f"⏳ Connection error for {symbol}, retrying: {e}")
            time.sleep(5)
        time.sleep(2)

# ---------------- Start all ----------------
def start_all():
    # Initialize persistent structures
    for sym in SYMBOLS:
        results.setdefault(sym, {"win":0,"loss":0})
    persist_state({"results": results, "open_signals": open_signals})
    send_telegram("🚀 D-SmartTrader starting — strict-confirmation mode ON.")
    threads = []
    for sym in SYMBOLS:
        t = threading.Thread(target=run_symbol, args=(sym,), daemon=True)
        t.start()
        threads.append(t)
        time.sleep(1)  # slight stagger
    # keep main alive
    try:
        while True:
            time.sleep(30)
            # persist periodically
            persist_state({"results": results, "open_signals": open_signals})
    except KeyboardInterrupt:
        send_telegram("🛑 D-SmartTrader stopping by user.")
        persist_state({"results": results, "open_signals": open_signals})

if __name__ == "__main__":
    # quick safety checks for tokens
    missing = []
    if not DERIV_TOKEN:
        missing.append("DERIV_TOKEN")
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        missing.append("TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID")
    if missing:
        print("Missing environment variables:", missing)
        raise SystemExit("Set required environment variables and restart.")

    start_all()
