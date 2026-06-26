"""
BPR (Balanced Price Range) live trading bot for MetaTrader 5.

Implements the TradingView Pine logic you provided (BPR [TFO]) to detect
active bullish/bearish BPR zones on H1, then applies the 5-step execution:

1) Left the zone = 3 consecutive H1 candles completely outside the zone
2) Returned = an H1 candle overlaps (trades back into) the zone
3) Closed inside = next H1 candle closes inside the zone
4) Enter next open = enter immediately at the next candle open (implemented as:
   after the confirmation candle closes, place a market order right away)

Notes:
- SL/TP and risk are configurable at the top of this file.
- This bot uses generic FX/metal pip padding and volume sizing based on
  symbol tick_value/tick_size.
"""

from __future__ import annotations

import csv
import math
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import MetaTrader5 as mt5


# ----------------------------
# User configuration
# ----------------------------
SYMBOLS = ["EURUSD"]  # next: add "XAUUSD" after you confirm the exact MT5 symbol name

TIMEFRAME = mt5.TIMEFRAME_H1

# Pine defaults from your script
BPR_THRESHOLD = 0.0
BARS_SINCE = 10
ONLY_CLEAN_BPR = False

# Execution rules (5-step)
LEFT_CANDLES_REQUIRED = 3

# Trading (configurable)
RISK_PERCENT_PER_TRADE = 0.01  # 1% of balance
RISK_REWARD_TP_R = 2.0
SL_PADDING_PIPS = 1.0  # padding outside the BPR boundary used for SL

# Order behavior
MAGIC_NUMBER = 27032026
DEVIATION_POINTS = 20  # max slippage in points for market order
POLL_SECONDS = 5

# Logging
LOG_CSV = Path(__file__).resolve().parent.parent / "data" / "bpr_trades.csv"


# ----------------------------
# Helpers
# ----------------------------
def utc_ts() -> str:
    return datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")


def get_symbol_pip_size(symbol_info) -> float:
    """
    Best-effort pip/point padding unit.
    We use pip_size = point * 10, which maps:
    - EURUSD (5 digits point=0.00001) -> pip_size=0.0001
    - USDJPY (3 digits point=0.001) -> pip_size=0.01
    - XAUUSD often fits point*10 too (depends on broker)
    """
    if symbol_info is None:
        return 0.0001
    return float(symbol_info.point) * 10.0


def format_price(x: float) -> float:
    return float(x)


def round_to_step(value: float, step: float) -> float:
    if step <= 0:
        return value
    return math.floor(value / step) * step


def compute_lots_for_risk(
    symbol: str,
    entry_price: float,
    sl_price: float,
    risk_amount: float,
) -> float:
    """
    Forex position sizing using tick_value/tick_size:
    loss_per_lot = stop_distance * (tick_value / tick_size)
    lots = risk_amount / loss_per_lot
    """
    symbol_info = mt5.symbol_info(symbol)
    if symbol_info is None:
        raise RuntimeError(f"symbol_info is None for {symbol}")
    if not symbol_info.visible:
        mt5.symbol_select(symbol, True)
        symbol_info = mt5.symbol_info(symbol)
        if symbol_info is None:
            raise RuntimeError(f"symbol_info unavailable for {symbol}")

    stop_distance = abs(entry_price - sl_price)
    if stop_distance <= 0:
        return symbol_info.volume_min

    tick_value = float(symbol_info.trade_tick_value)
    tick_size = float(symbol_info.trade_tick_size)
    if tick_value <= 0 or tick_size <= 0:
        raise RuntimeError(f"Invalid tick_value/tick_size for {symbol}: {tick_value}/{tick_size}")

    loss_per_lot = stop_distance * (tick_value / tick_size)
    if loss_per_lot <= 0:
        return symbol_info.volume_min

    raw_lots = risk_amount / loss_per_lot

    # Clamp to broker constraints
    step = float(symbol_info.volume_step)
    min_vol = float(symbol_info.volume_min)
    max_vol = float(symbol_info.volume_max)
    lots = round_to_step(raw_lots, step)
    lots = max(min_vol, min(max_vol, lots))
    return lots


