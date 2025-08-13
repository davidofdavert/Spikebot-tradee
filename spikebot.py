#!/usr/bin/env python3
# D-SmartTrader — strict confirmation bot for Deriv (forex + volatility, no Boom/Crash)
# Env vars (Railway → Variables):
#   DERIV_TOKEN=...           (Your Deriv API token)
#   TELEGRAM_BOT_TOKEN=...    (BotFather token)
#   TELEGRAM_CHAT_ID=...      (Your chat/user ID)
# Optional:
#   DERIV_APP_ID=1089         (or your own app_id from Deriv)
#
# Requirements (requirements.txt):
#   websocket-client==1.8.0
#   requests==2.32.4

import os, json, time, math, threading
from collections import deque, defaultdict
import websocket
import requests

# ---------- ENV ----------
DERIV_TOKEN = os.getenv("DERIV_TOKEN")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
DERIV_APP_ID = os.getenv("DERIV_APP_ID", "1089")

if not DERIV_TOKEN or not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
    print("Missing env: DERIV_TOKEN / TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID")
    raise SystemExit(1)

WS_URL = f"wss://ws.derivws.com/websockets/v3?app_id={DERIV_APP_ID}"

# ---------- CONFIG ----------
SYMBOLS = [
    # Volatility Indices
    "R_10", "R_25", "R_50", "R_75", "R_100",

    # Gold (Metals)
    "frxXAUUSD",

    # Forex
    "frxEURUSD", "frxUSDJPY", "frxGBPUSD", "frxAUDUSD", "frxUSDCAD",

    # You can add more synthetic indices here if you need
]




RSI_PERIOD = 14
MA_SHORT_1M = 14
MA_LONG_5M  = 50
RSI_OVERSOLD = 30
RSI_OVERBOUGHT = 70
VOLUME_MULTIPLIER = 1.2

# TP/SL expressed as % of entry (works for both forex & synthetics; tune if you want)
TP_PCT = 0.0050    # 0.50%
SL_PCT = 0.0025    # 0.25%

PERSIST_FILE = "d_smarttrader_state.json"
SIGNAL_COOLDOWN_SEC = 120           # prevent rapid flip-flop per symbol
MIN_TICKS_PER_MINUTE = 3            # basic volume proxy if candle volume missing

# ---------- STATE ----------
state_lock = threading.Lock()
results = defaultdict(lambda: {"win": 0, "loss": 0})     # per-symbol win/loss totals
open_signals = {}                                        # symbol -> dict
last_signal_time = defaultdict(lambda: 0)                # cooldown per symbol
last_error_time = defaultdict(lambda: 0)                 # throttle error spam

# deques for market data
candles_1m = {s: deque(maxlen=400) for s in SYMBOLS}   # elements: dict(open,high,low,close,epoch,volume)
candles_5m = {s: deque(maxlen=400) for s in SYMBOLS}
tick_counts_1m = defaultdict(int)                      # volume proxy

# ---------- UTIL ----------
def tg_send(text):
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            data={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "Markdown"}
        )
    except Exception as e:
        print("Telegram error:", e)

def persist():
    try:
        with open(PERSIST_FILE, "w") as f:
            json.dump({"results": results, "open_signals": open_signals}, f)
    except Exception as e:
        print("Persist error:", e)

def load_persist():
    try:
        if os.path.exists(PERSIST_FILE):
            with open(PERSIST_FILE, "r") as f:
                saved = json.load(f)
                for k, v in saved.get("results", {}).items():
                    results[k] = v
                for k, v in saved.get("open_signals", {}).items():
                    open_signals[k] = v
    except Exception as e:
        print("Load state error:", e)

# ---------- INDICATORS ----------
def sma(vals, n):
    if len(vals) < n: return None
    return sum(vals[-n:]) / n

def rsi(vals, n=14):
    if len(vals) < n + 1: return None
    gains = 0.0
    losses = 0.0
    for i in range(-n, 0):
        diff = vals[i] - vals[i-1]
        if diff >= 0: gains += diff
        else: losses += -diff
    if losses == 0: return 100.0
    rs = gains / losses
    return 100 - (100 / (1 + rs))

def engulfing(last2):
    # expects last2 = [prev, last] each dict with open, close
    if len(last2) < 2: return None
    a, b = last2[-2], last2[-1]
    # bullish engulfing
    if a["close"] < a["open"] and b["close"] > b["open"] and b["close"] >= a["open"] and b["open"] <= a["close"]:
        return "bullish"
    # bearish engulfing
    if a["close"] > a["open"] and b["close"] < b["open"] and b["open"] >= a["close"] and b["close"] <= a["open"]:
        return "bearish"
    return None

