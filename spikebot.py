import websocket
import json
import threading
import time
import requests
from collections import deque

# === CONFIG ===
DERIV_TOKEN = "SXdVTq3NnN4HaAY"  # replace with your valid token
BOT_TOKEN = "8343666564:AAGrM2fgR9hCREwTccmQovM3roNVCO5xdVA"
USER_ID = "6868476259"
SYMBOLS = ["R_75", "R_100", "frxEURUSD", "frxUSDJPY"]  # No Boom & Crash
RSI_PERIOD = 14
MA_PERIOD = 14
OVERBOUGHT = 70
OVERSOLD = 30
TP_DISTANCE = 1.0
SL_DISTANCE = 1.0
MAX_TRACK = 50

# === Data Stores ===
prices_map = {symbol: deque(maxlen=RSI_PERIOD + 50) for symbol in SYMBOLS}
candles_map = {symbol: deque(maxlen=2) for symbol in SYMBOLS}
results_map = {symbol: deque(maxlen=MAX_TRACK) for symbol in SYMBOLS}

# === Telegram ===
def send_alert(msg):
    requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                  data={"chat_id": USER_ID, "text": msg})

# === RSI Calculation ===
def calculate_rsi(prices, period):
    if len(prices) < period + 1:
        return None
    deltas = [prices[i] - prices[i - 1] for i in range(1, len(prices))]
    gains = sum(delta for delta in deltas[-period:] if delta > 0)
    losses = sum(-delta for delta in deltas[-period:] if delta < 0)
    if losses == 0:
        return 100
    rs = gains / losses
    return 100 - (100 / (1 + rs))

# === Moving Average ===
def moving_average(prices, period):
    if len(prices) < period:
        return None
    return sum(prices[-period:]) / period

# === Pattern Detection ===
def detect_pattern(candles):
    if len(candles) < 2:
        return None
    prev, last = candles[-2], candles[-1]
    if last["close"] > last["open"] and prev["close"] < prev["open"]:
        return "Bullish Engulfing"
    if last["close"] < last["open"] and prev["close"] > prev["open"]:
        return "Bearish Engulfing"
    return None

# === Signal Tracking ===
def track_result(symbol, result):
    results_map[symbol].append(result)

def win_rate(symbol):
    results = results_map[symbol]
    if not results:
        return 0.0
    wins = results.count("win")
    return round((wins / len(results)) * 100, 2)

# === WebSocket Callbacks ===
def make_ws(symbol):
    def on_message(ws, msg):
        try:
            data = json.loads(msg)
            if "error" in data:
                send_alert(f"❌ {symbol} Error: {data['error'].get('message')}")
                ws.close()
                return

            if "tick" in data:
                price = float(data["tick"]["quote"])
                prices_map[symbol].append(price)

                rsi = calculate_rsi(prices_map[symbol], RSI_PERIOD)
                ma = moving_average(prices_map[symbol], MA_PERIOD)

                if rsi is None or ma is None:
                    return

                direction = None
                if rsi > OVERBOUGHT and price < ma:
                    direction = "Sell"
                elif rsi < OVERSOLD and price > ma:
                    direction = "Buy"

                if direction:
                    pattern = detect_pattern(candles_map[symbol])
                    tp = price + TP_DISTANCE if direction == "Buy" else price - TP_DISTANCE
                    sl = price - SL_DISTANCE if direction == "Buy" else price + SL_DISTANCE
                    wr = win_rate(symbol)

                    alert = (
                        f"📢 D-SmartTrader Signal\n"
                        f"Pair: {symbol}\n"
                        f"Signal: {direction}\n"
                        f"Pattern: {pattern or 'N/A'}\n"
                        f"RSI: {round(rsi,2)} | MA: {round(ma,2)}\n"
                        f"TP: {round(tp, 2)} | SL: {round(sl, 2)}\n"
                        f"Win Rate: {wr}%"
                    )
                    send_alert(alert)
                    track_result(symbol, "win")  # For demo, assume win

            elif "candles" in data:
                for c in data["candles"]:
                    candles_map[symbol].append({
                        "open": float(c["open"]),
                        "close": float(c["close"])
                    })

        except Exception as e:
            send_alert(f"⚠️ WebSocket Error ({symbol}): {e}")

    def on_open(ws):
        ws.send(json.dumps({"authorize": DERIV_TOKEN}))
        time.sleep(1)
        ws.send(json.dumps({"ticks": symbol}))
        ws.send(json.dumps({
            "candles": symbol,
            "subscribe": 1,
            "end": "latest",
            "count": 2,
            "style": "candles",
            "granularity": 60
        }))
        send_alert(f"✅ D-SmartTrader ({symbol}) is now LIVE!")

    def on_close(ws, code, msg):
        send_alert(f"🔌 D-SmartTrader ({symbol}) disconnected.")

    def on_error(ws, err):
        send_alert(f"⚠️ WebSocket Error ({symbol}): {err}")

    return websocket.WebSocketApp(
        "wss://ws.derivws.com/websockets/v3",
        on_open=on_open,
        on_message=on_message,
        on_close=on_close,
        on_error=on_error
    )

# === START ALL MARKETS ===
def start_all():
    for symbol in SYMBOLS:
        ws = make_ws(symbol)
        threading.Thread(target=ws.run_forever).start()
        time.sleep(3)  # Avoid flood

# === MAIN ===
if __name__ == "__main__":
    try:
        send_alert("🚀 D-SmartTrader bot is starting up...")
        start_all()
    except Exception as e:
        send_alert(f"🔥 Startup error: {e}")
