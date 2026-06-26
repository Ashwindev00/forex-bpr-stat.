#property strict
#property version   "1.00"
#property description "BPR live EA (H1) based on BPR [TFO] logic"

#include <Trade/Trade.mqh>

input ENUM_TIMEFRAMES InpTimeframe = PERIOD_H1;
input int      InpBarsSince = 10;
input double   InpBprThreshold = 0.0;
input bool     InpOnlyCleanBpr = false;
input int      InpLeftCandlesRequired = 3;
input double   InpRiskPercent = 1.0;        // percent per trade
input double   InpFixedRiskUSD = 40.0;      // fixed risk per trade (0 = use InpRiskPercent)
input double   InpStopBufferPct = 30.0;     // buffer beyond zone edge as % of zone height
input long     InpMagic = 27032026;
input bool     InpDebugLogs = true;         // print detailed state/skip reasons
input bool     InpDrawZones = true;         // draw BPR rectangles on chart (visual tester)
input string   InpSymbols = "EURUSD,GBPUSD,USDJPY,AUDUSD"; // symbols EA is allowed to trade (comma-separated)
input int      InpTimerSeconds = 15;       // how often to check symbols for new H1 bars

CTrade trade;

#define MAX_SYMBOLS 8
string   g_symbols[MAX_SYMBOLS];
int      g_symbol_count = 0;
datetime g_lastBarTime[MAX_SYMBOLS];
int      g_hist_count[MAX_SYMBOLS];

// Per-symbol state (zones + history) so we can trade multiple symbols independently.
// NOTE: Memory cost is higher, but keeps the logic deterministic and stable.

void CheckSymbols();

enum ZoneState
{
   ZS_WAIT_LEFT = 0,
   ZS_WAIT_RETURN = 1,
   ZS_WAIT_CLOSE_INSIDE = 2,
   ZS_DONE = 3
};

struct BprZone
{
   bool     active;
   bool     bullish;
   double   top;
   double   bottom;
   int      state;
   int      leftCount;
   datetime createdBarTime;
};

#define MAX_ZONES 64
BprZone g_zones[MAX_SYMBOLS][MAX_ZONES];
BprZone g_zone_history[MAX_SYMBOLS][512];

double PipSize()
{
   // Generic pip size: point*10 (works for 5-digit FX, 3-digit JPY, many metals)
   return SymbolInfoDouble(_Symbol, SYMBOL_POINT) * 10.0;
}

double H(string sym, int shift) { return iHigh(sym, InpTimeframe, shift); }
double L(string sym, int shift) { return iLow(sym, InpTimeframe, shift); }
double C(string sym, int shift) { return iClose(sym, InpTimeframe, shift); }
datetime T(string sym, int shift) { return iTime(sym, InpTimeframe, shift); }

void Dbg(string msg)
{
   if(InpDebugLogs)
      Print(msg);
}

string ZoneStateName(int st)
{
   if(st == ZS_WAIT_LEFT) return "WAIT_LEFT";
   if(st == ZS_WAIT_RETURN) return "WAIT_RETURN";
   if(st == ZS_WAIT_CLOSE_INSIDE) return "WAIT_CLOSE_INSIDE";
   if(st == ZS_DONE) return "DONE";
   return "UNKNOWN";
}

string ZoneObjName(bool bullish, datetime createdTime, double top, double bottom)
{
   // keep name short-ish; include seconds + price hash-ish to avoid collisions
   return StringFormat("BPR_%s_%I64d_%d_%d",
                       bullish ? "BULL" : "BEAR",
                       (long)createdTime,
                       (int)MathRound(top * 100000),
                       (int)MathRound(bottom * 100000));
}

