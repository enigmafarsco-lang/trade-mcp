//+------------------------------------------------------------------+
//|                                              TradeMCPFile.mq5    |
//|                     https://github.com/enigmafarsco-lang/trade-mcp
//|                                                                  |
//|  File-only bridge between MetaTrader 5 and trade-mcp.            |
//|                                                                  |
//|  Python writes a JSON request into   MQL5\Files\trade-mcp\in\    |
//|  this EA answers it into             MQL5\Files\trade-mcp\out\   |
//|  and keeps a heartbeat in            MQL5\Files\trade-mcp\status.json
//|                                                                  |
//|  No sockets, no WebRequest, no terminal allow-list and no        |
//|  #include: everything here is plain MQL5, so it behaves the same |
//|  under Wine as on Windows.                                       |
//+------------------------------------------------------------------+
#property copyright "trade-mcp"
#property link      "https://github.com/enigmafarsco-lang/trade-mcp"
#property version   "1.00"
#property description "MetaTrader 5 <-> trade-mcp bridge over MQL5\\Files (no socket)"

//--- inputs ---------------------------------------------------------
input string InpDir           = "trade-mcp";  // folder inside MQL5\Files
input int    InpPollMs        = 150;          // inbox scan interval (ms)
input int    InpStatusMs      = 1000;         // status.json refresh (ms)
input bool   InpVerbose       = true;         // log every request
input bool   InpAllowTrading  = true;         // allow orders from Python
input int    InpMagic         = 20251112;     // magic number
input int    InpDeviation     = 30;           // max slippage (points)
input string InpComment       = "trade-mcp";  // default order comment
input int    InpMaxRatesCount = 5000;         // hard cap on bars per request
input int    InpMaxSymbols    = 2000;         // hard cap on symbols per request
input int    InpMaxPerTick    = 16;           // requests handled per timer tick

//--- globals --------------------------------------------------------
#define EA_NAME    "TradeMCPFile"
#define EA_VERSION "1.00"

long   g_requests  = 0;
long   g_errors    = 0;
uint   g_lastStatus = 0;
string g_errCode   = "";
string g_errMsg    = "";

long   g_retcode   = 0;
string g_retdesc   = "";
ulong  g_order     = 0;
ulong  g_deal      = 0;
double g_resVolume = 0.0;
double g_resPrice  = 0.0;

//+------------------------------------------------------------------+
//| JSON writing                                                     |
//+------------------------------------------------------------------+
string JEsc(const string s)
{
   string out = "";
   int n = StringLen(s);
   for(int i = 0; i < n; i++)
   {
      ushort c = StringGetCharacter(s, i);
      if(c == '"')       out += "\\\"";
      else if(c == '\\') out += "\\\\";
      else if(c == '\n') out += "\\n";
      else if(c == '\r') out += "\\r";
      else if(c == '\t') out += "\\t";
      else if(c < 32)    out += " ";
      else               out += ShortToString(c);
   }
   return out;
}

string JS(const string v)
{
   return "\"" + JEsc(v) + "\"";
}

string JN(const double v, const int digits)
{
   return DoubleToString(v, digits);
}

string JI(const long v)
{
   return (string)v;
}

string JB(const bool v)
{
   if(v) return "true";
   return "false";
}

string AddField(const string acc, const string item)
{
   if(StringLen(acc) == 0) return item;
   return acc + "," + item;
}

//+------------------------------------------------------------------+
//| JSON reading (flat search - our requests are tiny and flat)       |
//+------------------------------------------------------------------+
int ValueStart(const string js, const string key)
{
   string needle = "\"" + key + "\"";
   int p = StringFind(js, needle);
   if(p < 0) return -1;
   p = p + StringLen(needle);
   int n = StringLen(js);
   while(p < n)
   {
      ushort c = StringGetCharacter(js, p);
      if(c == ' ' || c == '\t' || c == '\r' || c == '\n' || c == ':') p++;
      else break;
   }
   if(p >= n) return -1;
   return p;
}

string GetStr(const string js, const string key, const string def)
{
   int p = ValueStart(js, key);
   if(p < 0) return def;
   if(StringGetCharacter(js, p) != '"') return def;
   p++;
   string out = "";
   int n = StringLen(js);
   while(p < n)
   {
      ushort c = StringGetCharacter(js, p);
      if(c == '"') return out;
      if(c == '\\' && p + 1 < n)
      {
         ushort e = StringGetCharacter(js, p + 1);
         if(e == 'n')      out += "\n";
         else if(e == 't') out += "\t";
         else if(e == 'r') out += "\r";
         else              out += ShortToString(e);
         p = p + 2;
         continue;
      }
      out += ShortToString(c);
      p++;
   }
   return out;
}

double GetNum(const string js, const string key, const double def)
{
   int p = ValueStart(js, key);
   if(p < 0) return def;
   int n = StringLen(js);
   string t = "";
   while(p < n)
   {
      ushort c = StringGetCharacter(js, p);
      bool digit = (c >= '0' && c <= '9');
      if(digit || c == '-' || c == '+' || c == '.' || c == 'e' || c == 'E')
      {
         t += ShortToString(c);
         p++;
      }
      else break;
   }
   if(StringLen(t) == 0) return def;
   return StringToDouble(t);
}

long GetLong(const string js, const string key, const long def)
{
   int p = ValueStart(js, key);
   if(p < 0) return def;
   return (long)GetNum(js, key, (double)def);
}

bool GetBool(const string js, const string key, const bool def)
{
   int p = ValueStart(js, key);
   if(p < 0) return def;
   if(StringSubstr(js, p, 4) == "true") return true;
   if(StringSubstr(js, p, 5) == "false") return false;
   return def;
}

//+------------------------------------------------------------------+
//| Error helper                                                     |
//+------------------------------------------------------------------+
void SetError(const string code, const string msg)
{
   g_errCode = code;
   g_errMsg  = msg;
   g_errors++;
   if(InpVerbose) PrintFormat("[trade-mcp] error %s: %s", code, msg);
}

