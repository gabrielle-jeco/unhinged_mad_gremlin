//+------------------------------------------------------------------+
//| Indicator5_Replay_v2.mq5                                         |
//| Replay EA with IDENTICAL LOGIC to Indicator5_EA.mq5              |
//| Only adapted for backtest/forward_test JSON structure            |
//| and Strategy Tester environment (pre-loaded signals, OnTick)     |
//+------------------------------------------------------------------+
#property copyright "indicator5"
#property version   "2.00"
#property strict

#include <Trade\Trade.mqh>

//--- Input parameters (SAME as Indicator5_EA.mq5)
input int      MagicNumber     = 12346;        // Magic number for trade identification
input double   RiskPercent     = 1.0;          // Risk per trade (% of balance)
input double   FixedLot        = 0.0;          // Fixed lot size (0 = use risk-based calculation)
input string   SignalFile      = "backtest_signals_XAUUSDm.json"; // JSON signal file
input bool     EnableTrading   = true;         // Enable live trading
input int      PendingExpiryMins = 45;         // Pending order expiry (mins, 45 = 3 bars for M15)
input bool     OOSOnly         = false;        // Only trade OOS signals (is_oos=1)

//--- Global variables (SAME structure as Indicator5_EA.mq5)
CTrade         trade;
datetime       last_signal_time = 0;           // Track last processed signal time
string         last_signal_hash = "";          // Track last processed signal hash (for duplicate detection)

//--- Signal tracking for replay
int            signal_count = 0;
int            current_signal_idx = 0;
bool           signals_loaded = false;

//--- Counters for end-of-test summary
int            signals_skipped_is = 0;           // IS signals skipped (OOSOnly filter)
int            signals_skipped_trading_disabled = 0; // Trading disabled
int            signals_skipped_permission = 0;   // Trading permission denied
int            signals_skipped_invalid = 0;      // Invalid signal data
int            signals_skipped_lot = 0;          // Invalid lot size
int            signals_skipped_duplicate = 0;    // Duplicate signals
int            signals_executed = 0;             // Signals actually executed

//--- Signal data structure (SAME fields as Live EA for identical processing)
struct SignalData
{
   datetime    signal_time;
   string      direction;
   double      entry_price;     // "price" in JSON
   double      sl_distance;     // SL distance for recalculation (SAME as Live EA)
   double      tp_rr_ratio;     // RR ratio for recalculation (SAME as Live EA)
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
   // Configure trade object (SAME as Live EA)
   trade.SetExpertMagicNumber(MagicNumber);
   trade.SetDeviationInPoints(10);  // 1 pip slippage tolerance

   // Log initialization info (SAME as Live EA)
   string sym = Symbol();
   double minlot = SymbolInfoDouble(sym, SYMBOL_VOLUME_MIN);
   double maxlot = SymbolInfoDouble(sym, SYMBOL_VOLUME_MAX);
   double lotstep = SymbolInfoDouble(sym, SYMBOL_VOLUME_STEP);
   int digits = (int)SymbolInfoInteger(sym, SYMBOL_DIGITS);
   double point = SymbolInfoDouble(sym, SYMBOL_POINT);
   long stops_level = SymbolInfoInteger(sym, SYMBOL_TRADE_STOPS_LEVEL);
   double contract_size = SymbolInfoDouble(sym, SYMBOL_TRADE_CONTRACT_SIZE);
   double tick_value = SymbolInfoDouble(sym, SYMBOL_TRADE_TICK_VALUE);
   double tick_size = SymbolInfoDouble(sym, SYMBOL_TRADE_TICK_SIZE);

   PrintFormat("[REPLAY_V2] Initialized | Symbol=%s | Digits=%d | Point=%.5f", sym, digits, point);
   PrintFormat("[REPLAY_V2] Lot: min=%.2f max=%.2f step=%.2f | StopsLevel=%lld", minlot, maxlot, lotstep, stops_level);
   PrintFormat("[REPLAY_V2] Contract: size=%.2f | tick_value=%.5f | tick_size=%.5f", contract_size, tick_value, tick_size);
   PrintFormat("[REPLAY_V2] Risk=%.1f%% | Magic=%d | Trading=%s | PendingExpiry=%d mins | OOSOnly=%s",
               RiskPercent, MagicNumber, EnableTrading ? "ON" : "OFF", PendingExpiryMins, OOSOnly ? "Yes" : "No");

