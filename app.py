import os
import sqlite3
from datetime import datetime, timedelta, timezone
import requests
from flask import Flask, jsonify, request

app = Flask(__name__)

API_KEY = os.getenv("TWELVE_DATA_API_KEY")
API_URL = "https://api.twelvedata.com/time_series"

DB_NAME = "signals.db"


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
                result TEXT DEFAULT 'PENDING',
                created_at TEXT NOT NULL,
                UNIQUE(symbol, interval, candle_time)
            )
        """)


init_db()
@app.route("/")
def home():
    return jsonify({
        "app": "CandleRadar AI",
        "status": "online",
        "message": "AI server is running"
    })


@app.route("/health")
def health():
    return jsonify({"status": "ok"})


@app.route("/candles")
def candles():
    if not API_KEY:
        return jsonify({
            "status": "error",
            "message": "API key is not configured"
        }), 500

    symbol = request.args.get("symbol", "EUR/USD").upper()
    interval = request.args.get("interval", "1min")

    allowed_intervals = {
        "1min", "5min", "15min", "30min",
        "45min", "1h", "2h", "4h", "1day"
    }

    if interval not in allowed_intervals:
        return jsonify({
            "status": "error",
            "message": "Unsupported interval"
        }), 400

    try:
        response = requests.get(
            API_URL,
            params={
                "symbol": symbol,
                "interval": interval,
                "outputsize": 100,
                "apikey": API_KEY
            },
            timeout=15
        )

        data = response.json()

        if response.status_code != 200 or "values" not in data:
            return jsonify({
                "status": "error",
                "message": data.get(
                    "message", "Market data unavailable"
                )
            }), 502

        return jsonify({
            "status": "ok",
            "symbol": symbol,
            "interval": interval,
            "candles": data["values"]
        })

    except requests.RequestException:
        return jsonify({
            "status": "error",
            "message": "Could not connect to market data provider"
        }), 502

def calculate_ema(prices, period=20):
    ema = prices[0]
    multiplier = 2 / (period + 1)

    for price in prices[1:]:
        ema = (price - ema) * multiplier + ema

    return ema


def analyze_candles(candles):
    if len(candles) < 50:
        return {
            "signal": "WAIT",
            "reason": "পর্যাপ্ত ক্যান্ডেল ডেটা নেই"
        }

    # পুরোনো ক্যান্ডেল আগে
    candles = sorted(
        candles,
        key=lambda c: c["datetime"]
    )

    # সর্বশেষ সম্পূর্ণ বন্ধ হওয়া ক্যান্ডেল
    latest = candles[-1]

    closes = [float(c["close"]) for c in candles]

    ema20 = calculate_ema(closes, 20)

    open_price = float(latest["open"])
    close_price = float(latest["close"])

    if close_price > ema20 and close_price > open_price:
        signal = "UP"
        reason = "দাম EMA 20-এর ওপরে এবং শেষ ক্যান্ডেল বুলিশ"

    elif close_price < ema20 and close_price < open_price:
        signal = "DOWN"
        reason = "দাম EMA 20-এর নিচে এবং শেষ ক্যান্ডেল বিয়ারিশ"

    else:
        signal = "WAIT"
        reason = "মার্কেটের দিক স্পষ্ট নয়"

    return {
        "signal": signal,
        "reason": reason,
        "ema20": round(ema20, 5),
        "last_close": close_price
}

@app.route("/predict")
def predict():
    if not API_KEY:
        return jsonify({
            "status": "error",
            "message": "API key missing"
        }), 500

    symbol = request.args.get("symbol", "EUR/USD")
    interval = request.args.get("interval", "1min")

    seconds = {
        "1min": 60,
        "5min": 300,
        "15min": 900
    }

    if interval not in seconds:
        return jsonify({
            "status": "error",
            "message": "Use 1min, 5min or 15min"
        }), 400

    try:
        response = requests.get(
            API_URL,
            params={
                "symbol": symbol,
                "interval": interval,
                "outputsize": 100,
                "apikey": API_KEY,
                "timezone": "UTC"
            },
            timeout=15
        )

        data = response.json()

        if response.status_code != 200 or "values" not in data:
            return jsonify({
                "status": "error",
                "message": data.get("message", "Data unavailable")
            }), 502

        now = datetime.now(timezone.utc)
        duration = timedelta(seconds=seconds[interval])

        closed = []

        for candle in data["values"]:
            start = datetime.strptime(
                candle["datetime"],
                "%Y-%m-%d %H:%M:%S"
            ).replace(tzinfo=timezone.utc)

            if start + duration <= now:
                closed.append(candle)

        if len(closed) < 50:
            return jsonify({
                "status": "error",
                "message": "Not enough closed candles"
            }), 503

        result = analyze_candles(closed)
        
        if result["signal"] in ("UP", "DOWN"):
            with sqlite3.connect(DB_NAME) as conn:
                conn.execute("""
                    INSERT OR IGNORE INTO signals
                    (symbol, interval, candle_time, signal,
                     entry_price, created_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, (
                    symbol,
                    interval,
                    max(c["datetime"] for c in closed),
                    result["signal"],
                    result["last_close"],
                                        datetime.now(timezone.utc).isoformat()
                ))
        return jsonify({
            "status": "ok",
            "symbol": symbol,
            "interval": interval,
            "last_closed_candle": max(
                c["datetime"] for c in closed
            ),
            "analysis": result,
            "model": "rule_based_v1"
        })

    except (requests.RequestException, ValueError, KeyError, TypeError):
        return jsonify({
            "status": "error",
            "message": "Prediction unavailable"
        }), 502

@app.route("/stats")
def stats():
    with sqlite3.connect(DB_NAME) as conn:
        total = conn.execute(
            "SELECT COUNT(*) FROM signals"
        ).fetchone()[0]

        pending = conn.execute(
            "SELECT COUNT(*) FROM signals WHERE result = 'PENDING'"
        ).fetchone()[0]

        wins = conn.execute(
            "SELECT COUNT(*) FROM signals WHERE result = 'WIN'"
        ).fetchone()[0]

        losses = conn.execute(
            "SELECT COUNT(*) FROM signals WHERE result = 'LOSS'"
        ).fetchone()[0]

    completed = wins + losses
    win_rate = round(wins / completed * 100, 2) if completed else 0

    return jsonify({
        "status": "ok",
        "total_signals": total,
        "pending": pending,
        "wins": wins,
        "losses": losses,
        "win_rate": win_rate
    })

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
