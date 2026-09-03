import os
import threading
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from flask import Flask, jsonify, render_template_string

from growwapi import GrowwAPI, GrowwFeed


# ============================================================
# CONFIGURATION
# ============================================================

IST = ZoneInfo("Asia/Kolkata")

API_KEY = os.getenv("GROWW_API_KEY")
API_SECRET = os.getenv("GROWW_API_SECRET")

app = Flask(__name__)

# Live NIFTY state
state = {
    "status": "STARTING",
    "nifty": None,
    "signal": "WAIT",
    "position": "NONE",
    "last_update": None,
    "candle": {
        "time": None,
        "open": None,
        "high": None,
        "low": None,
        "close": None
    },
    "message": "Starting...",
}

# Completed 5-minute candles
candles = []

# Current developing candle
current_candle = None

# Lock for thread safety
lock = threading.Lock()


# ============================================================
# HELPERS
# ============================================================

def now_ist():
    return datetime.now(IST)


def candle_start(dt):
    """
    Convert a timestamp to the beginning of its 5-minute candle.
    """
    minute = (dt.minute // 5) * 5
    return dt.replace(
        minute=minute,
        second=0,
        microsecond=0
    )


def ema(values, period):
    if len(values) < period:
        return None

    multiplier = 2 / (period + 1)

    value = sum(values[:period]) / period

    for price in values[period:]:
        value = (price - value) * multiplier + value

    return value


def rsi(values, period=14):
    if len(values) < period + 1:
        return None

    gains = []
    losses = []

    for i in range(1, len(values)):
        change = values[i] - values[i - 1]

        if change > 0:
            gains.append(change)
            losses.append(0)
        else:
            gains.append(0)
            losses.append(abs(change))

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    for i in range(period, len(gains)):
        avg_gain = ((avg_gain * (period - 1)) + gains[i]) / period
        avg_loss = ((avg_loss * (period - 1)) + losses[i]) / period

    if avg_loss == 0:
        return 100

    rs = avg_gain / avg_loss

    return 100 - (100 / (1 + rs))


def calculate_signal(price):
    """
    Simple deterministic signal engine.

    This is NOT yet a trained AI model.
    It is a live technical signal engine designed
    for testing/backtesting first.
    """

    with lock:
        completed = list(candles)

    if len(completed) < 25:
        return "WAIT"

    closes = [float(x["close"]) for x in completed]

    ema9 = ema(closes, 9)
    ema21 = ema(closes, 21)
    rsi14 = rsi(closes, 14)

    if ema9 is None or ema21 is None or rsi14 is None:
        return "WAIT"

    recent_high = max(closes[-10:])
    recent_low = min(closes[-10:])

    score = 0

    # Trend
    if ema9 > ema21:
        score += 1
    elif ema9 < ema21:
        score -= 1

    # Price vs EMA
    if price > ema9:
        score += 1
    elif price < ema9:
        score -= 1

    # RSI
    if 55 <= rsi14 <= 70:
        score += 1
    elif 30 <= rsi14 <= 45:
        score -= 1

    # Momentum
    if len(closes) >= 4:
        momentum = closes[-1] - closes[-4]

        if momentum > 0:
            score += 1
        elif momentum < 0:
            score -= 1

    # Breakout
    if price > recent_high:
        score += 1

    if price < recent_low:
        score -= 1

    # Strong signal only
    if score >= 3:
        return "BUY CALL"

    if score <= -3:
        return "BUY PUT"

    return "WAIT"


def process_price(price, timestamp_ms):
    """
    Build the live 5-minute candle from incoming NIFTY ticks.
    """

    global current_candle

    dt = datetime.fromtimestamp(
        timestamp_ms / 1000,
        tz=IST
    )

    start = candle_start(dt)

    with lock:

        # First candle
        if current_candle is None:

            current_candle = {
                "time": start.isoformat(),
                "open": price,
                "high": price,
                "low": price,
                "close": price
            }

        # New 5-minute candle
        elif start > datetime.fromisoformat(
            current_candle["time"]
        ):

            # Save completed candle
            candles.append(current_candle.copy())

            # Keep enough history in memory
            if len(candles) > 300:
                candles.pop(0)

            current_candle = {
                "time": start.isoformat(),
                "open": price,
                "high": price,
                "low": price,
                "close": price
            }

        # Same candle
        else:

            current_candle["high"] = max(
                current_candle["high"],
                price
            )

            current_candle["low"] = min(
                current_candle["low"],
                price
            )

            current_candle["close"] = price

        state["nifty"] = round(price, 2)

        state["candle"] = {
            "time": current_candle["time"],
            "open": round(current_candle["open"], 2),
            "high": round(current_candle["high"], 2),
            "low": round(current_candle["low"], 2),
            "close": round(current_candle["close"], 2)
        }

        state["last_update"] = dt.strftime(
            "%Y-%m-%d %H:%M:%S"
        )


# ============================================================
# LOAD HISTORICAL 5-MINUTE CANDLES
# ============================================================

def load_history(groww):

    global candles

    try:

        end = now_ist()
        start = end - timedelta(days=5)

        response = groww.get_historical_candles(
            exchange=groww.EXCHANGE_NSE,
            segment=groww.SEGMENT_CASH,
            groww_symbol="NSE-NIFTY",
            start_time=start.strftime(
                "%Y-%m-%d %H:%M:%S"
            ),
            end_time=end.strftime(
                "%Y-%m-%d %H:%M:%S"
            ),
            candle_interval="5minute"
        )

        raw = response.get("candles", [])

        loaded = []

        for c in raw:

            if len(c) < 5:
                continue

            loaded.append({
                "time": datetime.fromtimestamp(
                    float(c[0]),
                    tz=IST
                ).isoformat(),

                "open": float(c[1]),
                "high": float(c[2]),
                "low": float(c[3]),
                "close": float(c[4])
            })

        with lock:
            candles = loaded[-300:]

        state["message"] = (
            f"Loaded {len(candles)} historical candles"
        )

        print(
            f"Loaded {len(candles)} historical candles"
        )

    except Exception as e:

        print("Historical data error:", e)

        state["message"] = (
            "Historical data unavailable. "
            "Waiting for live data."
        )


# ============================================================
# LIVE SIGNAL ENGINE
# ============================================================

def signal_loop():

    while True:

        try:

            with lock:
                price = state["nifty"]
                old_position = state["position"]

            if price is not None:

                raw_signal = calculate_signal(price)

                with lock:

                    # ----------------------------------------
                    # NO POSITION
                    # ----------------------------------------

                    if old_position == "NONE":

                        if raw_signal == "BUY CALL":

                            state["signal"] = "BUY CALL"
                            state["position"] = "CALL"

                        elif raw_signal == "BUY PUT":

                            state["signal"] = "BUY PUT"
                            state["position"] = "PUT"

                        else:

                            state["signal"] = "WAIT"


                    # ----------------------------------------
                    # CALL POSITION
                    # ----------------------------------------

                    elif old_position == "CALL":

                        if raw_signal == "BUY PUT":

                            state["signal"] = "EXIT CALL"
                            state["position"] = "NONE"

                        else:

                            state["signal"] = "HOLD CALL"


                    # ----------------------------------------
                    # PUT POSITION
                    # ----------------------------------------

                    elif old_position == "PUT":

                        if raw_signal == "BUY CALL":

                            state["signal"] = "EXIT PUT"
                            state["position"] = "NONE"

                        else:

                            state["signal"] = "HOLD PUT"

        except Exception as e:

            print("Signal error:", e)

        time.sleep(1)


# ============================================================
# GROWW LIVE FEED
# ============================================================

def start_groww():

    if not API_KEY or not API_SECRET:

        with lock:
            state["status"] = "ERROR"
            state["message"] = (
                "Groww API credentials not configured"
            )

        print(
            "ERROR: GROWW_API_KEY and GROWW_API_SECRET "
            "are missing."
        )

        return

    try:

        with lock:
            state["status"] = "AUTHENTICATING"
            state["message"] = "Connecting to Groww..."

        # Current Groww API authentication method
        access_token = GrowwAPI.get_access_token(
            api_key=API_KEY,
            secret=API_SECRET
        )

        groww = GrowwAPI(access_token)

        # Load previous 5-minute candles
        load_history(groww)

        feed = GrowwFeed(groww)

        instruments = [
            {
                "exchange": "NSE",
                "segment": "CASH",
                "exchange_token": "NIFTY"
            }
        ]

        def on_data_received(meta):

            try:

                data = feed.get_index_value()

                nifty = (
                    data
                    .get("NSE", {})
                    .get("CASH", {})
                    .get("NIFTY", {})
                )

                price = nifty.get("value")
                timestamp = nifty.get("tsInMillis")

                if price is None:
                    return

                if timestamp is None:
                    timestamp = int(time.time() * 1000)

                process_price(
                    float(price),
                    float(timestamp)
                )

                with lock:
                    state["status"] = "LIVE"
                    state["message"] = "Live NIFTY feed connected"

            except Exception as e:

                print("Feed callback error:", e)

        print("Subscribing to NIFTY live feed...")

        feed.subscribe_index_value(
            instruments,
            on_data_received=on_data_received
        )

        with lock:
            state["status"] = "LIVE"
            state["message"] = "NIFTY live feed connected"

        # Blocking feed loop
        feed.consume()

    except Exception as e:

        print("Groww connection error:", e)

        with lock:
            state["status"] = "ERROR"
            state["message"] = str(e)


# ============================================================
# API
# ============================================================

@app.route("/api/status")
def api_status():

    with lock:

        return jsonify({
            "status": state["status"],
            "nifty": state["nifty"],
            "signal": state["signal"],
            "position": state["position"],
            "last_update": state["last_update"],
            "candle": state["candle"],
            "message": state["message"],
            "candles_loaded": len(candles)
        })


# ============================================================
# SIMPLE MOBILE UI
# ============================================================

HTML = """
<!DOCTYPE html>

<html>

<head>

<meta name="viewport"
      content="width=device-width, initial-scale=1">

<title>NIFTY AI SIGNAL</title>

<style>

body {
    margin: 0;
    background: #101114;
    color: white;
    font-family: Arial, sans-serif;
    text-align: center;
}

.container {
    max-width: 430px;
    margin: auto;
    padding: 25px 18px;
}

.title {
    font-size: 22px;
    font-weight: bold;
    margin-bottom: 25px;
}

.price {
    font-size: 38px;
    font-weight: bold;
    margin: 15px 0;
}

.signal {
    margin-top: 30px;
    padding: 35px 10px;
    border-radius: 20px;
    background: #202228;
    font-size: 34px;
    font-weight: bold;
}

.wait {
    color: #ffffff;
}

.buy {
    color: #00e676;
}

.exit {
    color: #ff5252;
}

.hold {
    color: #ffd740;
}

.status {
    margin-top: 20px;
    color: #aaa;
    font-size: 14px;
}

.candle {
    margin-top: 20px;
    background: #191b20;
    border-radius: 15px;
    padding: 15px;
    color: #aaa;
    font-size: 13px;
}

</style>

</head>

<body>

<div class="container">

<div class="title">
NIFTY AI SIGNAL
</div>

<div id="price" class="price">
--
</div>

<div id="signal" class="signal wait">
WAIT
</div>

<div id="status" class="status">
Connecting...
</div>

<div class="candle">

<div>
5 MIN CANDLE
</div>

<div id="candle">
O: -- &nbsp;
H: -- &nbsp;
L: -- &nbsp;
C: --
</div>

</div>

</div>


<script>

async function update() {

    try {

        const response =
            await fetch("/api/status");

        const data =
            await response.json();

        document.getElementById("price")
            .innerText =
            data.nifty !== null
            ? data.nifty.toFixed(2)
            : "--";

        const signal =
            document.getElementById("signal");

        signal.innerText =
            data.signal;

        signal.className =
            "signal " +
            (
                data.signal.includes("BUY")
                ? "buy"
                : data.signal.includes("EXIT")
                ? "exit"
                : data.signal.includes("HOLD")
                ? "hold"
                : "wait"
            );

        document.getElementById("status")
            .innerText =
            data.status +
            " | " +
            data.message;

        const c =
            data.candle;

        if (c) {

            document.getElementById("candle")
                .innerText =
                "O: " + (c.open ?? "--") +
                "   H: " + (c.high ?? "--") +
                "   L: " + (c.low ?? "--") +
                "   C: " + (c.close ?? "--");
        }

    } catch (e) {

        document.getElementById("status")
            .innerText =
            "Connection waiting...";
    }
}

update();

setInterval(update, 1000);

</script>

</body>

</html>
"""


@app.route("/")
def home():
    return render_template_string(HTML)


# ============================================================
# START BACKGROUND SERVICES
# ============================================================

threading.Thread(
    target=start_groww,
    daemon=True
).start()

threading.Thread(
    target=signal_loop,
    daemon=True
).start()


# ============================================================
# START SERVER
# ============================================================

if __name__ == "__main__":

    port = int(
        os.environ.get("PORT", 10000)
    )

    app.run(
        host="0.0.0.0",
        port=port,
        debug=False
    )
