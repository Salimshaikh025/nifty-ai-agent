import os
import threading
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from flask import Flask, jsonify, render_template_string

from growwapi import GrowwAPI, GrowwFeed


# ============================================================
# CONFIGURATION
# ============================================================

IST = ZoneInfo("Asia/Kolkata")

ACCESS_TOKEN = os.getenv("GROWW_ACCESS_TOKEN")
GROWW_TOTP_TOKEN = os.getenv("GROWW_TOTP_TOKEN")
GROWW_TOTP_SECRET = os.getenv("GROWW_TOTP_SECRET")

AI_MIN_CANDLES = 25

app = Flask(__name__)

groww = None
feed = None
groww_started = False
last_groww_attempt_at = 0.0
RECONNECT_COOLDOWN_SECONDS = 15

state = {
    "status": "STARTING",
    "nifty": None,
    "signal": "WAIT",
    "confidence": 0,
    "call_score": 0,
    "put_score": 0,
    "candles_loaded": 0,
    "last_update": None,
    "candle": {
        "open": None,
        "high": None,
        "low": None,
        "close": None
    },
    "message": "Starting..."
}

candles = []
current_candle = None

lock = threading.Lock()


# ============================================================
# HELPERS
# ============================================================

def candle_start(dt):

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

        avg_gain = (
            (avg_gain * (period - 1)) + gains[i]
        ) / period

        avg_loss = (
            (avg_loss * (period - 1)) + losses[i]
        ) / period

    if avg_loss == 0:
        return 100

    rs = avg_gain / avg_loss

    return 100 - (100 / (1 + rs))


# ============================================================
# SIGNAL ENGINE - RETURNS A PERCENTAGE, NOT JUST A LABEL
#
# Same underlying indicators as before (EMA9/21, RSI14,
# momentum, breakout) but expressed as a 0-100% confidence
# score for BOTH the call side and the put side, so the
# person can see the full picture and decide manually,
# instead of only getting a binary WAIT/BUY label.
# ============================================================

MAX_SCORE = 5

def calculate_scores():

    with lock:
        completed = list(candles)
        price = state["nifty"]

    if len(completed) < AI_MIN_CANDLES or price is None:
        return 0, 0, len(completed)

    closes = [float(c["close"]) for c in completed]

    ema9 = ema(closes, 9)
    ema21 = ema(closes, 21)
    rsi14 = rsi(closes, 14)

    if ema9 is None or ema21 is None or rsi14 is None:
        return 0, 0, len(completed)

    recent_high = max(closes[-10:])
    recent_low = min(closes[-10:])

    call_points = 0
    put_points = 0

    # Trend
    if ema9 > ema21:
        call_points += 1
    elif ema9 < ema21:
        put_points += 1

    # Price vs EMA
    if price > ema9:
        call_points += 1
    elif price < ema9:
        put_points += 1

    # RSI zone
    if 52 <= rsi14 <= 68:
        call_points += 1
    elif 32 <= rsi14 <= 48:
        put_points += 1

    # Momentum
    if len(closes) >= 4:

        momentum = closes[-1] - closes[-4]

        if momentum > 0:
            call_points += 1
        elif momentum < 0:
            put_points += 1

    # Breakout
    if price > recent_high:
        call_points += 1

    if price < recent_low:
        put_points += 1

    call_pct = round((call_points / MAX_SCORE) * 100)
    put_pct = round((put_points / MAX_SCORE) * 100)

    return call_pct, put_pct, len(completed)


def update_signal():

    call_pct, put_pct, candle_count = calculate_scores()

    if call_pct > put_pct:
        label = "CALL"
        confidence = call_pct
    elif put_pct > call_pct:
        label = "PUT"
        confidence = put_pct
    else:
        label = "WAIT"
        confidence = 0

    with lock:

        state["call_score"] = call_pct
        state["put_score"] = put_pct
        state["confidence"] = confidence
        state["signal"] = label
        state["candles_loaded"] = candle_count


# ============================================================
# BUILD LIVE 5-MINUTE CANDLES FROM TICKS
#
# This app deliberately does NOT call the historical-candles
# API (that endpoint requires Groww's separate paid Trading
# API subscription and returns "Access forbidden" without
# it). Candles build up live instead, so it needs about
# 2 hours after (re)starting before showing a real signal.
# ============================================================

def process_price(price, timestamp_ms):

    global current_candle

    dt = datetime.fromtimestamp(
        timestamp_ms / 1000,
        tz=IST
    )

    start = candle_start(dt)

    with lock:

        if current_candle is None:

            current_candle = {
                "time": start.isoformat(),
                "open": price,
                "high": price,
                "low": price,
                "close": price
            }

        elif start != datetime.fromisoformat(
            current_candle["time"]
        ):

            candles.append(current_candle.copy())

            if len(candles) > 300:
                candles.pop(0)

            current_candle = {
                "time": start.isoformat(),
                "open": price,
                "high": price,
                "low": price,
                "close": price
            }

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
            "open": round(current_candle["open"], 2),
            "high": round(current_candle["high"], 2),
            "low": round(current_candle["low"], 2),
            "close": round(current_candle["close"], 2)
        }

        state["last_update"] = dt.strftime(
            "%Y-%m-%d %H:%M:%S"
        )


