from flask import Flask, jsonify
from datetime import datetime, timezone

app = Flask(__name__)

@app.route("/")
def home():
    return jsonify({
        "app": "CandleRadar AI",
        "status": "online",
        "message": "AI Server is running"
    })

@app.route("/health")
def health():
    return jsonify({
        "status": "ok",
        "time": datetime.now(timezone.utc).isoformat()
    })

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
