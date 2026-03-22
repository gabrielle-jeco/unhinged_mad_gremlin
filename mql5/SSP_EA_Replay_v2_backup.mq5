//+------------------------------------------------------------------+
//| Indicator5_Replay.mq5                                            |
//| EA for replaying Python backtest/forward_test signals in MT5     |
//| Strategy Tester for realistic broker execution simulation        |
//+------------------------------------------------------------------+
#property copyright "indicator5"
#property version   "2.00"
#property strict

#include <Trade\Trade.mqh>

//--- Input parameters
input int      MagicNumber     = 12346;        // Magic number for trade identification
input double   RiskPercent     = 1.0;          // Risk per trade (% of balance)
input string   SignalFile      = "backtest_signals_XAUUSDm.json"; // JSON signal file
input bool     EnableTrading   = true;         // Enable live trading
input int      PendingExpiryMins = 45;         // Pending order expiry (mins)
input bool     OOSOnly         = false;        // Only trade OOS signals (is_oos=1)

//--- Global variables
CTrade         trade;
int            signal_count = 0;
int            current_signal_idx = 0;
bool           signals_loaded = false;
datetime       last_processed_signal_time = 0;  // Track last processed signal to avoid duplicates

//--- Counters for end-of-test summary
int            signals_skipped_is = 0;           // IS signals skipped (OOSOnly filter)
int            signals_skipped_trading_disabled = 0; // Trading disabled
int            signals_skipped_permission = 0;   // Trading permission denied
int            signals_skipped_lot = 0;          // Invalid lot size
int            signals_skipped_other = 0;        // Other reasons
int            signals_executed = 0;             // Signals actually executed

//--- Signal data structure
struct SignalData
{
   datetime    signal_time;
   string      direction;
   double      entry_price;
   double      sl_price;
   double      tp_price;
   double      probability;
   string      poi_type;
   int         is_oos;
};

SignalData     signals[];

//+------------------------------------------------------------------+
//| Expert initialization function                                    |
//+------------------------------------------------------------------+
int OnInit()
{
   // Configure trade object
   trade.SetExpertMagicNumber(MagicNumber);
   trade.SetDeviationInPoints(10);  // 1 pip slippage tolerance

   // Load signals from JSON
   if(!LoadSignalsFromJSON(SignalFile))
   {
      Print("[REPLAY] Failed to load signals from ", SignalFile);
      return(INIT_FAILED);
   }

   PrintFormat("[REPLAY] Initialized | %d signals loaded | OOSOnly=%s | Trading=%s",
               signal_count, OOSOnly ? "Yes" : "No", EnableTrading ? "ON" : "OFF");

   // Show first/last signal times for debugging
   if(signal_count > 0)
   {
      PrintFormat("[REPLAY] First signal: %s | Time=%s",
                  signals[0].direction, TimeToString(signals[0].signal_time, TIME_DATE | TIME_MINUTES));
      PrintFormat("[REPLAY] Last signal: %s | Time=%s",
                  signals[signal_count-1].direction,
                  TimeToString(signals[signal_count-1].signal_time, TIME_DATE | TIME_MINUTES));
   }

   // Find starting signal index based on current time
   datetime current = TimeCurrent();
   for(int i = 0; i < signal_count; i++)
   {
      if(signals[i].signal_time >= current)
      {
         current_signal_idx = i;
         break;
      }
   }

   PrintFormat("[REPLAY] Starting from signal #%d", current_signal_idx);

   return(INIT_SUCCEEDED);
}