void DrawZone(bool bullish, datetime t1, datetime t2, double top, double bottom, bool active)
{
   if(!InpDrawZones)
      return;

   string name = ZoneObjName(bullish, t1, top, bottom);
   if(ObjectFind(0, name) < 0)
   {
      ObjectCreate(0, name, OBJ_RECTANGLE, 0, t1, top, t2, bottom);
      ObjectSetInteger(0, name, OBJPROP_BACK, true);
      ObjectSetInteger(0, name, OBJPROP_SELECTABLE, false);
      ObjectSetInteger(0, name, OBJPROP_HIDDEN, true);
   }
   ObjectSetInteger(0, name, OBJPROP_TIME, 0, t1);
   ObjectSetDouble(0, name, OBJPROP_PRICE, 0, top);
   ObjectSetInteger(0, name, OBJPROP_TIME, 1, t2);
   ObjectSetDouble(0, name, OBJPROP_PRICE, 1, bottom);

   // Green for bullish, Red for bearish. Gray if inactive.
   color clr = bullish ? clrLime : clrTomato;
   if(!active) clr = clrGray;
   ObjectSetInteger(0, name, OBJPROP_COLOR, clr);
   ObjectSetInteger(0, name, OBJPROP_STYLE, STYLE_SOLID);
   ObjectSetInteger(0, name, OBJPROP_WIDTH, 1);
   ObjectSetInteger(0, name, OBJPROP_FILL, true);
   ObjectSetInteger(0, name, OBJPROP_TRANSPARENCY, active ? 80 : 90);
}

bool NewFvgBearishAt(string sym, int s)
{
   return (L(sym, s + 2) - H(sym, s) > 0.0);
}

bool NewFvgBullishAt(string sym, int s)
{
   return (L(sym, s) - H(sym, s + 2) > 0.0);
}

bool FindBarsSinceCond(string sym, bool forBull, int s, int &kOut)
{
   // ta.barssince(cond) equivalent limited by InpBarsSince
   for(int k = 0; k <= InpBarsSince; k++)
   {
      bool cond = forBull ? NewFvgBearishAt(sym, s + k) : NewFvgBullishAt(sym, s + k);
      if(cond)
      {
         kOut = k;
         return true;
      }
   }
   return false;
}

bool BuildBullZoneAt(string sym, int s, double &top, double &bottom)
{
   if(!NewFvgBullishAt(sym, s))
      return false;

   int k = -1;
   if(!FindBarsSinceCond(sym, true, s, k))
      return false;

   double left  = H(sym, s + k) + L(sym, s + k + 2) + H(sym, s + 2) + L(sym, s);
   double right = MathMax(L(sym, s + k + 2), L(sym, s)) - MathMin(H(sym, s + k), H(sym, s + 2));
   if(!(left > right))
      return false;

   double combinedLow  = MathMax(H(sym, s + k), H(sym, s + 2));
   double combinedHigh = MathMin(L(sym, s + k + 2), L(sym, s));

   if(InpOnlyCleanBpr)
   {
      for(int h = 2; h <= k; h++)
      {
         if(H(sym, s + h) > combinedLow)
            return false;
      }
   }

   if((combinedHigh - combinedLow) < InpBprThreshold)
      return false;

   top = combinedHigh;
   bottom = combinedLow;
   return true;
}

bool BuildBearZoneAt(string sym, int s, double &top, double &bottom)
{
   if(!NewFvgBearishAt(sym, s))
      return false;

   int k = -1;
   if(!FindBarsSinceCond(sym, false, s, k))
      return false;

   double left  = H(sym, s + k) + L(sym, s + k + 2) + H(sym, s + 2) + L(sym, s);
   double right = MathMax(L(sym, s + k + 2), L(sym, s)) - MathMin(H(sym, s + k), H(sym, s + 2));
   if(!(left > right))
      return false;

   double combinedLow  = MathMax(H(sym, s + k + 2), H(sym, s));
   double combinedHigh = MathMin(L(sym, s + k), L(sym, s + 2));

   if(InpOnlyCleanBpr)
   {
      for(int h = 2; h <= k; h++)
      {
         if(L(sym, s + h) < combinedHigh)
            return false;
      }
   }

   if((combinedHigh - combinedLow) < InpBprThreshold)
      return false;

   top = combinedHigh;
   bottom = combinedLow;
   return true;
}