//+------------------------------------------------------------------+
//| Small type helpers                                               |
//+------------------------------------------------------------------+
ENUM_TIMEFRAMES TfFromStr(const string raw)
{
   string s = raw;
   StringToUpper(s);
   StringTrimLeft(s);
   StringTrimRight(s);
   if(StringFind(s, "PERIOD_") == 0) s = StringSubstr(s, 7);
   if(s == "M1")  return PERIOD_M1;
   if(s == "M2")  return PERIOD_M2;
   if(s == "M3")  return PERIOD_M3;
   if(s == "M4")  return PERIOD_M4;
   if(s == "M5")  return PERIOD_M5;
   if(s == "M6")  return PERIOD_M6;
   if(s == "M10") return PERIOD_M10;
   if(s == "M12") return PERIOD_M12;
   if(s == "M15") return PERIOD_M15;
   if(s == "M20") return PERIOD_M20;
   if(s == "M30") return PERIOD_M30;
   if(s == "H1")  return PERIOD_H1;
   if(s == "H2")  return PERIOD_H2;
   if(s == "H3")  return PERIOD_H3;
   if(s == "H4")  return PERIOD_H4;
   if(s == "H6")  return PERIOD_H6;
   if(s == "H8")  return PERIOD_H8;
   if(s == "H12") return PERIOD_H12;
   if(s == "D1")  return PERIOD_D1;
   if(s == "W1")  return PERIOD_W1;
   if(s == "MN1") return PERIOD_MN1;
   return PERIOD_CURRENT;
}

string TfName(const ENUM_TIMEFRAMES tf)
{
   if(tf == PERIOD_M1)  return "M1";
   if(tf == PERIOD_M2)  return "M2";
   if(tf == PERIOD_M3)  return "M3";
   if(tf == PERIOD_M4)  return "M4";
   if(tf == PERIOD_M5)  return "M5";
   if(tf == PERIOD_M6)  return "M6";
   if(tf == PERIOD_M10) return "M10";
   if(tf == PERIOD_M12) return "M12";
   if(tf == PERIOD_M15) return "M15";
   if(tf == PERIOD_M20) return "M20";
   if(tf == PERIOD_M30) return "M30";
   if(tf == PERIOD_H1)  return "H1";
   if(tf == PERIOD_H2)  return "H2";
   if(tf == PERIOD_H3)  return "H3";
   if(tf == PERIOD_H4)  return "H4";
   if(tf == PERIOD_H6)  return "H6";
   if(tf == PERIOD_H8)  return "H8";
   if(tf == PERIOD_H12) return "H12";
   if(tf == PERIOD_D1)  return "D1";
   if(tf == PERIOD_W1)  return "W1";
   if(tf == PERIOD_MN1) return "MN1";
   return "CURRENT";
}

ENUM_ORDER_TYPE_FILLING FillMode(const string symbol)
{
   long mode = SymbolInfoInteger(symbol, SYMBOL_FILLING_MODE);
   if((mode & SYMBOL_FILLING_IOC) != 0) return ORDER_FILLING_IOC;
   if((mode & SYMBOL_FILLING_FOK) != 0) return ORDER_FILLING_FOK;
   return ORDER_FILLING_RETURN;
}

//+------------------------------------------------------------------+
//| Read-only commands                                               |
//+------------------------------------------------------------------+
string DataPing()
{
   string b = "";
   b = AddField(b, "\"pong\":" + JB(true));
   b = AddField(b, "\"server_time\":" + JS(TimeToString(TimeCurrent(), TIME_DATE | TIME_SECONDS)));
   b = AddField(b, "\"epoch\":" + JI((long)TimeCurrent()));
   b = AddField(b, "\"symbol\":" + JS(_Symbol));
   b = AddField(b, "\"requests\":" + JI(g_requests));
   b = AddField(b, "\"errors\":" + JI(g_errors));
   b = AddField(b, "\"ea\":" + JS(EA_NAME));
   b = AddField(b, "\"ea_version\":" + JS(EA_VERSION));
   return "{" + b + "}";
}

string DataTerminalInfo()
{
   string b = "";
   b = AddField(b, "\"connected\":" + JB((bool)TerminalInfoInteger(TERMINAL_CONNECTED)));
   b = AddField(b, "\"trade_allowed\":" + JB((bool)TerminalInfoInteger(TERMINAL_TRADE_ALLOWED)));
   b = AddField(b, "\"dll_allowed\":" + JB((bool)TerminalInfoInteger(TERMINAL_DLLS_ALLOWED)));
   b = AddField(b, "\"community\":" + JB((bool)TerminalInfoInteger(TERMINAL_COMMUNITY_ACCOUNT)));
   b = AddField(b, "\"build\":" + JI((long)TerminalInfoInteger(TERMINAL_BUILD)));
   b = AddField(b, "\"company\":" + JS(TerminalInfoString(TERMINAL_COMPANY)));
   b = AddField(b, "\"name\":" + JS(TerminalInfoString(TERMINAL_NAME)));
   b = AddField(b, "\"path\":" + JS(TerminalInfoString(TERMINAL_PATH)));
   b = AddField(b, "\"data_path\":" + JS(TerminalInfoString(TERMINAL_DATA_PATH)));
   b = AddField(b, "\"language\":" + JS(TerminalInfoString(TERMINAL_LANGUAGE)));
   b = AddField(b, "\"ea_trading_allowed\":" + JB((bool)MQLInfoInteger(MQL_TRADE_ALLOWED)));
   b = AddField(b, "\"ea_allow_trading_input\":" + JB(InpAllowTrading));
   b = AddField(b, "\"ea\":" + JS(EA_NAME));
   b = AddField(b, "\"ea_version\":" + JS(EA_VERSION));
   return "{" + b + "}";
}

