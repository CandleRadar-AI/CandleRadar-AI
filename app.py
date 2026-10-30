import os
import requests
from flask import Flask, jsonify, request

app = Flask(__name__)

API_KEY = os.getenv("TWELVE_DATA_API_KEY")
API_URL = "https://api.twelvedata.com/time_series"


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
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