void AddZone(int si, string sym, bool bullish, double top, double bottom, datetime barTime)
{
   for(int i = 0; i < MAX_ZONES; i++)
   {
      if(!g_zones[si][i].active)
      {
         g_zones[si][i].active = true;
         g_zones[si][i].bullish = bullish;
         g_zones[si][i].top = top;
         g_zones[si][i].bottom = bottom;
         g_zones[si][i].state = ZS_WAIT_LEFT;
         g_zones[si][i].leftCount = 0;
         g_zones[si][i].createdBarTime = barTime;
         Dbg(StringFormat("Zone created: %s top=%.5f bottom=%.5f createdBarTime=%s",
                          bullish ? "BULL" : "BEAR",
                          top, bottom, TimeToString(barTime, TIME_DATE|TIME_MINUTES)));
         if(sym == _Symbol)
            DrawZone(bullish, barTime, barTime + PeriodSeconds(InpTimeframe) * 50, top, bottom, true);
         if(g_hist_count[si] < 512)
         {
            g_zone_history[si][g_hist_count[si]] = g_zones[si][i];
            g_hist_count[si]++;
         }
         return;
      }
   }
}

bool FindOppositeTarget(int si, bool bullishEntry, datetime nowBarTime, double entry, double &tpOut)
{
   // nearest opposite-color BPR that was already visible before entry
   // long -> most recent bearish zone with target = bearish bottom and target > entry
   // short -> most recent bullish zone with target = bullish top and target < entry
   for(int i = g_hist_count[si] - 1; i >= 0; i--)
   {
      BprZone z = g_zone_history[si][i];
      if(z.createdBarTime >= nowBarTime)
         continue;
      if(bullishEntry && z.bullish)
         continue;
      if(!bullishEntry && !z.bullish)
         continue;

      double candidate = bullishEntry ? z.bottom : z.top;
      if(bullishEntry && candidate > entry)
      {
         tpOut = candidate;
         return true;
      }
      if(!bullishEntry && candidate < entry)
      {
         tpOut = candidate;
         return true;
      }
   }
   return false;
}

bool HasOpenPosition(string symbol)
{
   for(int i = PositionsTotal() - 1; i >= 0; i--)
   {
      ulong ticket = PositionGetTicket(i);
      if(ticket == 0)
         continue;
      if(!PositionSelectByTicket(ticket))
         continue;
      string posSym = PositionGetString(POSITION_SYMBOL);
      if(posSym != symbol)
         continue;
      long magic = (long)PositionGetInteger(POSITION_MAGIC);
      if(magic == InpMagic)
         return true;
   }
   return false;
}

double NormalizeVolume(string sym, double lots)
{
   double vmin  = SymbolInfoDouble(sym, SYMBOL_VOLUME_MIN);
   double vmax  = SymbolInfoDouble(sym, SYMBOL_VOLUME_MAX);
   double vstep = SymbolInfoDouble(sym, SYMBOL_VOLUME_STEP);

   if(vstep <= 0.0)
      vstep = 0.01;

   lots = MathMax(vmin, MathMin(vmax, lots));
   lots = MathFloor(lots / vstep) * vstep;
   lots = NormalizeDouble(lots, 2);
   return lots;
}

double ComputeRiskLots(string sym, double entry, double sl)
{
   double balance = AccountInfoDouble(ACCOUNT_BALANCE);
   double riskAmount = (InpFixedRiskUSD > 0.0) ? InpFixedRiskUSD : (balance * (InpRiskPercent / 100.0));
   if(riskAmount <= 0.0)
      return 0.0;

   double tickValue = SymbolInfoDouble(sym, SYMBOL_TRADE_TICK_VALUE);
   double tickSize  = SymbolInfoDouble(sym, SYMBOL_TRADE_TICK_SIZE);
   if(tickValue <= 0.0 || tickSize <= 0.0)
      return 0.0;

   double stopDistance = MathAbs(entry - sl);
   double lossPerLot = (stopDistance / tickSize) * tickValue;
   if(lossPerLot <= 0.0)
      return 0.0;

   double lots = riskAmount / lossPerLot;
   return NormalizeVolume(sym, lots);
}

