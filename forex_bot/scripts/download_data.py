import MetaTrader5 as mt5
import pandas as pd
from datetime import datetime
import os

# Initialize MT5
if not mt5.initialize():
    print("MT5 initialization failed")
    quit()

symbols = ["EURUSD", "GBPUSD", "USDJPY", "AUDUSD"]
start = datetime(2015, 1, 1)
end = datetime(2025, 1, 1)

output_folder = "../data/raw/"
os.makedirs(output_folder, exist_ok=True)

for symbol in symbols:
    print(f"Downloading {symbol}...")

    rates = mt5.copy_rates_range(
        symbol,
        mt5.TIMEFRAME_H4,
        start,
        end
    )

    if rates is None:
        print(f"Failed to get data for {symbol}")
        continue

    df = pd.DataFrame(rates)
    df["time"] = pd.to_datetime(df["time"], unit="s")

    filename = f"{output_folder}{symbol}_4H.csv"
    df.to_csv(filename, index=False)

    print(f"Saved: {filename}")

mt5.shutdown()
print("Download complete.")
