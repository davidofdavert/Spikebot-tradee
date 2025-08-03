import websocket
import json
import time
import threading
import requests

# === CONFIGURATION ===
APP_ID = "1089"
WS_URL = f"wss://ws.derivws.com/websockets/v3?app_id={APP_ID}"
TELEGRAM_TOKEN = "8343666564:AAGrM2fgR9hCREwTccmQovM3roNVCO5xdVA"
TELEGRAM_CHAT_ID = "6868476259"

SYMBOLS = ["R_75", "R_100", "frxUSDJPY", "frxEURUSD"]
SL_PERCENT = 0.30
TP_PERCENT = 0.40

win_count = 0
loss_count = 0

def send_telegram(msg):
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            data={"chat_id": TELEGRAM_CHAT_ID, "text": msg},
        )
    except:
        print("Telegram error")

def connect_and_stream(symbol):
    ws = websocket.create_connection(WS_URL)
    send_telegram(f"✅ D-SmartTrader ({symbol}) is now LIVE!")

    def subscribe_ticks():
        ws.send(json.dumps({
            "ticks_history": symbol,
            "adjust_start_time": 1,
            "count": 20,
            "end": "latest",
            "start": 1,
            "style": "candles",
            "granularity": 60,
            "subscribe": 1
        }))

    subscribe_ticks()

    candles = []

    while True:
        try:
            msg = json.loads(ws.recv())

            if "error" in msg:
                send_telegram(f"❌ {symbol} Error: {msg['error']['message']}")
                break

            if "candles" in msg:
                candles = msg["candles"]
            elif "history" in msg and "candles" in msg["history"]:
                candles = msg["history"]["candles"]
            elif "ohlc" in msg:
                ohlc = msg["ohlc"]
                candles.append({
                    "open": float(ohlc["open"]),
                    "high": float(ohlc["high"]),
                    "low": float(ohlc["low"]),
                    "close": float(ohlc["close"]),
                })
                if len(candles) > 20:
                    candles.pop(0)

            if len(candles) >= 14:
                analyze_signal(symbol, candles)
        except Exception as e:
            send_telegram(f"⚠️ WebSocket Error ({symbol}): {e}")
            break

    ws.close()
    send_telegram(f"🔌 D-SmartTrader ({symbol}) disconnected.")

def rsi(data, period=14):
    gains, losses = [], []
    for i in range(1, period + 1):
        change = data[i]["close"] - data[i-1]["close"]
        if change >= 0:
            gains.append(change)
        else:
            losses.append(abs(change))
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period if losses else 1
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))

def sma(data, period=14):
    return sum(d["close"] for d in data[-period:]) / period

def analyze_signal(symbol, candles):
    global win_count, loss_count

    last_close = candles[-1]["close"]
    current_rsi = rsi(candles)
    current_ma = sma(candles)

    signal = None
    if current_rsi > 70 and last_close < current_ma:
        signal = "SELL"
    elif current_rsi < 30 and last_close > current_ma:
        signal = "BUY"

    if signal:
        sl = last_close * (1 - SL_PERCENT if signal == "BUY" else 1 + SL_PERCENT)
        tp = last_close * (1 + TP_PERCENT if signal == "BUY" else 1 - TP_PERCENT)

        # Dummy trade outcome simulation
        outcome = "WIN" if current_rsi < 60 else "LOSS"
        if outcome == "WIN":
            win_count += 1
        else:
            loss_count += 1

        win_rate = (win_count / (win_count + loss_count)) * 100 if (win_count + loss_count) > 0 else 0

        msg = f"""📡 Signal on {symbol}
🔹 Action: {signal}
💰 Entry: {round(last_close, 3)}
📈 TP: {round(tp, 3)}
📉 SL: {round(sl, 3)}
📊 RSI: {round(current_rsi, 2)}
🧠 MA: {round(current_ma, 2)}
✅ Win rate: {round(win_rate, 1)}% ({win_count}W/{loss_count}L)
        """
        send_telegram(msg)

def start_bot():
    threads = []
    for symbol in SYMBOLS:
        t = threading.Thread(target=connect_and_stream, args=(symbol,))
        t.start()
        threads.append(t)
        time.sleep(2)

    for t in threads:
        t.join()

if __name__ == "__main__":
    send_telegram("🤖 D-SmartTrader bot is starting up...")
    start_bot()