bool PlaceTrade(int si, string sym, bool bullish, double zoneTop, double zoneBottom)
{
   if(HasOpenPosition(sym))
   {
      Dbg("Trade skipped: already have an open position for this symbol+magic.");
      return false;
   }

   // When this EA detects a new bar, ProcessClosedBar() is executed and the
   // current bar (shift=0) has just opened. We use the bar open as the
   // backtest-consistent entry price for SL/TP and sizing calculations.
   double entry = iOpen(sym, InpTimeframe, 0);
   if(entry <= 0.0)
   {
      Dbg("Trade skipped: entry price unavailable (iOpen returned <= 0).");
      return false;
   }

   double zoneHeight = MathAbs(zoneTop - zoneBottom);
   if(zoneHeight <= 0.0)
   {
      Dbg("Trade skipped: invalid zone height.");
      return false;
   }
   double slPad = zoneHeight * (InpStopBufferPct / 100.0);
   double sl = bullish ? (zoneBottom - slPad) : (zoneTop + slPad);
   double risk = MathAbs(entry - sl);
   if(risk <= 0.0)
   {
      Dbg("Trade skipped: invalid risk distance (entry == sl).");
      return false;
   }
   double tp = 0.0;
   if(!FindOppositeTarget(si, bullish, T(sym, 0), entry, tp))
   {
      Print("Trade skipped: no opposite-color BPR target visible before entry.");
      return false;
   }

   // Enforce broker minimum stop distance (SYMBOL_TRADE_STOPS_LEVEL is in points)
   int stopsLevelPoints = (int)SymbolInfoInteger(sym, SYMBOL_TRADE_STOPS_LEVEL);
   double point = SymbolInfoDouble(sym, SYMBOL_POINT);
   if(stopsLevelPoints > 0 && point > 0.0)
   {
      double minDist = stopsLevelPoints * point;
      if(MathAbs(entry - sl) < minDist)
      {
         sl = bullish ? (entry - minDist) : (entry + minDist);
         risk = MathAbs(entry - sl);
      }
   }

   // Keep TP on correct side; if too close, skip instead of forcing synthetic RR.
   if(bullish && tp <= entry + point)
   {
      Print("Trade skipped: bearish target not above entry.");
      return false;
   }
   if(!bullish && tp >= entry - point)
   {
      Print("Trade skipped: bullish target not below entry.");
      return false;
   }
   if(stopsLevelPoints > 0 && point > 0.0)
   {
      double minDist = stopsLevelPoints * point;
      if(MathAbs(entry - tp) < minDist)
      {
         Print("Trade skipped: target too close to entry for broker stops level.");
         return false;
      }
   }

   double lots = ComputeRiskLots(sym, entry, sl);
   if(lots <= 0.0)
   {
      Dbg("Trade skipped: lot size computed as 0 (tick value/size or risk calc issue).");
      return false;
   }

   trade.SetExpertMagicNumber(InpMagic);
   trade.SetDeviationInPoints(20);

   bool ok = bullish
             ? trade.Buy(lots, sym, 0.0, sl, tp, "BPR_H1_live")
             : trade.Sell(lots, sym, 0.0, sl, tp, "BPR_H1_live");

   if(!ok)
   {
      Print("Order failed. Retcode=", trade.ResultRetcode(), " ", trade.ResultRetcodeDescription());
   }
   else
   {
      int digits = (int)SymbolInfoInteger(sym, SYMBOL_DIGITS);
      Print("Order placed: ", (bullish ? "BUY " : "SELL "), sym,
            " lots=", DoubleToString(lots, 2),
            " SL=", DoubleToString(sl, digits),
            " TP=", DoubleToString(tp, digits));
   }

   return ok;
}

