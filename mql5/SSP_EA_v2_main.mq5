//+------------------------------------------------------------------+
//| Indicator5_EA.mq5                                                |
//| EA for executing signals from indicator5 Python engine           |
//| Uses PENDING orders (LIMIT/STOP) for accurate entry matching     |
//+------------------------------------------------------------------+
#property copyright "indicator5"
#property version   "2.00"
#property strict

#include <Trade\Trade.mqh>

//--- Input parameters
input int      MagicNumber     = 12345;        // Magic number for trade identification
input double   RiskPercent     = 1.0;          // Risk per trade (% of balance)
input int      RefreshMs       = 1000;         // JSON poll interval (ms)
input string   SignalFile      = "ssp_ea_signals.json";
input bool     EnableTrading   = true;         // Enable live trading
input int      PendingExpiryMins = 45;         // Pending order expiry (mins, 45 = 3 bars for M15)

//--- Global variables
CTrade         trade;
datetime       last_signal_time = 0;           // Track last processed signal time
string         last_signal_hash = "";          // Track last processed signal hash (for duplicate detection)
string         last_file_content = "";         // Detect file changes

//+------------------------------------------------------------------+
//| Expert initialization function                                    |
//+------------------------------------------------------------------+
int OnInit()
{
   // Configure trade object
   trade.SetExpertMagicNumber(MagicNumber);
   trade.SetDeviationInPoints(10);  // 1 pip slippage tolerance

   // Log initialization info
   string sym = Symbol();
   double minlot = SymbolInfoDouble(sym, SYMBOL_VOLUME_MIN);
   double maxlot = SymbolInfoDouble(sym, SYMBOL_VOLUME_MAX);
   double lotstep = SymbolInfoDouble(sym, SYMBOL_VOLUME_STEP);
   int digits = (int)SymbolInfoInteger(sym, SYMBOL_DIGITS);
   double point = SymbolInfoDouble(sym, SYMBOL_POINT);
   long stops_level = SymbolInfoInteger(sym, SYMBOL_TRADE_STOPS_LEVEL);

   PrintFormat("[IND5] Initialized | Symbol=%s | Digits=%d | Point=%.5f", sym, digits, point);
   PrintFormat("[IND5] Lot: min=%.2f max=%.2f step=%.2f | StopsLevel=%lld", minlot, maxlot, lotstep, stops_level);
   PrintFormat("[IND5] Risk=%.1f%% | Magic=%d | Trading=%s | PendingExpiry=%d mins",
               RiskPercent, MagicNumber, EnableTrading ? "ON" : "OFF", PendingExpiryMins);

   // Start timer for polling
   EventSetMillisecondTimer(RefreshMs);

   return(INIT_SUCCEEDED);
}

//+------------------------------------------------------------------+
//| Expert deinitialization function                                  |
//+------------------------------------------------------------------+
void OnDeinit(const int reason)
{
   EventKillTimer();
   PrintFormat("[IND5] Deinitialized | Reason=%d", reason);
}

//+------------------------------------------------------------------+
//| Timer function - polls JSON file                                  |
//+------------------------------------------------------------------+
void OnTimer()
{
   // Read signal file
   string content = ReadSignalFile();
   if(content == "" || content == last_file_content)
      return;

   last_file_content = content;

   // Parse signals array
   string signals[];
   int signal_count = ParseJsonArray(content, "signals", signals);

   if(signal_count == 0)
      return;

   // Process each signal
   for(int i = 0; i < signal_count; i++)
   {
      ProcessSignal(signals[i]);
   }
}

