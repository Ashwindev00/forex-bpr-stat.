"""
Full Backtesting Engine
FVG strategy with Backtrader: Entry, SL, TP, Spread, Slippage, Commission.
Metrics: Net profit, Max drawdown, Sharpe ratio, Profit factor, Expectancy, Win/Loss ratio.
"""

import pandas as pd
import backtrader as bt
from pathlib import Path


# --- Configuration ---
SPREAD_PIPS = 1.0
SLIPPAGE_PIPS = 0.5
COMMISSION_PIPS = 0.5
LOT_SIZE = 0.01
PIP_VALUE = 0.0001
PIP_WORTH_PER_STANDARD_LOT = 10  # $10 per pip
MIN_STOP_PIPS = 5
LOOKAHEAD_BARS = 50
INITIAL_CASH = 10000.0


def get_data_path() -> Path:
    return Path(__file__).resolve().parent.parent / "data" / "raw"


class FVGStrategy(bt.Strategy):
    params = (
        ("lookahead", LOOKAHEAD_BARS),
        ("lot", LOT_SIZE),
    )

    def __init__(self):
        self.pending_fvgs = []
        self.atr = bt.indicators.ATR(self.data)
        self.ema = bt.indicators.EMA(self.data, period=200)

    def next(self):

        if len(self.data) < 250:
            return

        if self.position:
            return

        c1_high = self.data.high[-2]
        c1_low = self.data.low[-2]
        c3_high = self.data.high[0]
        c3_low = self.data.low[0]
        c3_close = self.data.close[0]

        # Bullish FVG
        if c1_high < c3_low and c3_close > self.ema[0]:

            entry = (c1_high + c3_low) / 2

            atr_stop = 1.5 * self.atr[0]
            min_stop = MIN_STOP_PIPS * PIP_VALUE
            stop_distance = max(atr_stop, min_stop)

            sl = entry - stop_distance
            tp = entry + 2 * stop_distance

            self.pending_fvgs.append({
                "type": "bullish",
                "entry": entry,
                "sl": sl,
                "tp": tp,
                "bar": len(self.data) - 1,
            })

        # Bearish FVG
        if c1_low > c3_high and c3_close < self.ema[0]:

            entry = (c1_low + c3_high) / 2

            atr_stop = 1.5 * self.atr[0]
            min_stop = MIN_STOP_PIPS * PIP_VALUE
            stop_distance = max(atr_stop, min_stop)

            sl = entry + stop_distance
            tp = entry - 2 * stop_distance

            self.pending_fvgs.append({
                "type": "bearish",
                "entry": entry,
                "sl": sl,
                "tp": tp,
                "bar": len(self.data) - 1,
            })

        current_bar = len(self.data) - 1
        self.pending_fvgs = [
            f for f in self.pending_fvgs
            if current_bar - f["bar"] < self.params.lookahead
        ]

        bar_low = self.data.low[0]
        bar_high = self.data.high[0]
        prev_close = self.data.close[-1]

        for fvg in self.pending_fvgs:

            entry = fvg["entry"]

            if (prev_close > entry >= bar_low) or (prev_close < entry <= bar_high):

                account_value = self.broker.getvalue()
                risk_amount = account_value * 0.01

                stop_distance = abs(entry - fvg["sl"])
                stop_pips = max(stop_distance / PIP_VALUE, MIN_STOP_PIPS)

                lot_size = risk_amount / (stop_pips * PIP_WORTH_PER_STANDARD_LOT)
                size = int(lot_size * 100000)
                size = max(1000, min(size, 100000))

                if fvg["type"] == "bullish":
                    self.buy_bracket(price=entry,
                                     stopprice=fvg["sl"],
                                     limitprice=fvg["tp"],
                                     size=size)
                else:
                    self.sell_bracket(price=entry,
                                      stopprice=fvg["sl"],
                                      limitprice=fvg["tp"],
                                      size=size)

                self.pending_fvgs.remove(fvg)
                break


SYMBOLS = ["EURUSD", "GBPUSD", "USDJPY", "AUDUSD"]
START_DATE = "2015-01-01"
END_DATE = "2024-12-31"


