#!/usr/bin/env python3
# spikebot.py  — D-SmartTrader (SAFE/STRICT switch, confidence %, FX + Volatility)
# Requires: websocket-client, requests

import os
import json
import time
import math
import threading
from collections import deque, defaultdict

import websocket
import requests

# ========= ENV / CONFIG =========
DERIV_TOKEN      = os.getenv("DERIV_TOKEN")
DERIV_APP_ID     = os.getenv("DERIV_APP_ID", "1089")
TELEGRAM_TOKEN   = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# Your requested symbols
SYMBOLS = [
    # Volatility Indices (continuous)
    "R_10", "R_25", "R_50", "R_75", "R_100",

    # Gold (Metals)
    "frxXAUUSD",

    # Forex majors
    "frxEURUSD", "frxUSDJPY", "frxGBPUSD", "frxAUDUSD", "frxUSDCAD",
]

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
