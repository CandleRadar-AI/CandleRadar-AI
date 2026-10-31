import os
import sqlite3
from datetime import datetime, timedelta, timezone

import requests
from flask import Flask, jsonify, request, render_template

app = Flask(__name__)
@app.route("/")
def home():
    return render_template("index.html")

API_KEY = os.getenv("TWELVE_DATA_API_KEY", "").strip()
API_URL = "https://api.twelvedata.com/time_series"
DB_NAME = os.getenv("DB_NAME", "signals.db")

ALLOWED_INTERVALS = {
    "1min": 60,
    "5min": 300,
    "15min": 900,
}


# -------------------------
# Database
# -------------------------

def init_db():
    with sqlite3.connect(DB_NAME) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS signals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL,
                interval TEXT NOT NULL,
                candle_time TEXT NOT NULL,
                signal TEXT NOT NULL,
                entry_price REAL NOT NULL,
                result TEXT NOT NULL DEFAULT 'PENDING',
                created_at TEXT NOT NULL,
                UNIQUE(symbol, interval, candle_time)
            )
        """)


init_db()


# -------------------------
# Market status
# -------------------------

def is_forex_market_closed():
    """
    Basic forex weekend filter.
    Uses UTC and approximate 21:00 UTC closing/reopening time.
    This is not an exact holiday or broker-hours calendar.
    """
    now = datetime.now(timezone.utc)
    weekday = now.weekday()  # Monday=0, Sunday=6

    if weekday == 5:  # Saturday
        return True

    if weekday == 4 and now.hour >= 21:  # Friday evening
        return True

    if weekday == 6 and now.hour < 21:  # Sunday before reopening
        return True

    return False


# -------------------------
# Fetch candles
# -------------------------

def fetch_candles(symbol, interval, outputsize=100):
    if not API_KEY:
        raise ValueError("Twelve Data API key is missing.")

    response = requests.get(
        API_URL,
        params={
            "symbol": symbol,
            "interval": interval,
            "outputsize": outputsize,
            "apikey": API_KEY,
            "timezone": "UTC",
        },
        timeout=20,
    )

    try:
        data = response.json()
    except ValueError:
        raise ValueError("API returned an invalid response.")

    if response.status_code != 200 or "values" not in data:
        message = data.get("message", "Candle data unavailable")
        raise ValueError(message)

    # Twelve Data normally returns newest candle first.
    candles = sorted(
        data["values"],
        key=lambda candle: candle["datetime"]
    )

    return candles


# -------------------------
# Closed candles only
# -------------------------

def get_closed_candles(candles, interval):
    now = datetime.now(timezone.utc)
    duration = timedelta(seconds=ALLOWED_INTERVALS[interval])
    closed = []

    for candle in candles:
        try:
            start = datetime.strptime(
                candle["datetime"],
                "%Y-%m-%d %H:%M:%S"
            ).replace(tzinfo=timezone.utc)

            if start + duration <= now:
                closed.append(candle)

        except (KeyError, ValueError):
            continue

    return closed


def detect_candle_patterns(candles):
    if len(candles) < 3:
        return []

    patterns = []

    a = candles[-3]
    b = candles[-2]
    c = candles[-1]

    def values(candle):
        o = float(candle["open"])
        h = float(candle["high"])
        l = float(candle["low"])
        cl = float(candle["close"])
        return o, h, l, cl

    o, h, l, cl = values(c)

    body = abs(cl - o)
    lower_wick = min(o, cl) - l
    upper_wick = h - max(o, cl)

    # Single candle patterns
    if body > 0 and lower_wick >= body * 2 and upper_wick <= body:
        if cl > o:
            patterns.append("Hammer")

    if body > 0 and upper_wick >= body * 2 and lower_wick <= body:
        if cl < o:
            patterns.append("Shooting Star")

    if body <= (h - l) * 0.1 and h > l:
        patterns.append("Doji")

    # Double candle patterns
    bo, bh, bl, bc = values(b)

    if bc < bo and cl > o and o <= bc and cl >= bo:
        patterns.append("Bullish Engulfing")

    if bc > bo and cl < o and o >= bc and cl <= bo:
        patterns.append("Bearish Engulfing")

    # Multiple candle patterns
    ao, ah, al, ac = values(a)

    if ac < ao and cl > o and cl > (ao + ac) / 2:
        if abs(bc - bo) < abs(ac - ao) * 0.5:
            patterns.append("Morning Star")

    if ac > ao and cl < o and cl < (ao + ac) / 2:
        if abs(bc - bo) < abs(ac - ao) * 0.5:
            patterns.append("Evening Star")

    return patterns
# -------------------------
# EMA 20 analysis
# -------------------------

def analyze_candles(candles):
    if len(candles) < 60:
        raise ValueError("বিশ্লেষণের জন্য কমপক্ষে ৬০টি ক্যান্ডেল প্রয়োজন।")

    closes = [float(c["close"]) for c in candles]
    highs = [float(c["high"]) for c in candles]
    lows = [float(c["low"]) for c in candles]

    def calculate_ema(data, period):
        multiplier = 2 / (period + 1)
        ema = sum(data[:period]) / period

        for price in data[period:]:
            ema = price * multiplier + ema * (1 - multiplier)

        return ema

    # EMA 20 and EMA 50
    ema20 = calculate_ema(closes, 20)
    ema50 = calculate_ema(closes, 50)

    # RSI 14
    changes = [
        closes[i] - closes[i - 1]
        for i in range(1, len(closes))
    ]

    gains = [max(change, 0) for change in changes[-14:]]
    losses = [max(-change, 0) for change in changes[-14:]]

    avg_gain = sum(gains) / 14
    avg_loss = sum(losses) / 14

    if avg_loss == 0:
        rsi = 100 if avg_gain > 0 else 50
    else:
        rs = avg_gain / avg_loss
        rsi = 100 - (100 / (1 + rs))

    # MACD 12, 26, 9
    multiplier12 = 2 / 13
    multiplier26 = 2 / 27

    ema12 = sum(closes[:12]) / 12
    ema26 = sum(closes[:26]) / 26

    ema12_values = []
    ema26_values = []

    for price in closes[12:]:
        ema12 = price * multiplier12 + ema12 * (1 - multiplier12)
        ema12_values.append(ema12)

    for price in closes[26:]:
        ema26 = price * multiplier26 + ema26 * (1 - multiplier26)
        ema26_values.append(ema26)

    macd_values = [
        fast - slow
        for fast, slow in zip(ema12_values[-len(ema26_values):], ema26_values)
    ]

    macd = macd_values[-1]
    signal_line = calculate_ema(macd_values, 9)
    macd_histogram = macd - signal_line

    # Support and Resistance (last 20 candles)
    support = min(lows[-20:])
    resistance = max(highs[-20:])

    last_close = closes[-1]
    patterns = detect_candle_patterns(candles)

    bullish_patterns = [
        "Hammer",
        "Bullish Engulfing",
        "Morning Star"
    ]

    bearish_patterns = [
        "Shooting Star",
        "Bearish Engulfing",
        "Evening Star"
    ]

    bullish_score = 0
    bearish_score = 0

    if last_close > ema20:
        bullish_score += 1
    elif last_close < ema20:
        bearish_score += 1

    if ema20 > ema50:
        bullish_score += 1
    elif ema20 < ema50:
        bearish_score += 1

    if rsi > 50:
        bullish_score += 1
    elif rsi < 50:
        bearish_score += 1

    if macd_histogram > 0:
        bullish_score += 1
    elif macd_histogram < 0:
        bearish_score += 1

    if any(p in patterns for p in bullish_patterns):
        bullish_score += 1

    if any(p in patterns for p in bearish_patterns):
        bearish_score += 1

    if bullish_score >= 4 and bullish_score > bearish_score:
        signal = "UP"
        reason = "একাধিক সূচকে Bullish প্রবণতা পাওয়া গেছে।"

    elif bearish_score >= 4 and bearish_score > bullish_score:
        signal = "DOWN"
        reason = "একাধিক সূচকে Bearish প্রবণতা পাওয়া গেছে।"

    else:
        signal = "WAIT"
        reason = "পর্যাপ্ত মিল পাওয়া যায়নি। অপেক্ষা করো।"

    return {
        "signal": signal,
        "ema20": round(ema20, 6),
        "ema50": round(ema50, 6),
        "rsi": round(rsi, 2),
        "macd": round(macd, 6),
        "macd_signal": round(signal_line, 6),
        "macd_histogram": round(macd_histogram, 6),
        "support": round(support, 6),
        "resistance": round(resistance, 6),
        "last_close": last_close,
        "bullish_score": bullish_score,
        "bearish_score": bearish_score,
        "reason": reason,
        "patterns": patterns,
    }


# -------------------------
# Save signal
# -------------------------

def save_signal(symbol, interval, candle_time, result):
    if result["signal"] not in ("UP", "DOWN"):
        return

    with sqlite3.connect(DB_NAME) as conn:
        conn.execute("""
            INSERT OR IGNORE INTO signals
            (symbol, interval, candle_time, signal, entry_price, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (
            symbol,
            interval,
            candle_time,
            result["signal"],
            result["last_close"],
            datetime.now(timezone.utc).isoformat(),
        ))


