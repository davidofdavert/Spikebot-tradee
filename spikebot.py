#!/usr/bin/env python3
"""
D-SmartTrader – FX + Volatility signal bot for Deriv WebSocket
- Toggle mode via Telegram: /strict, /safe, /mode, /status
- Confidence % per signal (RSI, trend, volume, pattern, body/ATR)
- Tracks TP/SL hits and per-symbol win rate
- Designed for Railway (reads env vars)

ENV REQUIRED:
  DERIV_TOKEN            -> your Deriv API token
  TELEGRAM_BOT_TOKEN     -> your Telegram bot token
  TELEGRAM_CHAT_ID       -> your chat id
OPTIONAL:
  DERIV_APP_ID           -> your Deriv app_id (recommended; else default 1089)
  SYMBOLS_CSV            -> override watched symbols, e.g. "frxEURUSD,frxGBPUSD,R_75,1HZ75V"
  DEFAULT_MODE           -> "safe" or "strict" (default "safe")
"""

import os, json, time, threading
from collections import deque, defaultdict
import requests
import websocket

# ---------- ENV ----------
DERIV_TOKEN        = os.getenv("DERIV_TOKEN")
DERIV_APP_ID       = os.getenv("DERIV_APP_ID", "1089")
TELEGRAM_TOKEN     = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID")
DEFAULT_MODE       = os.getenv("DEFAULT_MODE", "safe").lower().strip()

# Symbols: FX majors + XAU + a few Vols (no Boom/Crash)
DEFAULT_SYMBOLS = [
    # FX majors + Gold
    "frxEURUSD", "frxGBPUSD", "frxUSDJPY", "frxXAUUSD",
    # Volatility (1-second)
    "1HZ25V", "1HZ75V",
    # Volatility continuous
    "R_75", "R_100"
]
SYMBOLS = [s.strip() for s in os.getenv("SYMBOLS_CSV", ",".join(DEFAULT_SYMBOLS)).split(",") if s.strip()]

# ---------- CONSTANTS / SETTINGS ----------
WS_URL = f"wss://ws.derivws.com/websockets/v3?app_id={DERIV_APP_ID}"

RSI_PERIOD = 14
MA_SHORT   = 14            # 1m
MA_LONG    = 50            # 5m
RSI_OVERSOLD  = 30
RSI_OVERBOUGHT = 70
VOL_MULTIPLIER = 1.2       # strong volume threshold
ATR_PERIOD = 14

# TP/SL % per instrument group
TP_SL_BY_GROUP = {
    "fx":   {"tp": 0.0015, "sl": 0.0010},   # 0.15% / 0.10%
    "gold": {"tp": 0.0020, "sl": 0.0012},   # 0.20% / 0.12%
    "vol":  {"tp": 0.0040, "sl": 0.0025},   # 0.40% / 0.25%
}

STATE_FILE = "dsmart_state.json"

# ---------- TELEGRAM ----------
def tg_send(text: str):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram not configured.")
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            data={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "Markdown"}
        )
    except Exception as e:
        print("Telegram send error:", e)

# Simple polling for commands (/strict, /safe, /mode, /status)
def tg_poll_commands():
    last_update_id = None
    while True:
        try:
            resp = requests.get(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates",
                params={"timeout": 30, "offset": (last_update_id + 1) if last_update_id else None},
                timeout=40
            ).json()
            if resp.get("ok"):
                for upd in resp.get("result", []):
                    last_update_id = upd["update_id"]
                    msg = upd.get("message") or {}
                    text = (msg.get("text") or "").strip().lower()
                    if not text:
                        continue
                    if text.startswith("/strict"):
                        set_mode("strict")
                        tg_send("🔒 Mode switched to *STRICT* (most confirmations).")
                    elif text.startswith("/safe"):
                        set_mode("safe")
                        tg_send("🟢 Mode switched to *SAFE* (high confidence but more signals).")
                    elif text.startswith("/mode"):
                        tg_send(f"⚙️ Current mode: *{get_mode().upper()}*")
                    elif text.startswith("/status"):
                        tg_send(status_summary())
        except Exception:
            pass
        time.sleep(2)

# ---------- PERSISTENCE ----------
def load_state():
    try:
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE, "r") as f:
                return json.load(f)
    except Exception:
        pass
    return {"mode": DEFAULT_MODE, "results": {}, "open": {}}

