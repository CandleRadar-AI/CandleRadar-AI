import os
import sqlite3
from datetime import datetime, timedelta, timezone

import requests
from flask import Flask, jsonify, request, render_template

app = Flask(__name__)

API_KEY = os.getenv("TWELVE_DATA_API_KEY", "").strip()

API_URL = "https://api.twelvedata.com/time_series"
SEARCH_API_URL = "https://api.twelvedata.com/symbol_search"

DB_NAME = os.getenv("DB_NAME", "signals.db")

ALLOWED_INTERVALS = {
    "1min": 60,
    "5min": 300,
    "15min": 900,
}

# Maximum allowed data delay
MAX_DATA_DELAY_SECONDS = {
    "1min": 150,
    "5min": 750,
    "15min": 2250,
}


def check_data_freshness(candles, interval):
    if not candles:
        return {
            "fresh": False,
            "age_seconds": None,
            "message": "কোনো ক্যান্ডেল পাওয়া যায়নি।"
        }

    try:
        latest = datetime.strptime(
            candles[-1]["datetime"],
            "%Y-%m-%d %H:%M:%S"
        ).replace(tzinfo=timezone.utc)

        duration = timedelta(
            seconds=ALLOWED_INTERVALS[interval]
        )

        candle_close_time = latest + duration
        now = datetime.now(timezone.utc)

        raw_age = (now - candle_close_time).total_seconds()
        if raw_age < -5:
            return {
                "fresh": False,
                "age_seconds": None,
                "latest_candle": candles[-1]["datetime"],
                "message": "ক্যান্ডেলের সময় ভবিষ্যতের; ডেটা যাচাই করা যাচ্ছে না।",
            }

        age_seconds = max(0, int(raw_age))
        max_delay = MAX_DATA_DELAY_SECONDS[interval]

        return {
            "fresh": age_seconds <= max_delay,
            "age_seconds": age_seconds,
            "latest_candle": candles[-1]["datetime"],
            "message": (
                "ডেটা আপডেট আছে।"
                if age_seconds <= max_delay
                else "ডেটা পুরোনো। নতুন সিগন্যাল দেওয়া বন্ধ।"
            )
        }

    except (KeyError, ValueError):
        return {
            "fresh": False,
            "age_seconds": None,
            "message": "ক্যান্ডেলের সময় যাচাই করা যায়নি।"
        }


# -------------------------
# Home
# -------------------------

