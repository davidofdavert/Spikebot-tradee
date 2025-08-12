import time
import pandas as pd
import numpy as np
from deriv_api import DerivAPI
from ta.momentum import RSIIndicator
from ta.trend import SMAIndicator
from ta.volume import VolumeWeightedAveragePrice
from telegram import Bot

# ==== CONFIG ====
DERIV_APP_ID = "YOUR_DERIV_APP_ID"
DERIV_TOKEN = "YOUR_DERIV_API_TOKEN"
SYMBOLS = ["R_10", "R_25", "R_50", "V_10", "V_25", "V_50", "V_75", "V_100"]  # Example list
CANDLE_INTERVAL = 1  # minutes
LOOKBACK = 100
TELEGRAM_TOKEN = "YOUR_TELEGRAM_BOT_TOKEN"
TELEGRAM_CHAT_ID = "YOUR_TELEGRAM_CHAT_ID"

# Init
bot = Bot(token=TELEGRAM_TOKEN)
api = DerivAPI(app_id=DERIV_APP_ID)
api.authorize(DERIV_TOKEN)

def fetch_candles(symbol, count=LOOKBACK):
    ticks = api.candles(
        symbol=symbol,
        count=count,
        granularity=CANDLE_INTERVAL * 60
    )
    df = pd.DataFrame(ticks)
    df['open'] = df['open'].astype(float)
    df['high'] = df['high'].astype(float)
    df['low'] = df['low'].astype(float)
    df['close'] = df['close'].astype(float)
    df['volume'] = df['volume'].astype(float)
    return df

def analyze(df):
    # Indicators
    rsi = RSIIndicator(df['close'], window=14).rsi().iloc[-1]
    sma_short = SMAIndicator(df['close'], window=5).sma_indicator().iloc[-1]
    sma_long = SMAIndicator(df['close'], window=20).sma_indicator().iloc[-1]
    vwap = VolumeWeightedAveragePrice(df['high'], df['low'], df['close'], df['volume'], window=14).volume_weighted_average_price().iloc[-1]
    
    # Candlestick Pattern (Engulfing)
    prev_body = df['close'].iloc[-2] - df['open'].iloc[-2]
    curr_body = df['close'].iloc[-1] - df['open'].iloc[-1]
    engulfing_bull = curr_body > abs(prev_body) and df['close'].iloc[-1] > df['open'].iloc[-1] and df['open'].iloc[-1] < df['close'].iloc[-2]
    engulfing_bear = curr_body < -abs(prev_body) and df['close'].iloc[-1] < df['open'].iloc[-1] and df['open'].iloc[-1] > df['close'].iloc[-2]
    
    # Trend & Volume Confirmation
    bullish_trend = sma_short > sma_long and df['close'].iloc[-1] > vwap
    bearish_trend = sma_short < sma_long and df['close'].iloc[-1] < vwap
    
    # Signal Logic + Confidence
    if engulfing_bull and rsi < 70 and bullish_trend:
        confidence = round(min(100, (100 - rsi) + (5 if bullish_trend else 0)), 1)
        return "BUY", confidence
    elif engulfing_bear and rsi > 30 and bearish_trend:
        confidence = round(min(100, rsi + (5 if bearish_trend else 0)), 1)
        return "SELL", confidence
    else:
        return None, None

def send_signal(symbol, signal, confidence):
    msg = f"📊 *{symbol}* | Signal: *{signal}*\n🎯 Confidence: {confidence}%\n#DSmartTrader"
    bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=msg, parse_mode="Markdown")

while True:
    for symbol in SYMBOLS:
        try:
            df = fetch_candles(symbol)
            signal, confidence = analyze(df)
            if signal:
                send_signal(symbol, signal, confidence)
        except Exception as e:
            print(f"Error on {symbol}: {e}")
    time.sleep(60)  # check every minute