//+------------------------------------------------------------------+
//| Expert deinitialization function                                  |
//+------------------------------------------------------------------+
void OnDeinit(const int reason)
{
   Print("========================================================");
   Print("[REPLAY] ============ FINAL SUMMARY ============");
   Print("========================================================");
   PrintFormat("[REPLAY] OOSOnly filter: %s", OOSOnly ? "ENABLED" : "DISABLED");
   PrintFormat("[REPLAY] Total signals loaded: %d", signal_count);

   // Count IS vs OOS in loaded signals
   int is_count = 0, oos_count = 0;
   for(int i = 0; i < signal_count; i++)
   {
      if(signals[i].is_oos == 0) is_count++;
      else oos_count++;
   }
   PrintFormat("[REPLAY] IS signals (is_oos=0): %d", is_count);
   PrintFormat("[REPLAY] OOS signals (is_oos=1): %d", oos_count);

   Print("--------------------------------------------------------");
   PrintFormat("[REPLAY] SKIPPED - IS filter: %d", signals_skipped_is);
   PrintFormat("[REPLAY] SKIPPED - Trading disabled: %d", signals_skipped_trading_disabled);
   PrintFormat("[REPLAY] SKIPPED - Permission denied: %d", signals_skipped_permission);
   PrintFormat("[REPLAY] SKIPPED - Invalid lot size: %d", signals_skipped_lot);
   PrintFormat("[REPLAY] EXECUTED: %d", signals_executed);
   Print("========================================================");

   PrintFormat("[REPLAY] Deinitialized | Reason=%d", reason);
}

//+------------------------------------------------------------------+
//| Expert tick function                                              |
//+------------------------------------------------------------------+
void OnTick()
{
   if(!signals_loaded || current_signal_idx >= signal_count)
      return;

   datetime current = TimeCurrent();

   // Process signal if current bar is at or past signal time
   // Use >= to ensure we catch signals (signal might be intra-bar)
   if(current_signal_idx < signal_count)
   {
      SignalData sig = signals[current_signal_idx];

      // Debug: show current time vs signal time every N ticks
      static int tick_count = 0;
      tick_count++;
      if(tick_count % 500 == 0)
      {
         PrintFormat("[DEBUG] Tick %d | Current: %s | Signal #%d: %s | SigTime: %s",
                     tick_count, TimeToString(current, TIME_DATE | TIME_MINUTES),
                     current_signal_idx, sig.direction,
                     TimeToString(sig.signal_time, TIME_DATE | TIME_MINUTES));
      }

      // Only process signal once per unique signal_time
      // For duplicate timestamps, use signal index as tiebreaker
      if(sig.signal_time <= current && sig.signal_time > last_processed_signal_time)
      {
         PrintFormat("[REPLAY] TIME MATCH: Signal #%d ready | Current: %s | Signal: %s",
                     current_signal_idx, TimeToString(current, TIME_DATE | TIME_MINUTES),
                     TimeToString(sig.signal_time, TIME_DATE | TIME_MINUTES));
         ProcessSignal(current_signal_idx);
         last_processed_signal_time = sig.signal_time;
         current_signal_idx++;
      }
      // Handle duplicate timestamps - if time equals last, still advance
      else if(sig.signal_time == last_processed_signal_time && last_processed_signal_time > 0 && sig.signal_time <= current)
      {
         PrintFormat("[REPLAY] DUPLICATE TIME: Signal #%d (same as previous) | Time: %s",
                     current_signal_idx, TimeToString(sig.signal_time, TIME_DATE | TIME_MINUTES));
         ProcessSignal(current_signal_idx);
         current_signal_idx++;  // Still advance to next signal
      }
   }
}

