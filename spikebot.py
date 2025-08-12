# main.py
# D-SmartTrader (Railway-ready)
# env vars required:
# DERIV_TOKEN, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
# optional DERIV_APP_ID (defaults to 1089)

import os
import json
import time
import threading
from collections import deque
import websocket
import requests

# ---------------- CONFIG (from env) ----------------
DERIV_TOKEN = os.getenv("DERIV_TOKEN")
DERIV_APP_ID = os.getenv("DERIV_APP_ID", "1089")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

if not DERIV_TOKEN or not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
    print("Missing required environment variables. Set DERIV_TOKEN, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID.")
    raise SystemExit(1)

# Choose symbols (no Boom/Crash)
SYMBOLS = ["R_75", "R_100", "frxEURUSD", "frxUSDJPY"]

# thresholds / params
RSI_PERIOD = 14
MA_SHORT = 14
MA_LONG = 50
RSI_OVERSOLD = 30
RSI_OVERBOUGHT = 70
VOLUME_MULTIPLIER = 1.2
TP_PCT = 0.004   # 0.4%
SL_PCT = 0.0025  # 0.25%
CONFIDENCE_THRESHOLD = 75.0  # only send signals >= this confidence

STATE_FILE = "d_smarttrader_state.json"
WS_URL = f"wss://ws.derivws.com/websockets/v3?app_id={DERIV_APP_ID}"

# runtime storage
candles_1m = {s: deque(maxlen=500) for s in SYMBOLS}
candles_5m = {s: deque(maxlen=500) for s in SYMBOLS}
tick_count_1m = {s: 0 for s in SYMBOLS}
open_signals = {}   # symbol -> {direction, entry, tp, sl, time, confidence}
results = {}        # symbol -> {"win":int, "loss":int}

lock = threading.Lock()

# ---------------- persistence ----------------
def load_state():
    global open_signals, results
    try:
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE, "r") as f:
                data = json.load(f)
                open_signals = data.get("open_signals", {})
                results = data.get("results", {})
                # ensure symbols keys exist
                for s in SYMBOLS:
                    results.setdefault(s, {"win": 0, "loss": 0})
    except Exception as e:
        print("Load state error:", e)

def save_state():
    try:
        with open(STATE_FILE, "w") as f:
            json.dump({"open_signals": open_signals, "results": results}, f, indent=2)
    except Exception as e:
        print("Save state error:", e)

# ---------------- Telegram ----------------
def send_telegram(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "Markdown"}
    try:
        requests.post(url, data=payload, timeout=8)
    except Exception as e:
        print("Telegram error:", e)

# ---------------- indicators ----------------
def sma(values, period):
    if len(values) < period:
        return None
    return sum(values[-period:]) / period

def rsi(values, period=14):
    if len(values) < period + 1:
        return None
    gains = 0.0
    losses = 0.0
    for i in range(-period, 0):
        diff = values[i] - values[i - 1]
        if diff >= 0:
            gains += diff
        else:
            losses += -diff
    avg_gain = gains / period
    avg_loss = losses / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))

def detect_engulfing(last_two_candles):
    # expects a list of two candle dicts: [prev, last]
    if len(last_two_candles) < 2:
        return None
    prev = last_two_candles[-2]
    last = last_two_candles[-1]
    # bullish engulfing
    if prev["close"] < prev["open"] and last["close"] > last["open"] and last["close"] > prev["open"] and last["open"] < prev["close"]:
        return "bullish"
    # bearish engulfing
    if prev["close"] > prev["open"] and last["close"] < last["open"] and last["open"] > prev["close"] and last["close"] < prev["open"]:
        return "bearish"
    return None

# confidence scoring
def compute_confidence(symbol, rsi_val, ma1, ma5, pattern, vol_ok):
    """
    We combine factors into a percent confidence.
    Weights (example):
      RSI extremeness: up to 40 pts
      MA trend agreement: 25 pts
      Candlestick pattern: 20 pts
      Volume strength: 15 pts
    """
    score = 0.0
    # RSI: closer to extreme adds more
    if rsi_val is not None:
        if rsi_val <= RSI_OVERSOLD:
            # for buy: lower rsi -> stronger
            score += (RSI_OVERSOLD - rsi_val) / RSI_OVERSOLD * 40
        elif rsi_val >= RSI_OVERBOUGHT:
            score += (rsi_val - RSI_OVERBOUGHT) / (100 - RSI_OVERBOUGHT) * 40
    # MA trend
    if ma1 is not None and ma5 is not None:
        if ma1 > ma5:
            score += 25  # bullish trend
        else:
            score += 25  # bearish trend (same weight)
    # pattern
    if pattern is not None:
        score += 20
    # volume
    if vol_ok:
        score += 15
    return min(100.0, round(score, 2))

