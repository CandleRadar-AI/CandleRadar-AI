import os
import sqlite3
from datetime import datetime, timedelta, timezone

import requests
from flask import Flask, jsonify, request

app = Flask(__name__)

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
    if len(candles) < 50:
        raise ValueError("Not enough candles for analysis.")

    closes = [float(candle["close"]) for candle in candles]

    period = 20
    multiplier = 2 / (period + 1)

    ema = sum(closes[:period]) / period

    for price in closes[period:]:
        ema = (price * multiplier) + (ema * (1 - multiplier))

    last_close = closes[-1]
    previous_close = closes[-2]

    # Basic rule-based signal, not a guaranteed prediction.
    if last_close > ema and last_close > previous_close:
        signal = "UP"
        reason = "দাম EMA 20-এর ওপরে এবং শেষ ক্লোজ আগের ক্লোজের চেয়ে বেশি।"

    elif last_close < ema and last_close < previous_close:
        signal = "DOWN"
        reason = "দাম EMA 20-এর নিচে এবং শেষ ক্লোজ আগের ক্লোজের চেয়ে কম।"

    else:
        signal = "WAIT"
        reason = "মার্কেটের দিক স্পষ্ট নয়।"

    patterns = detect_candle_patterns(candles)

    return {
        "signal": signal,
        "ema20": round(ema, 6),
        "last_close": last_close,
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