string DataAccountInfo()
{
   long mode = (long)AccountInfoInteger(ACCOUNT_TRADE_MODE);
   string modeName = "real";
   if(mode == ACCOUNT_TRADE_MODE_DEMO)    modeName = "demo";
   if(mode == ACCOUNT_TRADE_MODE_CONTEST) modeName = "contest";

   string b = "";
   b = AddField(b, "\"login\":" + JI((long)AccountInfoInteger(ACCOUNT_LOGIN)));
   b = AddField(b, "\"server\":" + JS(AccountInfoString(ACCOUNT_SERVER)));
   b = AddField(b, "\"name\":" + JS(AccountInfoString(ACCOUNT_NAME)));
   b = AddField(b, "\"company\":" + JS(AccountInfoString(ACCOUNT_COMPANY)));
   b = AddField(b, "\"currency\":" + JS(AccountInfoString(ACCOUNT_CURRENCY)));
   b = AddField(b, "\"leverage\":" + JI((long)AccountInfoInteger(ACCOUNT_LEVERAGE)));
   b = AddField(b, "\"balance\":" + JN(AccountInfoDouble(ACCOUNT_BALANCE), 2));
   b = AddField(b, "\"equity\":" + JN(AccountInfoDouble(ACCOUNT_EQUITY), 2));
   b = AddField(b, "\"margin\":" + JN(AccountInfoDouble(ACCOUNT_MARGIN), 2));
   b = AddField(b, "\"margin_free\":" + JN(AccountInfoDouble(ACCOUNT_MARGIN_FREE), 2));
   b = AddField(b, "\"margin_level\":" + JN(AccountInfoDouble(ACCOUNT_MARGIN_LEVEL), 2));
   b = AddField(b, "\"profit\":" + JN(AccountInfoDouble(ACCOUNT_PROFIT), 2));
   b = AddField(b, "\"credit\":" + JN(AccountInfoDouble(ACCOUNT_CREDIT), 2));
   b = AddField(b, "\"trade_allowed\":" + JB((bool)AccountInfoInteger(ACCOUNT_TRADE_ALLOWED)));
   b = AddField(b, "\"trade_expert\":" + JB((bool)AccountInfoInteger(ACCOUNT_TRADE_EXPERT)));
   b = AddField(b, "\"trade_mode\":" + JS(modeName));
   return "{" + b + "}";
}

string DataSymbolsList(const string req)
{
   string pattern = GetStr(req, "pattern", "");
   StringToUpper(pattern);
   long limit = GetLong(req, "limit", 500);
   if(limit <= 0) limit = 500;
   if(limit > InpMaxSymbols) limit = InpMaxSymbols;

   string parts = "";
   int total = SymbolsTotal(false);
   int added = 0;
   for(int i = 0; i < total && added < limit; i++)
   {
      string sym = SymbolName(i, false);
      if(sym == "") continue;
      string desc = SymbolInfoString(sym, SYMBOL_DESCRIPTION);
      if(pattern != "")
      {
         string upSym = sym;
         string upDesc = desc;
         StringToUpper(upSym);
         StringToUpper(upDesc);
         if(StringFind(upSym, pattern) < 0 && StringFind(upDesc, pattern) < 0) continue;
      }
      string b = "";
      b = AddField(b, "\"symbol\":" + JS(sym));
      b = AddField(b, "\"description\":" + JS(desc));
      b = AddField(b, "\"path\":" + JS(SymbolInfoString(sym, SYMBOL_PATH)));
      b = AddField(b, "\"visible\":" + JB((bool)SymbolInfoInteger(sym, SYMBOL_VISIBLE)));
      b = AddField(b, "\"selected\":" + JB((bool)SymbolInfoInteger(sym, SYMBOL_SELECT)));
      parts = AddField(parts, "{" + b + "}");
      added++;
   }
   return "[" + parts + "]";
}

string DataSymbolInfo(const string symbol)
{
   if(!SymbolSelect(symbol, true))
   {
      SetError("unknown_symbol", "symbol not found or cannot be selected: " + symbol);
      return "";
   }
   int digits = (int)SymbolInfoInteger(symbol, SYMBOL_DIGITS);
   long tmode = (long)SymbolInfoInteger(symbol, SYMBOL_TRADE_MODE);
   string modeName = "long_only";
   if(tmode == SYMBOL_TRADE_MODE_FULL)      modeName = "full";
   if(tmode == SYMBOL_TRADE_MODE_DISABLED)  modeName = "disabled";
   if(tmode == SYMBOL_TRADE_MODE_CLOSEONLY) modeName = "close_only";

   long filling = (long)SymbolInfoInteger(symbol, SYMBOL_FILLING_MODE);
   string fillName = "both";
   if(filling == SYMBOL_FILLING_FOK) fillName = "fok";
   if(filling == SYMBOL_FILLING_IOC) fillName = "ioc";

   string b = "";
   b = AddField(b, "\"name\":" + JS(symbol));
   b = AddField(b, "\"description\":" + JS(SymbolInfoString(symbol, SYMBOL_DESCRIPTION)));
   b = AddField(b, "\"path\":" + JS(SymbolInfoString(symbol, SYMBOL_PATH)));
   b = AddField(b, "\"digits\":" + JI(digits));
   b = AddField(b, "\"point\":" + JN(SymbolInfoDouble(symbol, SYMBOL_POINT), 8));
   b = AddField(b, "\"spread\":" + JN((double)SymbolInfoInteger(symbol, SYMBOL_SPREAD), 2));
   b = AddField(b, "\"spread_points\":" + JN((double)SymbolInfoInteger(symbol, SYMBOL_SPREAD), 2));
   b = AddField(b, "\"spread_float\":" + JN((double)SymbolInfoInteger(symbol, SYMBOL_SPREAD_FLOAT), 0));
   b = AddField(b, "\"trade_stops_level\":" + JI((long)SymbolInfoInteger(symbol, SYMBOL_TRADE_STOPS_LEVEL)));
   b = AddField(b, "\"trade_freeze_level\":" + JI((long)SymbolInfoInteger(symbol, SYMBOL_TRADE_FREEZE_LEVEL)));
   b = AddField(b, "\"trade_tick_value\":" + JN(SymbolInfoDouble(symbol, SYMBOL_TRADE_TICK_VALUE), 5));
   b = AddField(b, "\"trade_tick_size\":" + JN(SymbolInfoDouble(symbol, SYMBOL_TRADE_TICK_SIZE), 8));
   b = AddField(b, "\"trade_contract_size\":" + JN(SymbolInfoDouble(symbol, SYMBOL_TRADE_CONTRACT_SIZE), 2));
   b = AddField(b, "\"volume_min\":" + JN(SymbolInfoDouble(symbol, SYMBOL_VOLUME_MIN), 4));
   b = AddField(b, "\"volume_max\":" + JN(SymbolInfoDouble(symbol, SYMBOL_VOLUME_MAX), 4));
   b = AddField(b, "\"volume_step\":" + JN(SymbolInfoDouble(symbol, SYMBOL_VOLUME_STEP), 4));
   b = AddField(b, "\"swap_long\":" + JN(SymbolInfoDouble(symbol, SYMBOL_SWAP_LONG), 4));
   b = AddField(b, "\"swap_short\":" + JN(SymbolInfoDouble(symbol, SYMBOL_SWAP_SHORT), 4));
   b = AddField(b, "\"bid\":" + JN(SymbolInfoDouble(symbol, SYMBOL_BID), digits));
   b = AddField(b, "\"ask\":" + JN(SymbolInfoDouble(symbol, SYMBOL_ASK), digits));
   b = AddField(b, "\"currency_base\":" + JS(SymbolInfoString(symbol, SYMBOL_CURRENCY_BASE)));
   b = AddField(b, "\"currency_profit\":" + JS(SymbolInfoString(symbol, SYMBOL_CURRENCY_PROFIT)));
   b = AddField(b, "\"currency_margin\":" + JS(SymbolInfoString(symbol, SYMBOL_CURRENCY_MARGIN)));
   b = AddField(b, "\"visible\":" + JB((bool)SymbolInfoInteger(symbol, SYMBOL_VISIBLE)));
   b = AddField(b, "\"trade_mode\":" + JS(modeName));
   b = AddField(b, "\"filling_mode\":" + JS(fillName));
   return "{" + b + "}";
}