# ---------------- signal decisions ----------------
def evaluate_strict_signal(symbol):
    # need enough candles
    c1 = list(candles_1m[symbol])
    c5 = list(candles_5m[symbol])
    if len(c1) < max(RSI_PERIOD + 1, MA_SHORT) or len(c5) < MA_LONG:
        return None
    closes1 = [c["close"] for c in c1]
    closes5 = [c["close"] for c in c5]
    rsi_val = rsi(closes1, RSI_PERIOD)
    ma1 = sma(closes1, MA_SHORT)
    ma5 = sma(closes5, MA_LONG)
    # pattern on last two closed 1m candles
    pattern = None
    if len(c1) >= 2:
        pattern = detect_engulfing(c1[-2:])
    # volume check: use candle 'volume' if exists else tick_count
    last_vol = c1[-1].get("volume", 0) or tick_count_1m[symbol]
    vol_list = [c.get("volume", 0) for c in c1[-(RSI_PERIOD + 5):]]
    nonzero = [v for v in vol_list if v]
    avg_vol = (sum(nonzero) / len(nonzero)) if nonzero else None
    vol_ok = (avg_vol is not None and last_vol > avg_vol * VOLUME_MULTIPLIER) or (tick_count_1m[symbol] >= 3)
    # trend check
    if ma1 is None or ma5 is None or rsi_val is None:
        return None
    trend = "bull" if ma1 > ma5 else "bear"
    # candidate signal + compute confidence
    # BUY
    if rsi_val < RSI_OVERSOLD and trend == "bull" and pattern == "bullish" and vol_ok:
        conf = compute_confidence(symbol, rsi_val, ma1, ma5, pattern, vol_ok)
        return {"signal": "BUY", "rsi": rsi_val, "ma1": ma1, "ma5": ma5, "pattern": pattern, "conf": conf}
    # SELL
    if rsi_val > RSI_OVERBOUGHT and trend == "bear" and pattern == "bearish" and vol_ok:
        conf = compute_confidence(symbol, rsi_val, ma1, ma5, pattern, vol_ok)
        return {"signal": "SELL", "rsi": rsi_val, "ma1": ma1, "ma5": ma5, "pattern": pattern, "conf": conf}
    return None

def open_new_signal(symbol, candidate, entry_price):
    direction = candidate["signal"]
    conf = candidate["conf"]
    if conf < CONFIDENCE_THRESHOLD:
        return None
    if direction == "BUY":
        tp = entry_price * (1 + TP_PCT)
        sl = entry_price * (1 - SL_PCT)
    else:
        tp = entry_price * (1 - TP_PCT)
        sl = entry_price * (1 + SL_PCT)
    sig = {"direction": direction, "entry": entry_price, "tp": tp, "sl": sl, "time": int(time.time()), "conf": conf}
    with lock:
        # prevent flipping: only open if there is no open signal for symbol
        if symbol in open_signals:
            return None
        open_signals[symbol] = sig
        results.setdefault(symbol, {"win": 0, "loss": 0})
        save_state()
    # send telegram
    send_telegram(
        f"*D-SmartTrader* Signal\nPair: `{symbol}`\nDirection: *{direction}*  Confidence: *{conf}%*\nEntry: `{entry_price}`\nTP: `{round(tp,6)}` | SL: `{round(sl,6)}`\nRSI: {round(candidate.get('rsi',0),2)}  Pattern: {candidate.get('pattern','N/A')}"
    )
    return sig

def check_open_signals_on_price(symbol, price):
    with lock:
        sig = open_signals.get(symbol)
        if not sig:
            return
        direction = sig["direction"]
        tp = sig["tp"]
        sl = sig["sl"]
        if direction == "BUY":
            if price >= tp:
                results.setdefault(symbol, {"win": 0, "loss": 0})
                results[symbol]["win"] += 1
                send_telegram(f"✅ `{symbol}` BUY hit TP at `{price}` — WinRate: {compute_winrate(symbol)}%")
                del open_signals[symbol]
                save_state()
            elif price <= sl:
                results.setdefault(symbol, {"win": 0, "loss": 0})
                results[symbol]["loss"] += 1
                send_telegram(f"❌ `{symbol}` BUY hit SL at `{price}` — WinRate: {compute_winrate(symbol)}%")
                del open_signals[symbol]
                save_state()
        else:  # SELL
            if price <= tp:
                results.setdefault(symbol, {"win": 0, "loss": 0})
                results[symbol]["win"] += 1
                send_telegram(f"✅ `{symbol}` SELL hit TP at `{price}` — WinRate: {compute_winrate(symbol)}%")
                del open_signals[symbol]
                save_state()
            elif price >= sl:
                results.setdefault(symbol, {"win": 0, "loss": 0})
                results[symbol]["loss"] += 1
                send_telegram(f"❌ `{symbol}` SELL hit SL at `{price}` — WinRate: {compute_winrate(symbol)}%")
                del open_signals[symbol]
                save_state()

def compute_winrate(symbol):
    r = results.get(symbol, {"win": 0, "loss": 0})
    total = r["win"] + r["loss"]
    if total == 0:
        return 0.0
    return round(r["win"] / total * 100, 2)