def save_state():
    try:
        with open(STATE_FILE, "w") as f:
            json.dump({"mode": MODE[0], "results": RESULTS, "open": OPEN}, f, indent=2)
    except Exception:
        pass

STATE = load_state()
MODE = [STATE.get("mode", DEFAULT_MODE)]  # list wrapper so closures can mutate
RESULTS = STATE.get("results", {})        # {symbol: {"win":int,"loss":int}}
OPEN = STATE.get("open", {})              # {symbol: {direction, entry, tp, sl, time, info...}}
LOCK = threading.Lock()

def set_mode(m: str):
    with LOCK:
        MODE[0] = "strict" if m == "strict" else "safe"
        save_state()

def get_mode() -> str:
    with LOCK:
        return MODE[0]

def win_rate(symbol: str) -> float:
    r = RESULTS.get(symbol, {"win":0, "loss":0})
    tot = r["win"] + r["loss"]
    if tot == 0: return 0.0
    return 100.0 * r["win"] / tot

def status_summary() -> str:
    lines = [f"⚙️ Mode: *{get_mode().upper()}*"]
    with LOCK:
        for s in SYMBOLS:
            r = RESULTS.get(s, {"win":0,"loss":0})
            wr = win_rate(s)
            if s in OPEN:
                o = OPEN[s]
                lines.append(f"• {s}: Open {o['direction']} @ {o['entry']:.5f} | TP {o['tp']:.5f} | SL {o['sl']:.5f} | WR {wr:.1f}%")
            else:
                lines.append(f"• {s}: {r['win']}W/{r['loss']}L | WR {wr:.1f}%")
    return "\n".join(lines)

# ---------- DATA CONTAINERS ----------
C1 = {s: deque(maxlen=400) for s in SYMBOLS}  # 1m candles dicts: {open,high,low,close,volume,epoch}
C5 = {s: deque(maxlen=400) for s in SYMBOLS}  # 5m
LAST_EPOCH_1M = {s: None for s in SYMBOLS}
LAST_EPOCH_5M = {s: None for s in SYMBOLS}
TICKS_IN_MIN = {s: 0 for s in SYMBOLS}        # volume proxy
LAST_PRICE = {s: None for s in SYMBOLS}

# ---------- INDICATORS ----------
def sma(vals, n):
    if len(vals) < n: return None
    return sum(vals[-n:]) / n

def rsi(vals, n=14):
    if len(vals) < n+1: return None
    gains = 0.0; losses = 0.0
    for i in range(-n, 0):
        d = vals[i] - vals[i-1]
        if d >= 0: gains += d
        else: losses += -d
    if losses == 0: return 100.0
    rs = gains / losses
    return 100 - (100/(1+rs))

def atr(candles, n=14):
    if len(candles) < n+1: return None
    trs = []
    for i in range(-n, 0):
        c = candles[i]
        p = candles[i-1]
        tr = max(c["high"]-c["low"], abs(c["high"]-p["close"]), abs(c["low"]-p["close"]))
        trs.append(tr)
    return sum(trs)/n if trs else None

def detect_engulfing(candles):
    if len(candles) < 2: return None
    a, b = candles[-2], candles[-1]
    # bullish engulfing
    if a["close"] < a["open"] and b["close"] > b["open"] and b["close"] > a["open"] and b["open"] < a["close"]:
        return "bullish"
    # bearish engulfing
    if a["close"] > a["open"] and b["close"] < b["open"] and b["open"] > a["close"] and b["close"] < a["open"]:
        return "bearish"
    return None

def group_for_symbol(sym: str) -> str:
    if sym.startswith("frxXAU"): return "gold"
    if sym.startswith("frx"):    return "fx"
    return "vol"

def tp_sl_for_symbol(sym: str, entry: float, direction: str):
    g = group_for_symbol(sym)
    tp_pct = TP_SL_BY_GROUP[g]["tp"]
    sl_pct = TP_SL_BY_GROUP[g]["sl"]
    if direction == "BUY":
        return entry*(1+tp_pct), entry*(1-sl_pct)
    else:
        return entry*(1-tp_pct), entry*(1+sl_pct)

