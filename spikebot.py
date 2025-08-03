import websocket import json import threading import time import requests from collections import deque

=== CONFIG ===

DERIV_TOKEN = "WxzZUJRFwj49vHe" BOT_TOKEN = "8343666564:AAGrM2fgR9hCREwTccmQovM3roNVCO5xdVA" USER_ID = "6868476259"

=== TRACKING ===

history = {}  # Holds price candles per symbol signals_sent = {}  # Prevents spam win_count = 0 loss_count = 0

=== SETTINGS ===

watchlist = [ "R_75", "R_50", "R_25", "R_100", "frxEURUSD", "frxUSDJPY" ] TP_PIPS = 40 SL_PIPS = 20 RSI_PERIOD = 14 MA_PERIOD = 5

=== TELEGRAM ALERT ===

def send_telegram(msg): url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage" payload = {"chat_id": USER_ID, "text": msg, "parse_mode": "Markdown"} try: requests.post(url, data=payload) except: print("Telegram send error.")

=== RSI CALC ===

def compute_rsi(prices, period=14): if len(prices) < period + 1: return None gains, losses = [], [] for i in range(1, period + 1): diff = prices[-i] - prices[-i - 1] if diff >= 0: gains.append(diff) else: losses.append(abs(diff)) avg_gain = sum(gains) / period if gains else 0 avg_loss = sum(losses) / period if losses else 0 if avg_loss == 0: return 100 rs = avg_gain / avg_loss return round(100 - (100 / (1 + rs)), 2)

=== MOVING AVERAGE ===

def compute_ma(prices, period=5): if len(prices) < period: return None return sum(prices[-period:]) / period

=== CANDLE PATTERNS ===

def detect_pattern(candles): if len(candles) < 2: return None c1 = candles[-2] c2 = candles[-1] o1, h1, l1, cl1 = c1 o2, h2, l2, cl2 = c2

# Engulfing
if cl1 < o1 and cl2 > o2 and cl2 > o1 and o2 < cl1:
    return "Bullish Engulfing"
if cl1 > o1 and cl2 < o2 and cl2 < o1 and o2 > cl1:
    return "Bearish Engulfing"

# Doji
if abs(cl2 - o2) <= 0.0001:
    return "Doji"

# Pinbar
body = abs(cl2 - o2)
wick_top = h2 - max(cl2, o2)
wick_bottom = min(cl2, o2) - l2
if wick_top > 2 * body:
    return "Shooting Star"
if wick_bottom > 2 * body:
    return "Hammer"

return None

=== TICK HANDLER ===

def on_message(ws, msg): global win_count, loss_count data = json.loads(msg)

if 'tick' in data:
    tick = data['tick']
    symbol = tick['symbol']
    price = float(tick['quote'])
    epoch = tick['epoch']

    if symbol not in history:
        history[symbol] = deque(maxlen=100)

    history[symbol].append((epoch, price))

    if len(history[symbol]) >= 10:
        prices = [p for _, p in list(history[symbol])[-10:]]
        o = prices[0]
        c = prices[-1]
        h = max(prices)
        l = min(prices)

        if symbol + "_candles" not in history:
            history[symbol + "_candles"] = deque(maxlen=100)
        history[symbol + "_candles"].append((o, h, l, c))

        close_prices = [candle[3] for candle in history[symbol + "_candles"]]
        rsi = compute_rsi(close_prices, RSI_PERIOD)
        ma = compute_ma(close_prices, MA_PERIOD)
        pattern = detect_pattern(history[symbol + "_candles"])

        if not pattern or rsi is None or ma is None:
            return

        direction = None
        if pattern in ["Hammer", "Bullish Engulfing"] and rsi < 30 and c > ma:
            direction = "BUY"
        elif pattern in ["Shooting Star", "Bearish Engulfing"] and rsi > 70 and c < ma:
            direction = "SELL"

        if direction and time.time() - signals_sent.get(symbol, 0) > 60:
            signals_sent[symbol] = time.time()

            tp = c + (TP_PIPS * 0.0001) if direction == "BUY" else c - (TP_PIPS * 0.0001)
            sl = c - (SL_PIPS * 0.0001) if direction == "BUY" else c + (SL_PIPS * 0.0001)

            winrate = 100 * win_count / max(1, win_count + loss_count)

            msg = f"""📉 *{symbol}* SIGNAL

🎯 Direction: {direction} 📊 Pattern: {pattern} 📈 RSI: {rsi} 📏 MA: {round(ma, 5)} 🎯 Entry: {c} ✅ TP: {round(tp, 5)} | ❌ SL: {round(sl, 5)} 🕒 Time: {time.strftime('%H:%M:%S')}

⚙ Confirmations met. 📊 Current Win Rate: {round(winrate)}% ({win_count}W/{loss_count}L)""" send_telegram(msg)

def check_result(entry=c, tp=tp, sl=sl, sym=symbol):
                time.sleep(30)
                current = history[sym][-1][1]
                result = "TP HIT ✅" if (direction == "BUY" and current >= tp) or (direction == "SELL" and current <= tp) else \
                         "SL HIT ❌" if (direction == "BUY" and current <= sl) or (direction == "SELL" and current >= sl) else "Still Running"

                global win_count, loss_count
                if result == "TP HIT ✅":
                    win_count += 1
                elif result == "SL HIT ❌":
                    loss_count += 1

                winrate = 100 * win_count / max(1, win_count + loss_count)
                summary = f"\n📊 Result: {result}\n🏆 Win Rate: {round(winrate)}% ({win_count}W/{loss_count}L)"
                send_telegram(summary)

            threading.Thread(target=check_result).start()

=== ON OPEN ===

def on_open(ws): ws.send(json.dumps({"authorize": DERIV_TOKEN})) time.sleep(1) for s in watchlist: ws.send(json.dumps({"ticks_subscribe": 1, "symbol": s})) send_telegram("🤖 SpikeBot Pro is live.\nTracking Volatility & Forex markets...")

=== WS CLIENT ===

def run_bot(): websocket.enableTrace(False) ws = websocket.WebSocketApp( "wss://ws.derivws.com/websockets/v3?app_id=1089", on_message=on_message, on_open=on_open ) ws.run_forever()

=== MAIN ===

if name == "main": threading.Thread(target=run_bot).start()