# ---------------- WebSocket callbacks factory ----------------
def make_ws_handlers(symbol):
    """
    Return (on_open, on_message, on_error, on_close) functions bound to symbol
    """
    def on_open(ws):
        # send authorize; subscription happens after authorize response in on_message
        try:
            ws.send(json.dumps({"authorize": DERIV_TOKEN}))
        except Exception as e:
            send_telegram(f"⚠️ {symbol} on_open send error: {e}")

    def on_message(ws, message):
        try:
            msg = json.loads(message)
        except Exception:
            return
        # handle authorize response - msg_type 'authorize'
        if isinstance(msg, dict) and msg.get("msg_type") == "authorize":
            # authorize success
            send_telegram(f"✅ D-SmartTrader ({symbol}) authorized with Deriv.")
            # now subscribe to 1m candles, 5m candles, and ticks
            try:
                ws.send(json.dumps({"candles": symbol, "subscribe": 1, "granularity": 60}))
                ws.send(json.dumps({"candles": symbol, "subscribe": 1, "granularity": 300}))
                ws.send(json.dumps({"ticks": symbol}))
            except Exception as e:
                send_telegram(f"⚠️ {symbol} subscribe error: {e}")
            return

        # handle errors
        if "error" in msg:
            err = msg["error"].get("message") if isinstance(msg["error"], dict) else str(msg["error"])
            send_telegram(f"❌ {symbol} Error: {err}")
            return

        # handle candles array (initial snapshot)
        if "candles" in msg and isinstance(msg["candles"], list):
            for c in msg["candles"]:
                try:
                    candle = {
                        "open": float(c["open"]),
                        "high": float(c["high"]),
                        "low": float(c["low"]),
                        "close": float(c["close"]),
                        "volume": float(c.get("volume", 0)),
                        "epoch": int(c.get("epoch", time.time()))
                    }
                    # Deriv may send both granularities to same connection; append to 1m and 5m if present
                    # We'll append to 1m for safety; the 'granularity' may not be included
                    candles_1m[symbol].append(candle)
                except Exception:
                    continue
            return

        # handle ohlc (single candle update)
        if "ohlc" in msg:
            try:
                o = msg["ohlc"]
                candle = {
                    "open": float(o["open"]),
                    "high": float(o["high"]),
                    "low": float(o["low"]),
                    "close": float(o["close"]),
                    "volume": float(o.get("volume", 0)),
                    "epoch": int(o.get("epoch", time.time()))
                }
                gran = msg.get("granularity")
                if gran == 300:
                    candles_5m[symbol].append(candle)
                else:
                    candles_1m[symbol].append(candle)
                    # reset tick count for new 1m candle
                    tick_count_1m[symbol] = 0
            except Exception:
                pass
            return

        # handle tick
        if "tick" in msg:
            try:
                t = msg["tick"]
                price = float(t["quote"])
                # increment tick count
                tick_count_1m[symbol] += 1
                # check open signals for TP/SL
                check_open_signals_on_price(symbol, price)
                # Evaluate strict candidate using candle dataset
                candidate = evaluate_strict_signal(symbol)
                if candidate:
                    # Only open if no open signal exists (no flipping)
                    with lock:
                        if symbol in open_signals:
                            return
                    # use current tick price as entry
                    open_new_signal(symbol, candidate, price)
            except Exception:
                pass
            return

    def on_error(ws, error):
        send_telegram(f"⚠️ WebSocket Error ({symbol}): {error}")

    def on_close(ws, close_status_code, close_msg):
        send_telegram(f"🔌 D-SmartTrader ({symbol}) disconnected. Code: {close_status_code} Msg: {close_msg}")

    return on_open, on_message, on_error, on_close

# ---------------- runner for each symbol ----------------
def run_symbol(symbol):
    while True:
        try:
            on_open, on_message, on_error, on_close = make_ws_handlers(symbol)
            ws = websocket.WebSocketApp(
                WS_URL,
                on_open=on_open,
                on_message=on_message,
                on_error=on_error,
                on_close=on_close,
            )
            send_telegram(f"🔁 Starting connection for {symbol} ...")
            ws.run_forever()
        except Exception as e:
            send_telegram(f"⏳ Connection error for {symbol}, retrying: {e}")
            time.sleep(5)
        time.sleep(2)

# ---------------- start ----------------
def start_all():
    load_state()
    for s in SYMBOLS:
        results.setdefault(s, {"win": 0, "loss": 0})
    save_state()
    send_telegram("🚀 D-SmartTrader starting — strict-confirmation mode ON.")
    threads = []
    for sym in SYMBOLS:
        t = threading.Thread(target=run_symbol, args=(sym,), daemon=True)
        t.start()
        threads.append(t)
        time.sleep(1)
    # keep alive
    try:
        while True:
            time.sleep(30)
            save_state()
    except KeyboardInterrupt:
        save_state()
        send_telegram("🛑 D-SmartTrader stopped by user.")

if __name__ == "__main__":
    start_all()
