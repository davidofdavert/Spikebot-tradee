import os
import time
import talib
import pandas as pd
import numpy as np
import requests
from datetime import datetime
from telegram import Bot

# ========== CONFIG ==========
API_KEY = os.getenv("DERIV_API_KEY")  # your Deriv API key (set in Railway vars)
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
PAIR = "R_75"  # Example: Volatility 75
TIMEFRAME = "1m"
BARS = 100

bot = Bot(token=TELEGRAM_TOKEN)

# ========== FETCH MARKET DATA ==========
def fetch_candles():
    url = f"https://api.deriv.com/api/exchange/v1/history?symbol={PAIR}&granularity=60&count={BARS}"
    resp = requests.get(url)
    data = resp.json()
    candles = pd.DataFrame(data['candles'])
    candles['time'] = pd.to_datetime(candles['time'], unit='s')
    return candles

# ========== ANALYSIS ==========
def analyze_signal(df):
    close = df['close'].values
    high = df['high'].values
    low = df['low'].values

    # RSI
    rsi = talib.RSI(close, timeperiod=14)
    latest_rsi = rsi[-1]

    # Moving Average (Trend)
    ma = talib.SMA(close, timeperiod=20)
    latest_ma = ma[-1]

    # Candlestick pattern
    engulfing = talib.CDLENGULFING(open=df['open'].values, high=high, low=low, close=close)[-1]

    # Volume strength
    volume_strength = (df['volume'].iloc[-1] / df['volume'].mean()) * 100

    # Confirmation checks
    score = 0
    if latest_rsi < 30:
        score += 25
    elif latest_rsi > 70:
        score += 25

    if close[-1] > latest_ma:
        score += 25
    else:
        score += 15

    if engulfing != 0:
        score += 25

    if volume_strength > 120:
        score += 25

    win_rate_estimate = min(score, 100)  # cap at 100

    # Generate signal
    if latest_rsi < 30 and close[-1] > latest_ma and engulfing > 0:
        return "BUY", win_rate_estimate
    elif latest_rsi > 70 and close[-1] < latest_ma and engulfing < 0:
        return "SELL", win_rate_estimate
    else:
        return None, None

# ========== SEND ALERT ==========
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
                    f"📊 *D-SmartTrader Signal*\n"
                    f"Pair: {PAIR}\n"
                    f"Signal: {signal}\n"
                    f"Estimated Win Rate: {win_rate}%\n"
                    f"Time: {datetime.utcnow()} UTC"
                )
                send_telegram(msg)
                print(msg)

            time.sleep(60)  # run every minute
        except Exception as e:
            print(f"Error: {e}")
            time.sleep(30)

if __name__ == "__main__":
    main()