# -------------------------
# Evaluate pending signals
# -------------------------

def evaluate_pending_signals(symbol, interval, candles):
    if not candles:
        return

    latest_time = candles[-1]["datetime"]
    latest_close = float(candles[-1]["close"])

    with sqlite3.connect(DB_NAME) as conn:
        pending = conn.execute("""
            SELECT id, candle_time, signal, entry_price
            FROM signals
            WHERE symbol = ?
              AND interval = ?
              AND result = 'PENDING'
              AND candle_time < ?
        """, (symbol, interval, latest_time)).fetchall()

        for row in pending:
            signal_id, candle_time, signal, entry_price = row

            if signal == "UP":
                outcome = (
                    "WIN" if latest_close > entry_price else "LOSS"
                )
            else:
                outcome = (
                    "WIN" if latest_close < entry_price else "LOSS"
                )

            conn.execute("""
                UPDATE signals
                SET result = ?
                WHERE id = ? AND result = 'PENDING'
            """, (outcome, signal_id))


# -------------------------
# Health check
# -------------------------

@app.route("/health")
def health():
    return jsonify({"status": "ok"})


# -------------------------
# Candle endpoint
# -------------------------

@app.route("/candles")
def candles_endpoint():
    symbol = request.args.get("symbol", "EUR/USD").strip()
    interval = request.args.get("interval", "1min").strip()

    if interval not in ALLOWED_INTERVALS:
        return jsonify({
            "status": "error",
            "message": "Use 1min, 5min or 15min",
        }), 400

    try:
        candles = fetch_candles(symbol, interval, 20)

        return jsonify({
            "status": "ok",
            "symbol": symbol,
            "interval": interval,
            "candles": candles,
        })

    except (ValueError, requests.RequestException) as error:
        return jsonify({
            "status": "error",
            "message": str(error),
        }), 502