//+------------------------------------------------------------------+
//| Process a single signal                                           |
//+------------------------------------------------------------------+
void ProcessSignal(int idx)
{
   SignalData sig = signals[idx];

   // Skip IS signals if OOSOnly is enabled
   if(OOSOnly && sig.is_oos == 0)
   {
      signals_skipped_is++;  // Count for final summary
      return;  // Silent skip, will show in final summary
   }

   // Check if trading is enabled
   if(!EnableTrading)
   {
      signals_skipped_trading_disabled++;
      return;
   }

   // Check terminal/account trading permission
   if(!TerminalInfoInteger(TERMINAL_TRADE_ALLOWED) ||
      !AccountInfoInteger(ACCOUNT_TRADE_ALLOWED))
   {
      signals_skipped_permission++;
      return;
   }

   // Get current market price
   string sym = Symbol();
   double ask = SymbolInfoDouble(sym, SYMBOL_ASK);
   double bid = SymbolInfoDouble(sym, SYMBOL_BID);
   double ticksize = SymbolInfoDouble(sym, SYMBOL_TRADE_TICK_SIZE);
   int digits = (int)SymbolInfoInteger(sym, SYMBOL_DIGITS);
   double point = SymbolInfoDouble(sym, SYMBOL_POINT);
   long stops_level = SymbolInfoInteger(sym, SYMBOL_TRADE_STOPS_LEVEL);
   double min_dist = stops_level * point;

   // Normalize prices
   double ref_price = NormalizePrice(sig.entry_price, ticksize, digits);
   double sl_price = NormalizePrice(sig.sl_price, ticksize, digits);
   double tp_price = NormalizePrice(sig.tp_price, ticksize, digits);

   // Calculate distances for market order recalculation (matches Live EA logic)
   double sl_distance = MathAbs(ref_price - sl_price);
   double tp_distance = MathAbs(ref_price - tp_price);

   // Calculate lot size
   double lot_size = CalculateLotSize(ref_price, sl_price);
   if(lot_size <= 0)
   {
      signals_skipped_lot++;  // Count for final summary
      return;
   }

   // Calculate expiry
   datetime expiry = TimeCurrent() + PendingExpiryMins * 60;

   // Place order
   bool success = false;
   string order_type = "";

   if(sig.direction == "bullish")
   {
      if(ref_price < ask - min_dist)
      {
         order_type = "BUY LIMIT";
         success = trade.BuyLimit(lot_size, ref_price, sym, sl_price, tp_price,
                                  ORDER_TIME_SPECIFIED, expiry, "IND5_REPLAY");
      }
      else if(ref_price > ask + min_dist)
      {
         order_type = "BUY STOP";
         success = trade.BuyStop(lot_size, ref_price, sym, sl_price, tp_price,
                                 ORDER_TIME_SPECIFIED, expiry, "IND5_REPLAY");
      }
      else
      {
         // Price is at entry level → Market order (recalculate SL/TP from actual entry like Live EA)
         order_type = "BUY MARKET";
         double market_sl = NormalizePrice(ask - sl_distance, ticksize, digits);
         double market_tp = NormalizePrice(ask + tp_distance, ticksize, digits);
         success = trade.Buy(lot_size, sym, 0.0, market_sl, market_tp, "IND5_REPLAY");
      }
   }
   else if(sig.direction == "bearish")
   {
      if(ref_price > bid + min_dist)
      {
         order_type = "SELL LIMIT";
         success = trade.SellLimit(lot_size, ref_price, sym, sl_price, tp_price,
                                   ORDER_TIME_SPECIFIED, expiry, "IND5_REPLAY");
      }
      else if(ref_price < bid - min_dist)
      {
         order_type = "SELL STOP";
         success = trade.SellStop(lot_size, ref_price, sym, sl_price, tp_price,
                                  ORDER_TIME_SPECIFIED, expiry, "IND5_REPLAY");
      }
      else
      {
         // Price is at entry level → Market order (recalculate SL/TP from actual entry like Live EA)
         order_type = "SELL MARKET";
         double market_sl = NormalizePrice(bid + sl_distance, ticksize, digits);
         double market_tp = NormalizePrice(bid - tp_distance, ticksize, digits);
         success = trade.Sell(lot_size, sym, 0.0, market_sl, market_tp, "IND5_REPLAY");
      }
   }

   // Log result
   if(success)
   {
      signals_executed++;  // Count for final summary
      PrintFormat("[REPLAY] Signal #%d: %s SUCCESS", idx, order_type);
   }
   else
   {
      signals_skipped_other++;  // Count for final summary
      PrintFormat("[REPLAY] Signal #%d: %s FAILED (%d)", idx, order_type, trade.ResultRetcode());
   }
}

