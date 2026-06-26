import MetaTrader5 as mt5

if not mt5.initialize():
    print("MT5 initialize() failed")
else:
    print("MT5 connected successfully")

mt5.shutdown()
