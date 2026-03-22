//+------------------------------------------------------------------+
//| Indicator5_Overlay.mq5                                           |
//| Reads indicator5_signals.json and draws POIs + signals on chart  |
//+------------------------------------------------------------------+
#property copyright "indicator5"
#property indicator_chart_window
#property indicator_plots 0

#include <Files\File.mqh>

input int    RefreshMs   = 1000;  // Refresh interval (ms)
input string SignalFile  = "ssp_ea_signals.json";
input color  BullOBColor = clrTeal;
input color  BearOBColor = clrMaroon;
input color  BullFVGColor= clrLimeGreen;
input color  BearFVGColor= clrCrimson;
input color  LiqColor    = clrGold;
input color  BuySignal   = clrLime;
input color  SellSignal  = clrRed;

string PREFIX = "IND5_";
string last_content = "";

//+------------------------------------------------------------------+
int OnInit()
{
   EventSetMillisecondTimer(RefreshMs);
   return INIT_SUCCEEDED;
}

//+------------------------------------------------------------------+
void OnDeinit(const int reason)
{
   ObjectsDeleteAll(0, PREFIX);
   Comment("");
   EventKillTimer();
}

//+------------------------------------------------------------------+
void OnTimer()
{
   string content = ReadSignalFile();
   if(content == "" || content == last_content)
      return;
   last_content = content;

   // Clean old objects
   ObjectsDeleteAll(0, PREFIX);

   // Parse and draw
   DrawFromContent(content);
}

//+------------------------------------------------------------------+
int OnCalculate(const int rates_total, const int prev_calculated,
                const datetime &time[], const double &open[],
                const double &high[], const double &low[],
                const double &close[], const long &tick_volume[],
                const long &volume[], const int &spread[])
{
   return rates_total;
}

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
      content += FileReadString(handle) + "\n";

   FileClose(handle);
   return content;
}

//+------------------------------------------------------------------+
// Simple JSON value extractor (no full parser needed)
//+------------------------------------------------------------------+
string GetJsonValue(const string &json, const string &key)
{
   string search = "\"" + key + "\"";
   int pos = StringFind(json, search);
   if(pos < 0) return "";

   // Find the colon after the key
   int colon = StringFind(json, ":", pos + StringLen(search));
   if(colon < 0) return "";

   int start = colon + 1;
   // Skip whitespace
   while(start < StringLen(json))
   {
      ushort ch = StringGetCharacter(json, start);
      if(ch != ' ' && ch != '\t' && ch != '\n' && ch != '\r')
         break;
      start++;
   }

   ushort first_char = StringGetCharacter(json, start);

   // String value
   if(first_char == '"')
   {
      int end = StringFind(json, "\"", start + 1);
      if(end < 0) return "";
      return StringSubstr(json, start + 1, end - start - 1);
   }

   // Number, bool, null
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
// Extract array blocks from JSON
//+------------------------------------------------------------------+
int GetJsonArray(const string &json, const string &key, string &items[])
{
   string search = "\"" + key + "\"";
   int pos = StringFind(json, search);
   if(pos < 0) return 0;

   // Find opening bracket
   int bracket_start = StringFind(json, "[", pos);
   if(bracket_start < 0) return 0;

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
         if(depth == 0) { bracket_end = i; break; }
      }
   }
   if(bracket_end < 0) return 0;

   string arr_content = StringSubstr(json, bracket_start + 1,
                                     bracket_end - bracket_start - 1);

   // Split by objects {...}
   int count = 0;
   ArrayResize(items, 0);
   int obj_start = -1;
   int obj_depth = 0;

   for(int i = 0; i < StringLen(arr_content); i++)
   {
      ushort ch = StringGetCharacter(arr_content, i);
      if(ch == '{')
      {
         if(obj_depth == 0) obj_start = i;
         obj_depth++;
      }
      else if(ch == '}')
      {
         obj_depth--;
         if(obj_depth == 0 && obj_start >= 0)
         {
            count++;
            ArrayResize(items, count);
            items[count - 1] = StringSubstr(arr_content, obj_start,
                                            i - obj_start + 1);
            obj_start = -1;
         }
      }
   }
   return count;
}