string DataSymbolSelect(const string req)
{
   string symbol = GetStr(req, "symbol", _Symbol);
   bool enable = GetBool(req, "enable", true);
   if(!SymbolSelect(symbol, enable))
   {
      SetError("unknown_symbol", "cannot select " + symbol);
      return "";
   }
   string b = "";
   b = AddField(b, "\"symbol\":" + JS(symbol));
   b = AddField(b, "\"visible\":" + JB((bool)SymbolInfoInteger(symbol, SYMBOL_VISIBLE)));
   return "{" + b + "}";
}

string DataRates(const string req)
{
   string symbol = GetStr(req, "symbol", _Symbol);
   string tfStr  = GetStr(req, "timeframe", "M15");
   long count    = GetLong(req, "count", 200);
   if(count <= 0) count = 200;
   if(count > InpMaxRatesCount) count = InpMaxRatesCount;

   ENUM_TIMEFRAMES tf = TfFromStr(tfStr);
   if(tf == PERIOD_CURRENT)
   {
      SetError("bad_timeframe", "unsupported timeframe: " + tfStr);
      return "";
   }
   if(!SymbolSelect(symbol, true))
   {
      SetError("unknown_symbol", "symbol not found: " + symbol);
      return "";
   }
   MqlRates r[];
   int n = CopyRates(symbol, tf, 0, (int)count, r);
   if(n <= 0)
   {
      SetError("rates_unavailable",
               StringFormat("CopyRates(%s,%s,%d) failed, err=%d",
                            symbol, tfStr, (int)count, GetLastError()));
      return "";
   }
   int digits = (int)SymbolInfoInteger(symbol, SYMBOL_DIGITS);
   string parts = "";
   for(int i = 0; i < n; i++)
   {
      string b = "";
      b = AddField(b, "\"time\":" + JI((long)r[i].time));
      b = AddField(b, "\"open\":" + JN(r[i].open, digits));
      b = AddField(b, "\"high\":" + JN(r[i].high, digits));
      b = AddField(b, "\"low\":" + JN(r[i].low, digits));
      b = AddField(b, "\"close\":" + JN(r[i].close, digits));
      b = AddField(b, "\"tick_volume\":" + JN((double)r[i].tick_volume, 0));
      b = AddField(b, "\"spread\":" + JI((long)r[i].spread));
      b = AddField(b, "\"real_volume\":" + JN((double)r[i].real_volume, 0));
      parts = AddField(parts, "{" + b + "}");
   }
   string out = "";
   out = AddField(out, "\"symbol\":" + JS(symbol));
   out = AddField(out, "\"timeframe\":" + JS(TfName(tf)));
   out = AddField(out, "\"count\":" + JI(n));
   out = AddField(out, "\"rates\":[" + parts + "]");
   return "{" + out + "}";
}

string DataTick(const string req)
{
   string symbol = GetStr(req, "symbol", _Symbol);
   if(!SymbolSelect(symbol, true))
   {
      SetError("unknown_symbol", "symbol not found: " + symbol);
      return "";
   }
   int digits = (int)SymbolInfoInteger(symbol, SYMBOL_DIGITS);
   double point = SymbolInfoDouble(symbol, SYMBOL_POINT);
   MqlTick t;
   if(!SymbolInfoTick(symbol, t))
   {
      SetError("tick_unavailable", "SymbolInfoTick failed for " + symbol);
      return "";
   }
   double spread = 0.0;
   if(point > 0) spread = (t.ask - t.bid) / point;

   string b = "";
   b = AddField(b, "\"time\":" + JI((long)t.time));
   b = AddField(b, "\"time_str\":" + JS(TimeToString(t.time, TIME_DATE | TIME_SECONDS)));
   b = AddField(b, "\"bid\":" + JN(t.bid, digits));
   b = AddField(b, "\"ask\":" + JN(t.ask, digits));
   b = AddField(b, "\"last\":" + JN(t.last, digits));
   b = AddField(b, "\"volume\":" + JN((double)t.volume, 0));
   b = AddField(b, "\"spread_points\":" + JN(spread, 1));
   return "{" + b + "}";
}

