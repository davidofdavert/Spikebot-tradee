import os
import time
import pandas as pd
import pandas_ta as ta
import requests
from datetime import datetime
from telegram import Bot

# ========== CONFIG ==========
API_KEY = os.getenv("DERIV_API_KEY")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
PAIR = "R_75"
BARS = 100

bot = Bot(token=TELEGRAM_TOKEN)

# ========== FETCH MARKET DATA ==========
def fetch_candles():
    url = f"https://api.deriv.com/api/exchange/v1/history?symbol={PAIR}&granularity=60&count={BARS}"
    resp = requests.get(url)
    data = resp.json()
    df = pd.DataFrame(data['candles'])
    df['time'] = pd.to_datetime(df['time'], unit='s')
    return df

# ========== ANALYSIS ==========
def analyze_signal(df):
    df['rsi'] = ta.rsi(df['close'], length=14)
    df['sma'] = ta.sma(df['close'], length=20)
    df['engulfing'] = ta.cdl_pattern(df, name="engulfing")  # pandas_ta has candlestick patterns
    volume_strength = (df['volume'].iloc[-1] / df['volume'].mean()) * 100

    latest_rsi = df['rsi'].iloc[-1]
    latest_sma = df['sma'].iloc[-1]
    engulfing = df['engulfing'].iloc[-1]

    score = 0
    if latest_rsi < 30:
        score += 25
    elif latest_rsi > 70:
        score += 25

    if df['close'].iloc[-1] > latest_sma:
        score += 25
    else:
        score += 15

    if engulfing != 0:
        score += 25

    if volume_strength > 120:
        score += 25

    win_rate = min(score, 100)

    if latest_rsi < 30 and df['close'].iloc[-1] > latest_sma and engulfing > 0:
        return "BUY", win_rate
    elif latest_rsi > 70 and df['close'].iloc[-1] < latest_sma and engulfing < 0:
        return "SELL", win_rate
    return None, None

# ========== TELEGRAM ==========
def send_telegram(message):
    bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=message)

# ========== MAIN LOOP ==========
def main():
    while True:
        try:
            df = fetch_candles()
            signal, win_rate = analyze_signal(df)

            if signal:
                msg = (
                    f"📊 D-SmartTrader Signal\n"
                    f"Pair: {PAIR}\n"
                    f"Signal: {signal}\n"
                    f"Estimated Win Rate: {win_rate}%\n"
                    f"Time: {datetime.utcnow()} UTC"
                )
                send_telegram(msg)
                print(msg)

            time.sleep(60)
        except Exception as e:
            print(f"Error: {e}")
            time.sleep(30)

if __name__ == "__main__":
    main()