# ---------- SIGNAL ENGINE ----------
def compute_confidence(direction, rsi_val, ma1, ma5, pattern, last_vol, avg_vol, cndl_body, atr_val):
    score = 0.0
    # RSI (30%)
    if direction == "BUY":
        r = max(0.0, min(1.0, (RSI_OVERSOLD - rsi_val)/20.0))
    else:
        r = max(0.0, min(1.0, (rsi_val - RSI_OVERBOUGHT)/20.0))
    score += 30.0 * r
    # Trend (25%)
    align = (direction == "BUY" and ma1 > ma5) or (direction == "SELL" and ma1 < ma5)
    if ma5 and ma1:
        ratio = abs((ma1 - ma5) / ma5)
        trend_factor = min(1.0, ratio / 0.002)  # cap at 0.2%
    else:
        trend_factor = 0.0
    score += 25.0 * (1.0 if align else 0.0) * trend_factor
    # Pattern (20%)
    if (direction == "BUY" and pattern == "bullish") or (direction == "SELL" and pattern == "bearish"):
        score += 20.0
    # Volume (15%)
    if avg_vol and last_vol:
        vr = last_vol / max(1e-9, avg_vol)
        vol_factor = max(0.0, min(1.0, (vr - 1.0) / 1.0))  # 1x->0, 2x->1
        score += 15.0 * vol_factor
    # Body/ATR (10%)
    if atr_val and atr_val > 0:
        body_ratio = cndl_body / atr_val
        body_factor = min(1.0, body_ratio / 0.7)
        score += 10.0 * body_factor
    return round(max(0.0, min(100.0, score)), 1)

def strict_or_safe_decision(symbol):
    c1 = list(C1[symbol]); c5 = list(C5[symbol])
    if len(c1) < max(RSI_PERIOD+1, MA_SHORT+1) or len(c5) < MA_LONG+1:
        return None

    closes_1m = [c["close"] for c in c1]
    closes_5m = [c["close"] for c in c5]
    rsi_val = rsi(closes_1m, RSI_PERIOD)
    ma1 = sma(closes_1m, MA_SHORT)
    ma5 = sma(closes_5m, MA_LONG)
    if rsi_val is None or ma1 is None or ma5 is None:
        return None

    pattern = detect_engulfing(c1[-2:])
    last = c1[-1]
    body = abs(last["close"] - last["open"])
    atr_val = atr(c1, ATR_PERIOD)

    # volume
    last_vol = last.get("volume") or TICKS_IN_MIN[symbol]
    vols = [c.get("volume") or 0 for c in c1[-(RSI_PERIOD+5):]]
    nz = [v for v in vols if v]
    avg_vol = sum(nz)/len(nz) if nz else None
    vol_ok = (avg_vol is not None and last_vol > avg_vol * VOL_MULTIPLIER) or (TICKS_IN_MIN[symbol] >= 3)

    mode = get_mode()
    decision = None

    # Trend align
    trend_bull = ma1 > ma5
    trend_bear = ma1 < ma5

    if mode == "strict":
        # Require EVERYTHING
        if rsi_val < RSI_OVERSOLD and trend_bull and pattern == "bullish" and vol_ok:
            decision = "BUY"
        elif rsi_val > RSI_OVERBOUGHT and trend_bear and pattern == "bearish" and vol_ok:
            decision = "SELL"
    else:  # safe (still conservative but allows pattern OR volume)
        if rsi_val < RSI_OVERSOLD and trend_bull and (pattern == "bullish" or vol_ok):
            decision = "BUY"
        elif rsi_val > RSI_OVERBOUGHT and trend_bear and (pattern == "bearish" or vol_ok):
            decision = "SELL"

    if not decision:
        return None

    conf = compute_confidence(decision, rsi_val, ma1, ma5, pattern, last_vol, avg_vol, body, atr_val)
    info = {
        "rsi": round(rsi_val or 0, 2),
        "ma1": round(ma1 or 0, 6),
        "ma5": round(ma5 or 0, 6),
        "pattern": pattern or "none",
        "vol": int(last_vol or 0),
        "avg_vol": round(avg_vol, 2) if avg_vol else 0,
        "atr": round(atr_val, 6) if atr_val else 0,
        "body": round(body, 6),
        "confidence": conf,
        "mode": mode,
    }
    return {"signal": decision, "info": info}