def build_confidence(direction, rsi_val, trend_ok, pattern, vol_ratio):
    score = 50
    if direction == "BUY" and rsi_val is not None:
        # deeper oversold => more confidence
        boost = max(0.0, (RSI_OVERSOLD - min(RSI_OVERSOLD, rsi_val)) / max(1, RSI_OVERSOLD)) * 30.0
        score += boost
    if direction == "SELL" and rsi_val is not None:
        boost = max(0.0, (max(RSI_OVERBOUGHT, rsi_val) - RSI_OVERBOUGHT) / max(1, 100 - RSI_OVERBOUGHT)) * 30.0
        score += boost
    if trend_ok: score += 15
    if (direction == "BUY" and pattern == "bullish") or (direction == "SELL" and pattern == "bearish"):
        score += 10
    if vol_ratio is not None:
        score += min(15.0, max(0.0, (vol_ratio - 1.0) * 10.0))  # mild bump for >1.0x
    return round(max(0, min(100, score)), 1)

def symbol_winrate(sym):
    r = results[sym]
    total = r["win"] + r["loss"]
    return 0.0 if total == 0 else round((r["win"] / total) * 100.0, 2)

# ---------- SIGNAL LOGIC ----------
def maybe_signal(sym):
    c1 = list(candles_1m[sym])
    c5 = list(candles_5m[sym])
    if len(c1) < max(RSI_PERIOD + 1, MA_SHORT_1M + 1) or len(c5) < MA_LONG_5M + 1:
        return None

    closes1 = [c["close"] for c in c1]
    closes5 = [c["close"] for c in c5]

    rsi_val = rsi(closes1, RSI_PERIOD)
    ma1 = sma(closes1, MA_SHORT_1M)
    ma5 = sma(closes5, MA_LONG_5M)
    if rsi_val is None or ma1 is None or ma5 is None:
        return None

    trend_is_bull = ma1 > ma5
    trend_is_bear = ma1 < ma5
    patt = engulfing(c1[-2:])

    # volume (prefer candle volume if present; else tick count)
    last_vol = c1[-1].get("volume") or tick_counts_1m[sym]
    vols = [c.get("volume", 0) for c in c1[-(RSI_PERIOD+5):]]
    nonzero = [v for v in vols if v]
    avg_vol = (sum(nonzero)/len(nonzero)) if nonzero else None
    vol_ratio = (last_vol / avg_vol) if (avg_vol and last_vol) else None
    vol_ok = (vol_ratio is not None and vol_ratio >= VOLUME_MULTIPLIER) or (tick_counts_1m[sym] >= MIN_TICKS_PER_MINUTE)

    # strict confirmations
    if rsi_val < RSI_OVERSOLD and trend_is_bull and patt == "bullish" and vol_ok:
        direction = "BUY"
    elif rsi_val > RSI_OVERBOUGHT and trend_is_bear and patt == "bearish" and vol_ok:
        direction = "SELL"
    else:
        return None

    conf = build_confidence(direction, rsi_val, True, patt, vol_ratio)
    return {
        "direction": direction,
        "rsi": round(rsi_val, 2),
        "ma1": round(ma1, 6),
        "ma5": round(ma5, 6),
        "pattern": patt,
        "confidence": conf,
        "vol_ratio": round(vol_ratio, 2) if vol_ratio else None
    }

def open_trade(sym, direction, entry, details):
    if direction == "BUY":
        tp = entry * (1 + TP_PCT)
        sl = entry * (1 - SL_PCT)
    else:
        tp = entry * (1 - TP_PCT)
        sl = entry * (1 + SL_PCT)

    with state_lock:
        open_signals[sym] = {
            "symbol": sym,
            "direction": direction,
            "entry": entry,
            "tp": tp,
            "sl": sl,
            "time": int(time.time())
        }
        persist()

    msg = (
        f"📡 *D-SmartTrader* Signal\n"
        f"Pair: `{sym}`\n"
        f"Direction: *{direction}*\n"
        f"Entry: `{entry}`\n"
        f"TP: `{round(tp,6)}` | SL: `{round(sl,6)}`\n"
        f"RSI: `{details['rsi']}`  MA(1m): `{details['ma1']}`  /  MA(5m): `{details['ma5']}`\n"
        f"Pattern: `{details['pattern']}`  Volume xAvg: `{details['vol_ratio'] if details['vol_ratio'] else 'n/a'}`\n"
        f"Confidence: *{details['confidence']}%*  |  Symbol WinRate: *{symbol_winrate(sym)}%*"
    )
    tg_send(msg)

def trail_trade_outcomes(sym, price):
    with state_lock:
        sig = open_signals.get(sym)
        if not sig:
            return
        d = sig["direction"]
        if d == "BUY":
            if price >= sig["tp"]:
                results[sym]["win"] += 1
                del open_signals[sym]
                persist()
                tg_send(f"✅ `{sym}` BUY hit TP `{round(price,6)}` — WinRate now: *{symbol_winrate(sym)}%*")
            elif price <= sig["sl"]:
                results[sym]["loss"] += 1
                del open_signals[sym]
                persist()
                tg_send(f"❌ `{sym}` BUY hit SL `{round(price,6)}` — WinRate now: *{symbol_winrate(sym)}%*")
        else:
            if price <= sig["tp"]:
                results[sym]["win"] += 1
                del open_signals[sym]
                persist()
                tg_send(f"✅ `{sym}` SELL hit TP `{round(price,6)}` — WinRate now: *{symbol_winrate(sym)}%*")
            elif price >= sig["sl"]:
                results[sym]["loss"] += 1
                del open_signals[sym]
                persist()
                tg_send(f"❌ `{sym}` SELL hit SL `{round(price,6)}` — WinRate now: *{symbol_winrate(sym)}%*")