@app.route("/")
def home():
    return render_template("index.html")


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
                expiry_candle_time TEXT,
                result TEXT NOT NULL DEFAULT 'PENDING',
                created_at TEXT NOT NULL,
                UNIQUE(symbol, interval, candle_time)
            )
        """)


init_db()

# Safe migration for databases created by older app versions.
with sqlite3.connect(DB_NAME) as _conn:
    _columns = {row[1] for row in _conn.execute("PRAGMA table_info(signals)")}
    if "expiry_candle_time" not in _columns:
        _conn.execute("ALTER TABLE signals ADD COLUMN expiry_candle_time TEXT")


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
    weekday = now.weekday()

    if weekday == 5:
        return True

    if weekday == 4 and now.hour >= 21:
        return True

    if weekday == 6 and now.hour < 21:
        return True

    return False


# -------------------------
# Smart Pair Search
# -------------------------

@app.route("/search-pair")
def search_pair():
    query = request.args.get("q", "").strip()

    if not query:
        return jsonify({
            "status": "error",
            "message": "পেয়ারের নাম লিখুন।"
        }), 400

    if not API_KEY:
        return jsonify({
            "status": "error",
            "message": "Twelve Data API key missing."
        }), 500

    try:
        response = requests.get(
            SEARCH_API_URL,
            params={
                "symbol": query,
                "apikey": API_KEY,
            },
            timeout=20,
        )

        data = response.json()

        if response.status_code != 200 or "data" not in data:
            return jsonify({
                "status": "error",
                "message": data.get(
                    "message",
                    "পেয়ার খুঁজে পাওয়া যায়নি।"
                )
            }), 404

        results = []

        for item in data.get("data", []):
            symbol = item.get("symbol", "").strip()

            instrument_type = item.get(
                "instrument_type",
                item.get("type", "")
            )

            if not symbol or "/" not in symbol:
                continue

            results.append({
                "symbol": symbol,
                "name": item.get("instrument_name", ""),
                "exchange": item.get("exchange", ""),
                "type": instrument_type,
            })

        return jsonify({
            "status": "ok",
            "query": query,
            "results": results,
            "count": len(results),
            "message": "পেয়ার সার্চ সম্পন্ন হয়েছে।"
        })

    except (ValueError, requests.RequestException) as error:
        return jsonify({
            "status": "error",
            "message": str(error)
        }), 502


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
        message = data.get(
            "message",
            "Candle data unavailable"
        )
        raise ValueError(message)

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

    duration = timedelta(
        seconds=ALLOWED_INTERVALS[interval]
    )

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


# -------------------------
# Candle patterns
# -------------------------

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
# Candle analysis
# -------------------------

def analyze_candles(candles):

    if len(candles) < 60:
        raise ValueError(
            "বিশ্লেষণের জন্য কমপক্ষে ৬০টি ক্যান্ডেল প্রয়োজন।"
        )

    closes = [float(c["close"]) for c in candles]
    highs = [float(c["high"]) for c in candles]
    lows = [float(c["low"]) for c in candles]

    def calculate_ema(data, period):
        if len(data) < period:
            raise ValueError("EMA calculation needs more candles.")

        multiplier = 2 / (period + 1)
        ema = sum(data[:period]) / period

        for price in data[period:]:
            ema = (
                price * multiplier
                + ema * (1 - multiplier)
            )

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
        ema12 = (
            price * multiplier12
            + ema12 * (1 - multiplier12)
        )
        ema12_values.append(ema12)

    for price in closes[26:]:
        ema26 = (
            price * multiplier26
            + ema26 * (1 - multiplier26)
        )
        ema26_values.append(ema26)

    macd_values = [
        fast - slow
        for fast, slow in zip(
            ema12_values[-len(ema26_values):],
            ema26_values
        )
    ]

    macd = macd_values[-1]
    signal_line = calculate_ema(macd_values, 9)
    macd_histogram = macd - signal_line

    # Support and Resistance

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

    # RSI extreme-zone safety filter.
    # Do not block every RSI reading above 70/below 30: strong trends can
    # remain overbought/oversold. Only block a fresh entry at extreme levels.
    rsi_zone = "neutral"
    if rsi >= 70:
        rsi_zone = "overbought"
    elif rsi <= 30:
        rsi_zone = "oversold"

    if bullish_score >= 4 and bullish_score > bearish_score:
        if rsi >= 80:
            signal = "WAIT"
            reason = (
                "Bullish সূচক মিলেছে, কিন্তু RSI 80 বা তার বেশি। "
                "দাম অতিরিক্ত বেড়ে থাকতে পারে; নতুন UP এন্ট্রির আগে অপেক্ষা করো।"
            )
        else:
            signal = "UP"
            reason = "একাধিক সূচকে Bullish প্রবণতা পাওয়া গেছে।"
            if rsi >= 70:
                reason += " RSI 70-এর ওপরে; অতিরিক্ত সতর্ক থাকো।"

    elif bearish_score >= 4 and bearish_score > bullish_score:
        if rsi <= 20:
            signal = "WAIT"
            reason = (
                "Bearish সূচক মিলেছে, কিন্তু RSI 20 বা তার কম। "
                "দাম অতিরিক্ত নেমে থাকতে পারে; নতুন DOWN এন্ট্রির আগে অপেক্ষা করো।"
            )
        else:
            signal = "DOWN"
            reason = "একাধিক সূচকে Bearish প্রবণতা পাওয়া গেছে।"
            if rsi <= 30:
                reason += " RSI 30-এর নিচে; অতিরিক্ত সতর্ক থাকো।"

    else:
        signal = "WAIT"
        reason = "পর্যাপ্ত মিল পাওয়া যায়নি। অপেক্ষা করো।"

    return {
        "signal": signal,
        "ema20": round(ema20, 6),
        "ema50": round(ema50, 6),
        "rsi": round(rsi, 2),
        "rsi_zone": rsi_zone,
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

def _parse_candle_time(value):
    """Parse Twelve Data UTC candle timestamps safely."""
    return datetime.strptime(value, "%Y-%m-%d %H:%M:%S").replace(
        tzinfo=timezone.utc
    )


def save_signal(symbol, interval, candle_time, result):
    """Save a signal and its exact next-candle expiry target."""
    if result["signal"] not in ("UP", "DOWN"):
        return

    expiry_time = _parse_candle_time(candle_time) + timedelta(
        seconds=ALLOWED_INTERVALS[interval]
    )
    expiry_candle_time = expiry_time.strftime("%Y-%m-%d %H:%M:%S")

    with sqlite3.connect(DB_NAME) as conn:
        conn.execute("""
            INSERT OR IGNORE INTO signals
            (symbol, interval, candle_time, signal, entry_price,
             expiry_candle_time, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (
            symbol, interval, candle_time, result["signal"],
            float(result["last_close"]), expiry_candle_time,
            datetime.now(timezone.utc).isoformat(),
        ))