# ============================================================
# BACKGROUND SIGNAL LOOP
# ============================================================

def signal_loop():

    while True:

        try:
            update_signal()
        except Exception as e:
            print("Signal loop error:", repr(e))

        time.sleep(2)


# ============================================================
# GROWW LIVE FEED
# ============================================================

def start_groww():

    global groww
    global feed
    global groww_started

    resolved_token = ACCESS_TOKEN
    token_source = "static GROWW_ACCESS_TOKEN"

    if GROWW_TOTP_TOKEN and GROWW_TOTP_SECRET:

        try:

            import pyotp

            totp_code = pyotp.TOTP(
                GROWW_TOTP_SECRET
            ).now()

            resolved_token = GrowwAPI.get_access_token(
                api_key=GROWW_TOTP_TOKEN,
                totp=totp_code
            )

            token_source = "TOTP (auto-generated)"

        except Exception as e:

            print(
                "TOTP token generation failed, "
                "falling back to static token:",
                repr(e)
            )

            resolved_token = ACCESS_TOKEN
            token_source = (
                "static GROWW_ACCESS_TOKEN (TOTP failed)"
            )

    if not resolved_token:

        with lock:
            state["status"] = "ERROR"
            state["message"] = (
                "No Groww credentials configured"
            )

        groww_started = False
        return

    try:

        with lock:
            state["status"] = "AUTHENTICATING"
            state["message"] = (
                "Connecting to Groww using "
                + token_source
                + "..."
            )

        groww = GrowwAPI(resolved_token)

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
                    state["message"] = "NIFTY live feed connected"

            except Exception as e:

                print("Feed callback error:", repr(e))

        feed.subscribe_index_value(
            instruments,
            on_data_received=on_data_received
        )

        with lock:
            state["status"] = "LIVE"
            state["message"] = "NIFTY live feed connected"

        feed.consume()

    except Exception as e:

        print("Groww connection error:", repr(e))

        with lock:
            state["status"] = "ERROR"
            state["message"] = str(e)

    finally:

        groww_started = False


def ensure_groww_started():

    global groww_started
    global last_groww_attempt_at

    if groww_started:
        return

    with lock:

        if groww_started:
            return

        now = time.time()

        if (
            now - last_groww_attempt_at
            < RECONNECT_COOLDOWN_SECONDS
        ):
            return

        last_groww_attempt_at = now
        groww_started = True

    threading.Thread(
        target=start_groww,
        daemon=True
    ).start()


# ============================================================
# API
# ============================================================

@app.route("/api/status")
def api_status():

    ensure_groww_started()

    with lock:

        return jsonify({
            "status": state["status"],
            "nifty": state["nifty"],
            "signal": state["signal"],
            "confidence": state["confidence"],
            "call_score": state["call_score"],
            "put_score": state["put_score"],
            "candles_loaded": state["candles_loaded"],
            "last_update": state["last_update"],
            "candle": state["candle"],
            "message": state["message"]
        })


# ============================================================
# UI
# ============================================================