def ensure_sl_tp_distance(symbol: str, price: float, sl: float, tp: float) -> Tuple[float, float]:
    """
    Ensure SL/TP are at least trade_stops_level points away.
    If too close, we widen SL/TP away from entry by minimum points.
    """
    si = mt5.symbol_info(symbol)
    if si is None:
        return sl, tp
    min_points = int(si.trade_stops_level)  # points
    point = float(si.point)
    if min_points <= 0:
        return sl, tp

    entry = price
    min_dist = min_points * point
    if abs(entry - sl) < min_dist:
        if sl < entry:
            sl = entry - min_dist
        else:
            sl = entry + min_dist
    if abs(entry - tp) < min_dist:
        if tp < entry:
            tp = entry - min_dist
        else:
            tp = entry + min_dist
    return sl, tp


# ----------------------------
# BPR detection (from Pine)
# ----------------------------
@dataclass
class BPRZone:
    """
    Zone boundaries follow Pine:
    - bullish: bottom=bull_combined_low, top=bull_combined_high
    - bearish: bottom=bear_combined_low, top=bear_combined_high
    """

    kind: str  # "bullish" or "bearish"
    top: float
    bottom: float
    created_bar_time: int  # MT5 time (seconds since epoch)

    # Pattern state machine
    state: int = 0  # 0 waiting left, 1 waiting return overlap, 2 waiting close inside, 3 entered
    left_count: int = 0
    return_seen: bool = False


class BPRDetector:
    """
    Replicates your Pine BPR [TFO] zone creation logic.

    We produce "pending" zones when bull_result/bear_result is true on bar i.
    Pine activates box using bull_result[1]/bear_result[1], so we optionally
    activate next bar; in live logic we can activate immediately after the
    confirmation candle closes.
    """

    def __init__(self, threshold: float, bars_since: int, only_clean_bpr: bool):
        self.threshold = threshold
        self.bars_since = bars_since
        self.only_clean_bpr = only_clean_bpr

        # store last indices of FVG conditions (for ta.barssince behavior)
        self.last_new_fvg_bearish: Optional[int] = None  # for bull_num_since
        self.last_new_fvg_bullish: Optional[int] = None  # for bear_num_since

        self._high: List[float] = []
        self._low: List[float] = []
        self._open: List[float] = []
        self._close: List[float] = []
        self._times: List[int] = []

    def append_bar(self, t: int, o: float, h: float, l: float, c: float) -> None:
        self._times.append(t)
        self._open.append(o)
        self._high.append(h)
        self._low.append(l)
        self._close.append(c)

    def _compute_new_fvgs(self, i: int) -> Tuple[bool, bool]:
        # new_fvg_bearish = low[2] - high > 0
        new_fvg_bearish = self._low[i - 2] - self._high[i] > 0
        # new_fvg_bullish = low - high[2] > 0
        new_fvg_bullish = self._low[i] - self._high[i - 2] > 0
        return new_fvg_bearish, new_fvg_bullish

    def update_and_maybe_create_zones(self) -> Tuple[Optional[Tuple[str, float, float]], Optional[Tuple[str, float, float]]]:
        """
        Returns (pending_bull, pending_bear) where each element is:
        (kind, bottom, top) or (kind, top, bottom) depending on usage.
        We return explicit top/bottom for clarity:
          pending_bull = ("bullish", top, bottom)
          pending_bear = ("bearish", top, bottom)
        """
        i = len(self._high) - 1
        if i < 2:
            return None, None

        pending_bull = None
        pending_bear = None

        new_fvg_bearish, new_fvg_bullish = self._compute_new_fvgs(i)

        # Update barssince pointers AFTER computing new_fvg flags
        if new_fvg_bearish:
            self.last_new_fvg_bearish = i
        if new_fvg_bullish:
            self.last_new_fvg_bullish = i

        # -------------------
        # Bullish BPR
        # -------------------
        if new_fvg_bullish and self.last_new_fvg_bearish is not None:
            bull_num_since = i - self.last_new_fvg_bearish
            if bull_num_since <= self.bars_since:
                # Need indices for low[bull_num_since + 2] => low[i-(bull_num_since+2)]
                if i - (bull_num_since + 2) >= 0:
                    hs = i - bull_num_since
                    ls2 = i - (bull_num_since + 2)
                    high_2 = self._high[i - 2]
                    low_i = self._low[i]

                    left = self._high[hs] + self._low[ls2] + high_2 + low_i
                    right = max(self._low[ls2], low_i) - min(self._high[hs], high_2)
                    bull_bpr_cond_2 = left > right

                    if bull_bpr_cond_2:
                        bull_combined_low = max(self._high[hs], high_2)
                        bull_combined_high = min(self._low[ls2], low_i)

                        bull_cond_3 = True
                        if self.only_clean_bpr:
                            # for h = 2 to bull_num_since:
                            for h in range(2, bull_num_since + 1):
                                if self._high[i - h] > bull_combined_low:
                                    bull_cond_3 = False
                                    break

                        bull_result = (
                            bull_cond_3
                            and (bull_combined_high - bull_combined_low >= self.threshold)
                        )
                        if bull_result:
                            # Pine: top=bull_combined_high, bottom=bull_combined_low
                            pending_bull = ("bullish", bull_combined_high, bull_combined_low)

        # -------------------
        # Bearish BPR
        # -------------------
        if new_fvg_bearish and self.last_new_fvg_bullish is not None:
            bear_num_since = i - self.last_new_fvg_bullish
            if bear_num_since <= self.bars_since:
                if i - (bear_num_since + 2) >= 0 and i - bear_num_since >= 0:
                    hs = i - bear_num_since
                    ls2 = i - (bear_num_since + 2)
                    high_2 = self._high[i - 2]
                    low_2 = self._low[i - 2]
                    high_i = self._high[i]
                    low_i = self._low[i]

                    # bear_bpr_cond_2 uses the same expression as Pine
                    left = self._high[hs] + self._low[ls2] + high_2 + low_i
                    right = max(self._low[ls2], low_i) - min(self._high[hs], high_2)
                    bear_bpr_cond_2 = left > right

                    if bear_bpr_cond_2:
                        bear_combined_low = max(self._high[ls2], high_i)  # high[bear_num_since + 2], high
                        bear_combined_high = min(self._low[hs], low_2)  # low[bear_num_since], low[2]

                        bear_cond_3 = True
                        if self.only_clean_bpr:
                            for h in range(2, bear_num_since + 1):
                                if self._low[i - h] < bear_combined_high:
                                    bear_cond_3 = False
                                    break

                        bear_result = (
                            bear_cond_3
                            and (bear_combined_high - bear_combined_low >= self.threshold)
                        )
                        if bear_result:
                            pending_bear = ("bearish", bear_combined_high, bear_combined_low)

        return pending_bull, pending_bear