string DataPositions()
{
   int total = PositionsTotal();
   string parts = "";
   for(int i = 0; i < total; i++)
   {
      ulong ticket = PositionGetTicket(i);
      if(ticket == 0) continue;
      string symbol = PositionGetString(POSITION_SYMBOL);
      int digits = (int)SymbolInfoInteger(symbol, SYMBOL_DIGITS);
      long type = (long)PositionGetInteger(POSITION_TYPE);
      string side = "sell";
      if(type == POSITION_TYPE_BUY) side = "buy";
      string b = "";
      b = AddField(b, "\"ticket\":" + JI((long)ticket));
      b = AddField(b, "\"symbol\":" + JS(symbol));
      b = AddField(b, "\"type\":" + JS(side));
      b = AddField(b, "\"volume\":" + JN(PositionGetDouble(POSITION_VOLUME), 4));
      b = AddField(b, "\"price_open\":" + JN(PositionGetDouble(POSITION_PRICE_OPEN), digits));
      b = AddField(b, "\"sl\":" + JN(PositionGetDouble(POSITION_SL), digits));
      b = AddField(b, "\"tp\":" + JN(PositionGetDouble(POSITION_TP), digits));
      b = AddField(b, "\"price_current\":" + JN(PositionGetDouble(POSITION_PRICE_CURRENT), digits));
      b = AddField(b, "\"profit\":" + JN(PositionGetDouble(POSITION_PROFIT), 2));
      b = AddField(b, "\"swap\":" + JN(PositionGetDouble(POSITION_SWAP), 2));
      b = AddField(b, "\"magic\":" + JI((long)PositionGetInteger(POSITION_MAGIC)));
      b = AddField(b, "\"comment\":" + JS(PositionGetString(POSITION_COMMENT)));
      b = AddField(b, "\"time\":" + JI((long)PositionGetInteger(POSITION_TIME)));
      parts = AddField(parts, "{" + b + "}");
   }
   return "[" + parts + "]";
}

string DataOrders()
{
   int total = OrdersTotal();
   string parts = "";
   for(int i = 0; i < total; i++)
   {
      ulong ticket = OrderGetTicket(i);
      if(ticket == 0) continue;
      string symbol = OrderGetString(ORDER_SYMBOL);
      int digits = (int)SymbolInfoInteger(symbol, SYMBOL_DIGITS);
      string b = "";
      b = AddField(b, "\"ticket\":" + JI((long)ticket));
      b = AddField(b, "\"symbol\":" + JS(symbol));
      b = AddField(b, "\"type\":" + JS(EnumToString((ENUM_ORDER_TYPE)OrderGetInteger(ORDER_TYPE))));
      b = AddField(b, "\"volume\":" + JN(OrderGetDouble(ORDER_VOLUME_CURRENT), 4));
      b = AddField(b, "\"price_open\":" + JN(OrderGetDouble(ORDER_PRICE_OPEN), digits));
      b = AddField(b, "\"sl\":" + JN(OrderGetDouble(ORDER_SL), digits));
      b = AddField(b, "\"tp\":" + JN(OrderGetDouble(ORDER_TP), digits));
      b = AddField(b, "\"magic\":" + JI((long)OrderGetInteger(ORDER_MAGIC)));
      b = AddField(b, "\"time_setup\":" + JI((long)OrderGetInteger(ORDER_TIME_SETUP)));
      b = AddField(b, "\"comment\":" + JS(OrderGetString(ORDER_COMMENT)));
      parts = AddField(parts, "{" + b + "}");
   }
   return "[" + parts + "]";
}

string DataHistoryDeals(const string req)
{
   long from = GetLong(req, "date_from", 0);
   long to   = GetLong(req, "date_to", 0);
   datetime tFrom = (datetime)((long)TimeCurrent() - 30 * 86400);
   datetime tTo   = (datetime)((long)TimeCurrent() + 86400);
   if(from > 0) tFrom = (datetime)from;
   if(to > 0)   tTo   = (datetime)to;

   if(!HistorySelect(tFrom, tTo))
   {
      SetError("history_unavailable",
               "HistorySelect failed, err=" + IntegerToString(GetLastError()));
      return "";
   }
   int total = HistoryDealsTotal();
   string parts = "";
   for(int i = 0; i < total; i++)
   {
      ulong ticket = HistoryDealGetTicket(i);
      if(ticket == 0) continue;
      long type = (long)HistoryDealGetInteger(ticket, DEAL_TYPE);
      if(type != DEAL_TYPE_BUY && type != DEAL_TYPE_SELL) continue;
      long entry = (long)HistoryDealGetInteger(ticket, DEAL_ENTRY);
      string symbol = HistoryDealGetString(ticket, DEAL_SYMBOL);
      int digits = (int)SymbolInfoInteger(symbol, SYMBOL_DIGITS);
      string side = "sell";
      if(type == DEAL_TYPE_BUY) side = "buy";
      string entryName = "inout";
      if(entry == DEAL_ENTRY_IN)  entryName = "in";
      if(entry == DEAL_ENTRY_OUT) entryName = "out";

      string b = "";
      b = AddField(b, "\"ticket\":" + JI((long)ticket));
      b = AddField(b, "\"position_id\":" + JI((long)HistoryDealGetInteger(ticket, DEAL_POSITION_ID)));
      b = AddField(b, "\"order_id\":" + JI((long)HistoryDealGetInteger(ticket, DEAL_ORDER)));
      b = AddField(b, "\"symbol\":" + JS(symbol));
      b = AddField(b, "\"type\":" + JS(side));
      b = AddField(b, "\"entry\":" + JS(entryName));
      b = AddField(b, "\"volume\":" + JN(HistoryDealGetDouble(ticket, DEAL_VOLUME), 4));
      b = AddField(b, "\"price\":" + JN(HistoryDealGetDouble(ticket, DEAL_PRICE), digits));
      b = AddField(b, "\"profit\":" + JN(HistoryDealGetDouble(ticket, DEAL_PROFIT), 2));
      b = AddField(b, "\"swap\":" + JN(HistoryDealGetDouble(ticket, DEAL_SWAP), 2));
      b = AddField(b, "\"commission\":" + JN(HistoryDealGetDouble(ticket, DEAL_COMMISSION), 2));
      b = AddField(b, "\"magic\":" + JI((long)HistoryDealGetInteger(ticket, DEAL_MAGIC)));
      b = AddField(b, "\"time\":" + JI((long)HistoryDealGetInteger(ticket, DEAL_TIME)));
      b = AddField(b, "\"comment\":" + JS(HistoryDealGetString(ticket, DEAL_COMMENT)));
      parts = AddField(parts, "{" + b + "}");
   }
   return "[" + parts + "]";
}