//+------------------------------------------------------------------+
//| Load signals from JSON file                                       |
//+------------------------------------------------------------------+
bool LoadSignalsFromJSON(string filename)
{
   // Read file content
   if(!FileIsExist(filename))
   {
      PrintFormat("[REPLAY] File not found: %s", filename);
      string fullpath = TerminalInfoString(TERMINAL_DATA_PATH) + "\\MQL5\\Files\\" + filename;
      PrintFormat("[REPLAY] Expected at: %s", fullpath);
      return false;
   }

   int handle = FileOpen(filename, FILE_READ | FILE_TXT | FILE_ANSI);
   if(handle == INVALID_HANDLE)
   {
      int err = GetLastError();
      PrintFormat("[REPLAY] Cannot open %s | Error=%d", filename, err);
      return false;
   }

   string content = "";
   while(!FileIsEnding(handle))
   {
      content += FileReadString(handle) + "\n";
   }
   FileClose(handle);

   PrintFormat("[REPLAY] File read successfully: %s (%d chars)", filename, StringLen(content));

   // Parse signals array from JSON
   string signal_items[];
   int count = ParseJsonArray(content, "signals", signal_items);

   if(count == 0)
   {
      Print("[REPLAY] No signals found in JSON");
      return false;
   }

   PrintFormat("[REPLAY] Found %d signals in JSON", count);

   // Parse each signal
   ArrayResize(signals, count);
   int valid_count = 0;

   for(int i = 0; i < count; i++)
   {
      SignalData sig;

      sig.signal_time = (datetime)(long)GetJsonDouble(signal_items[i], "time");
      sig.direction = GetJsonString(signal_items[i], "direction");
      sig.entry_price = GetJsonDouble(signal_items[i], "price");
      sig.sl_price = GetJsonDouble(signal_items[i], "sl_price");
      sig.tp_price = GetJsonDouble(signal_items[i], "tp_price");
      sig.probability = GetJsonDouble(signal_items[i], "probability");
      sig.poi_type = GetJsonString(signal_items[i], "poi_type");
      sig.is_oos = (int)GetJsonDouble(signal_items[i], "is_oos");

      // Validate signal - detailed per-field checks
      if(sig.entry_price <= 0)
      {
         PrintFormat("[REPLAY] Skipping signal #%d - entry_price=%.5f (must be > 0)", i, sig.entry_price);
         continue;
      }
      if(sig.sl_price <= 0)
      {
         PrintFormat("[REPLAY] Skipping signal #%d - sl_price=%.5f (must be > 0)", i, sig.sl_price);
         continue;
      }
      if(sig.tp_price <= 0)
      {
         PrintFormat("[REPLAY] Skipping signal #%d - tp_price=%.5f (must be > 0)", i, sig.tp_price);
         continue;
      }

      signals[valid_count] = sig;
      valid_count++;
   }

   // Resize to actual count
   ArrayResize(signals, valid_count);
   signal_count = valid_count;
   signals_loaded = (signal_count > 0);

   PrintFormat("[REPLAY] Total valid signals: %d", signal_count);

   return signals_loaded;
}

//+------------------------------------------------------------------+
//| Parse JSON array and return items                                 |
//+------------------------------------------------------------------+
int ParseJsonArray(const string &json, const string &key, string &items[])
{
   string search = "\"" + key + "\"";
   int pos = StringFind(json, search);
   if(pos < 0)
      return 0;

   int bracket_start = StringFind(json, "[", pos);
   if(bracket_start < 0)
      return 0;

   // Find matching closing bracket
   int depth = 0;
   int bracket_end = -1;
   for(int i = bracket_start; i < StringLen(json); i++)
   {
      ushort ch = StringGetCharacter(json, i);
      if(ch == '[') depth++;
      else if(ch == ']')
      {
         depth--;
         if(depth == 0)
         {
            bracket_end = i;
            break;
         }
      }
   }

   if(bracket_end < 0)
      return 0;

   string arr_content = StringSubstr(json, bracket_start + 1, bracket_end - bracket_start - 1);

   // Parse individual objects
   int count = 0;
   ArrayResize(items, 0);
   int obj_start = -1;
   int obj_depth = 0;

   for(int i = 0; i < StringLen(arr_content); i++)
   {
      ushort ch = StringGetCharacter(arr_content, i);
      if(ch == '{')
      {
         if(obj_depth == 0)
            obj_start = i;
         obj_depth++;
      }
      else if(ch == '}')
      {
         obj_depth--;
         if(obj_depth == 0 && obj_start >= 0)
         {
            count++;
            ArrayResize(items, count);
            items[count - 1] = StringSubstr(arr_content, obj_start, i - obj_start + 1);
            obj_start = -1;
         }
      }
   }

   return count;
}