def open_signal(symbol, direction, entry, info):
    tp, sl = tp_sl_for_symbol(symbol, entry, direction)
    with LOCK:
        OPEN[symbol] = {
            "symbol": symbol, "direction": direction, "entry": entry,
            "tp": tp, "sl": sl, "time": int(time.time()), **info
        }
        RESULTS.setdefault(symbol, {"win":0,"loss":0})
        save_state()

    tg_send(
        "📡 *D-SmartTrader Signal*\n"
        f"• Pair: `{symbol}`\n"
        f"• Direction: *{direction}*\n"
        f"• Entry: `{entry:.5f}`\n"
        f"• TP / SL: `{tp:.5f}` / `{sl:.5f}`\n"
        f"• RSI: `{info['rsi']}` | MA(1m/5m): `{info['ma1']}` / `{info['ma5']}`\n"
        f"• Pattern: `{info['pattern']}` | Vol: `{info['vol']}` (avg `{info['avg_vol']}`)\n"
        f"• ATR: `{info['atr']}` | Body: `{info['body']}`\n"
        f"• Confidence: *{info['confidence']}%* | Mode: `{info['mode']}`\n"
        f"• WinRate: *{win_rate(symbol):.1f}%*"
    )

def check_open_signal(symbol, price):
    with LOCK:
        sig = OPEN.get(symbol)
        if not sig: return
        d = sig["direction"]
        tp = sig["tp"]; sl = sig["sl"]

        def win():
            RESULTS[symbol]["win"] += 1
            tg_send(f"✅ `{symbol}` {d} *TP hit* at `{price:.5f}` | WR: *{win_rate(symbol):.1f}%*")
        def loss():
            RESULTS[symbol]["loss"] += 1
            tg_send(f"❌ `{symbol}` {d} *SL hit* at `{price:.5f}` | WR: *{win_rate(symbol):.1f}%*")

        hit = False
        if d == "BUY":
            if price >= tp: win(); hit = True
            elif price <= sl: loss(); hit = True
        else:
            if price <= tp: win(); hit = True
            elif price >= sl: loss(); hit = True

        if hit:
            del OPEN[symbol]
            save_state()

# ---------- DERIV WS HELPERS ----------
def auth_request():
    return {"authorize": DERIV_TOKEN}

def subscribe_ohlc(symbol, granularity_sec):
    # Stream of 1m/5m candles
    return {
        "ticks_history": symbol,
        "adjust_start_time": 1,
        "count": 120,
        "granularity": granularity_sec,
        "end": "latest",
        "style": "candles",
        "subscribe": 1
    }

def subscribe_ticks(symbol):
    return {"ticks": symbol, "subscribe": 1}

def push_candle(container, symbol, candle, is_1m=True):
    # candle fields: open, high, low, close, epoch, volume
    if is_1m:
        last_epoch = LAST_EPOCH_1M[symbol]
    else:
        last_epoch = LAST_EPOCH_5M[symbol]
    if last_epoch == candle["epoch"]:
        # replace last if same epoch (update)
        if container[symbol]:
            container[symbol][-1] = candle
    else:
        container[symbol].append(candle)
        if is_1m: LAST_EPOCH_1M[symbol] = candle["epoch"]
        else:     LAST_EPOCH_5M[symbol] = candle["epoch"]