# -------------------------
# Prediction endpoint
# -------------------------

@app.route("/predict")
def predict():
    symbol = request.args.get("symbol", "EUR/USD").strip()
    interval = request.args.get("interval", "1min").strip()

    if interval not in ALLOWED_INTERVALS:
        return jsonify({
            "status": "error",
            "message": "Use 1min, 5min or 15min",
        }), 400

    # Weekend filter for forex pairs.
    if "/" in symbol and is_forex_market_closed():
        return jsonify({
            "status": "market_closed",
            "symbol": symbol,
            "interval": interval,
            "signal": "MARKET CLOSED",
            "message": "ফরেক্স মার্কেট বন্ধ। এখন নতুন সিগন্যাল দেওয়া হচ্ছে না।",
        })

    try:
        all_candles = fetch_candles(symbol, interval, 100)
        closed = get_closed_candles(all_candles, interval)

        if len(closed) < 50:
            return jsonify({
                "status": "error",
                "message": "বিশ্লেষণের জন্য পর্যাপ্ত বন্ধ ক্যান্ডেল পাওয়া যায়নি।",
            }), 503

        # Update outcomes for previously saved signals.
        evaluate_pending_signals(symbol, interval, closed)

        result = analyze_candles(closed)
        last_candle_time = closed[-1]["datetime"]

        # Save only one signal per candle.
        save_signal(
            symbol,
            interval,
            last_candle_time,
            result
        )

        return jsonify({
            "status": "ok",
            "symbol": symbol,
            "interval": interval,
            "signal": result["signal"],
            "analysis": result,
            "last_closed_candle": last_candle_time,
            "model": "rule_based_v1",
            "message": (
                "এটি সাধারণ নিয়মভিত্তিক বিশ্লেষণ; "
                "সিগন্যাল নিশ্চিত নয়।"
            ),
        })

    except (ValueError, requests.RequestException) as error:
        return jsonify({
            "status": "error",
            "message": str(error),
        }), 502


# -------------------------
# Statistics endpoint
# -------------------------

@app.route("/stats")
def stats():
    with sqlite3.connect(DB_NAME) as conn:
        total = conn.execute(
            "SELECT COUNT(*) FROM signals"
        ).fetchone()[0]

        wins = conn.execute(
            "SELECT COUNT(*) FROM signals WHERE result = 'WIN'"
        ).fetchone()[0]

        losses = conn.execute(
            "SELECT COUNT(*) FROM signals WHERE result = 'LOSS'"
        ).fetchone()[0]

        pending = conn.execute(
            "SELECT COUNT(*) FROM signals WHERE result = 'PENDING'"
        ).fetchone()[0]

    completed = wins + losses
    win_rate = round((wins / completed) * 100, 2) if completed else 0

    return jsonify({
        "status": "ok",
        "total_signals": total,
        "wins": wins,
        "losses": losses,
        "pending": pending,
        "win_rate": win_rate,
    })


# -------------------------
# Start server
# -------------------------

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)))