# ----------------------------
# Pattern state machine
# ----------------------------
def overlaps_zone(bar_low: float, bar_high: float, zone_bottom: float, zone_top: float) -> bool:
    return (bar_low <= zone_top) and (bar_high >= zone_bottom)


def close_inside(bar_close: float, zone_bottom: float, zone_top: float) -> bool:
    return (bar_close >= zone_bottom) and (bar_close <= zone_top)


def left_condition_met(zone: BPRZone, bar_low: float, bar_high: float) -> bool:
    # "Left the zone" = completely outside for 3 consecutive candles.
    if zone.kind == "bullish":
        # Completely above top
        return bar_low > zone.top
    else:
        # Completely below bottom
        return bar_high < zone.bottom


class BPRSymbolTrader:
    def __init__(self, symbol: str):
        self.symbol = symbol
        self.detector = BPRDetector(BPR_THRESHOLD, BARS_SINCE, ONLY_CLEAN_BPR)
        self.active_zones: List[BPRZone] = []
        self.last_processed_closed_time: Optional[int] = None

    def _invalidate_zones_if_needed(self, bar_low: float, bar_high: float):
        remaining = []
        for z in self.active_zones:
            if z.kind == "bullish":
                # Pine invalidates when low < box bottom
                if bar_low < z.bottom:
                    continue
            else:
                # Pine invalidates when high > box top
                if bar_high > z.top:
                    continue
            remaining.append(z)
        self.active_zones = remaining

    def on_new_closed_bar(
        self,
        t: int,
        o: float,
        h: float,
        l: float,
        c: float,
        activate_time: int,
    ) -> Optional[Dict]:
        """
        Returns an order plan dict if we get an entry signal; otherwise None.
        """
        self.detector.append_bar(t, o, h, l, c)

        # Pine-like zone invalidation using active zones
        self._invalidate_zones_if_needed(l, h)

        # Update zone creation
        pending_bull, pending_bear = self.detector.update_and_maybe_create_zones()
        if pending_bull is not None:
            _, top, bottom = pending_bull
            self.active_zones.append(BPRZone(kind="bullish", top=top, bottom=bottom, created_bar_time=activate_time))
        if pending_bear is not None:
            _, top, bottom = pending_bear
            self.active_zones.append(BPRZone(kind="bearish", top=top, bottom=bottom, created_bar_time=activate_time))

        # Run the 5-step pattern per active zone
        for z in list(self.active_zones):
            # Newly created zones should become active on the next candle open.
            # So skip evaluation until their created time arrives.
            if z.created_bar_time > t:
                continue
            if z.state == 0:
                if left_condition_met(z, l, h):
                    z.left_count += 1
                else:
                    z.left_count = 0
                if z.left_count >= LEFT_CANDLES_REQUIRED:
                    z.state = 1  # waiting for return overlap

            elif z.state == 1:
                if overlaps_zone(l, h, z.bottom, z.top):
                    z.state = 2  # waiting next close-inside
                # else: keep waiting

            elif z.state == 2:
                if close_inside(c, z.bottom, z.top):
                    # Enter on next candle open.
                    z.state = 3
                    self.active_zones.remove(z)
                    return self._make_order_plan(z, entry_price=None)
                else:
                    # Confirmation failed; reset state and wait for a new left sequence
                    z.state = 0
                    z.left_count = 0
                    # Note: keep the zone active unless invalidated by Pine invalidation rules

        return None

    def _make_order_plan(self, z: BPRZone, entry_price: Optional[float]) -> Dict:
        """
        SL/TP derived from zone boundaries. Entry price resolved at execution time.
        """
        symbol_info = mt5.symbol_info(self.symbol)
        pip_size = get_symbol_pip_size(symbol_info)
        sl_padding = SL_PADDING_PIPS * pip_size

        # entry_price is not known here; we compute SL/TP using entry at execution time
        return {
            "symbol": self.symbol,
            "zone_kind": z.kind,
            "zone_top": z.top,
            "zone_bottom": z.bottom,
            "sl_padding": sl_padding,
            "rr": RISK_REWARD_TP_R,
        }