//+------------------------------------------------------------------+
void DrawFromContent(const string &content)
{
   string trend = GetJsonValue(content, "trend");
   string symbol = GetJsonValue(content, "symbol");
   string tf = GetJsonValue(content, "timeframe");

   // Draw Order Blocks
   string obs[];
   int ob_count = GetJsonArray(content, "order_blocks", obs);
   for(int i = 0; i < ob_count; i++)
   {
      double top = StringToDouble(GetJsonValue(obs[i], "top"));
      double bottom = StringToDouble(GetJsonValue(obs[i], "bottom"));
      datetime t = (datetime)StringToInteger(GetJsonValue(obs[i], "time"));
      string bull_str = GetJsonValue(obs[i], "bullish");
      bool is_bull = (bull_str == "true");
      string ps_str = GetJsonValue(obs[i], "post_sweep");
      bool post_sweep = (ps_str == "true");

      string name = PREFIX + "OB_" + IntegerToString(i);
      ObjectCreate(0, name, OBJ_RECTANGLE, 0, t, top, TimeCurrent(), bottom);
      color clr = is_bull ? BullOBColor : BearOBColor;
      ObjectSetInteger(0, name, OBJPROP_COLOR, clr);
      ObjectSetInteger(0, name, OBJPROP_FILL, true);
      ObjectSetInteger(0, name, OBJPROP_BACK, true);

      if(post_sweep)
      {
         ObjectSetInteger(0, name, OBJPROP_WIDTH, 2);
         ObjectSetInteger(0, name, OBJPROP_STYLE, STYLE_SOLID);
      }
   }

   // Draw FVGs
   string fvgs[];
   int fvg_count = GetJsonArray(content, "fvgs", fvgs);
   for(int i = 0; i < fvg_count; i++)
   {
      double top = StringToDouble(GetJsonValue(fvgs[i], "top"));
      double bottom = StringToDouble(GetJsonValue(fvgs[i], "bottom"));
      datetime t = (datetime)StringToInteger(GetJsonValue(fvgs[i], "time"));
      string bull_str = GetJsonValue(fvgs[i], "bullish");
      bool is_bull = (bull_str == "true");

      string name = PREFIX + "FVG_" + IntegerToString(i);
      ObjectCreate(0, name, OBJ_RECTANGLE, 0, t, top, TimeCurrent(), bottom);
      color clr = is_bull ? BullFVGColor : BearFVGColor;
      ObjectSetInteger(0, name, OBJPROP_COLOR, clr);
      ObjectSetInteger(0, name, OBJPROP_FILL, true);
      ObjectSetInteger(0, name, OBJPROP_BACK, true);
   }

   // Draw Liquidity Pools
   string pools[];
   int pool_count = GetJsonArray(content, "liquidity_pools", pools);
   for(int i = 0; i < pool_count; i++)
   {
      double price = StringToDouble(GetJsonValue(pools[i], "price"));
      string swept_str = GetJsonValue(pools[i], "swept");
      bool swept = (swept_str == "true");
      int touches = (int)StringToInteger(GetJsonValue(pools[i], "touches"));

      if(swept) continue;  // Don't draw swept pools

      string name = PREFIX + "LIQ_" + IntegerToString(i);
      ObjectCreate(0, name, OBJ_HLINE, 0, 0, price);
      ObjectSetInteger(0, name, OBJPROP_COLOR, LiqColor);
      ObjectSetInteger(0, name, OBJPROP_STYLE, STYLE_DOT);
      ObjectSetInteger(0, name, OBJPROP_WIDTH, 1);
      ObjectSetString(0, name, OBJPROP_TEXT,
                      IntegerToString(touches) + "x touches");
   }

   // Draw Signals
   string sigs[];
   int sig_count = GetJsonArray(content, "signals", sigs);
   for(int i = 0; i < sig_count; i++)
   {
      string dir = GetJsonValue(sigs[i], "direction");
      double price = StringToDouble(GetJsonValue(sigs[i], "price"));
      double prob = StringToDouble(GetJsonValue(sigs[i], "probability"));
      datetime t = (datetime)StringToInteger(GetJsonValue(sigs[i], "time"));
      string poi_type = GetJsonValue(sigs[i], "poi_type");

      string name = PREFIX + "SIG_" + IntegerToString(i);

      if(dir == "bullish")
      {
         ObjectCreate(0, name, OBJ_ARROW_UP, 0, t, price);
         ObjectSetInteger(0, name, OBJPROP_COLOR, BuySignal);
      }
      else
      {
         ObjectCreate(0, name, OBJ_ARROW_DOWN, 0, t, price);
         ObjectSetInteger(0, name, OBJPROP_COLOR, SellSignal);
      }
      ObjectSetInteger(0, name, OBJPROP_WIDTH, 3);

      // Probability label
      string lbl_name = PREFIX + "SIG_LBL_" + IntegerToString(i);
      string lbl_text = StringFormat("%s %.0f%%", poi_type, prob * 100);
      ObjectCreate(0, lbl_name, OBJ_TEXT, 0, t, price);
      ObjectSetString(0, lbl_name, OBJPROP_TEXT, lbl_text);
      ObjectSetInteger(0, lbl_name, OBJPROP_COLOR,
                       dir == "bullish" ? BuySignal : SellSignal);
      ObjectSetInteger(0, lbl_name, OBJPROP_FONTSIZE, 10);
   }

   // Draw structure breaks
   string brks[];
   int brk_count = GetJsonArray(content, "structure_breaks", brks);
   for(int i = 0; i < brk_count; i++)
   {
      double price = StringToDouble(GetJsonValue(brks[i], "price"));
      datetime t = (datetime)StringToInteger(GetJsonValue(brks[i], "time"));
      string btype = GetJsonValue(brks[i], "type");
      string dir = GetJsonValue(brks[i], "direction");

      string name = PREFIX + "BRK_" + IntegerToString(i);
      ObjectCreate(0, name, OBJ_TEXT, 0, t, price);
      ObjectSetString(0, name, OBJPROP_TEXT, btype);
      ObjectSetInteger(0, name, OBJPROP_FONTSIZE, 8);
      ObjectSetInteger(0, name, OBJPROP_COLOR,
                       btype == "CHoCH" ? clrOrange : clrGray);
   }

   // Dashboard comment
   string dash = "";
   dash += "--- INDICATOR 5 ---\n";
   dash += "Symbol: " + symbol + " | TF: " + tf + "\n";
   dash += "Trend: " + trend + "\n";
   dash += "Active OBs: " + IntegerToString(ob_count) + "\n";
   dash += "Active FVGs: " + IntegerToString(fvg_count) + "\n";
   dash += "Liq. Pools: " + IntegerToString(pool_count) + "\n";
   dash += "Signals: " + IntegerToString(sig_count) + "\n";

   // Show scored POIs
   string pois[];
   int poi_count = GetJsonArray(content, "active_pois", pois);
   if(poi_count > 0)
   {
      dash += "--- SCORED POIs ---\n";
      for(int i = 0; i < poi_count; i++)
      {
         string ptype = GetJsonValue(pois[i], "type");
         string pdir = GetJsonValue(pois[i], "direction");
         double hold = StringToDouble(GetJsonValue(pois[i], "hold_prob"));
         double prior = StringToDouble(GetJsonValue(pois[i], "prior"));
         double fpt = StringToDouble(GetJsonValue(pois[i], "fpt_break"));

         dash += StringFormat("%s %s | Prior=%.2f FPT=%.2f P(hold)=%.2f\n",
                              ptype, pdir, prior, fpt, hold);
      }
   }

   Comment(dash);
   ChartRedraw(0);
}
//+------------------------------------------------------------------+
