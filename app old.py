import os
import time
import threading
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo

from flask import Flask, jsonify, render_template_string, request
from growwapi import GrowwAPI, GrowwFeed


# ============================================================
# CONFIGURATION
# ============================================================

IST = ZoneInfo("Asia/Kolkata")

ACCESS_TOKEN = os.getenv("GROWW_ACCESS_TOKEN")

app = Flask(__name__)

AI_MIN_CONFIDENCE = 70.0

STOP_LOSS_PERCENT = 5.0
TARGET_PERCENT = 15.0

DEFAULT_MAX_TRADES = 1
DEFAULT_MAX_AMOUNT = 10000.0

MARKET_START = "09:20"
MARKET_END = "15:15"


# ============================================================
# STATE
# ============================================================

lock = threading.RLock()

groww = None
feed = None

groww_started = False
groww_thread = None

candles = []
current_candle = None

last_trade_date = None


state = {
    "status": "STARTING",
    "message": "Starting Groww connection...",

    "nifty": None,
    "last_update": None,

    "signal": "WAIT",
    "confidence": 0,

    "auto_pilot": False,

    "max_trades_today": DEFAULT_MAX_TRADES,
    "max_amount_today": DEFAULT_MAX_AMOUNT,

    "trades_today": 0,
    "amount_today": 0.0,

    "position": "NONE",

    "trading_symbol": None,
    "option_type": None,
    "strike": None,
    "expiry": None,

    "quantity": 0,
    "lot_size": 0,

    "entry_price": None,
    "current_price": None,

    "stop_loss": None,
    "target": None,

    "unrealised_pnl": 0.0,
    "realised_pnl": 0.0,
    "day_pnl": 0.0,

    "buy_order_id": None,
    "oco_order_id": None,

    "protection_status": "NONE",

    "trade_message": "No open position",

    "candle": {
        "time": None,
        "open": None,
        "high": None,
        "low": None,
        "close": None,
    },
}


# ============================================================
# TIME HELPERS
# ============================================================

def now_ist():
    return datetime.now(IST)


def today_string():
    return now_ist().strftime("%Y-%m-%d")


def market_is_open():

    t = now_ist().strftime("%H:%M")

    return MARKET_START <= t <= MARKET_END