def run_single_backtest(symbol: str) -> dict:
    """Run backtest for one symbol, return metrics dict."""
    cerebro = bt.Cerebro()

    path = get_data_path() / f"{symbol}_4H.csv"
    if not path.exists():
        return None
    df = pd.read_csv(path)
    df["time"] = pd.to_datetime(df["time"])
    df.set_index("time", inplace=True)
    df = df.loc[START_DATE:END_DATE]
    if len(df) < 300:
        return None
    df.rename(columns={"tick_volume": "volume"}, inplace=True)
    df = df[["open", "high", "low", "close", "volume"]]

    data = bt.feeds.PandasData(dataname=df)
    cerebro.adddata(data)
    cerebro.addstrategy(FVGStrategy)
    cerebro.broker.setcash(INITIAL_CASH)

    commission_pct = (COMMISSION_PIPS + SPREAD_PIPS / 2) * PIP_VALUE
    cerebro.broker.setcommission(commission=commission_pct)
    cerebro.broker.set_slippage_fixed(SLIPPAGE_PIPS * PIP_VALUE)

    cerebro.addanalyzer(bt.analyzers.SharpeRatio, _name="sharpe", timeframe=bt.TimeFrame.Days, annualize=True)
    cerebro.addanalyzer(bt.analyzers.DrawDown, _name="drawdown")
    cerebro.addanalyzer(bt.analyzers.TradeAnalyzer, _name="trades")

    results = cerebro.run()
    strat = results[0]

    final_value = cerebro.broker.getvalue()
    net_profit = final_value - INITIAL_CASH

    sharpe_analysis = strat.analyzers.sharpe.get_analysis()
    sr_val = sharpe_analysis.get("sharperatio")
    sharpe_ratio = sr_val if isinstance(sr_val, (int, float)) else (sr_val[0] if sr_val and len(sr_val) else 0)
    sharpe_ratio = sharpe_ratio if sharpe_ratio is not None else 0

    dd = strat.analyzers.drawdown.get_analysis()
    max_dd = dd.get("max", {}) or {}
    max_dd_pct = max_dd.get("drawdown", 0) or 0
    if max_dd_pct <= 1:
        max_dd_pct *= 100
    max_dd_len = max_dd.get("len", 0)

    ta = strat.analyzers.trades.get_analysis()
    total_trades = (ta.get("total", {}) or {}).get("closed", 0) or 0
    won = (ta.get("won", {}) or {}).get("total", 0) or 0
    lost = (ta.get("lost", {}) or {}).get("total", 0) or 0
    gross_profit = ((ta.get("won", {}) or {}).get("pnl") or {}).get("total", 0) or 0
    gross_loss = abs(((ta.get("lost", {}) or {}).get("pnl") or {}).get("total", 0) or 0)
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else (float("inf") if gross_profit > 0 else 0)

    win_rate = (won / total_trades * 100) if total_trades > 0 else 0
    loss_rate = (lost / total_trades * 100) if total_trades > 0 else 0
    avg_win = gross_profit / won if won > 0 else 0
    avg_loss = gross_loss / lost if lost > 0 else 0
    expectancy = (win_rate / 100 * avg_win) - (loss_rate / 100 * avg_loss) if total_trades > 0 else 0
    win_loss_ratio = (avg_win / avg_loss) if avg_loss > 0 else 0

    return {
        "symbol": symbol,
        "net_profit": net_profit,
        "final_value": final_value,
        "total_trades": total_trades,
        "won": won,
        "lost": lost,
        "gross_profit": gross_profit,
        "gross_loss": gross_loss,
        "profit_factor": profit_factor,
        "max_dd_pct": max_dd_pct,
        "max_dd_len": max_dd_len,
        "sharpe_ratio": sharpe_ratio,
        "expectancy": expectancy,
        "win_rate": win_rate,
        "win_loss_ratio": win_loss_ratio,
    }


def run_backtest():
    """Run backtest on all pairs (2015–2024) and aggregate results."""
    all_results = []
    for symbol in SYMBOLS:
        print(f"Running {symbol}...")
        r = run_single_backtest(symbol)
        if r:
            all_results.append(r)

    if not all_results:
        print("No data available.")
        return

    # Per-pair report
    print("\n" + "=" * 60)
    print("PER-PAIR RESULTS (2015–2024)")
    print("=" * 60)
    for r in all_results:
        print(f"\n{r['symbol']}:")
        print(f"  Trades: {r['total_trades']} | Wins: {r['won']} | Losses: {r['lost']} | Win%: {r['win_rate']:.1f}%")
        print(f"  Net P&L: ${r['net_profit']:,.2f} | Max DD: {r['max_dd_pct']:.2f}% | PF: {r['profit_factor']:.2f} | Exp: ${r['expectancy']:.2f}")

    # Aggregate
    agg_trades = sum(r["total_trades"] for r in all_results)
    agg_won = sum(r["won"] for r in all_results)
    agg_lost = sum(r["lost"] for r in all_results)
    agg_gross_profit = sum(r["gross_profit"] for r in all_results)
    agg_gross_loss = sum(r["gross_loss"] for r in all_results)
    agg_net_profit = sum(r["net_profit"] for r in all_results)
    agg_max_dd = max(r["max_dd_pct"] for r in all_results)
    agg_win_rate = (agg_won / agg_trades * 100) if agg_trades > 0 else 0
    agg_avg_win = agg_gross_profit / agg_won if agg_won > 0 else 0
    agg_avg_loss = agg_gross_loss / agg_lost if agg_lost > 0 else 0
    agg_expectancy = (agg_win_rate / 100 * agg_avg_win) - ((100 - agg_win_rate) / 100 * agg_avg_loss) if agg_trades > 0 else 0
    agg_pf = agg_gross_profit / agg_gross_loss if agg_gross_loss > 0 else (float("inf") if agg_gross_profit > 0 else 0)
    total_capital = INITIAL_CASH * len(all_results)

    print("\n" + "=" * 60)
    print("AGGREGATE RESULTS (All Pairs, 2015–2024)")
    print("=" * 60)
    print(f"Pairs:               {', '.join(r['symbol'] for r in all_results)}")
    print(f"Date range:          {START_DATE} – {END_DATE}")
    print(f"Total trades:        {agg_trades}")
    print(f"Wins:                {agg_won} | Losses: {agg_lost}")
    print(f"Win rate:            {agg_win_rate:.1f}%")
    print(f"Net profit:          ${agg_net_profit:,.2f} ({agg_net_profit/total_capital*100:.1f}%)")
    print(f"Max drawdown:        {agg_max_dd:.2f}% (worst pair)")
    print(f"Profit factor:       {agg_pf:.2f}")
    print(f"Expectancy:          ${agg_expectancy:.2f} per trade")
    print("=" * 60 + "\n")


if __name__ == "__main__":
    run_backtest()