def handle_msg(symbol, msg):
    # Errors
    if "error" in msg:
        m = msg["error"].get("message") if isinstance(msg["error"], dict) else str(msg["error"])
        tg_send(f"❌ {symbol} Error: {m}")
        return

    mt = msg.get("msg_type")

    # Authorization ok
    if mt == "authorize":
        tg_send(f"✅ D-SmartTrader ({symbol}) authorized with Deriv.")
        return

    # Initial candle history
    if "candles" in msg and isinstance(msg["candles"], list):
        # We don't know which granularity unless echoed; try by length in stream context
        # We push to 1m by default; 5m stream will also arrive separately
        for c in msg["candles"]:
            candle = {
                "open": float(c["open"]), "high": float(c["high"]),
                "low": float(c["low"]),   "close": float(c["close"]),
                "volume": float(c.get("volume", 0)),
                "epoch": int(c.get("epoch", c.get("open_time", int(time.time()))))
            }
            push_candle(C1, symbol, candle, is_1m=True)
        return

    # Streaming OHLC
    if mt == "ohlc":
        o = msg["ohlc"]
        gran = int(msg.get("granularity", o.get("granularity", 60)))
        candle = {
            "open": float(o["open"]), "high": float(o["high"]),
            "low": float(o["low"]),   "close": float(o["close"]),
            "volume": float(o.get("volume", 0)),
            "epoch": int(o.get("open_time", o.get("epoch", int(time.time()))))
        }
        if gran == 300:
            push_candle(C5, symbol, candle, is_5m := False)  # push_candle expects is_1m flag
            # Correct the flag: for 5m, is_1m=False
            push_candle(C5, symbol, candle, is_1m=False)
        else:
            push_candle(C1, symbol, candle, is_1m=True)
            # new minute -> reset volume proxy
            if TICKS_IN_MIN.get(symbol) is not None:
                TICKS_IN_MIN[symbol] = 0

        # Evaluate on each closed 1m update
        decide = strict_or_safe_decision(symbol)
        if decide and symbol not in OPEN:
            price = LAST_PRICE.get(symbol) or candle["close"]
            open_signal(symbol, decide["signal"], price, decide["info"])
        return

    # Tick stream
    if mt == "tick" and "tick" in msg:
        q = float(msg["tick"]["quote"])
        LAST_PRICE[symbol] = q
        # volume proxy
        TICKS_IN_MIN[symbol] += 1
        check_open_signal(symbol, q)
        return

def run_symbol(symbol):
    def _on_open(ws):
        try:
            ws.send(json.dumps(auth_request()))
        except Exception:
            pass

    def _on_message(ws, raw):
        try:
            msg = json.loads(raw)
            handle_msg(symbol, msg)
            # after auth response, subscribe feeds
            if msg.get("msg_type") == "authorize":
                # 1m candles
                ws.send(json.dumps(subscribe_ohlc(symbol, 60)))
                time.sleep(0.1)
                # 5m candles
                ws.send(json.dumps(subscribe_ohlc(symbol, 300)))
                time.sleep(0.1)
                # ticks
                ws.send(json.dumps(subscribe_ticks(symbol)))
        except Exception as e:
            tg_send(f"⚠️ {symbol} parse error: {e}")

    def _on_error(ws, err):
        tg_send(f"⚠️ WebSocket Error ({symbol}): {err}")

    def _on_close(ws, code, reason):
        tg_send(f"🔌 D-SmartTrader ({symbol}) disconnected.")

    while True:
        try:
            tg_send(f"🔁 Starting connection for {symbol} ...")
            ws = websocket.WebSocketApp(
                WS_URL,
                on_open=_on_open,
                on_message=_on_message,
                on_error=_on_error,
                on_close=_on_close
            )
            ws.run_forever()
        except Exception as e:
            tg_send(f"⏳ Reconnecting {symbol} after error: {e}")
        time.sleep(3)

# ---------- MAIN ----------
def main():
    missing = []
    if not DERIV_TOKEN:      missing.append("DERIV_TOKEN")
    if not TELEGRAM_TOKEN:   missing.append("TELEGRAM_BOT_TOKEN")
    if not TELEGRAM_CHAT_ID: missing.append("TELEGRAM_CHAT_ID")
    if missing:
        print("Missing env vars:", missing)
        raise SystemExit("Set required environment variables in Railway > Variables and redeploy.")

    # init results entries
    with LOCK:
        for s in SYMBOLS:
            RESULTS.setdefault(s, {"win":0,"loss":0})
        save_state()

    tg_send(f"D-SmartTrader is starting (*{get_mode().upper()}* mode)…")

    # Start Telegram polling in a background thread
    threading.Thread(target=tg_poll_commands, daemon=True).start()

    # Spin up a thread per symbol
    threads = []
    for s in SYMBOLS:
        t = threading.Thread(target=run_symbol, args=(s,), daemon=True)
        t.start()
        threads.append(t)
        time.sleep(0.4)

    # Keep alive + periodic persistence
    try:
        while True:
            time.sleep(30)
            save_state()
    except KeyboardInterrupt:
        tg_send("🛑 D-SmartTrader stopped.")

if __name__ == "__main__":
    main()