def candle_start(dt):

    minute = (dt.minute // 5) * 5

    return dt.replace(
        minute=minute,
        second=0,
        microsecond=0
    )


# ============================================================
# DAILY RESET
# ============================================================

def check_new_day():

    global last_trade_date

    today = today_string()

    with lock:

        if last_trade_date is None:

            last_trade_date = today

        elif last_trade_date != today:

            state["trades_today"] = 0
            state["amount_today"] = 0.0
            state["realised_pnl"] = 0.0
            state["day_pnl"] = 0.0

            last_trade_date = today


# ============================================================
# EMA
# ============================================================

def ema(values, period):

    if len(values) < period:
        return None

    multiplier = 2 / (period + 1)

    result = sum(
        values[:period]
    ) / period

    for value in values[period:]:

        result = (
            (value - result)
            * multiplier
            + result
        )

    return result


# ============================================================
# RSI
# ============================================================

def rsi(values, period=14):

    if len(values) < period + 1:
        return None

    gains = []
    losses = []

    for i in range(1, len(values)):

        change = (
            values[i]
            - values[i - 1]
        )

        gains.append(
            max(change, 0)
        )

        losses.append(
            max(-change, 0)
        )

    avg_gain = (
        sum(gains[:period])
        / period
    )

    avg_loss = (
        sum(losses[:period])
        / period
    )

    for i in range(period, len(gains)):

        avg_gain = (
            avg_gain * (period - 1)
            + gains[i]
        ) / period

        avg_loss = (
            avg_loss * (period - 1)
            + losses[i]
        ) / period

    if avg_loss == 0:
        return 100.0

    rs = avg_gain / avg_loss

    return 100 - (
        100 / (1 + rs)
    )


# ============================================================
# RESEARCH ENGINE
#
# IMPORTANT:
# This is a research score.
# It is NOT yet a statistically calibrated probability.
# ============================================================

def calculate_research_signal():

    with lock:

        completed = list(candles)

    if len(completed) < 25:

        return "WAIT", 0

    closes = [
        float(c["close"])
        for c in completed
    ]

    ema9 = ema(closes, 9)
    ema21 = ema(closes, 21)

    rsi14 = rsi(
        closes,
        14
    )

    if (
        ema9 is None
        or ema21 is None
        or rsi14 is None
    ):

        return "WAIT", 0

    price = closes[-1]

    previous = closes[-2]

    momentum = price - previous

    recent_high = max(
        closes[-6:-1]
    )

    recent_low = min(
        closes[-6:-1]
    )

    call_score = 0
    put_score = 0

    # Trend
    if ema9 > ema21:
        call_score += 20

    elif ema9 < ema21:
        put_score += 20

    # Price vs EMA
    if price > ema9:
        call_score += 15

    elif price < ema9:
        put_score += 15

    # RSI
    if 52 <= rsi14 <= 68:
        call_score += 20

    elif 32 <= rsi14 <= 48:
        put_score += 20

    # Momentum
    if momentum > 0:
        call_score += 15

    elif momentum < 0:
        put_score += 15

    # Breakout
    if price > recent_high:
        call_score += 20

    elif price < recent_low:
        put_score += 20

    if call_score > put_score:

        return (
            "BUY CALL",
            float(call_score)
        )

    if put_score > call_score:

        return (
            "BUY PUT",
            float(put_score)
        )

    return "WAIT", 0


# ============================================================
# UPDATE SIGNAL
# ============================================================

def update_signal():

    signal, confidence = (
        calculate_research_signal()
    )

    with lock:

        state["confidence"] = confidence

        if state["position"] == "NONE":

            if confidence >= AI_MIN_CONFIDENCE:

                state["signal"] = signal

            else:

                state["signal"] = "WAIT"

        elif state["position"] == "CALL":

            state["signal"] = (
                "HOLD CALL"
            )

        elif state["position"] == "PUT":

            state["signal"] = (
                "HOLD PUT"
            )


# ============================================================
# LIVE NIFTY PROCESSING
# ============================================================

def process_nifty_price(
    price,
    timestamp_ms
):

    global current_candle
    global candles

    dt = datetime.fromtimestamp(
        float(timestamp_ms) / 1000,
        tz=IST
    )

    start = candle_start(dt)

    price = float(price)

    with lock:

        state["nifty"] = round(
            price,
            2
        )

        state["last_update"] = (
            dt.isoformat()
        )

        state["status"] = "LIVE"

        if current_candle is None:

            current_candle = {
                "time":
                    start.isoformat(),
                "open": price,
                "high": price,
                "low": price,
                "close": price,
            }

        elif start != datetime.fromisoformat(
            current_candle["time"]
        ):

            candles.append(
                current_candle
            )

            if len(candles) > 300:

                candles = candles[-300:]

            current_candle = {
                "time":
                    start.isoformat(),
                "open": price,
                "high": price,
                "low": price,
                "close": price,
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

        state["candle"] = dict(
            current_candle
        )


# ============================================================
# OPTION EXPIRY
# ============================================================

def get_nearest_expiry():

    today = now_ist().date()

    expiries = []

    try:

        response = groww.get_expiries(
            exchange=groww.EXCHANGE_NSE,
            underlying_symbol="NIFTY",
            year=today.year,
            month=today.month
        )

        expiries = response.get(
            "expiries",
            []
        )

    except Exception as e:

        print(
            "Expiry lookup error:",
            repr(e)
        )

    valid = []

    for expiry in expiries:

        try:

            d = datetime.strptime(
                expiry,
                "%Y-%m-%d"
            ).date()

            if d >= today:
                valid.append(
                    (d, expiry)
                )

        except Exception:
            continue

    if valid:

        valid.sort()

        return valid[0][1]

    # Try next month
    month = today.month + 1
    year = today.year

    if month > 12:

        month = 1
        year += 1

    try:

        response = groww.get_expiries(
            exchange=groww.EXCHANGE_NSE,
            underlying_symbol="NIFTY",
            year=year,
            month=month
        )

        expiries = response.get(
            "expiries",
            []
        )

        valid = []

        for expiry in expiries:

            try:

                d = datetime.strptime(
                    expiry,
                    "%Y-%m-%d"
                ).date()

                if d >= today:

                    valid.append(
                        (d, expiry)
                    )

            except Exception:
                pass

        if valid:

            valid.sort()

            return valid[0][1]

    except Exception as e:

        print(
            "Next expiry error:",
            repr(e)
        )

    return None


# ============================================================
# SELECT OPTION
# ============================================================

def select_option(signal):

    if groww is None:

        return None

    expiry = get_nearest_expiry()

    if expiry is None:

        return None

    try:

        chain = groww.get_option_chain(
            exchange=groww.EXCHANGE_NSE,
            underlying="NIFTY",
            expiry_date=expiry
        )

    except Exception as e:

        print(
            "Option chain error:",
            repr(e)
        )

        return None

    strikes = chain.get(
        "strikes",
        {}
    )

    underlying_ltp = chain.get(
        "underlying_ltp"
    )

    if underlying_ltp is None:

        with lock:
            underlying_ltp = (
                state["nifty"]
            )

    if underlying_ltp is None:

        return None

    # Select nearest ATM strike
    candidates = []

    for strike_key in strikes.keys():

        try:

            strike = float(
                strike_key
            )

            candidates.append(
                strike
            )

        except Exception:
            continue

    if not candidates:

        return None

    strike = min(
        candidates,
        key=lambda x:
            abs(
                x - float(
                    underlying_ltp
                )
            )
    )

    option_type = (
        "CE"
        if signal == "BUY CALL"
        else "PE"
    )

    option_data = strikes.get(
        str(int(strike)),
        {}
    ).get(
        option_type
    )

    if option_data is None:

        # Try exact key as decimal
        option_data = strikes.get(
            str(strike),
            {}
        ).get(
            option_type
        )

    if option_data is None:

        return None

    trading_symbol = option_data.get(
        "trading_symbol"
    )

    ltp = option_data.get(
        "ltp"
    )

    if not trading_symbol or not ltp:

        return None

    # Liquidity protection
    volume = float(
        option_data.get(
            "volume",
            0
        ) or 0
    )

    open_interest = float(
        option_data.get(
            "open_interest",
            0
        ) or 0
    )

    if volume <= 0 and open_interest <= 0:

        return None

    # Get instrument details for lot size
    try:

        instrument = (
            groww.get_instrument_by_groww_symbol(
                groww_symbol=(
                    f"NSE-NIFTY-"
                    f"{datetime.strptime(expiry, '%Y-%m-%d').strftime('%d%b%y')}-"
                    f"{int(strike)}-"
                    f"{option_type}"
                )
            )
        )

    except Exception:

        instrument = None

    lot_size = 0

    if instrument:

        try:

            lot_size = int(
                float(
                    instrument.get(
                        "lot_size",
                        0
                    )
                )
            )

        except Exception:
            lot_size = 0

    # Fallback to instrument master
    if lot_size <= 0:

        try:

            df = groww.get_all_instruments()

            rows = df[
                (
                    df["trading_symbol"]
                    == trading_symbol
                )
            ]

            if len(rows) > 0:

                lot_size = int(
                    rows.iloc[0]["lot_size"]
                )

        except Exception as e:

            print(
                "Lot size lookup error:",
                repr(e)
            )

    if lot_size <= 0:

        return None

    return {
        "expiry": expiry,
        "strike": strike,
        "option_type": option_type,
        "trading_symbol": trading_symbol,
        "ltp": float(ltp),
        "lot_size": lot_size,
        "volume": volume,
        "open_interest": open_interest,
    }


# ============================================================
# HARD ENTRY CHECKS
# ============================================================

def validate_trade(option):

    if option is None:

        return False, (
            "Option contract unavailable"
        )

    price = float(
        option["ltp"]
    )

    lot_size = int(
        option["lot_size"]
    )

    if price <= 0:

        return False, (
            "Invalid option price"
        )

    if lot_size <= 0:

        return False, (
            "Invalid lot size"
        )

    with lock:

        if not state["auto_pilot"]:

            return False, (
                "Auto-Pilot OFF"
            )

        if state["position"] != "NONE":

            return False, (
                "Position already open"
            )

        if (
            state["trades_today"]
            >= state["max_trades_today"]
        ):

            return False, (
                "Daily trade limit reached"
            )

        estimated_amount = (
            price * lot_size
        )

        if (
            state["amount_today"]
            + estimated_amount
            > state["max_amount_today"]
        ):

            return False, (
                "Daily amount limit reached"
            )

        if (
            state["confidence"]
            < AI_MIN_CONFIDENCE
        ):

            return False, (
                "AI confidence below 70%"
            )

    # Protection calculation
    stop_loss = round(
        price * (
            1
            - STOP_LOSS_PERCENT / 100
        ),
        2
    )

    target = round(
        price * (
            1
            + TARGET_PERCENT / 100
        ),
        2
    )

    if (
        stop_loss <= 0
        or target <= 0
        or stop_loss >= price
        or target <= price
    ):

        return False, (
            "SL/Target validation failed"
        )

    return True, "OK"


# ============================================================
# PLACE LIVE BUY
# ============================================================

def place_live_buy(option):

    valid, reason = (
        validate_trade(option)
    )

    if not valid:

        with lock:
            state["trade_message"] = (
                "ORDER BLOCKED: "
                + reason
            )

        return False

    symbol = option[
        "trading_symbol"
    ]

    quantity = int(
        option["lot_size"]
    )

    reference = (
        "AI"
        + uuid.uuid4()
        .hex[:16]
    )

    try:

        response = groww.place_order(

            trading_symbol=symbol,

            quantity=quantity,

            validity=groww.VALIDITY_DAY,

            exchange=groww.EXCHANGE_NSE,

            segment=groww.SEGMENT_FNO,

            product=groww.PRODUCT_NRML,

            order_type=groww.ORDER_TYPE_MARKET,

            transaction_type=(
                groww.TRANSACTION_TYPE_BUY
            ),

            order_reference_id=reference
        )

        order_id = response.get(
            "groww_order_id"
        )

        status = response.get(
            "order_status",
            ""
        )

        if not order_id:

            with lock:
                state["trade_message"] = (
                    "BUY BLOCKED: "
                    "No order ID returned"
                )

            return False

        print(
            "BUY ORDER:",
            response
        )

        # ----------------------------------------------------
        # Wait for actual execution
        # ----------------------------------------------------

        fill_price = None
        filled_quantity = 0

        for _ in range(50):

            try:

                status_response = (
                    groww.get_order_status(
                        groww_order_id=order_id,
                        segment=groww.SEGMENT_FNO
                    )
                )

                order_status = (
                    status_response
                    .get(
                        "order_status",
                        ""
                    )
                    .upper()
                )

                filled_quantity = int(
                    status_response.get(
                        "filled_quantity",
                        0
                    )
                    or 0
                )

                if order_status in (
                    "EXECUTED",
                    "COMPLETED",
                    "FILLED"
                ):

                    break

                if order_status in (
                    "REJECTED",
                    "CANCELLED"
                ):

                    with lock:
                        state["trade_message"] = (
                            "BUY "
                            + order_status
                        )

                    return False

            except Exception as e:

                print(
                    "Order status error:",
                    repr(e)
                )

            time.sleep(0.05)

        # ----------------------------------------------------
        # Get actual trade execution
        # ----------------------------------------------------

        try:

            trades_response = (
                groww.get_trade_list_for_order(
                    groww_order_id=order_id,
                    segment=groww.SEGMENT_FNO,
                    page=0,
                    page_size=50
                )
            )

            trade_list = (
                trades_response
                .get(
                    "trade_list",
                    []
                )
            )

            if trade_list:

                fill_price = float(
                    trade_list[0]["price"]
                )

                filled_quantity = sum(
                    int(
                        t.get(
                            "quantity",
                            0
                        )
                    )
                    for t in trade_list
                    if t.get(
                        "trade_status",
                        ""
                    ).upper()
                    == "EXECUTED"
                )

        except Exception as e:

            print(
                "Trade lookup error:",
                repr(e)
            )

        if (
            fill_price is None
            or filled_quantity <= 0
        ):

            # Do not pretend the order is protected.
            # Reconcile actual broker position before continuing.
            with lock:
                state["trade_message"] = (
                    "BUY EXECUTED/UNKNOWN: "
                    "fill not confirmed"
                )

            return False

        # ----------------------------------------------------
        # Calculate protection from ACTUAL fill
        # ----------------------------------------------------

        stop_loss = round(
            fill_price * 0.95,
            2
        )

        target = round(
            fill_price * 1.15,
            2
        )

        if (
            stop_loss <= 0
            or target <= 0
        ):

            with lock:
                state["trade_message"] = (
                    "PROTECTION FAILED"
                )

            emergency_close_position(
                symbol,
                filled_quantity
            )

            return False

        # ----------------------------------------------------
        # Create OCO immediately
        # ----------------------------------------------------

        oco_reference = (
            "OC"
            + uuid.uuid4()
            .hex[:17]
        )

        try:

            oco_response = (
                groww.create_smart_order(

                    smart_order_type=(
                        groww.SMART_ORDER_TYPE_OCO
                    ),

                    reference_id=(
                        oco_reference
                    ),

                    segment=(
                        groww.SEGMENT_FNO
                    ),

                    trading_symbol=symbol,

                    quantity=filled_quantity,

                    product_type=(
                        groww.PRODUCT_NRML
                    ),

                    exchange=(
                        groww.EXCHANGE_NSE
                    ),

                    duration=(
                        groww.VALIDITY_DAY
                    ),

                    net_position_quantity=(
                        filled_quantity
                    ),

                    transaction_type=(
                        groww.TRANSACTION_TYPE_SELL
                    ),

                    target={
                        "trigger_price":
                            str(target),

                        "order_type":
                            groww.ORDER_TYPE_LIMIT,

                        "price":
                            str(target)
                    },

                    stop_loss={
                        "trigger_price":
                            str(stop_loss),

                        "order_type":
                            groww.ORDER_TYPE_STOP_LOSS_MARKET,

                        "price":
                            None
                    }
                )
            )

            print(
                "OCO RESPONSE:",
                oco_response
            )

            oco_id = (
                oco_response
                .get("payload", {})
                .get("smart_order_id")
            )

            oco_status = (
                oco_response
                .get("payload", {})
                .get("status")
            )

            if not oco_id:

                # Protection could not be confirmed.
                emergency_close_position(
                    symbol,
                    filled_quantity
                )

                with lock:

                    state["trade_message"] = (
                        "PROTECTION NOT "
                        "CONFIRMED - "
                        "EMERGENCY EXIT SENT"
                    )

                    state["protection_status"] = (
                        "FAILED"
                    )

                return False

        except Exception as e:

            print(
                "OCO creation error:",
                repr(e)
            )

            emergency_close_position(
                symbol,
                filled_quantity
            )

            with lock:

                state["trade_message"] = (
                    "OCO FAILED - "
                    "EMERGENCY EXIT SENT"
                )

                state["protection_status"] = (
                    "FAILED"
                )

            return False

        # ----------------------------------------------------
        # Only now mark position ACTIVE
        # ----------------------------------------------------

        with lock:

            state["position"] = (
                "CALL"
                if option["option_type"]
                == "CE"
                else "PUT"
            )

            state["trading_symbol"] = symbol

            state["option_type"] = (
                option["option_type"]
            )

            state["strike"] = (
                option["strike"]
            )

            state["expiry"] = (
                option["expiry"]
            )

            state["quantity"] = (
                filled_quantity
            )

            state["lot_size"] = (
                option["lot_size"]
            )

            state["entry_price"] = (
                fill_price
            )

            state["current_price"] = (
                fill_price
            )

            state["stop_loss"] = (
                stop_loss
            )

            state["target"] = (
                target
            )

            state["buy_order_id"] = (
                order_id
            )

            state["oco_order_id"] = (
                oco_id
            )

            state["protection_status"] = (
                "ACTIVE"
            )

            amount = (
                fill_price
                * filled_quantity
            )

            state["amount_today"] += (
                amount
            )

            state["trades_today"] += 1

            state["trade_message"] = (
                "LIVE TRADE ACTIVE | "
                "SL 5% | TARGET 15%"
            )

        return True

    except Exception as e:

        print(
            "LIVE BUY ERROR:",
            repr(e)
        )

        with lock:
            state["trade_message"] = (
                "BUY ERROR: "
                + str(e)
            )

        return False


# ============================================================
# EMERGENCY CLOSE
# ============================================================

def emergency_close_position(
    symbol,
    quantity
):

    try:

        response = groww.place_order(

            trading_symbol=symbol,

            quantity=int(quantity),

            validity=groww.VALIDITY_DAY,

            exchange=groww.EXCHANGE_NSE,

            segment=groww.SEGMENT_FNO,

            product=groww.PRODUCT_NRML,

            order_type=groww.ORDER_TYPE_MARKET,

            transaction_type=(
                groww.TRANSACTION_TYPE_SELL
            ),

            order_reference_id=(
                "EX"
                + uuid.uuid4()
                .hex[:17]
            )
        )

        print(
            "EMERGENCY EXIT:",
            response
        )

        return True

    except Exception as e:

        print(
            "EMERGENCY EXIT ERROR:",
            repr(e)
        )

        return False


# ============================================================
# LIVE POSITION / P&L RECONCILIATION
# ============================================================

def reconcile_position():

    if groww is None:
        return

    try:

        response = (
            groww.get_positions_for_user(
                segment=groww.SEGMENT_FNO
            )
        )

        positions = response.get(
            "positions",
            []
        )

        active = None

        for p in positions:

            qty = int(
                p.get(
                    "quantity",
                    0
                )
                or 0
            )

            if qty != 0:

                active = p

                break

        if active is None:

            with lock:

                # Keep realised P&L if available
                state["position"] = "NONE"

                state["trading_symbol"] = None

                state["option_type"] = None

                state["strike"] = None

                state["expiry"] = None

                state["quantity"] = 0

                state["entry_price"] = None

                state["current_price"] = None

                state["stop_loss"] = None

                state["target"] = None

                state["oco_order_id"] = None

                state["protection_status"] = (
                    "NONE"
                )

            return

        symbol = active.get(
            "trading_symbol"
        )

        quantity = int(
            active.get(
                "quantity",
                0
            )
            or 0
        )

        net_price = float(
            active.get(
                "net_price",
                0
            )
            or 0
        )

        realised = float(
            active.get(
                "realised_pnl",
                0
            )
            or 0
        )

        if quantity == 0:
            return

        # Current option price
        ltp_response = (
            groww.get_ltp(
                segment=groww.SEGMENT_FNO,
                exchange_trading_symbols=(
                    "NSE_" + symbol
                )
            )
        )

        current_price = ltp_response.get(
            "NSE_" + symbol
        )

        if current_price is None:

            current_price = net_price

        current_price = float(
            current_price
        )

        # For a long option
        unrealised = (
            current_price
            - net_price
        ) * quantity

        with lock:

            state["position"] = (
                "CALL"
                if symbol.endswith("CE")
                else "PUT"
            )

            state["trading_symbol"] = (
                symbol
            )

            state["quantity"] = (
                quantity
            )

            state["entry_price"] = (
                net_price
            )

            state["current_price"] = (
                current_price
            )

            state["unrealised_pnl"] = (
                unrealised
            )

            state["realised_pnl"] = (
                realised
            )

            state["day_pnl"] = (
                realised
                + unrealised
            )


    except Exception as e:

        print(
            "Position reconciliation error:",
            repr(e)
        )


# ============================================================
# AUTO-PILOT
# ============================================================

def auto_pilot_loop():

    while True:

        try:

            check_new_day()

            update_signal()

            reconcile_position()

            with lock:

                autopilot = (
                    state["auto_pilot"]
                )

                position = (
                    state["position"]
                )

                signal = (
                    state["signal"]
                )

                confidence = (
                    state["confidence"]
                )

            if (
                autopilot
                and position == "NONE"
                and market_is_open()
                and confidence >= AI_MIN_CONFIDENCE
                and signal in (
                    "BUY CALL",
                    "BUY PUT"
                )
            ):

                # Re-check account position
                reconcile_position()

                with lock:

                    if (
                        state["position"]
                        != "NONE"
                    ):

                        continue

                option = select_option(
                    signal
                )

                valid, reason = (
                    validate_trade(option)
                )

                if not valid:

                    with lock:
                        state["trade_message"] = (
                            "ORDER BLOCKED: "
                            + reason
                        )

                else:

                    place_live_buy(
                        option
                    )

        except Exception as e:

            print(
                "Auto-Pilot error:",
                repr(e)
            )

        time.sleep(2)


# ============================================================
# GROWW LIVE FEED
# ============================================================

def start_groww():

    global groww
    global feed
    global groww_started

    if not ACCESS_TOKEN:

        with lock:

            state["status"] = "ERROR"

            state["message"] = (
                "GROWW_ACCESS_TOKEN missing"
            )

        groww_started = False

        return

    try:

        with lock:

            state["status"] = (
                "AUTHENTICATING"
            )

            state["message"] = (
                "Connecting to Groww..."
            )

        print(
            "Connecting to Groww using "
            "Access Token..."
        )

        groww = GrowwAPI(
            ACCESS_TOKEN
        )

        print(
            "Groww API connected."
        )

        feed = GrowwFeed(
            groww
        )

        instruments = [
            {
                "exchange": "NSE",
                "segment": "CASH",
                "exchange_token": "NIFTY"
            }
        ]

        def on_data_received(meta):

            try:

                data = (
                    feed.get_index_value()
                )

                nifty = (
                    data
                    .get("NSE", {})
                    .get("CASH", {})
                    .get("NIFTY", {})
                )

                price = nifty.get(
                    "value"
                )

                timestamp = nifty.get(
                    "tsInMillis"
                )

                if price is None:
                    return

                if timestamp is None:

                    timestamp = int(
                        time.time()
                        * 1000
                    )

                print(
                    "NIFTY LIVE DATA:",
                    nifty
                )

                process_nifty_price(
                    price,
                    timestamp
                )

            except Exception as e:

                print(
                    "Feed callback error:",
                    repr(e)
                )

        print(
            "Subscribing to NIFTY live feed..."
        )

        feed.subscribe_index_value(
            instruments,
            on_data_received=(
                on_data_received
            )
        )

        print(
            "NIFTY live feed subscription "
            "created."
        )

        with lock:

            state["status"] = "LIVE"

            state["message"] = (
                "NIFTY live feed connected"
            )

        feed.consume()

    except Exception as e:

        print(
            "Groww connection error:",
            repr(e)
        )

        with lock:

            state["status"] = "ERROR"

            state["message"] = (
                "Groww error: "
                + str(e)
            )

    finally:

        groww_started = False


# ============================================================
# START GROWW
# ============================================================

def ensure_groww_started():

    global groww_started
    global groww_thread

    if groww_started:
        return

    with lock:

        if groww_started:
            return

        groww_started = True

    groww_thread = threading.Thread(
        target=start_groww,
        daemon=True
    )

    groww_thread.start()


# ============================================================
# AUTO-PILOT ON/OFF
# ============================================================

@app.route(
    "/api/autopilot",
    methods=["POST"]
)
def autopilot_control():

    data = (
        request.get_json(
            silent=True
        )
        or {}
    )

    enabled = bool(
        data.get(
            "enabled",
            False
        )
    )

    with lock:

        if enabled:

            try:

                max_trades = int(
                    data.get(
                        "max_trades",
                        1
                    )
                )

                max_amount = float(
                    data.get(
                        "max_amount",
                        10000
                    )
                )

            except Exception:

                return jsonify({
                    "ok": False,
                    "message":
                        "Invalid settings"
                }), 400

            if max_trades < 1:

                return jsonify({
                    "ok": False,
                    "message":
                        "Trades must be at least 1"
                }), 400

            if max_amount <= 0:

                return jsonify({
                    "ok": False,
                    "message":
                        "Amount must be greater than zero"
                }), 400

            # HARD SAFETY LIMITS
            max_trades = min(
                max_trades,
                20
            )

            max_amount = min(
                max_amount,
                10000
            )

            state[
                "max_trades_today"
            ] = max_trades

            state[
                "max_amount_today"
            ] = max_amount

            state[
                "auto_pilot"
            ] = True

            state[
                "trade_message"
            ] = (
                "LIVE AUTO-PILOT ON | "
                f"{max_trades} trades | "
                f"₹{max_amount:,.2f} limit"
            )

        else:

            state[
                "auto_pilot"
            ] = False

            state[
                "trade_message"
            ] = (
                "Auto-Pilot OFF"
            )

    return jsonify({
        "ok": True,
        "auto_pilot":
            state["auto_pilot"],
        "max_trades_today":
            state["max_trades_today"],
        "max_amount_today":
            state["max_amount_today"]
    })


# ============================================================
# CHANGE LIMITS WHILE AUTO-PILOT IS ON
# ============================================================

@app.route(
    "/api/autopilot/limits",
    methods=["POST"]
)
def change_limits():

    data = (
        request.get_json(
            silent=True
        )
        or {}
    )

    try:

        max_trades = int(
            data.get(
                "max_trades"
            )
        )

        max_amount = float(
            data.get(
                "max_amount"
            )
        )

    except Exception:

        return jsonify({
            "ok": False,
            "message":
                "Invalid limits"
        }), 400

    max_trades = max(
        1,
        min(max_trades, 20)
    )

    max_amount = max(
        1,
        min(max_amount, 10000)
    )

    with lock:

        if not state["auto_pilot"]:

            return jsonify({
                "ok": False,
                "message":
                    "Auto-Pilot is OFF"
            }), 400

        state[
            "max_trades_today"
        ] = max_trades

        state[
            "max_amount_today"
        ] = max_amount

        state[
            "trade_message"
        ] = (
            "Limits updated"
        )

    return jsonify({
        "ok": True
    })


# ============================================================
# MANUAL EMERGENCY EXIT
# ============================================================

@app.route(
    "/api/emergency-exit",
    methods=["POST"]
)
def emergency_exit():

    with lock:

        symbol = (
            state["trading_symbol"]
        )

        quantity = (
            state["quantity"]
        )

    if not symbol or quantity <= 0:

        return jsonify({
            "ok": False,
            "message":
                "No open position"
        })

    success = (
        emergency_close_position(
            symbol,
            quantity
        )
    )

    if success:

        with lock:

            state[
                "trade_message"
            ] = (
                "Emergency exit sent"
            )

        return jsonify({
            "ok": True
        })

    return jsonify({
        "ok": False,
        "message":
            "Emergency exit failed"
    }), 500


# ============================================================
# STATUS
# ============================================================

@app.route("/api/status")
def status():

    ensure_groww_started()

    with lock:

        return jsonify({
            "status":
                state["status"],

            "message":
                state["message"],

            "nifty":
                state["nifty"],

            "signal":
                state["signal"],

            "confidence":
                state["confidence"],

            "auto_pilot":
                state["auto_pilot"],

            "max_trades_today":
                state["max_trades_today"],

            "trades_today":
                state["trades_today"],

            "max_amount_today":
                state["max_amount_today"],

            "amount_today":
                state["amount_today"],

            "position":
                state["position"],

            "trading_symbol":
                state["trading_symbol"],

            "option_type":
                state["option_type"],

            "strike":
                state["strike"],

            "expiry":
                state["expiry"],

            "quantity":
                state["quantity"],

            "entry_price":
                state["entry_price"],

            "current_price":
                state["current_price"],

            "stop_loss":
                state["stop_loss"],

            "target":
                state["target"],

            "unrealised_pnl":
                state["unrealised_pnl"],

            "realised_pnl":
                state["realised_pnl"],

            "day_pnl":
                state["day_pnl"],

            "buy_order_id":
                state["buy_order_id"],

            "oco_order_id":
                state["oco_order_id"],

            "protection_status":
                state["protection_status"],

            "trade_message":
                state["trade_message"],

            "last_update":
                state["last_update"],

            "candle":
                state["candle"]
        })


# ============================================================
# MOBILE UI
# ============================================================

HTML = """

<!DOCTYPE html>

<html>

<head>

<meta
name="viewport"
content="width=device-width,initial-scale=1"
>

<title>NIFTY AI AUTO PILOT</title>

<style>

body {
    margin:0;
    background:#101010;
    color:white;
    font-family:Arial,sans-serif;
}

.container {
    max-width:500px;
    margin:auto;
    padding:18px;
}

h1 {
    text-align:center;
    font-size:26px;
}

.card {
    background:#181818;
    border:1px solid #333;
    border-radius:15px;
    padding:16px;
    margin-bottom:14px;
}

.label {
    color:#999;
    font-size:13px;
}

.price {
    text-align:center;
    font-size:44px;
    font-weight:bold;
    margin:5px;
}

.signal {
    text-align:center;
    font-size:28px;
    font-weight:bold;
    padding:15px;
    border:1px solid #444;
    border-radius:12px;
}

.confidence {
    text-align:center;
    color:#aaa;
    margin-top:10px;
}

.row {
    display:flex;
    justify-content:space-between;
    padding:7px 0;
    border-bottom:1px solid #292929;
}

button {
    width:100%;
    padding:13px;
    border:0;
    border-radius:9px;
    margin-top:8px;
    font-size:16px;
    font-weight:bold;
}

input,select {
    width:100%;
    box-sizing:border-box;
    padding:12px;
    margin-top:6px;
    border-radius:8px;
    background:#222;
    color:white;
    border:1px solid #444;
    font-size:16px;
}

.on {
    background:#ddd;
    color:#111;
}

.off {
    background:#333;
    color:white;
}

.exit {
    background:#555;
    color:white;
}

.warning {
    color:#aaa;
    font-size:12px;
    line-height:1.5;
}

.good {
    font-weight:bold;
}

.pnl {
    font-size:25px;
    font-weight:bold;
    text-align:center;
}

.status {
    text-align:center;
    color:#999;
    font-size:12px;
}

</style>

</head>

<body>

<div class="container">

<h1>NIFTY AI AUTO PILOT</h1>


<div class="card">

<div class="label">NIFTY</div>

<div
id="price"
class="price"
>--</div>

<div
id="signal"
class="signal"
>WAIT</div>

<div class="confidence">

AI Confidence:
<b id="confidence">0%</b>

</div>

</div>


<div class="card">

<div class="label">
AUTO PILOT
</div>


<div class="row">

<span>Status</span>

<b id="auto">OFF</b>

</div>


<div class="row">

<span>Trades</span>

<b>
<span id="trades">0</span> /
<span id="maxtrades">1</span>
</b>

</div>


<div class="row">

<span>Amount</span>

<b>
₹<span id="amount">0</span>
/
₹<span id="maxamount">10000</span>
</b>

</div>


<select id="tradeLimit">

<option value="1">1 Trade</option>
<option value="2">2 Trades</option>
<option value="3">3 Trades</option>
<option value="5">5 Trades</option>
<option value="10">10 Trades</option>

</select>


<input
id="amountLimit"
type="number"
placeholder="Daily ₹ limit"
value="10000"
max="10000"
>


<button
class="on"
onclick="turnOn()"
>
ENABLE LIVE AUTO-PILOT
</button>


<button
class="off"
onclick="turnOff()"
>
TURN AUTO-PILOT OFF
</button>


<button
class="exit"
onclick="emergencyExit()"
>
EMERGENCY EXIT
</button>

</div>


<div class="card">

<div class="label">
LIVE POSITION
</div>


<div class="row">
<span>Position</span>
<b id="position">NONE</b>
</div>


<div class="row">
<span>Option</span>
<b id="symbol">--</b>
</div>


<div class="row">
<span>Quantity</span>
<b id="qty">--</b>
</div>


<div class="row">
<span>Entry</span>
<b id="entry">--</b>
</div>


<div class="row">
<span>Current</span>
<b id="current">--</b>
</div>


<div class="row">
<span>Stop Loss</span>
<b id="sl">--</b>
</div>


<div class="row">
<span>Target</span>
<b id="target">--</b>
</div>


<div class="row">
<span>Protection</span>
<b id="protection">NONE</b>
</div>


<div
id="positionPnl"
class="pnl"
>
₹0
</div>

</div>


<div class="card">

<div class="label">
TODAY'S P&L
</div>


<div class="row">
<span>Realised</span>
<b id="realised">₹0</b>
</div>


<div class="row">
<span>Unrealised</span>
<b id="unrealised">₹0</b>
</div>


<div class="row">
<span>Total P&L</span>
<b id="daypnl">₹0</b>
</div>

</div>


<div class="card">

<div class="label">
5 MIN CANDLE
</div>

<div class="row">
<span>Open</span>
<b id="open">--</b>
</div>

<div class="row">
<span>High</span>
<b id="high">--</b>
</div>

<div class="row">
<span>Low</span>
<b id="low">--</b>
</div>

<div class="row">
<span>Close</span>
<b id="close">--</b>
</div>

</div>


<div
id="status"
class="status"
>
Connecting...
</div>


<div
id="message"
class="status"
>
Starting...
</div>


<div class="warning">

LIVE TRADING ENABLES REAL ORDERS.
Auto-Pilot is OFF by default.
Never enable it without checking the
daily trade and amount limits.

</div>

</div>


<script>

async function update() {
    try {
        const response = await fetch("/api/status?t=" + Date.now(), {
            cache: "no-store"
        });

        if (!response.ok) {
            throw new Error("HTTP " + response.status);
        }

        const d = await response.json();

        const setText = (id, value) => {
            const el = document.getElementById(id);
            if (el) el.innerText = value;
        };

        setText(
            "price",
            d.nifty !== null && d.nifty !== undefined
                ? Number(d.nifty).toFixed(2)
                : "--"
        );

        setText("signal", d.signal || "WAIT");
        setText("confidence", Number(d.confidence || 0).toFixed(0) + "%");
        setText("auto", d.auto_pilot ? "ON" : "OFF");
        setText("trades", d.trades_today || 0);
        setText("maxtrades", d.max_trades_today || 1);
        setText("amount", Number(d.amount_today || 0).toFixed(0));
        setText("maxamount", Number(d.max_amount_today || 10000).toFixed(0));

        setText("position", d.position || "NONE");
        setText("symbol", d.trading_symbol || "--");
        setText("qty", d.quantity || "--");

        setText(
            "entry",
            d.entry_price !== null && d.entry_price !== undefined
                ? "₹" + Number(d.entry_price).toFixed(2)
                : "--"
        );

        setText(
            "current",
            d.current_price !== null && d.current_price !== undefined
                ? "₹" + Number(d.current_price).toFixed(2)
                : "--"
        );

        setText(
            "sl",
            d.stop_loss !== null && d.stop_loss !== undefined
                ? "₹" + Number(d.stop_loss).toFixed(2)
                : "--"
        );

        setText(
            "target",
            d.target !== null && d.target !== undefined
                ? "₹" + Number(d.target).toFixed(2)
                : "--"
        );

        setText("protection", d.protection_status || "NONE");
        setText("positionPnl", "₹" + Number(d.unrealised_pnl || 0).toFixed(2));
        setText("realised", "₹" + Number(d.realised_pnl || 0).toFixed(2));
        setText("unrealised", "₹" + Number(d.unrealised_pnl || 0).toFixed(2));
        setText("daypnl", "₹" + Number(d.day_pnl || 0).toFixed(2));

        const c = d.candle || {};
        setText("open", c.open ?? "--");
        setText("high", c.high ?? "--");
        setText("low", c.low ?? "--");
        setText("close", c.close ?? "--");

        setText(
            "status",
            (d.status || "UNKNOWN") + " | " + (d.message || "")
        );
        setText("message", d.trade_message || "");

    } catch (e) {
        console.error("Status update error:", e);
        setText("status", "APP ERROR: " + e.message);
    }
}


async function turnOn() {
    const trades = parseInt(
        document.getElementById("tradeLimit").value,
        10
    );

    const amount = parseFloat(
        document.getElementById("amountLimit").value
    );

    if (!trades || !amount || amount <= 0) {
        alert("Enter valid daily trade and amount limits.");
        return;
    }

    if (amount > 10000) {
        alert("Maximum daily amount is ₹10,000.");
        return;
    }

    const ok = confirm(
        "ENABLE LIVE AUTO-PILOT?\n\n" +
        "Maximum trades: " + trades + "\n" +
        "Maximum amount: ₹" + amount.toFixed(2) + "\n\n" +
        "REAL MONEY ORDERS CAN BE PLACED."
    );

    if (!ok) return;

    try {
        const response = await fetch("/api/autopilot", {
            method: "POST",
            headers: {
                "Content-Type": "application/json"
            },
            body: JSON.stringify({
                enabled: true,
                max_trades: trades,
                max_amount: amount
            })
        });

        if (!response.ok) {
            const result = await response.json().catch(() => ({}));
            throw new Error(result.message || "Unable to enable Auto-Pilot");
        }

        await update();

    } catch (e) {
        alert("Auto-Pilot error: " + e.message);
    }
}


async function turnOff() {
    try {
        const response = await fetch("/api/autopilot", {
            method: "POST",
            headers: {
                "Content-Type": "application/json"
            },
            body: JSON.stringify({ enabled: false })
        });

        if (!response.ok) {
            throw new Error("Unable to turn off Auto-Pilot");
        }

        await update();

    } catch (e) {
        alert("Unable to turn off Auto-Pilot: " + e.message);
    }
}


async function emergencyExit() {
    const ok = confirm("Send LIVE MARKET EXIT?");
    if (!ok) return;

    try {
        const response = await fetch("/api/emergency-exit", {
            method: "POST"
        });

        if (!response.ok) {
            const result = await response.json().catch(() => ({}));
            throw new Error(result.message || "Emergency exit failed");
        }

        await update();

    } catch (e) {
        alert("Emergency exit request failed: " + e.message);
    }
}


setInterval(update, 1000);
update();

</script>

</body>

</html>

"""


# ============================================================
# HOME
# ============================================================

@app.route("/")
def home():

    ensure_groww_started()

    return render_template_string(
        HTML
    )


# ============================================================
# BACKGROUND ENGINE
# ============================================================

threading.Thread(
    target=auto_pilot_loop,
    daemon=True
).start()


# ============================================================
# LOCAL RUN
# ============================================================

if __name__ == "__main__":

    ensure_groww_started()

    app.run(
        host="0.0.0.0",
        port=int(
            os.environ.get(
                "PORT",
                10000
            )
        )
    )