//+------------------------------------------------------------------+
//| Trading                                                          |
//+------------------------------------------------------------------+
bool TradingAllowed(const string where)
{
   if(!InpAllowTrading)
   {
      SetError("trading_disabled", "InpAllowTrading is false in the EA inputs");
      return false;
   }
   if(!(bool)MQLInfoInteger(MQL_TRADE_ALLOWED))
   {
      SetError("trading_disabled", "AutoTrading is disabled (toolbar button / session)");
      return false;
   }
   if(!(bool)TerminalInfoInteger(TERMINAL_TRADE_ALLOWED))
   {
      SetError("trading_disabled", "the terminal does not allow trading");
      return false;
   }
   if(!(bool)AccountInfoInteger(ACCOUNT_TRADE_ALLOWED))
   {
      SetError("trading_disabled", "trading is not allowed for this account");
      return false;
   }
   if(!(bool)AccountInfoInteger(ACCOUNT_TRADE_EXPERT))
   {
      SetError("trading_disabled", "algo trading is disabled for this account");
      return false;
   }
   if(InpVerbose) PrintFormat("[trade-mcp] trading allowed for %s", where);
   return true;
}

bool RetcodeOk(const long rc)
{
   if(rc == TRADE_RETCODE_DONE)         return true;
   if(rc == TRADE_RETCODE_PLACED)       return true;
   if(rc == TRADE_RETCODE_DONE_PARTIAL) return true;
   return false;
}

string TradeResultJson(const ulong ticket)
{
   string b = "";
   b = AddField(b, "\"retcode\":" + JI(g_retcode));
   b = AddField(b, "\"retcode_text\":" + JS(g_retdesc));
   b = AddField(b, "\"order\":" + JI((long)g_order));
   b = AddField(b, "\"deal\":" + JI((long)g_deal));
   b = AddField(b, "\"ticket\":" + JI((long)ticket));
   b = AddField(b, "\"volume\":" + JN(g_resVolume, 4));
   b = AddField(b, "\"price\":" + JN(g_resPrice, 8));
   b = AddField(b, "\"comment\":" + JS(g_retdesc));
   return "{" + b + "}";
}

ulong SendTradeRequest(MqlTradeRequest &req)
{
   MqlTradeResult res;
   ResetLastError();
   bool sent = OrderSend(req, res);
   g_retcode   = (long)res.retcode;
   g_retdesc   = res.comment;
   g_order     = res.order;
   g_deal      = res.deal;
   g_resVolume = res.volume;
   g_resPrice  = res.price;
   if(!sent && g_retcode == 0)
   {
      g_retcode = (long)GetLastError();
      g_retdesc = "OrderSend failed without a retcode";
   }
   return res.order;
}

void ClearRequest(MqlTradeRequest &req)
{
   req.action       = TRADE_ACTION_DEAL;
   req.magic        = 0;
   req.order        = 0;
   req.symbol       = "";
   req.volume       = 0.0;
   req.price        = 0.0;
   req.stoplimit    = 0.0;
   req.sl           = 0.0;
   req.tp           = 0.0;
   req.deviation    = 0;
   req.type         = ORDER_TYPE_BUY;
   req.type_filling = ORDER_FILLING_IOC;
   req.type_time    = ORDER_TIME_GTC;
   req.expiration   = 0;
   req.comment      = "";
   req.position     = 0;
   req.position_by  = 0;
}

double BidOf(const string symbol)
{
   return SymbolInfoDouble(symbol, SYMBOL_BID);
}

double AskOf(const string symbol)
{
   return SymbolInfoDouble(symbol, SYMBOL_ASK);
}

string DataCalcOrder(const string req)
{
   string symbol = GetStr(req, "symbol", _Symbol);
   string side   = GetStr(req, "side", "buy");
   double volume = GetNum(req, "volume", 0.01);
   double price  = GetNum(req, "price", 0.0);

   if(!SymbolSelect(symbol, true))
   {
      SetError("unknown_symbol", "symbol not found: " + symbol);
      return "";
   }
   ENUM_ORDER_TYPE type = ORDER_TYPE_BUY;
   if(side == "sell") type = ORDER_TYPE_SELL;
   if(price <= 0)
   {
      if(type == ORDER_TYPE_BUY) price = AskOf(symbol);
      else                       price = BidOf(symbol);
   }
   double margin = 0.0;
   double profit = 0.0;
   if(!OrderCalcMargin(type, symbol, volume, price, margin))
   {
      SetError("calc_failed", "OrderCalcMargin failed, err=" + IntegerToString(GetLastError()));
      return "";
   }
   int digits = (int)SymbolInfoInteger(symbol, SYMBOL_DIGITS);

   string b = "";
   b = AddField(b, "\"margin\":" + JN(margin, 2));
   b = AddField(b, "\"price\":" + JN(price, digits));
   b = AddField(b, "\"volume\":" + JN(volume, 4));
   b = AddField(b, "\"free_margin_after\":" + JN(AccountInfoDouble(ACCOUNT_MARGIN_FREE) - margin, 2));
   if(OrderCalcProfit(type, symbol, volume, price, price, profit))
      b = AddField(b, "\"profit_per_price_unit\":" + JN(profit, 2));
   return "{" + b + "}";
}

string DataTradeOpen(const string req)
{
   if(!TradingAllowed("trade_open")) return "";

   string symbol  = GetStr(req, "symbol", _Symbol);
   string side    = GetStr(req, "side", "buy");
   double volume  = GetNum(req, "volume", 0.01);
   double sl      = GetNum(req, "sl", 0.0);
   double tp      = GetNum(req, "tp", 0.0);
   string comment = GetStr(req, "comment", InpComment);
   long magic     = GetLong(req, "magic", InpMagic);
   long dev       = GetLong(req, "deviation", InpDeviation);

   if(side != "buy" && side != "sell")
   {
      SetError("bad_request", "side must be 'buy' or 'sell'");
      return "";
   }
   if(!SymbolSelect(symbol, true))
   {
      SetError("unknown_symbol", "symbol not found: " + symbol);
      return "";
   }
   double vmin = SymbolInfoDouble(symbol, SYMBOL_VOLUME_MIN);
   double vmax = SymbolInfoDouble(symbol, SYMBOL_VOLUME_MAX);
   if(volume < vmin || volume > vmax)
   {
      SetError("bad_volume", StringFormat("volume %.4f outside broker range %.4f-%.4f",
                                          volume, vmin, vmax));
      return "";
   }
   int digits = (int)SymbolInfoInteger(symbol, SYMBOL_DIGITS);

   MqlTradeRequest rq;
   ClearRequest(rq);
   rq.action       = TRADE_ACTION_DEAL;
   rq.symbol       = symbol;
   rq.volume       = volume;
   rq.sl           = NormalizeDouble(sl, digits);
   rq.tp           = NormalizeDouble(tp, digits);
   rq.magic        = (ulong)(magic == 0 ? InpMagic : magic);
   rq.deviation    = (ulong)(dev <= 0 ? InpDeviation : dev);
   rq.comment      = comment;
   rq.type_filling = FillMode(symbol);
   if(side == "buy")
   {
      rq.type  = ORDER_TYPE_BUY;
      rq.price = AskOf(symbol);
   }
   else
   {
      rq.type  = ORDER_TYPE_SELL;
      rq.price = BidOf(symbol);
   }
   ulong order = SendTradeRequest(rq);
   string payload = TradeResultJson(order);
   if(!RetcodeOk(g_retcode))
      SetError("broker_rejected", g_retdesc);
   return payload;
}