//+------------------------------------------------------------------+
//| Process a single signal from JSON                                 |
//+------------------------------------------------------------------+
void ProcessSignal(const string &signal_json)
{
   // Parse signal fields
   string direction = GetJsonString(signal_json, "direction");
   double ref_price = GetJsonDouble(signal_json, "price");        // Target entry price
   double sl_distance = GetJsonDouble(signal_json, "sl_distance"); // POI height (fixed)
   double tp_rr_ratio = GetJsonDouble(signal_json, "tp_rr_ratio"); // RR ratio from config
   double probability = GetJsonDouble(signal_json, "probability");
   long signal_time = (long)GetJsonDouble(signal_json, "time");
   string poi_type = GetJsonString(signal_json, "poi_type");      // POI type for hash uniqueness

   // Validate signal - detailed per-field checks
   if(direction == "")
   {
      Print("[IND5] SKIP: Invalid signal - direction is empty");
      return;
   }
   if(poi_type == "")
   {
      Print("[IND5] SKIP: Invalid signal - poi_type is empty");
      return;
   }
   if(sl_distance <= 0)
   {
      PrintFormat("[IND5] SKIP: Invalid signal - sl_distance=%.5f (must be > 0)", sl_distance);
      return;
   }
   if(tp_rr_ratio <= 0)
   {
      PrintFormat("[IND5] SKIP: Invalid signal - tp_rr_ratio=%.4f (must be > 0)", tp_rr_ratio);
      return;
   }
   if(ref_price <= 0)
   {
      PrintFormat("[IND5] SKIP: Invalid signal - ref_price=%.5f (must be > 0)", ref_price);
      return;
   }

   // Create unique hash for this signal (direction + price + time + poi_type)
   string signal_hash = direction + DoubleToString(ref_price, 5) + IntegerToString(signal_time) + poi_type;

   // Check for duplicate - handle same-timestamp signals correctly
   if(signal_time < last_signal_time)
   {
      return;  // Old signal, definitely already processed
   }

   // Handle same timestamp: check if this exact signal was already processed
   if(signal_time == last_signal_time && signal_hash == last_signal_hash)
   {
      return;  // Exact same signal, skip
   }

   // Update tracking
   last_signal_time = (datetime)signal_time;
   last_signal_hash = signal_hash;

   // Check if trading is enabled
   if(!EnableTrading)
   {
      PrintFormat("[IND5] Signal detected: %s @ %.5f | P(win)=%.2f%% | Trading disabled - logged only",
                  direction, ref_price, probability * 100);
      return;
   }

   // Check terminal trading permission
   if(!TerminalInfoInteger(TERMINAL_TRADE_ALLOWED))
   {
      Print("[IND5] Trading not allowed by terminal");
      return;
   }

   // Check account trading permission
   if(!AccountInfoInteger(ACCOUNT_TRADE_ALLOWED))
   {
      Print("[IND5] Trading not allowed for this account");
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

   // Normalize ref_price to tick size
   ref_price = NormalizePrice(ref_price, ticksize, digits);

   // Calculate SL/TP from ref_price (pending order entry point)
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

   // Normalize prices
   sl_price = NormalizePrice(sl_price, ticksize, digits);
   tp_price = NormalizePrice(tp_price, ticksize, digits);

   // Calculate position size based on risk
   double lot_size = CalculateLotSize(ref_price, sl_price);
   if(lot_size <= 0)
   {
      Print("[IND5] Invalid lot size calculated");
      return;
   }

   // Calculate order expiry time
   datetime expiry = TimeCurrent() + PendingExpiryMins * 60;

   // Log signal details
   PrintFormat("[IND5] Signal: %s | Ref=%.5f | Ask=%.5f Bid=%.5f | SL=%.5f TP=%.5f | P(win)=%.2f%%",
               direction, ref_price, ask, bid, sl_price, tp_price, probability * 100);

   // Place pending order based on current price vs ref_price
   bool success = false;
   string order_type = "";

   if(direction == "bullish")
   {
      if(ref_price < ask - min_dist)
      {
         // Price is above entry → BUY LIMIT (wait for pullback)
         order_type = "BUY LIMIT";
         success = trade.BuyLimit(lot_size, ref_price, sym, sl_price, tp_price, ORDER_TIME_SPECIFIED, expiry, "IND5");
      }
      else if(ref_price > ask + min_dist)
      {
         // Price is below entry → BUY STOP (wait for breakout)
         order_type = "BUY STOP";
         success = trade.BuyStop(lot_size, ref_price, sym, sl_price, tp_price, ORDER_TIME_SPECIFIED, expiry, "IND5");
      }
      else
      {
         // Price is at entry level → Market order (instant fill)
         order_type = "BUY MARKET";
         sl_price = ask - sl_distance;
         tp_price = ask + (sl_distance * tp_rr_ratio);
         sl_price = NormalizePrice(sl_price, ticksize, digits);
         tp_price = NormalizePrice(tp_price, ticksize, digits);
         success = trade.Buy(lot_size, sym, 0.0, sl_price, tp_price, "IND5");
      }
   }
   else if(direction == "bearish")
   {
      if(ref_price > bid + min_dist)
      {
         // Price is below entry → SELL LIMIT (wait for rally)
         order_type = "SELL LIMIT";
         success = trade.SellLimit(lot_size, ref_price, sym, sl_price, tp_price, ORDER_TIME_SPECIFIED, expiry, "IND5");
      }
      else if(ref_price < bid - min_dist)
      {
         // Price is above entry → SELL STOP (wait for breakdown)
         order_type = "SELL STOP";
         success = trade.SellStop(lot_size, ref_price, sym, sl_price, tp_price, ORDER_TIME_SPECIFIED, expiry, "IND5");
      }
      else
      {
         // Price is at entry level → Market order (instant fill)
         order_type = "SELL MARKET";
         sl_price = bid + sl_distance;
         tp_price = bid - (sl_distance * tp_rr_ratio);
         sl_price = NormalizePrice(sl_price, ticksize, digits);
         tp_price = NormalizePrice(tp_price, ticksize, digits);
         success = trade.Sell(lot_size, sym, 0.0, sl_price, tp_price, "IND5");
      }
   }

   // Log result
   if(success)
   {
      PrintFormat("[IND5] %s SUCCESS | %.2f lots @ %.5f | Ticket=%lld | Expiry=%s",
                  order_type, lot_size, ref_price, trade.ResultOrder(),
                  TimeToString(expiry, TIME_DATE | TIME_MINUTES));
   }
   else
   {
      PrintFormat("[IND5] %s FAILED | RetCode=%d | %s",
                  order_type, trade.ResultRetcode(), trade.ResultComment());
   }
}

//+------------------------------------------------------------------+
//| Calculate lot size based on risk percentage                       |
//+------------------------------------------------------------------+
double CalculateLotSize(double entry_price, double sl_price)
{
   string sym = Symbol();

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
   double minlot = SymbolInfoDouble(sym, SYMBOL_VOLUME_MIN);
   double maxlot = SymbolInfoDouble(sym, SYMBOL_VOLUME_MAX);
   double lotstep = SymbolInfoDouble(sym, SYMBOL_VOLUME_STEP);

   if(tick_value <= 0 || tick_size <= 0)
   {
      PrintFormat("[IND5] Invalid tick info: value=%.5f size=%.5f", tick_value, tick_size);
      return minlot;  // Fallback to minimum lot
   }

   // Calculate lot size: risk_amount = lot * sl_ticks * tick_value
   // sl_ticks = sl_distance / tick_size
   double sl_ticks = sl_distance / tick_size;
   double lot_size = risk_amount / (sl_ticks * tick_value);

   // Debug logging - IMPORTANT for verifying broker values
   PrintFormat("[IND5] DEBUG: tick_value=%.5f tick_size=%.5f sl_ticks=%.2f",
               tick_value, tick_size, sl_ticks);
   PrintFormat("[IND5] DEBUG: Formula: %.2f / (%.2f * %.5f) = %.4f lot",
               risk_amount, sl_ticks, tick_value, lot_size);

   // Normalize to lot step (matches Replay EA logic)
   double original_lot = lot_size;  // Save for comparison
   lot_size = MathFloor(lot_size / lotstep) * lotstep;

   // Clamp to min/max
   lot_size = MathMax(lot_size, minlot);
   lot_size = MathMin(lot_size, maxlot);

   // Log if clamped
   if(lot_size != original_lot)
   {
      double actual_risk = lot_size * sl_ticks * tick_value;
      PrintFormat("[IND5] CLAMP: lot size adjusted %.4f → %.4f (requested risk %.2f USD, actual risk %.2f USD)",
                  original_lot, lot_size, risk_amount, actual_risk);
   }

   PrintFormat("[IND5] Lot calc: Balance=%.2f Risk=%.2f%% (%.2f) | SL=%.5f | Lot=%.2f",
               balance, RiskPercent, risk_amount, sl_distance, lot_size);

   return lot_size;
}

//+------------------------------------------------------------------+
//| Normalize price to tick size                                      |
//+------------------------------------------------------------------+
double NormalizePrice(double price, double ticksize, int digits)
{
   if(ticksize <= 0)
   {
      if(ticksize < 0)
         PrintFormat("[IND5] WARNING: ticksize=%.5f is invalid (negative), falling back to digit normalization", ticksize);
      return NormalizeDouble(price, digits);
   }

   return NormalizeDouble(MathRound(price / ticksize) * ticksize, digits);
}

//+------------------------------------------------------------------+
//| Read signal file from MQL5/Files                                  |
//+------------------------------------------------------------------+
string ReadSignalFile()
{
   if(!FileIsExist(SignalFile))
      return "";

   int handle = FileOpen(SignalFile, FILE_READ | FILE_TXT | FILE_ANSI);
   if(handle == INVALID_HANDLE)
      return "";

   string content = "";
   while(!FileIsEnding(handle))
   {
      content += FileReadString(handle) + "\n";
   }
   FileClose(handle);

   return content;
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
//| Get string value from JSON                                        |
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
//+------------------------------------------------------------------+
double GetJsonDouble(const string &json, const string &key)
{
   string val = GetJsonString(json, key);
   if(val == "")
      return 0.0;
   return StringToDouble(val);
}

//+------------------------------------------------------------------+
//| OnTick - not used, we use timer-based polling                     |
//+------------------------------------------------------------------+
void OnTick()
{
   // All logic in OnTimer()
}
//+------------------------------------------------------------------+