# ----------------------------
# Live runner
# ----------------------------
def init_mt5() -> None:
    if not mt5.initialize():
        raise RuntimeError(f"MT5 initialize() failed: {mt5.last_error()}")


def get_rates(symbol: str, timeframe, bars: int):
    rates = mt5.copy_rates_from_pos(symbol, timeframe, 0, bars)
    return rates


def get_current_tick(symbol: str):
    tick = mt5.symbol_info_tick(symbol)
    if tick is None:
        raise RuntimeError(f"symbol_info_tick failed for {symbol}")
    return tick


def append_trade_csv(row: Dict):
    LOG_CSV.parent.mkdir(parents=True, exist_ok=True)
    file_exists = LOG_CSV.exists()
    with open(LOG_CSV, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(row.keys()))
        if not file_exists:
            writer.writeheader()
        writer.writerow(row)


def place_order(plan: Dict) -> Optional[int]:
    """
    Execute a market order based on order plan.
    """
    symbol = plan["symbol"]
    tick = get_current_tick(symbol)
    symbol_info = mt5.symbol_info(symbol)
    pip_size = get_symbol_pip_size(symbol_info)
    _ = pip_size  # kept for future use

    is_buy = plan["zone_kind"] == "bullish"
    entry_price = float(tick.ask if is_buy else tick.bid)

    zone_top = plan["zone_top"]
    zone_bottom = plan["zone_bottom"]
    sl_padding = plan["sl_padding"]
    rr = plan["rr"]

    if is_buy:
        sl = zone_bottom - sl_padding
        risk_dist = entry_price - sl
        tp = entry_price + rr * risk_dist
        order_type = mt5.ORDER_TYPE_BUY
        price = float(tick.ask)
    else:
        sl = zone_top + sl_padding
        risk_dist = sl - entry_price
        tp = entry_price - rr * risk_dist
        order_type = mt5.ORDER_TYPE_SELL
        price = float(tick.bid)

    sl, tp = ensure_sl_tp_distance(symbol, entry_price, sl, tp)

    account_info = mt5.account_info()
    if account_info is None:
        raise RuntimeError("account_info() is None")
    balance = float(account_info.balance)
    risk_amount = balance * RISK_PERCENT_PER_TRADE

    lots = compute_lots_for_risk(symbol, entry_price, sl, risk_amount)

    request = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": symbol,
        "volume": lots,
        "type": order_type,
        "price": price,
        "sl": float(sl),
        "tp": float(tp),
        "deviation": DEVIATION_POINTS,
        "magic": MAGIC_NUMBER,
        "comment": "BPR_live_bot",
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": symbol_info.trade_fill_mode if symbol_info is not None else mt5.ORDER_FILLING_RETURN,
    }

    result = mt5.order_send(request)
    if result is None:
        raise RuntimeError("order_send returned None")

    # Persist trade details
    row = {
        "utc_time": utc_ts(),
        "symbol": symbol,
        "side": "BUY" if is_buy else "SELL",
        "entry_price": entry_price,
        "lots": lots,
        "sl": float(sl),
        "tp": float(tp),
        "zone_kind": plan["zone_kind"],
        "zone_top": float(zone_top),
        "zone_bottom": float(zone_bottom),
        "balance": balance,
        "risk_amount": risk_amount,
        "deviation_points": DEVIATION_POINTS,
        "order_retcode": result.retcode,
        "order_id": result.order,
        "deal": result.deal,
        "request_comment": request["comment"],
    }
    append_trade_csv(row)

    if result.retcode != mt5.TRADE_RETCODE_DONE and result.retcode != mt5.TRADE_RETCODE_PLACED:
        print(f"[{utc_ts()}] Order failed: retcode={result.retcode}, comment={result.comment}")
    else:
        print(f"[{utc_ts()}] Order placed: {symbol} {row['side']} lots={lots} entry={entry_price}")

    return result.order


