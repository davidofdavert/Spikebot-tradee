import websocket
import json
import threading
import time
import requests
from collections import deque

# === CONFIGURATION ===
DERIV_TOKEN = "SXdVTq3NnN4HaAY"  # Replace with valid token
BOT_TOKEN = "8343666564:AAGrM2fgR9hCREwTccmQovM3roNVCO5xdVA"
USER_ID = "6868476259"

SYMBOLS = ["R_75", "R_100", "1HZ100V", "frxEURUSD"]
RSI_PERIOD = 14
OVERBOUGHT = 70
OVERSOLD = 30
MOVING_AVERAGE_PERIOD = 14
MAX_SIGNALS = 50
STOP_LOSS_PIPS = 10
TAKE_PROFIT_PIPS = 15

# === TELEGRAM ALERT ===
def send_alert(message):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    data = {"chat_id": USER_ID, "text": message}
    requests.post(url, data=data)

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

# === SIGNAL TRACKING ===
signal_results = deque(maxlen=MAX_SIGNALS)
def track_signal(result):
    signal_results.append(result)

def calculate_win_rate():
    if not signal_results:
        return 0.0
    wins = signal_results.count("win")
    return round((wins / len(signal_results)) * 100, 2)

# === SIGNAL STRATEGY ===
def check_signal(symbol, prices, recent_candles):
    if len(prices) < RSI_PERIOD + 1:
        return

    rsi = calculate_rsi(prices, RSI_PERIOD)
    ma = moving_average(prices, MOVING_AVERAGE_PERIOD)
    current_price = prices[-1]

    if rsi is None or ma is None:
        return

    direction = None
    if rsi > OVERBOUGHT and current_price < ma:
        direction = "Sell"
    elif rsi < OVERSOLD and current_price > ma:
        direction = "Buy"

    if direction:
        sl = current_price - STOP_LOSS_PIPS if direction == "Buy" else current_price + STOP_LOSS_PIPS
        tp = current_price + TAKE_PROFIT_PIPS if direction == "Buy" else current_price - TAKE_PROFIT_PIPS
        pattern = detect_pattern(recent_candles)
        win_rate = calculate_win_rate()
        alert_msg = (
            f"📡 D-SmartTrader Signal\n"
            f"Pair: {symbol}\n"
            f"Signal: {direction}\n"
            f"Pattern: {pattern or 'N/A'}\n"
            f"RSI: {round(rsi, 2)} | MA: {round(ma, 2)}\n"
            f"Entry: {current_price:.2f}\n"
            f"TP: {tp:.2f} | SL: {sl:.2f}\n"
            f"Win Rate: {win_rate}%"
        )
        send_alert(alert_msg)
        track_signal("win")  # Replace with real tracking in production

# === WEBSOCKET CALLBACKS ===
def create_ws(symbol):
    def on_message(ws, message):
        data = json.loads(message)
        if "tick" not in data:
            return
        price = float(data["tick"]["quote"])
        prices[symbol].append(price)
        check_signal(symbol, prices[symbol], recent_candles[symbol])

    def on_error(ws, error):
        print(f"WebSocket Error on {symbol}: {error}")
        send_alert(f"⚠️ D-SmartTrader ({symbol}) offline.")

    def on_close(ws, close_status_code, close_msg):
        print(f"WebSocket closed for {symbol}")
        send_alert(f"🔌 D-SmartTrader ({symbol}) disconnected.")

    def on_open(ws):
        ws.send(json.dumps({"authorize": DERIV_TOKEN}))
        def run():
            time.sleep(1)
            ws.send(json.dumps({"ticks": symbol}))
        threading.Thread(target=run).start()
        send_alert(f"✅ D-SmartTrader ({symbol}) is now LIVE!")

    return websocket.WebSocketApp(
        "wss://ws.derivws.com/websockets/v3",
        on_open=on_open,
        on_message=on_message,
        on_error=on_error,
        on_close=on_close
    )

# === RUN BOT FOR ALL SYMBOLS ===
prices = {symbol: deque(maxlen=RSI_PERIOD + 50) for symbol in SYMBOLS}
recent_candles = {symbol: deque(maxlen=2) for symbol in SYMBOLS}

def start_all():
    for symbol in SYMBOLS:
        ws = create_ws(symbol)
        threading.Thread(target=ws.run_forever).start()

if __name__ == "__main__":
    start_all()