HTML = """
<!DOCTYPE html>
<html>
<head>
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>NIFTY SIGNAL</title>
<style>

:root {
    --bg:#0b0f14;
    --card:#141a22;
    --card-border:#232c3a;
    --text:#e8edf2;
    --text-dim:#7c8a9a;
    --green:#22c55e;
    --green-bg:rgba(34,197,94,0.12);
    --red:#f5455c;
    --red-bg:rgba(245,69,92,0.12);
    --amber:#f5b942;
}

* { box-sizing:border-box; }

body {
    margin:0;
    background:var(--bg);
    color:var(--text);
    font-family:-apple-system,Arial,sans-serif;
}

.container {
    max-width:480px;
    margin:auto;
    padding:16px;
}

h1 {
    text-align:center;
    font-size:18px;
    letter-spacing:0.5px;
    margin:6px 0 16px 0;
}

.card {
    background:var(--card);
    border:1px solid var(--card-border);
    border-radius:14px;
    padding:16px;
    margin-bottom:12px;
}

.price {
    text-align:center;
    font-size:40px;
    font-weight:bold;
    margin:4px;
}

.signal {
    text-align:center;
    font-size:28px;
    font-weight:bold;
    padding:16px;
    border:1px solid var(--card-border);
    border-radius:12px;
    background:#1a222c;
    color:var(--text-dim);
}

.signal.call {
    border-color:var(--green);
    background:var(--green-bg);
    color:var(--green);
}

.signal.put {
    border-color:var(--red);
    background:var(--red-bg);
    color:var(--red);
}

.confidence {
    text-align:center;
    font-size:15px;
    color:var(--text-dim);
    margin-top:10px;
}

.bars {
    margin-top:14px;
}

.bar-row {
    display:flex;
    align-items:center;
    gap:8px;
    margin-bottom:8px;
    font-size:13px;
}

.bar-label {
    width:50px;
    color:var(--text-dim);
}

.bar-track {
    flex:1;
    height:10px;
    background:#1a222c;
    border-radius:6px;
    overflow:hidden;
}

.bar-fill {
    height:100%;
}

.bar-fill.call {
    background:var(--green);
}

.bar-fill.put {
    background:var(--red);
}

.bar-pct {
    width:38px;
    text-align:right;
}

.row {
    display:flex;
    justify-content:space-between;
    padding:6px 0;
    font-size:14px;
    border-bottom:1px solid #1e2733;
}

.row:last-child {
    border-bottom:none;
}

.status {
    text-align:center;
    color:var(--text-dim);
    font-size:11px;
    margin-top:6px;
}

.status.live { color:var(--green); }
.status.err { color:var(--red); }

.note {
    color:var(--text-dim);
    font-size:11px;
    line-height:1.5;
    text-align:center;
    margin-top:10px;
}

</style>
</head>
<body>
<div class="container">

<h1>NIFTY SIGNAL (MANUAL)</h1>

<div class="card">

<div id="price" class="price">--</div>

<div id="signal" class="signal">WAIT</div>

<div id="confidence" class="confidence">Confidence: 0%</div>

<div class="bars">

<div class="bar-row">
<div class="bar-label">CALL</div>
<div class="bar-track"><div id="callBar" class="bar-fill call" style="width:0%"></div></div>
<div id="callPct" class="bar-pct">0%</div>
</div>

<div class="bar-row">
<div class="bar-label">PUT</div>
<div class="bar-track"><div id="putBar" class="bar-fill put" style="width:0%"></div></div>
<div id="putPct" class="bar-pct">0%</div>
</div>

</div>

</div>

<div class="card">

<div class="row"><span>Candles ready</span><b id="candleCount">0 / 25</b></div>
<div class="row"><span>Open</span><b id="open">--</b></div>
<div class="row"><span>High</span><b id="high">--</b></div>
<div class="row"><span>Low</span><b id="low">--</b></div>
<div class="row"><span>Close</span><b id="close">--</b></div>

</div>

<div id="status" class="status">Connecting...</div>

<div class="note">
This app only shows a live confidence reading. It never
places any order. Needs about 25 completed 5-minute
candles (roughly 2 hours) after each restart before it
shows a real reading.
</div>

</div>

<script>

function setText(id, value) {
    const el = document.getElementById(id);
    if (el) el.innerText = value;
}

async function update() {
    const controller = new AbortController();
    const timeoutId = setTimeout(() => controller.abort(), 8000);
    try {
        const response = await fetch("/api/status", {
            method: "GET",
            cache: "no-store",
            headers: {"Cache-Control": "no-cache"},
            signal: controller.signal
        });
        clearTimeout(timeoutId);

        if (!response.ok) throw new Error("HTTP " + response.status);
        const d = await response.json();

        setText("price", d.nifty != null ? Number(d.nifty).toFixed(2) : "--");

        const sig = d.signal || "WAIT";
        setText("signal", sig);
        const sigEl = document.getElementById("signal");
        if (sigEl) {
            sigEl.className = "signal" + (sig === "CALL" ? " call" : (sig === "PUT" ? " put" : ""));
        }

        setText("confidence", "Confidence: " + Number(d.confidence || 0) + "%");

        const callPct = Number(d.call_score || 0);
        const putPct = Number(d.put_score || 0);

        setText("callPct", callPct + "%");
        setText("putPct", putPct + "%");

        const callBar = document.getElementById("callBar");
        if (callBar) callBar.style.width = callPct + "%";

        const putBar = document.getElementById("putBar");
        if (putBar) putBar.style.width = putPct + "%";

        setText("candleCount", (d.candles_loaded || 0) + " / 25");

        const c = d.candle || {};
        setText("open", c.open !== undefined && c.open !== null ? c.open : "--");
        setText("high", c.high !== undefined && c.high !== null ? c.high : "--");
        setText("low", c.low !== undefined && c.low !== null ? c.low : "--");
        setText("close", c.close !== undefined && c.close !== null ? c.close : "--");

        setText("status", (d.status || "UNKNOWN") + " | " + (d.message || ""));
        const statusEl = document.getElementById("status");
        if (statusEl) statusEl.className = "status " + (d.status === "LIVE" ? "live" : "err");

    } catch (e) {
        clearTimeout(timeoutId);
        setText("status", "APP ERROR: " + (e.name === "AbortError" ? "request timed out" : e.message));
        const statusEl2 = document.getElementById("status");
        if (statusEl2) statusEl2.className = "status err";
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

    ensure_groww_started()

    response = app.make_response(
        render_template_string(HTML)
    )

    response.headers["Cache-Control"] = (
        "no-store, no-cache, must-revalidate, max-age=0"
    )

    return response


# ============================================================
# START BACKGROUND SERVICES
# ============================================================

threading.Thread(
    target=signal_loop,
    daemon=True
).start()


# ============================================================
# START SERVER
# ============================================================

if __name__ == "__main__":

    port = int(os.environ.get("PORT", 10000))

    app.run(
        host="0.0.0.0",
        port=port,
        debug=False
    )
