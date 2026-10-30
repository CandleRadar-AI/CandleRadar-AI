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
                "outputsize": 20,
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


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