def has_open_position(symbol: str) -> bool:
    positions = mt5.positions_get(symbol=symbol)
    if positions is None:
        return False
    for p in positions:
        if p.magic == MAGIC_NUMBER:
            return True
    return False


def main():
    init_mt5()
    print(f"[{utc_ts()}] MT5 connected.")

    for sym in SYMBOLS:
        if not mt5.symbol_select(sym, True):
            raise RuntimeError(f"Failed to select symbol: {sym}")

    traders = {sym: BPRSymbolTrader(sym) for sym in SYMBOLS}

    # Warm up detector state
    lookback = 400
    for sym, trader in traders.items():
        rates = get_rates(sym, TIMEFRAME, lookback)
        if rates is None or len(rates) < 300:
            raise RuntimeError(f"Not enough rates for {sym}: {0 if rates is None else len(rates)}")
        # feed all except the latest (current forming) bar
        # We'll consider last bar in rates as potentially forming, so start from len-2.
        for idx in range(len(rates) - 2):
            r = rates[idx]
            trader.detector.append_bar(int(r["time"]), float(r["open"]), float(r["high"]), float(r["low"]), float(r["close"]))
        # initialize last processed closed time
        trader.last_processed_closed_time = int(rates[-2]["time"])

    print(f"[{utc_ts()}] Warmup completed. Starting live loop...")

    while True:
        try:
            for sym, trader in traders.items():
                rates = get_rates(sym, TIMEFRAME, 300)
                if rates is None or len(rates) < 10:
                    continue

                # last closed = -2, current forming = -1
                last_closed = rates[-2]
                current_forming = rates[-1]

                last_closed_time = int(last_closed["time"])
                if trader.last_processed_closed_time == last_closed_time:
                    continue

                # If we have an open position, we still process detection but avoid entering again
                # (we could also pause detection)
                trader.last_processed_closed_time = last_closed_time

                plan = trader.on_new_closed_bar(
                    t=last_closed_time,
                    o=float(last_closed["open"]),
                    h=float(last_closed["high"]),
                    l=float(last_closed["low"]),
                    c=float(last_closed["close"]),
                    activate_time=int(current_forming["time"]),
                )

                if plan is None:
                    continue

                if has_open_position(sym):
                    print(f"[{utc_ts()}] Signal ignored (position already open): {sym}")
                    continue

                # Place immediately (enter on next open approximation)
                place_order(plan)

            time.sleep(POLL_SECONDS)
        except KeyboardInterrupt:
            print("Stopping bot...")
            break
        except Exception as e:
            print(f"[{utc_ts()}] ERROR: {e}")
            time.sleep(10)

    mt5.shutdown()


if __name__ == "__main__":
    main()