void ProcessClosedBar(int si, string sym)
{
   // We process using the just-closed candle at shift=1.
   int s = 1;
   if(Bars(sym, InpTimeframe) < 200)
      return;

   datetime closedBarTime = T(sym, s);
   double barHigh = H(sym, s);
   double barLow  = L(sym, s);
   double barClose= C(sym, s);

   // 1) Create new zones from BPR logic
   double bullTop, bullBottom, bearTop, bearBottom;
   bool bull = BuildBullZoneAt(sym, s, bullTop, bullBottom);
   bool bear = BuildBearZoneAt(sym, s, bearTop, bearBottom);
   if(InpDebugLogs && (bull || bear))
   {
      Dbg(StringFormat("BPR signal on closed bar %s: bull=%s bear=%s",
                       TimeToString(closedBarTime, TIME_DATE|TIME_MINUTES),
                       bull ? "true" : "false",
                       bear ? "true" : "false"));
   }

   // Pine uses bull_result[1]/bear_result[1] for drawing activation.
   // We mirror next-candle activation by stamping created time as current bar (shift 0).
   if(bull)
      AddZone(si, sym, true, bullTop, bullBottom, T(sym, 0));
   if(bear)
      AddZone(si, sym, false, bearTop, bearBottom, T(sym, 0));

   // 2) Update existing zones with invalidation + state machine
   for(int i = 0; i < MAX_ZONES; i++)
   {
      if(!g_zones[si][i].active)
         continue;

      // Avoid struct references for older compilers
      bool bullish = g_zones[si][i].bullish;
      double top = g_zones[si][i].top;
      double bottom = g_zones[si][i].bottom;
      int state = g_zones[si][i].state;
      int leftCount = g_zones[si][i].leftCount;
      datetime createdBarTime = g_zones[si][i].createdBarTime;

      // Not active until next bar open
      if(createdBarTime > closedBarTime)
         continue;

      // Invalidation same style as Pine boxes
      if(bullish && barLow < bottom)
      {
         Dbg(StringFormat("Zone invalidated (BULL): barLow %.5f < bottom %.5f", barLow, bottom));
         if(sym == _Symbol)
            DrawZone(bullish, createdBarTime, closedBarTime, top, bottom, false);
         g_zones[si][i].active = false;
         continue;
      }
      if(!bullish && barHigh > top)
      {
         Dbg(StringFormat("Zone invalidated (BEAR): barHigh %.5f > top %.5f", barHigh, top));
         if(sym == _Symbol)
            DrawZone(bullish, createdBarTime, closedBarTime, top, bottom, false);
         g_zones[si][i].active = false;
         continue;
      }

      if(state == ZS_WAIT_LEFT)
      {
         bool leftNow = bullish ? (barLow > top) : (barHigh < bottom);
         leftCount = leftNow ? (leftCount + 1) : 0;
         if(InpDebugLogs && leftNow)
            Dbg(StringFormat("Zone %s LEFT count=%d (need %d)", bullish ? "BULL" : "BEAR", leftCount, InpLeftCandlesRequired));
         state = (leftCount >= InpLeftCandlesRequired) ? ZS_WAIT_RETURN : state;
         if(state == ZS_WAIT_RETURN)
            Dbg(StringFormat("Zone %s state -> WAIT_RETURN", bullish ? "BULL" : "BEAR"));
      }
      else if(state == ZS_WAIT_RETURN)
      {
         bool overlaps = (barLow <= top && barHigh >= bottom);
         if(overlaps)
         {
            Dbg(StringFormat("Zone %s returned into zone (overlap). state -> WAIT_CLOSE_INSIDE", bullish ? "BULL" : "BEAR"));
            state = ZS_WAIT_CLOSE_INSIDE;
         }
      }
      else if(state == ZS_WAIT_CLOSE_INSIDE)
      {
         bool inside = (barClose >= bottom && barClose <= top);
         if(inside)
         {
            Dbg(StringFormat("Zone %s close inside confirmed at %s (close=%.5f). Attempting trade...",
                             bullish ? "BULL" : "BEAR",
                             TimeToString(closedBarTime, TIME_DATE|TIME_MINUTES),
                             barClose));
            bool placed = PlaceTrade(si, sym, bullish, top, bottom);
            g_zones[si][i].active = false;
            state = ZS_DONE;
            if(placed)
            {
               if(sym == _Symbol)
                  DrawZone(bullish, createdBarTime, closedBarTime, top, bottom, false);
               break; // one trade per bar
            }
         }
         else
         {
            // reset and wait for a new "left zone" sequence
            Dbg(StringFormat("Zone %s close not inside. Reset to WAIT_LEFT. close=%.5f zone[%.5f..%.5f]",
                             bullish ? "BULL" : "BEAR", barClose, bottom, top));
            state = ZS_WAIT_LEFT;
            leftCount = 0;
         }
      }

      // write back
      g_zones[si][i].state = state;
      g_zones[si][i].leftCount = leftCount;
   }
}

