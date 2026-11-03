# CandleRadar AI

## Files
- `app.py` — Flask backend
- `templates/index.html` — Bengali web interface
- `requirements.txt` — Python dependencies

## Run locally
1. Install Python 3.10+.
2. Install dependencies: `pip install -r requirements.txt`
3. Set `TWELVE_DATA_API_KEY` to your Twelve Data API key.
4. Start: `gunicorn app:app` (or `python app.py` for local development).

## Render settings
- Build command: `pip install -r requirements.txt`
- Start command: `gunicorn app:app`
- Environment variable: `TWELVE_DATA_API_KEY`
- Optional: set `DB_NAME` to a path on a Render persistent disk if signal history must survive restarts/redeploys.

## Signal result rules
A signal created from a closed candle uses the following candle as its expiry candle: 1-minute signals expire after the next 1-minute candle, 5-minute signals after the next 5-minute candle, and 15-minute signals after the next 15-minute candle. The result is checked against that exact candle's close. Equal entry and expiry prices are recorded as `DRAW`. If the exact expiry candle is not available yet, the result remains `PENDING`.

## Important limitations
- The signal engine is rule-based (`rule_based_v1`), not a trained AI model.
- Twelve Data prices and instrument availability may differ from Quotex. The app does not connect to Quotex's internal price feed and cannot guarantee that a pair is available there.
- Signals are not guaranteed to win. Test on historical/demo data before risking money.
- The weekend filter is approximate and is not a broker holiday/session calendar.
- SQLite data may be lost on redeploy/restart unless `DB_NAME` points to persistent storage.