//+------------------------------------------------------------------+
//| Get string value from JSON - improved parsing                      |
//+------------------------------------------------------------------+
string GetJsonString(const string &json, const string &key)
{
   string search = "\"" + key + "\"";
   int pos = StringFind(json, search);
   if(pos < 0)
      return "";

   // Ensure this is actually a key (must be preceded by { or ,)
   if(pos > 0)
   {
      ushort prev_char = StringGetCharacter(json, pos - 1);
      if(prev_char != '{' && prev_char != ',' && prev_char != '\n' && prev_char != ' ')
         return "";  // Not a key, it's inside another value
   }

   int colon = StringFind(json, ":", pos + StringLen(search));
   if(colon < 0)
      return "";

   // Skip whitespace
   int start = colon + 1;
   while(start < StringLen(json))
   {
      ushort ch = StringGetCharacter(json, start);
      if(ch != ' ' && ch != '\t' && ch != '\n' && ch != '\r')
         break;
      start++;
   }

   ushort first_char = StringGetCharacter(json, start);

   // String value (quoted)
   if(first_char == '"')
   {
      int end = StringFind(json, "\"", start + 1);
      if(end < 0)
         return "";
      return StringSubstr(json, start + 1, end - start - 1);
   }

   // Unquoted value (number, boolean, null)
   int end = start;
   while(end < StringLen(json))
   {
      ushort ch = StringGetCharacter(json, end);
      if(ch == ',' || ch == '}' || ch == ']' || ch == '\n')
         break;
      end++;
   }

   string val = StringSubstr(json, start, end - start);
   StringTrimRight(val);
   StringTrimLeft(val);
   return val;
}

//+------------------------------------------------------------------+
//| Get double value from JSON                                        |
//+------------------------------------------------------------------+
double GetJsonDouble(const string &json, const string &key)
{
   string val = GetJsonString(json, key);
   if(val == "")
      return 0.0;
   return StringToDouble(val);
}

//+------------------------------------------------------------------+
//| Normalize price to tick size                                      |
//+------------------------------------------------------------------+
double NormalizePrice(double price, double ticksize, int digits)
{
   if(ticksize <= 0)
   {
      if(ticksize < 0)
         PrintFormat("[REPLAY] WARNING: ticksize=%.5f is invalid (negative), falling back to digit normalization", ticksize);
      return NormalizeDouble(price, digits);
   }
   return NormalizeDouble(MathRound(price / ticksize) * ticksize, digits);
}

//+------------------------------------------------------------------+
//| Calculate lot size based on risk percentage                       |
//+------------------------------------------------------------------+
double CalculateLotSize(double entry_price, double sl_price)
{
   string sym = Symbol();

   double balance = AccountInfoDouble(ACCOUNT_BALANCE);
   double risk_amount = balance * (RiskPercent / 100.0);

   double sl_distance = MathAbs(entry_price - sl_price);
   if(sl_distance <= 0)
      return 0;

   double tick_value = SymbolInfoDouble(sym, SYMBOL_TRADE_TICK_VALUE);
   double tick_size = SymbolInfoDouble(sym, SYMBOL_TRADE_TICK_SIZE);
   double minlot = SymbolInfoDouble(sym, SYMBOL_VOLUME_MIN);
   double maxlot = SymbolInfoDouble(sym, SYMBOL_VOLUME_MAX);
   double lotstep = SymbolInfoDouble(sym, SYMBOL_VOLUME_STEP);

   if(tick_value <= 0 || tick_size <= 0)
      return minlot;

   double sl_ticks = sl_distance / tick_size;
   double lot_size = risk_amount / (sl_ticks * tick_value);

   // Normalize to lot step
   double original_lot = lot_size;  // Save for comparison
   lot_size = MathFloor(lot_size / lotstep) * lotstep;

   // Clamp to min/max
   lot_size = MathMax(lot_size, minlot);
   lot_size = MathMin(lot_size, maxlot);

   // Log if clamped
   if(lot_size != original_lot)
   {
      double actual_risk = lot_size * sl_ticks * tick_value;
      PrintFormat("[REPLAY] CLAMP: lot size adjusted %.4f → %.4f (requested risk %.2f USD, actual risk %.2f USD)",
                  original_lot, lot_size, risk_amount, actual_risk);
   }

   return lot_size;
}
//+------------------------------------------------------------------+