   // Load signals from JSON (specific to Replay)
   if(!LoadSignalsFromJSON(SignalFile))
   {
      Print("[REPLAY_V2] Failed to load signals from ", SignalFile);
      return(INIT_FAILED);
   }

   PrintFormat("[REPLAY_V2] %d signals loaded", signal_count);

   // Show first/last signal times for debugging
   if(signal_count > 0)
   {
      PrintFormat("[REPLAY_V2] First signal: %s | Time=%s",
                  signals[0].direction, TimeToString(signals[0].signal_time, TIME_DATE | TIME_MINUTES));
      PrintFormat("[REPLAY_V2] Last signal: %s | Time=%s",
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

   PrintFormat("[REPLAY_V2] Starting from signal #%d", current_signal_idx);

   return(INIT_SUCCEEDED);
}

//+------------------------------------------------------------------+
//| Expert deinitialization function                                  |
//+------------------------------------------------------------------+
void OnDeinit(const int reason)
{
   Print("========================================================");
   Print("[REPLAY_V2] ============ FINAL SUMMARY ============");
   Print("========================================================");
   PrintFormat("[REPLAY_V2] OOSOnly filter: %s", OOSOnly ? "ENABLED" : "DISABLED");
   PrintFormat("[REPLAY_V2] Total signals loaded: %d", signal_count);

   // Count IS vs OOS in loaded signals
   int is_count = 0, oos_count = 0;
   for(int i = 0; i < signal_count; i++)
   {
      if(signals[i].is_oos == 0) is_count++;
      else oos_count++;
   }
   PrintFormat("[REPLAY_V2] IS signals (is_oos=0): %d", is_count);
   PrintFormat("[REPLAY_V2] OOS signals (is_oos=1): %d", oos_count);

   Print("--------------------------------------------------------");
   PrintFormat("[REPLAY_V2] SKIPPED - IS filter: %d", signals_skipped_is);
   PrintFormat("[REPLAY_V2] SKIPPED - Trading disabled: %d", signals_skipped_trading_disabled);
   PrintFormat("[REPLAY_V2] SKIPPED - Permission denied: %d", signals_skipped_permission);
   PrintFormat("[REPLAY_V2] SKIPPED - Invalid signal: %d", signals_skipped_invalid);
   PrintFormat("[REPLAY_V2] SKIPPED - Invalid lot size: %d", signals_skipped_lot);
   PrintFormat("[REPLAY_V2] SKIPPED - Duplicate: %d", signals_skipped_duplicate);
   PrintFormat("[REPLAY_V2] EXECUTED: %d", signals_executed);
   Print("========================================================");

   PrintFormat("[REPLAY_V2] Deinitialized | Reason=%d", reason);
}

//+------------------------------------------------------------------+
//| Expert tick function - processes signals as time passes          |
//+------------------------------------------------------------------+
void OnTick()
{
   if(!signals_loaded || current_signal_idx >= signal_count)
      return;

   datetime current = TimeCurrent();

   // Process signal if current time is at or past signal time
   while(current_signal_idx < signal_count)
   {
      SignalData sig = signals[current_signal_idx];

      if(sig.signal_time > current)
         break;  // Signal is in the future, wait

      // Process this signal
      ProcessSignal(current_signal_idx);
      current_signal_idx++;
   }
}

//+------------------------------------------------------------------+
//| Process a single signal - IDENTICAL LOGIC to Indicator5_EA.mq5   |
//+------------------------------------------------------------------+
void ProcessSignal(int idx)
{
   SignalData sig = signals[idx];

   // Parse signal fields - SAME as Live EA (uses sl_distance + tp_rr_ratio)
   string direction = sig.direction;
   double ref_price = sig.entry_price;
   double sl_distance = sig.sl_distance;     // From JSON (SAME as Live EA)
   double tp_rr_ratio = sig.tp_rr_ratio;     // From JSON (SAME as Live EA)
   double probability = sig.probability;
   long signal_time = (long)sig.signal_time;
   int is_oos = sig.is_oos;
   string poi_type = sig.poi_type;           // POI type for hash uniqueness

   // Validate signal - detailed per-field checks (SAME as Live EA)
   if(direction == "")
   {
      Print("[REPLAY_V2] SKIP: Invalid signal #", idx, " - direction is empty");
      signals_skipped_invalid++;
      return;
   }
   if(poi_type == "")
   {
      Print("[REPLAY_V2] SKIP: Invalid signal #", idx, " - poi_type is empty");
      signals_skipped_invalid++;
      return;
   }
   if(sl_distance <= 0)
   {
      PrintFormat("[REPLAY_V2] SKIP: Invalid signal #%d - sl_distance=%.5f (must be > 0)", idx, sl_distance);
      signals_skipped_invalid++;
      return;
   }
   if(tp_rr_ratio <= 0)
   {
      PrintFormat("[REPLAY_V2] SKIP: Invalid signal #%d - tp_rr_ratio=%.4f (must be > 0)", idx, tp_rr_ratio);
      signals_skipped_invalid++;
      return;
   }
   if(ref_price <= 0)
   {
      PrintFormat("[REPLAY_V2] SKIP: Invalid signal #%d - ref_price=%.5f (must be > 0)", idx, ref_price);
      signals_skipped_invalid++;
      return;
   }

   // Create unique hash for this signal (SAME as Live EA - direction + price + time + poi_type)
   string signal_hash = direction + DoubleToString(ref_price, 5) + IntegerToString(signal_time) + poi_type;

   // Check for duplicate - handle same-timestamp signals correctly (SAME as Live EA)
   if(signal_time < last_signal_time)
   {
      signals_skipped_duplicate++;
      return;  // Old signal, definitely already processed
   }

   // Handle same timestamp: check if this exact signal was already processed (SAME as Live EA)
   if(signal_time == last_signal_time && signal_hash == last_signal_hash)
   {
      signals_skipped_duplicate++;
      return;  // Exact same signal, skip
   }

   // Update tracking (SAME as Live EA)
   last_signal_time = (datetime)signal_time;
   last_signal_hash = signal_hash;

   // Skip IS signals if OOSOnly is enabled (specific to Replay)
   if(OOSOnly && is_oos == 0)
   {
      signals_skipped_is++;
      return;
   }

   // Check if trading is enabled (SAME as Live EA)
   if(!EnableTrading)
   {
      PrintFormat("[REPLAY_V2] Signal detected: %s @ %.5f | P(win)=%.2f%% | Trading disabled - logged only",
                  direction, ref_price, probability * 100);
      signals_skipped_trading_disabled++;
      return;
   }

   // Check terminal trading permission (SAME as Live EA)
   if(!TerminalInfoInteger(TERMINAL_TRADE_ALLOWED))
   {
      Print("[REPLAY_V2] Trading not allowed by terminal");
      signals_skipped_permission++;
      return;
   }

   // Check account trading permission (SAME as Live EA)
   if(!AccountInfoInteger(ACCOUNT_TRADE_ALLOWED))
   {
      Print("[REPLAY_V2] Trading not allowed for this account");
      signals_skipped_permission++;
      return;
   }

   // Get current market price (SAME as Live EA)
   string sym = Symbol();
   double ask = SymbolInfoDouble(sym, SYMBOL_ASK);
   double bid = SymbolInfoDouble(sym, SYMBOL_BID);
   double ticksize = SymbolInfoDouble(sym, SYMBOL_TRADE_TICK_SIZE);
   int digits = (int)SymbolInfoInteger(sym, SYMBOL_DIGITS);
   double point = SymbolInfoDouble(sym, SYMBOL_POINT);
   long stops_level = SymbolInfoInteger(sym, SYMBOL_TRADE_STOPS_LEVEL);
   double min_dist = stops_level * point;

   // Normalize ref_price to tick size (SAME as Live EA)
   ref_price = NormalizePrice(ref_price, ticksize, digits);

   // Calculate SL/TP from ref_price (SAME as Live EA - recalculates for pending orders)
   double sl_price, tp_price;

   if(direction == "bullish")
   {
      sl_price = ref_price - sl_distance;
      tp_price = ref_price + (sl_distance * tp_rr_ratio);
   }
   else
   {
      sl_price = ref_price + sl_distance;
      tp_price = ref_price - (sl_distance * tp_rr_ratio);
   }

   // Normalize prices (SAME as Live EA)
   sl_price = NormalizePrice(sl_price, ticksize, digits);
   tp_price = NormalizePrice(tp_price, ticksize, digits);

   // Calculate position size based on risk (SAME as Live EA)
   double lot_size = CalculateLotSize(ref_price, sl_price);
   if(lot_size <= 0)
   {
      Print("[REPLAY_V2] Invalid lot size calculated");
      signals_skipped_lot++;
      return;
   }


   // Calculate order expiry time (SAME as Live EA)
   datetime expiry = TimeCurrent() + PendingExpiryMins * 60;

   // Log signal details (SAME as Live EA)
   PrintFormat("[REPLAY_V2] Signal #%d: %s | Ref=%.5f | Ask=%.5f Bid=%.5f | SL=%.5f TP=%.5f | P(win)=%.2f%%",
               idx, direction, ref_price, ask, bid, sl_price, tp_price, probability * 100);

   // DEBUG: Log exact order parameters being sent to MT5
   PrintFormat("[REPLAY_V2] ORDER DEBUG: lot_size=%.4f sl_dist=%.5f tp_dist=%.5f",
               lot_size, sl_distance, sl_distance * tp_rr_ratio);

   // Place pending order based on current price vs ref_price (SAME LOGIC as Live EA)
   bool success = false;
   string order_type = "";

   if(direction == "bullish")
   {
      if(ref_price < ask - min_dist)
      {
         // Price is above entry → BUY LIMIT (wait for pullback)
         order_type = "BUY LIMIT";
         success = trade.BuyLimit(lot_size, ref_price, sym, sl_price, tp_price, ORDER_TIME_SPECIFIED, expiry, "IND5_V2");
      }
      else if(ref_price > ask + min_dist)
      {
         // Price is below entry → BUY STOP (wait for breakout)
         order_type = "BUY STOP";
         success = trade.BuyStop(lot_size, ref_price, sym, sl_price, tp_price, ORDER_TIME_SPECIFIED, expiry, "IND5_V2");
      }
      else
      {
         // Price is at entry level → Market order (instant fill)
         // RECALCULATE SL/TP from actual ask (SAME as Live EA)
         order_type = "BUY MARKET";
         sl_price = ask - sl_distance;
         tp_price = ask + (sl_distance * tp_rr_ratio);
         sl_price = NormalizePrice(sl_price, ticksize, digits);
         tp_price = NormalizePrice(tp_price, ticksize, digits);
         success = trade.Buy(lot_size, sym, 0.0, sl_price, tp_price, "IND5_V2");
      }
   }
   else if(direction == "bearish")
   {
      if(ref_price > bid + min_dist)
      {
         // Price is below entry → SELL LIMIT (wait for rally)
         order_type = "SELL LIMIT";
         success = trade.SellLimit(lot_size, ref_price, sym, sl_price, tp_price, ORDER_TIME_SPECIFIED, expiry, "IND5_V2");
      }
      else if(ref_price < bid - min_dist)
      {
         // Price is above entry → SELL STOP (wait for breakdown)
         order_type = "SELL STOP";
         success = trade.SellStop(lot_size, ref_price, sym, sl_price, tp_price, ORDER_TIME_SPECIFIED, expiry, "IND5_V2");
      }
      else
      {
         // Price is at entry level → Market order (instant fill)
         // RECALCULATE SL/TP from actual bid (SAME as Live EA)
         order_type = "SELL MARKET";
         sl_price = bid + sl_distance;
         tp_price = bid - (sl_distance * tp_rr_ratio);
         sl_price = NormalizePrice(sl_price, ticksize, digits);
         tp_price = NormalizePrice(tp_price, ticksize, digits);
         success = trade.Sell(lot_size, sym, 0.0, sl_price, tp_price, "IND5_V2");
      }
   }

   // Log result (SAME as Live EA)
   if(success)
   {
      signals_executed++;
      PrintFormat("[REPLAY_V2] %s SUCCESS | %.2f lots @ %.5f | Ticket=%lld | Expiry=%s",
                  order_type, lot_size, ref_price, trade.ResultOrder(),
                  TimeToString(expiry, TIME_DATE | TIME_MINUTES));
   }
   else
   {
      PrintFormat("[REPLAY_V2] %s FAILED | RetCode=%d | %s",
                  order_type, trade.ResultRetcode(), trade.ResultComment());
   }
}

//+------------------------------------------------------------------+
//| Calculate lot size based on risk percentage                       |
//| IDENTICAL to Indicator5_EA.mq5                                    |
//+------------------------------------------------------------------+
double CalculateLotSize(double entry_price, double sl_price)
{
   string sym = Symbol();
   double minlot = SymbolInfoDouble(sym, SYMBOL_VOLUME_MIN);
   double maxlot = SymbolInfoDouble(sym, SYMBOL_VOLUME_MAX);
   double lotstep = SymbolInfoDouble(sym, SYMBOL_VOLUME_STEP);

   // If FixedLot is set, use it directly (for debugging/testing)
   if(FixedLot > 0)
   {
      double fixed = MathMax(FixedLot, minlot);
      fixed = MathMin(fixed, maxlot);
      fixed = MathFloor(fixed / lotstep) * lotstep;
      PrintFormat("[REPLAY_V2] FIXED LOT MODE: Using %.2f lots (bypassing risk calculation)", fixed);
      return fixed;
   }

   // Get account balance
   double balance = AccountInfoDouble(ACCOUNT_BALANCE);
   double risk_amount = balance * (RiskPercent / 100.0);

   // Calculate SL distance in price
   double sl_distance = MathAbs(entry_price - sl_price);
   if(sl_distance <= 0)
      return 0;

   // Get symbol info for lot calculation
   double tick_value = SymbolInfoDouble(sym, SYMBOL_TRADE_TICK_VALUE);
   double tick_size = SymbolInfoDouble(sym, SYMBOL_TRADE_TICK_SIZE);

   if(tick_value <= 0 || tick_size <= 0)
   {
      PrintFormat("[REPLAY_V2] Invalid tick info: value=%.5f size=%.5f", tick_value, tick_size);
      return minlot;  // Fallback to minimum lot
   }

   // Calculate lot size: risk_amount = lot * sl_ticks * tick_value
   // sl_ticks = sl_distance / tick_size
   double sl_ticks = sl_distance / tick_size;
   double lot_size = risk_amount / (sl_ticks * tick_value);

   // Debug logging - IMPORTANT for verifying broker values
   PrintFormat("[REPLAY_V2] DEBUG: tick_value=%.5f tick_size=%.5f sl_ticks=%.2f",
               tick_value, tick_size, sl_ticks);
   PrintFormat("[REPLAY_V2] DEBUG: Formula: %.2f / (%.2f * %.5f) = %.4f lot",
               risk_amount, sl_ticks, tick_value, lot_size);

   // Normalize to lot step (IDENTICAL to Live EA)
   double original_lot = lot_size;
   lot_size = MathFloor(lot_size / lotstep) * lotstep;

   // Clamp to min/max
   lot_size = MathMax(lot_size, minlot);
   lot_size = MathMin(lot_size, maxlot);

   // Log if clamped
   if(lot_size != original_lot)
   {
      double actual_risk = lot_size * sl_ticks * tick_value;
      PrintFormat("[REPLAY_V2] CLAMP: lot size adjusted %.4f → %.4f (requested risk %.2f USD, actual risk %.2f USD)",
                  original_lot, lot_size, risk_amount, actual_risk);
   }

   PrintFormat("[REPLAY_V2] Lot calc: Balance=%.2f Risk=%.2f%% (%.2f) | SL=%.5f | Lot=%.2f",
               balance, RiskPercent, risk_amount, sl_distance, lot_size);

   return lot_size;
}

//+------------------------------------------------------------------+
//| Normalize price to tick size                                      |
//| IDENTICAL to Indicator5_EA.mq5                                    |
//+------------------------------------------------------------------+
double NormalizePrice(double price, double ticksize, int digits)
{
   if(ticksize <= 0)
   {
      if(ticksize < 0)
         PrintFormat("[REPLAY_V2] WARNING: ticksize=%.5f is invalid (negative), falling back to digit normalization", ticksize);
      return NormalizeDouble(price, digits);
   }

   return NormalizeDouble(MathRound(price / ticksize) * ticksize, digits);
}

//+------------------------------------------------------------------+
//| Load signals from JSON file (specific to Replay)                  |
//+------------------------------------------------------------------+
bool LoadSignalsFromJSON(string filename)
{
   // Read file content
   if(!FileIsExist(filename))
   {
      PrintFormat("[REPLAY_V2] File not found: %s", filename);
      string fullpath = TerminalInfoString(TERMINAL_DATA_PATH) + "\\MQL5\\Files\\" + filename;
      PrintFormat("[REPLAY_V2] Expected at: %s", fullpath);
      return false;
   }

   int handle = FileOpen(filename, FILE_READ | FILE_TXT | FILE_ANSI);
   if(handle == INVALID_HANDLE)
   {
      int err = GetLastError();
      PrintFormat("[REPLAY_V2] Cannot open %s | Error=%d", filename, err);
      return false;
   }

   string content = "";
   while(!FileIsEnding(handle))
   {
      content += FileReadString(handle) + "\n";
   }
   FileClose(handle);

   PrintFormat("[REPLAY_V2] File read successfully: %s (%d chars)", filename, StringLen(content));

   // Parse signals array from JSON
   string signal_items[];
   int count = ParseJsonArray(content, "signals", signal_items);

   if(count == 0)
   {
      Print("[REPLAY_V2] No signals found in JSON");
      return false;
   }

   PrintFormat("[REPLAY_V2] Found %d signals in JSON", count);

   // Parse each signal
   ArrayResize(signals, count);
   int valid_count = 0;

   for(int i = 0; i < count; i++)
   {
      SignalData sig;

      sig.signal_time = (datetime)(long)GetJsonDouble(signal_items[i], "time");
      sig.direction = GetJsonString(signal_items[i], "direction");
      sig.entry_price = GetJsonDouble(signal_items[i], "price");
      sig.sl_distance = GetJsonDouble(signal_items[i], "sl_distance");  // SAME as Live EA
      sig.tp_rr_ratio = GetJsonDouble(signal_items[i], "tp_rr_ratio");  // SAME as Live EA
      sig.probability = GetJsonDouble(signal_items[i], "probability");
      sig.poi_type = GetJsonString(signal_items[i], "poi_type");
      sig.is_oos = (int)GetJsonDouble(signal_items[i], "is_oos");

      // Validate signal - detailed per-field checks (SAME as Live EA validation)
      if(sig.entry_price <= 0)
      {
         PrintFormat("[REPLAY_V2] Skipping signal #%d - entry_price=%.5f (must be > 0)", i, sig.entry_price);
         continue;
      }
      if(sig.sl_distance <= 0)
      {
         PrintFormat("[REPLAY_V2] Skipping signal #%d - sl_distance=%.5f (must be > 0)", i, sig.sl_distance);
         continue;
      }
      if(sig.tp_rr_ratio <= 0)
      {
         PrintFormat("[REPLAY_V2] Skipping signal #%d - tp_rr_ratio=%.4f (must be > 0)", i, sig.tp_rr_ratio);
         continue;
      }

      signals[valid_count] = sig;
      valid_count++;
   }

   // Resize to actual count
   ArrayResize(signals, valid_count);
   signal_count = valid_count;
   signals_loaded = (signal_count > 0);

   PrintFormat("[REPLAY_V2] Total valid signals: %d", signal_count);

   return signals_loaded;
}

//+------------------------------------------------------------------+
//| Parse JSON array and return items                                 |
//| IDENTICAL to Indicator5_EA.mq5                                    |
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
//| Get string value from JSON                                        |
//| IDENTICAL to Indicator5_EA.mq5                                    |
//+------------------------------------------------------------------+
string GetJsonString(const string &json, const string &key)
{
   string search = "\"" + key + "\"";
   int pos = StringFind(json, search);
   if(pos < 0)
      return "";

   // Ensure this is actually a key (must be preceded by { or , or whitespace)
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

   // Unquoted value
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
//| IDENTICAL to Indicator5_EA.mq5                                    |
//+------------------------------------------------------------------+
double GetJsonDouble(const string &json, const string &key)
{
   string val = GetJsonString(json, key);
   if(val == "")
      return 0.0;
   return StringToDouble(val);
}
//+------------------------------------------------------------------+