def evaluate_pending_signals(symbol, interval, candles):
    """Resolve each signal only against its own exact expiry candle close."""
    if not candles:
        return

    candle_closes = {}
    for candle in candles:
        try:
            candle_closes[candle["datetime"]] = float(candle["close"])
        except (KeyError, TypeError, ValueError):
            continue

    with sqlite3.connect(DB_NAME) as conn:
        pending = conn.execute("""
            SELECT id, candle_time, signal, entry_price, expiry_candle_time
            FROM signals
            WHERE symbol = ? AND interval = ? AND result = 'PENDING'
        """, (symbol, interval)).fetchall()

        for signal_id, candle_time, signal, entry_price, expiry_time in pending:
            # Older records are migrated by calculating their intended expiry.
            if not expiry_time:
                try:
                    expiry_time = (
                        _parse_candle_time(candle_time)
                        + timedelta(seconds=ALLOWED_INTERVALS[interval])
                    ).strftime("%Y-%m-%d %H:%M:%S")
                    conn.execute(
                        "UPDATE signals SET expiry_candle_time = ? WHERE id = ?",
                        (expiry_time, signal_id),
                    )
                except (KeyError, ValueError):
                    continue

            # Do not use a later candle as a substitute. Wait until exact expiry candle exists.
            expiry_close = candle_closes.get(expiry_time)
            if expiry_close is None:
                continue

            entry = float(entry_price)
            if expiry_close == entry:
                outcome = "DRAW"
            elif signal == "UP":
                outcome = "WIN" if expiry_close > entry else "LOSS"
            else:
                outcome = "WIN" if expiry_close < entry else "LOSS"

            conn.execute("""
                UPDATE signals SET result = ?
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
        all_candles = fetch_candles(symbol, interval, 100)
        candles = get_closed_candles(all_candles, interval)
        freshness = check_data_freshness(candles, interval)

        return jsonify({
            "status": "ok",
            "symbol": symbol,
            "interval": interval,
            "candles": candles[-20:],
            "data_fresh": freshness["fresh"],
            "data_age_seconds": freshness["age_seconds"],
            "data_message": freshness["message"],
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

    # Weekend filter for forex pairs
    if "/" in symbol and is_forex_market_closed():
        return jsonify({
            "status": "market_closed",
            "symbol": symbol,
            "interval": interval,
            "signal": "MARKET CLOSED",
            "message": (
                "ফরেক্স মার্কেট বন্ধ। "
                "এখন নতুন সিগন্যাল দেওয়া হচ্ছে না।"
            ),
        })

    try:
        # Fetch market candles
        all_candles = fetch_candles(symbol, interval, 100)

        # Keep only closed candles
        closed = get_closed_candles(all_candles, interval)

        # Check data freshness
        freshness = check_data_freshness(closed, interval)

        if not freshness["fresh"]:
            return jsonify({
                "status": "stale_data",
                "symbol": symbol,
                "interval": interval,
                "signal": "WAIT",
                "data_fresh": False,
                "data_age_seconds": freshness["age_seconds"],
                "last_closed_candle": (
                    closed[-1]["datetime"] if closed else None
                ),
                "message": freshness["message"]
            }), 200

        # Check minimum candle requirement
        if len(closed) < 60:
            return jsonify({
                "status": "error",
                "message": (
                    "বিশ্লেষণের জন্য পর্যাপ্ত "
                    "বন্ধ ক্যান্ডেল পাওয়া যায়নি।"
                ),
            }), 503

        # Evaluate previous pending signals
        evaluate_pending_signals(symbol, interval, closed)

        # Analyze candles
        result = analyze_candles(closed)

        last_candle_time = closed[-1]["datetime"]

        # Save current signal
        save_signal(
            symbol,
            interval,
            last_candle_time,
            result
        )

        # Successful response
        return jsonify({
            "status": "ok",
            "symbol": symbol,
            "interval": interval,
            "signal": result["signal"],
            "analysis": result,
            "last_closed_candle": last_candle_time,
            "data_fresh": freshness["fresh"],
            "data_age_seconds": freshness["age_seconds"],
            "data_message": freshness["message"],
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
# Historical backtesting
# -------------------------

@app.route("/backtest")
def backtest():
    """Test the current rule-based strategy on historical closed candles.

    A signal is generated using candles available up to and including candle i.
    Entry is candle i close; outcome is measured only at the exact next candle
    close. Missing next candles are skipped rather than replaced with later data.
    """
    symbol = request.args.get("symbol", "EUR/USD").strip()
    interval = request.args.get("interval", "1min").strip()
    try:
        requested = int(request.args.get("candles", "500"))
    except (TypeError, ValueError):
        return jsonify({"status": "error", "message": "candles অবশ্যই সংখ্যা হতে হবে।"}), 400

    if interval not in ALLOWED_INTERVALS:
        return jsonify({"status": "error", "message": "Use 1min, 5min or 15min."}), 400
    if not symbol or len(symbol) > 40:
        return jsonify({"status": "error", "message": "সঠিক symbol দিন।"}), 400
    if requested < 100 or requested > 2000:
        return jsonify({"status": "error", "message": "candles 100 থেকে 2000-এর মধ্যে দিন।"}), 400

    try:
        raw = fetch_candles(symbol, interval, requested)
        closed = get_closed_candles(raw, interval)
        if len(closed) < 61:
            return jsonify({
                "status": "error",
                "message": "Backtesting-এর জন্য কমপক্ষে ৬১টি বন্ধ ক্যান্ডেল দরকার।",
                "available_closed_candles": len(closed),
            }), 503

        by_time = {c["datetime"]: c for c in closed}
        duration = ALLOWED_INTERVALS[interval]
        counts = {"WIN": 0, "LOSS": 0, "DRAW": 0}
        signal_counts = {"UP": 0, "DOWN": 0, "WAIT": 0}
        tested = 0
        skipped_missing_expiry = 0
        examples = []

        # Start after enough history for the indicators. Never use future candles
        # when calculating the signal for a historical candle.
        for i in range(59, len(closed) - 1):
            history = closed[:i + 1]
            current = history[-1]
            try:
                analysis = analyze_candles(history)
            except (ValueError, KeyError, TypeError):
                continue

            direction = analysis.get("signal")
            if direction not in signal_counts:
                continue
            signal_counts[direction] += 1
            if direction == "WAIT":
                continue

            expiry_dt = _parse_candle_time(current["datetime"]) + timedelta(seconds=duration)
            expiry_key = expiry_dt.strftime("%Y-%m-%d %H:%M:%S")
            expiry_candle = by_time.get(expiry_key)
            if expiry_candle is None:
                skipped_missing_expiry += 1
                continue

            entry = float(current["close"])
            exit_price = float(expiry_candle["close"])
            if exit_price == entry:
                outcome = "DRAW"
            elif direction == "UP":
                outcome = "WIN" if exit_price > entry else "LOSS"
            else:
                outcome = "WIN" if exit_price < entry else "LOSS"

            counts[outcome] += 1
            tested += 1
            if len(examples) < 20:
                examples.append({
                    "signal_candle": current["datetime"],
                    "expiry_candle": expiry_key,
                    "signal": direction,
                    "entry_price": entry,
                    "expiry_close": exit_price,
                    "result": outcome,
                })

        decisive = counts["WIN"] + counts["LOSS"]
        win_rate = round(counts["WIN"] / decisive * 100, 2) if decisive else 0
        return jsonify({
            "status": "ok",
            "symbol": symbol,
            "interval": interval,
            "expiry": f"{duration} seconds (next candle close)",
            "requested_candles": requested,
            "available_closed_candles": len(closed),
            "signals_generated": signal_counts,
            "tested_signals": tested,
            "wins": counts["WIN"],
            "losses": counts["LOSS"],
            "draws": counts["DRAW"],
            "skipped_missing_expiry": skipped_missing_expiry,
            "win_rate": win_rate,
            "win_rate_basis": "wins / (wins + losses); draws excluded",
            "sample_warning": "এটি অতীতের ডেটায় নিয়মভিত্তিক backtest; ভবিষ্যৎ ফল বা লাভের নিশ্চয়তা নয়। Spread, payout, execution delay ও Quotex-এর আলাদা price feed এতে অন্তর্ভুক্ত নয়।",
            "sample_trades": examples,
        })
    except (ValueError, requests.RequestException) as error:
        return jsonify({"status": "error", "message": str(error)}), 502


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

        draws = conn.execute(
            "SELECT COUNT(*) FROM signals WHERE result = 'DRAW'"
        ).fetchone()[0]

    completed = wins + losses + draws

    win_rate = (
        round((wins / completed) * 100, 2)
        if completed else 0
    )

    return jsonify({
        "status": "ok",
        "total_signals": total,
        "wins": wins,
        "losses": losses,
        "draws": draws,
        "pending": pending,
        "completed": completed,
        "win_rate": win_rate,
        "win_rate_basis": "wins / (wins + losses); draws excluded",
    })


# -------------------------
# Start server
# -------------------------

if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=int(os.getenv("PORT", "5000")),
    )