ulong FindPositionByTicket(const long ticket)
{
   int total = PositionsTotal();
   for(int i = 0; i < total; i++)
   {
      ulong t = PositionGetTicket(i);
      if((long)t == ticket) return t;
   }
   return 0;
}

string DataTradeModify(const string req)
{
   if(!TradingAllowed("trade_modify")) return "";

   long ticket = GetLong(req, "ticket", 0);
   double sl   = GetNum(req, "sl", 0.0);
   double tp   = GetNum(req, "tp", 0.0);
   if(FindPositionByTicket(ticket) == 0)
   {
      SetError("not_found", "position " + (string)ticket + " not found");
      return "";
   }
   string symbol = PositionGetString(POSITION_SYMBOL);
   int digits = (int)SymbolInfoInteger(symbol, SYMBOL_DIGITS);

   MqlTradeRequest rq;
   ClearRequest(rq);
   rq.action   = TRADE_ACTION_SLTP;
   rq.symbol   = symbol;
   rq.position = (ulong)ticket;
   rq.sl       = NormalizeDouble(sl, digits);
   rq.tp       = NormalizeDouble(tp, digits);
   SendTradeRequest(rq);
   string payload = TradeResultJson((ulong)ticket);
   if(!RetcodeOk(g_retcode))
      SetError("broker_rejected", g_retdesc);
   return payload;
}

string DataTradeClose(const string req)
{
   if(!TradingAllowed("trade_close")) return "";

   long ticket   = GetLong(req, "ticket", 0);
   double volume = GetNum(req, "volume", 0.0);
   long dev      = GetLong(req, "deviation", InpDeviation);

   if(FindPositionByTicket(ticket) == 0)
   {
      SetError("not_found", "position " + (string)ticket + " not found");
      return "";
   }
   string symbol   = PositionGetString(POSITION_SYMBOL);
   double posVol   = PositionGetDouble(POSITION_VOLUME);
   long posType    = (long)PositionGetInteger(POSITION_TYPE);

   double outVol = posVol;
   if(volume > 0 && volume < posVol - 0.0001) outVol = volume;

   MqlTradeRequest rq;
   ClearRequest(rq);
   rq.action       = TRADE_ACTION_DEAL;
   rq.symbol       = symbol;
   rq.position     = (ulong)ticket;
   rq.volume       = NormalizeDouble(outVol, 4);
   rq.deviation    = (ulong)(dev <= 0 ? InpDeviation : dev);
   rq.magic        = (ulong)PositionGetInteger(POSITION_MAGIC);
   rq.comment      = InpComment;
   rq.type_filling = FillMode(symbol);
   if(posType == POSITION_TYPE_BUY)
   {
      rq.type  = ORDER_TYPE_SELL;
      rq.price = BidOf(symbol);
   }
   else
   {
      rq.type  = ORDER_TYPE_BUY;
      rq.price = AskOf(symbol);
   }
   SendTradeRequest(rq);
   string payload = TradeResultJson((ulong)ticket);
   if(!RetcodeOk(g_retcode))
      SetError("broker_rejected", g_retdesc);
   return payload;
}

//+------------------------------------------------------------------+
//| Dispatch                                                         |
//+------------------------------------------------------------------+
string HandleRequest(const string body)
{
   string id  = GetStr(body, "id", "");
   string cmd = GetStr(body, "cmd", "");
   g_requests++;

   g_errCode = "";
   g_errMsg  = "";

   string payload = "";
   if(cmd == "ping")               payload = DataPing();
   else if(cmd == "terminal_info") payload = DataTerminalInfo();
   else if(cmd == "account_info")  payload = DataAccountInfo();
   else if(cmd == "symbols_list")  payload = DataSymbolsList(body);
   else if(cmd == "symbol_info")   payload = DataSymbolInfo(GetStr(body, "symbol", _Symbol));
   else if(cmd == "symbol_select") payload = DataSymbolSelect(body);
   else if(cmd == "rates")         payload = DataRates(body);
   else if(cmd == "tick")          payload = DataTick(body);
   else if(cmd == "positions")     payload = DataPositions();
   else if(cmd == "orders")        payload = DataOrders();
   else if(cmd == "history_deals") payload = DataHistoryDeals(body);
   else if(cmd == "calc_order")    payload = DataCalcOrder(body);
   else if(cmd == "trade_open")    payload = DataTradeOpen(body);
   else if(cmd == "trade_modify")  payload = DataTradeModify(body);
   else if(cmd == "trade_close")   payload = DataTradeClose(body);
   else
   {
      SetError("unknown_command", "unsupported command: '" + cmd + "'");
   }

   string resp = "";
   if(g_errCode == "")
   {
      if(payload == "") payload = "{}";
      resp = "{\"id\":" + JS(id) + ",\"ok\":true,\"data\":" + payload + "}";
   }
   else
   {
      resp = "{\"id\":" + JS(id) + ",\"ok\":false,\"error\":{\"code\":" +
             JS(g_errCode) + ",\"message\":" + JS(g_errMsg) + "}}";
   }
   return resp;
}

//+------------------------------------------------------------------+
//| File plumbing                                                    |
//+------------------------------------------------------------------+
string InFolder()
{
   return InpDir + "\\in";
}

string OutFolder()
{
   return InpDir + "\\out";
}

void EnsureFolders()
{
   FolderCreate(InpDir);
   FolderCreate(InpDir + "\\in");
   FolderCreate(InpDir + "\\out");
}

