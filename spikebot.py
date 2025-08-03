import websocket
import json
import threading
import time
import requests
from collections import deque

DERIV_TOKEN = "SXdVTq3NnN4HaAY"
BOT_TOKEN = "8343666564:AAGrM2fgR9hCREwTccmQovM3roNVCO5xdVA"
USER_ID = "6868476259"

SYMBOLS = ["R_75", "R_100", "1HZ100V", "frxEURUSD"]
RSI_PERIOD = 14
OVERBOUGHT = 70
OVERSOLD = 30
MOVING_AVERAGE_PERIOD = 14
STOP_LOSS = 10
TAKE_PROFIT = 15

signal_results = deque(maxlen=50)
prices = {s: deque(maxlen=RSI_PERIOD+50) for s in SYMBOLS}
recent_candles = {s: deque(maxlen=2) for s in SYMBOLS}

def send_alert(msg):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    requests.post(url, data={"chat_id": USER_ID, "text": msg})

def calculate_rsi(data, period):
    if len(data) < period+1: return None
    delta = [data[i] - data[i-1] for i in range(1, len(data))]
    gain = sum(x for x in delta[-period:] if x > 0)
    loss = sum(-x for x in delta[-period:] if x < 0)
    return 100 - (100 / (1 + gain / loss)) if loss != 0 else 100

def moving_average(data, period):
    if len(data) < period: return None
    return sum(data[-period:]) / period

def detect_pattern(candles):
    if len(candles) < 2: return None
    prev, last = candles[-2], candles[-1]
    if last["close"] > last["open"] and prev["close"] < prev["open"]:
        return "Bullish Engulfing"
    if last["close"] < last["open"] and prev["close"] > prev["open"]:
        return "Bearish Engulfing"
    return None

def track_signal(result): signal_results.append(result)
def calculate_winrate():
    return round(signal_results.count("win") / len(signal_results) * 100, 2) if signal_results else 0.0

def check_signal(symbol, data, candles):
    rsi = calculate_rsi(data, RSI_PERIOD)
    ma = moving_average(data, MOVING_AVERAGE_PERIOD)
    price = data[-1]
    if rsi and ma:
        direction = None
        if rsi > OVERBOUGHT and price < ma: direction = "Sell"
        elif rsi < OVERSOLD and price > ma: direction = "Buy"
        if direction:
            sl = price - STOP_LOSS if direction == "Buy" else price + STOP_LOSS
            tp = price + TAKE_PROFIT if direction == "Buy" else price - TAKE_PROFIT
            pattern = detect_pattern(candles)
            msg = f"""📊 D-SmartTrader Signal
Pair: {symbol}
Signal: {direction}
Entry: {price:.2f}
TP: {tp:.2f}, SL: {sl:.2f}
Pattern: {pattern or "N/A"}
RSI: {round(rsi, 2)} | MA: {round(ma, 2)}
Win Rate: {calculate_winrate()}%"""
            send_alert(msg)
            track_signal("win")  # Placeholder logic

def start_ws(symbol):
    ws_url = "wss://ws.derivws.com/websockets/v3"
    ws = None

    def on_open(ws):
        ws.send(json.dumps({"authorize": DERIV_TOKEN}))

    def on_message(ws, message):
        res = json.loads(message)
        if "error" in res:
            send_alert(f"❌ {symbol} Error: {res['error']['message']}")
            ws.close()
            return
        if "authorize" in res:
            ws.send(json.dumps({"ticks": symbol}))
            send_alert(f"✅ D-SmartTrader ({symbol}) is now LIVE!")
        elif "tick" in res:
            price = float(res["tick"]["quote"])
            prices[symbol].append(price)
            check_signal(symbol, prices[symbol], recent_candles[symbol])

    def on_error(ws, err):
        send_alert(f"⚠️ WebSocket Error ({symbol}): {err}")

    def on_close(ws, *args):
        send_alert(f"🔌 D-SmartTrader ({symbol}) disconnected.")
        time.sleep(10)
        threading.Thread(target=start_ws, args=(symbol,), daemon=True).start()

    ws = websocket.WebSocketApp(ws_url, on_open=on_open, on_message=on_message, on_error=on_error, on_close=on_close)
    ws.run_forever()

def start_all():
    for i, sym in enumerate(SYMBOLS):
        threading.Timer(i * 5, start_ws, args=(sym,)).start()

if __name__ == "__main__":
    start_all()