void TrimInPlace(string &s)
{
   // Trim leading/trailing spaces and tabs without relying on non-standard helpers.
   int len = (int)StringLen(s);
   while(len > 0)
   {
      ushort ch0 = StringGetCharacter(s, 0);
      // ASCII: space=32, tab=9
      if(ch0 == 32 || ch0 == 9)
      {
         // MQL5 StringSubstr(start, length) overload is the most portable.
         s = StringSubstr(s, 1, len - 1);
         len = (int)StringLen(s);
         continue;
      }
      break;
   }
   len = (int)StringLen(s);
   while(len > 0)
   {
      ushort chen = StringGetCharacter(s, len - 1);
      if(chen == 32 || chen == 9)
      {
         s = StringSubstr(s, 0, len - 1);
         len = (int)StringLen(s);
         continue;
      }
      break;
   }
}

void ParseSymbols()
{
   g_symbol_count = 0;
   string parts[];
   // MQL5 StringSplit expects a ushort delimiter (character code), not a string delimiter.
   ushort sep = StringGetCharacter(",", 0);
   int n = StringSplit(InpSymbols, sep, parts);
   for(int i = 0; i < n; i++)
   {
      if(g_symbol_count >= MAX_SYMBOLS)
         break;
      string sym = parts[i];
      TrimInPlace(sym);
      if(sym == "")
         continue;
      g_symbols[g_symbol_count] = sym;
      g_symbol_count++;
   }
}

int OnInit()
{
   trade.SetExpertMagicNumber(InpMagic);

   ParseSymbols();
   if(g_symbol_count <= 0)
   {
      Print("BPR_Live_EA: InpSymbols parsed to 0 symbols. Check input format.");
      return(INIT_FAILED);
   }

   int activeCount = 0;
   for(int si = 0; si < g_symbol_count; si++)
   {
      string sym = g_symbols[si];
      if(sym == "")
         continue;
      if(!SymbolSelect(sym, true))
      {
         Print("BPR_Live_EA: Failed to select symbol: ", sym, ". It will be skipped.");
         continue;
      }

      // Move selected symbol to the front to keep loops clean.
      g_symbols[activeCount] = sym;
      activeCount++;
   }

   g_symbol_count = activeCount;
   if(g_symbol_count <= 0)
      return(INIT_FAILED);

   for(int si = 0; si < g_symbol_count; si++)
   {
      string sym = g_symbols[si];
      g_lastBarTime[si] = T(sym, 0);
      g_hist_count[si] = 0;

      for(int i = 0; i < MAX_ZONES; i++)
      {
         g_zones[si][i].active = false;
         g_zones[si][i].bullish = false;
         g_zones[si][i].top = 0.0;
         g_zones[si][i].bottom = 0.0;
         g_zones[si][i].state = ZS_WAIT_LEFT;
         g_zones[si][i].leftCount = 0;
         g_zones[si][i].createdBarTime = 0;
      }

      for(int j = 0; j < 512; j++)
      {
         g_zone_history[si][j].active = false;
         g_zone_history[si][j].bullish = false;
         g_zone_history[si][j].top = 0.0;
         g_zone_history[si][j].bottom = 0.0;
         g_zone_history[si][j].state = ZS_WAIT_LEFT;
         g_zone_history[si][j].leftCount = 0;
         g_zone_history[si][j].createdBarTime = 0;
      }
   }

   // Timer-driven checks ensure multi-symbol logic works even if the chart symbol is quiet.
   if(InpTimerSeconds > 0)
      EventSetTimer(InpTimerSeconds);

   Print("BPR_Live_EA initialized. timeframe=", EnumToString(InpTimeframe), " symbols=", g_symbol_count);
   return(INIT_SUCCEEDED);
}

void OnDeinit(const int reason)
{
   Print("BPR_Live_EA stopped. reason=", reason);
   EventKillTimer();
}

void OnTick()
{
   // Keep logic identical for both tick-driven and timer-driven updates.
   CheckSymbols();
}

void OnTimer()
{
   CheckSymbols();
}

void CheckSymbols()
{
   for(int si = 0; si < g_symbol_count; si++)
   {
      string sym = g_symbols[si];
      if(sym == "")
         continue;

      datetime currentBarTime = T(sym, 0);
      if(currentBarTime == 0)
         continue;

      // New bar event for this specific symbol.
      if(currentBarTime != g_lastBarTime[si])
      {
         g_lastBarTime[si] = currentBarTime;
         ProcessClosedBar(si, sym);
      }
   }
}