string ReadTextFile(const string name)
{
   int h = FileOpen(name, FILE_READ | FILE_BIN);
   if(h == INVALID_HANDLE) return "";
   ulong size = FileSize(h);
   if(size == 0 || size > 8 * 1024 * 1024)
   {
      FileClose(h);
      return "";
   }
   uchar buf[];
   ArrayResize(buf, (int)size);
   int got = (int)FileReadArray(h, buf);
   FileClose(h);
   if(got <= 0) return "";
   string text = CharArrayToString(buf, 0, got, CP_UTF8);
   StringTrimLeft(text);
   StringTrimRight(text);
   return text;
}

bool WriteTextFile(const string name, const string text)
{
   int h = FileOpen(name, FILE_WRITE | FILE_BIN);
   if(h == INVALID_HANDLE) return false;
   bool ok = true;
   int len = StringLen(text);
   if(len > 0)
   {
      uchar buf[];
      int n = StringToCharArray(text, buf, 0, WHOLE_ARRAY, CP_UTF8);
      if(n > 0) n = n - 1;                 // drop the terminating zero
      if(n > 0)
      {
         int written = (int)FileWriteArray(h, buf, 0, n);
         if(written != n) ok = false;
      }
   }
   FileClose(h);
   return ok;
}

void DeleteQuiet(const string name)
{
   ResetLastError();
   FileDelete(name);
}

void WriteStatus(const bool alive)
{
   string b = "";
   b = AddField(b, "\"alive\":" + JB(alive));
   b = AddField(b, "\"ea\":" + JS(EA_NAME));
   b = AddField(b, "\"version\":" + JS(EA_VERSION));
   b = AddField(b, "\"transport\":\"file\"");
   b = AddField(b, "\"updated_at\":" + JI((long)TimeGMT()));
   b = AddField(b, "\"updated_local\":" + JI((long)TimeLocal()));
   b = AddField(b, "\"symbol\":" + JS(_Symbol));
   b = AddField(b, "\"timeframe\":" + JS(TfName((ENUM_TIMEFRAMES)Period())));
   b = AddField(b, "\"login\":" + JI((long)AccountInfoInteger(ACCOUNT_LOGIN)));
   b = AddField(b, "\"server\":" + JS(AccountInfoString(ACCOUNT_SERVER)));
   b = AddField(b, "\"company\":" + JS(AccountInfoString(ACCOUNT_COMPANY)));
   b = AddField(b, "\"currency\":" + JS(AccountInfoString(ACCOUNT_CURRENCY)));
   b = AddField(b, "\"leverage\":" + JI((long)AccountInfoInteger(ACCOUNT_LEVERAGE)));
   b = AddField(b, "\"balance\":" + JN(AccountInfoDouble(ACCOUNT_BALANCE), 2));
   b = AddField(b, "\"terminal_connected\":" + JB((bool)TerminalInfoInteger(TERMINAL_CONNECTED)));
   b = AddField(b, "\"trade_allowed\":" + JB((bool)TerminalInfoInteger(TERMINAL_TRADE_ALLOWED)));
   b = AddField(b, "\"ea_trading_allowed\":" + JB((bool)MQLInfoInteger(MQL_TRADE_ALLOWED)));
   b = AddField(b, "\"ea_allow_trading_input\":" + JB(InpAllowTrading));
   b = AddField(b, "\"build\":" + JI((long)TerminalInfoInteger(TERMINAL_BUILD)));
   b = AddField(b, "\"data_path\":" + JS(TerminalInfoString(TERMINAL_DATA_PATH)));
   b = AddField(b, "\"requests\":" + JI(g_requests));
   b = AddField(b, "\"errors\":" + JI(g_errors));
   WriteTextFile(InpDir + "\\status.json", "{" + b + "}");
}

void ProcessInbox()
{
   string name = "";
   long handle = FileFindFirst(InFolder() + "\\*.json", name);
   if(handle == INVALID_HANDLE) return;

   string names[];
   int count = 0;
   int cap = InpMaxPerTick;
   if(cap < 1) cap = 1;
   while(true)
   {
      if(StringLen(name) > 0)
      {
         ArrayResize(names, count + 1);
         names[count] = name;
         count++;
         if(count >= cap) break;
      }
      if(!FileFindNext(handle, name)) break;
   }
   FileFindClose(handle);

   for(int i = 0; i < count; i++)
   {
      string reqName = InFolder() + "\\" + names[i];
      string body = ReadTextFile(reqName);
      if(StringLen(body) == 0)
      {
         DeleteQuiet(reqName);
         continue;
      }
      if(InpVerbose) PrintFormat("[trade-mcp] request %s", names[i]);
      string resp = HandleRequest(body);
      if(!WriteTextFile(OutFolder() + "\\" + names[i], resp))
      {
         g_errors++;
         PrintFormat("[trade-mcp] cannot write response %s err=%d", names[i], GetLastError());
      }
      DeleteQuiet(reqName);
   }
}

//+------------------------------------------------------------------+
//| Lifecycle                                                        |
//+------------------------------------------------------------------+
int OnInit()
{
   EnsureFolders();
   g_requests   = 0;
   g_errors     = 0;
   g_lastStatus = GetTickCount();
   WriteStatus(true);
   int poll = InpPollMs;
   if(poll < 20) poll = 20;
   EventSetMillisecondTimer(poll);
   PrintFormat("[trade-mcp] %s started | folder=MQL5\\Files\\%s | trading=%s | chart=%s %s",
               EA_VERSION, InpDir, InpAllowTrading ? "ENABLED" : "disabled",
               _Symbol, TfName((ENUM_TIMEFRAMES)Period()));
   return INIT_SUCCEEDED;
}

void OnDeinit(const int reason)
{
   EventKillTimer();
   WriteStatus(false);
   PrintFormat("[trade-mcp] stopped (reason=%d, requests=%d, errors=%d)",
               reason, g_requests, g_errors);
}

void OnTimer()
{
   ProcessInbox();
   uint now = GetTickCount();
   int every = InpStatusMs;
   if(every < 200) every = 200;
   if(now - g_lastStatus >= (uint)every)
   {
      WriteStatus(true);
      g_lastStatus = now;
   }
}
//+------------------------------------------------------------------+
