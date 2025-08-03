import websocket
import json
import threading
import time
import requests
from collections import deque

# === CONFIGURATION ===
DERIV_TOKEN = "SXdVTq3NnN4HaAY"  # Replace with your actual Deriv API token
BOT_TOKEN = "8343666564:AAGrM2fgR9hCREwTccmQovM3roNVCO5xdVA"
USER_ID = "6868476259"
SYMBOL = "R_75"
RSI_PERIOD = 14
OVERBOUGHT = 70
OVERSOLD = 30
MOVING_AVERAGE_PERIOD = 14
MAX_SIGNALS = 50

# === TELEGRAM ALERT ===
def send_alert(message):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    data = {"chat_id": USER_ID, "text": message}
    try:
        requests.post(url, data=data)
    except Exception as e:
        print("Telegram error:", e)

# === RSI CALCULATION ===
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

# === MOVING AVERAGE ===
def moving_average(data, period):
    if len(data) < period:
        return None
    return sum(data[-period:]) / period

# === SIGNAL TRACKING ===
signal_results = deque(maxlen=MAX_SIGNALS)
def track_signal(result):
    signal_results.append(result)
def calculate_win_rate():
    if not signal_results:
        return 0.0
    wins = signal_results.count("win")
    return round((wins / len(signal_results)) * 100, 2)

# === CANDLESTICK PATTERN DETECTION ===
def detect_pattern(candles):
    if len(candles) < 2:
        return None
    prev = candles[-2]
    last = candles[-1]
    if last["close"] > last["open"] and prev["close"] < prev["open"]:
        return "Bullish Engulfing"
    elif last["close"] < last["open"] and prev["close"] > prev["open"]:
        return "Bearish Engulfing"
    return None

# === LIVE PRICE WEBSOCKET ===
def on_message(ws, message):
    data = json.loads(message)

    # Handle authorization error
    if "error" in data:
        print("Authorization Error:", data["error"])
        send_alert("❌ D-SmartTrader failed to authorize: " + str(data["error"]))
        ws.close()
        return

    # After successful authorization
    if data.get("msg_type") == "authorize":
        send_alert("✅ D-SmartTrader is now LIVE!")
        sub_msg = json.dumps({"ticks": SYMBOL})
        ws.send(sub_msg)
        return

    if "tick" not in data:
        return

    price = float(data["tick"]["quote"])
    prices.append(price)

    rsi = calculate_rsi(prices, RSI_PERIOD)
    ma = moving_average(prices, MOVING_AVERAGE_PERIOD)
    if rsi is None or ma is None:
        return

    direction = None
    if rsi > OVERBOUGHT and price < ma:
        direction = "Sell"
    elif rsi < OVERSOLD and price > ma:
        direction = "Buy"

    if direction:
        pattern = detect_pattern(recent_candles)
        win_rate = calculate_win_rate()
        alert_msg = (
            f"💹 D-SmartTrader Signal Alert\n"
            f"Pair: {SYMBOL}\n"
            f"Signal: {direction}\n"
            f"Pattern: {pattern or 'N/A'}\n"
            f"RSI: {round(rsi, 2)} | MA: {round(ma, 2)}\n"
            f"📊 Win Rate: {win_rate}%"
        )
        send_alert(alert_msg)
        track_signal("win")  # You can change logic here later

def on_error(ws, error):
    print(f"WebSocket Error: {error}")
    send_alert("⚠️ D-SmartTrader WebSocket Error occurred!")

def on_close(ws, close_status_code, close_msg):
    print("WebSocket closed")
    send_alert("🔴 D-SmartTrader is now OFFLINE!")

def on_open(ws):
    auth_msg = json.dumps({"authorize": DERIV_TOKEN})
    ws.send(auth_msg)

def start_websocket():
    ws_url = "wss://ws.derivws.com/websockets/v3"
    ws = websocket.WebSocketApp(
        ws_url,
        on_open=on_open,
        on_message=on_message,
        on_error=on_error,
        on_close=on_close
    )
    ws.run_forever()

# === MAIN EXECUTION ===
prices = deque(maxlen=RSI_PERIOD + 50)
recent_candles = deque(maxlen=2)
start_websocket()