# ---------- WEBSOCKET ----------
def on_open(ws):
    # Authorize once, then subscribe for all symbols
    ws.send(json.dumps({"authorize": DERIV_TOKEN}))

def on_message(ws, raw):
    try:
        msg = json.loads(raw)
    except Exception:
        return

    # Error handling
    if "error" in msg:
        # throttle same error spam
        sym = msg.get("echo_req", {}).get("ticks") or msg.get("echo_req", {}).get("ticks_history") or "GENERAL"
        now = time.time()
        if now - last_error_time[sym] > 10:
            tg_send(f"❌ {sym} Error: {msg['error'].get('message', str(msg['error']))}")
            last_error_time[sym] = now
        return

    mtype = msg.get("msg_type")

    # Authorization OK -> subscribe to everything
    if mtype == "authorize":
        tg_send("✅ *D-SmartTrader* authorized with Deriv. Subscribing feeds…")
        # subscribe candles & ticks per symbol
        for sym in SYMBOLS:
            # 1m candles (history + stream)
            ws.send(json.dumps({
                "ticks_history": sym,
                "style": "candles",
                "granularity": 60,
                "count": 300,
                "end": "latest",
                "subscribe": 1
            }))
            # 5m candles (history + stream)
            ws.send(json.dumps({
                "ticks_history": sym,
                "style": "candles",
                "granularity": 300,
                "count": 300,
                "end": "latest",
                "subscribe": 1
            }))
            # ticks stream
            ws.send(json.dumps({
                "ticks": sym,
                "subscribe": 1
            }))
        return

    # Historical candles init
    if mtype == "candles":
        sym = msg.get("echo_req", {}).get("ticks_history")
        gran = msg.get("echo_req", {}).get("granularity", 60)
        if not sym or sym not in SYMBOLS: return
        arr = msg.get("candles", [])
        store = candles_5m if gran == 300 else candles_1m
        for c in arr:
            store[sym].append({
                "open": float(c["open"]),
                "high": float(c["high"]),
                "low": float(c["low"]),
                "close": float(c["close"]),
                "epoch": int(c["epoch"]),
                "volume": float(c.get("volume", 0.0))
            })
        return

    # Streaming OHLC updates
    if mtype == "ohlc":
        o = msg.get("ohlc", {})
        sym = o.get("symbol")
        if not sym or sym not in SYMBOLS: return
        gran = msg.get("echo_req", {}).get("granularity", 60)
        candle = {
            "open": float(o["open"]),
            "high": float(o["high"]),
            "low": float(o["low"]),
            "close": float(o["close"]),
            "epoch": int(o.get("open_time", o.get("epoch", time.time()))),
            "volume": float(o.get("volume", 0.0))
        }
        if gran == 300:
            candles_5m[sym].append(candle)
        else:
            candles_1m[sym].append(candle)
            # new minute -> reset tick proxy
            tick_counts_1m[sym] = 0

        # After we have a fresh candle, evaluate possible signal
        candidate = maybe_signal(sym)
        if candidate:
            now = time.time()
            if sym in open_signals:
                return  # one at a time per symbol
            if now - last_signal_time[sym] < SIGNAL_COOLDOWN_SEC:
                return  # cooldown
            last_signal_time[sym] = now
            # use last close as entry baseline; ticks will firm up
            entry = candle["close"]
            open_trade(sym, candidate["direction"], entry, candidate)
        return

    # Streaming ticks
    if mtype == "tick":
        t = msg.get("tick", {})
        sym = t.get("symbol")
        if not sym or sym not in SYMBOLS: return
        price = float(t["quote"])
        tick_counts_1m[sym] += 1
        # monitor TP/SL
        trail_trade_outcomes(sym, price)
        return

def on_error(ws, err):
    tg_send(f"⚠️ WebSocket Error: {err}")

def on_close(ws, code, reason):
    tg_send("🔌 D-SmartTrader disconnected. Reconnecting shortly…")

def main():
    load_persist()
    tg_send("🚀 D-SmartTrader is starting (strict mode)…")
    while True:
        try:
            ws = websocket.WebSocketApp(
                WS_URL,
                on_open=on_open,
                on_message=on_message,
                on_error=on_error,
                on_close=on_close
            )
            ws.run_forever()
        except Exception as e:
            tg_send(f"⏳ WS loop error, retrying: {e}")
            time.sleep(5)

if __name__ == "__main__":
    main()
