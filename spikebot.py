import websocket
import json
import time
import os
import requests
import threading

DERIV_TOKEN = os.getenv("DERIV_TOKEN")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

markets = ["R_75", "R_100", "frxEURUSD", "frxUSDJPY"]  # No Boom & Crash

live_signals = []
wins = 0
losses = 0

def send_telegram(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message}
    requests.post(url, json=payload)

def calculate_win_rate():
    total = wins + losses
    if total == 0:
        return "0%"
    return f"{(wins / total) * 100:.1f}%"

def handle_signal(market, price):
    global wins, losses
    # Fake signal logic for demonstration
    sl = price - 0.2
    tp = price + 0.5
    signal = {"market": market, "price": price, "sl": sl, "tp": tp}
    live_signals.append(signal)

    msg = (
        f"📊 Signal on {market}\n"
        f"Price: {price}\n"
        f"TP: {tp}\n"
        f"SL: {sl}\n"
        f"Win Rate: {calculate_win_rate()}"
    )
    send_telegram(msg)

    # Simulate signal outcome
    time.sleep(3)
    result = "win" if tp > price else "loss"
    if result == "win":
        wins += 1
    else:
        losses += 1

def on_message(ws, message):
    data = json.loads(message)

    if "error" in data:
        send_telegram(f"❌ {data['error'].get('message', 'Unknown error')}")
        ws.close()
        return

    if "tick" in data:
        symbol = data["tick"]["symbol"]
        price = data["tick"]["quote"]
        handle_signal(symbol, price)

def on_open(ws):
    symbol = ws.market
    send_telegram(f"✅ D-SmartTrader ({symbol}) is now LIVE!")
    ws.send(json.dumps({
        "ticks": symbol,
        "subscribe": 1,
        "authorization": DERIV_TOKEN
    }))

def on_error(ws, error):
    send_telegram(f"⚠️ WebSocket Error ({ws.market}): {error}")
    try:
        ws.close()
    except:
        pass

def on_close(ws, close_status_code, close_msg):
    send_telegram(f"🔌 D-SmartTrader ({ws.market}) disconnected.")

def connect_market(market):
    def run():
        ws = websocket.WebSocketApp(
            f"wss://ws.derivws.com/websockets/v3",
            on_open=lambda ws: on_open(ws),
            on_message=on_message,
            on_error=on_error,
            on_close=on_close
        )
        ws.market = market
        ws.run_forever()
    threading.Thread(target=run).start()

if __name__ == "__main__":
    send_telegram("🤖 D-SmartTrader bot is starting up...")
    for market in markets:
        connect_market(market)
    while True:
        time.sleep(5)
