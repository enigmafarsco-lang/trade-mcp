//+------------------------------------------------------------------+
//|                                         TradeMCPConnector.mq5    |
//|                        https://github.com/enigmafarsco-lang/trade-mcp |
//|                                                                  |
//|  TCP/JSON bridge between MetaTrader 5 and the trade-mcp server.   |
//|  The EA is the CLIENT: it connects out to Python on InpHost:InpPort,|
//|  reads newline-delimited JSON commands and answers them.          |
//|                                                                  |
//|  Nothing about this needs Windows-specific Python: the terminal   |
//|  can run under Wine on Linux while the Python/AI stack stays      |
//|  native.                                                          |
//+------------------------------------------------------------------+
#property copyright "trade-mcp"
#property link      "https://github.com/enigmafarsco-lang/trade-mcp"
#property version   "1.10"
#property description "MetaTrader 5 <-> trade-mcp bridge (TCP, newline-delimited JSON)"

#include <Trade\Trade.mqh>

//--- inputs ---------------------------------------------------------
input group             "Connection"
input string InpHost            = "127.0.0.1"; // Python host
input int    InpPort            = 8787;        // Python port
input int    InpConnectTimeout  = 1500;        // connect timeout (ms)
input int    InpRetrySeconds    = 3;           // reconnect interval (s)
input int    InpTimerMs         = 50;          // poll interval (ms)
input bool   InpVerbose         = true;        // log every request

input group             "Trading"
input bool   InpAllowTrading    = true;        // allow orders from Python
input int    InpMagic           = 20251112;    // magic number
input int    InpDeviation       = 30;          // max slippage (points)
input string InpComment         = "trade-mcp"; // default order comment

input group             "Limits"
input int    InpMaxRatesCount   = 5000;        // hard cap on bars per request
input int    InpMaxSymbols      = 2000;        // hard cap on symbols per request

//--- globals --------------------------------------------------------
#define EA_VERSION "1.10"
#define SOCK_NONE  INVALID_HANDLE

int      g_socket        = SOCK_NONE;
string   g_rx            = "";
uint     g_lastAttempt   = 0;
long     g_requests      = 0;
long     g_errors        = 0;
datetime g_connectedAt   = 0;

CTrade   trade;

string   g_errCode       = "";
string   g_errMsg        = "";

//+------------------------------------------------------------------+
//| JSON helpers                                                     |
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

class CJson
{
public:
   string m_body;
   int    m_count;
   bool   m_isArray;

   CJson(bool isArray = false) { m_body = ""; m_count = 0; m_isArray = isArray; }

   void Raw(const string v)
   {
      if(m_count > 0) m_body += ",";
      m_body += v;
      m_count++;
   }
   void Str(const string k, const string v) { Raw("\"" + JEsc(k) + "\":\"" + JEsc(v) + "\""); }
   void Num(const string k, const double v, const int digits = 5) { Raw("\"" + JEsc(k) + "\":" + DoubleToString(v, digits)); }
   void Int(const string k, const long v) { Raw("\"" + JEsc(k) + "\":" + LongToString(v)); }
   void Bool(const string k, const bool v) { Raw("\"" + JEsc(k) + "\":" + (v ? "true" : "false")); }
   void Val(const string raw) { Raw(raw); }
   string ToString() { return (m_isArray ? "[" + m_body + "]" : "{" + m_body + "}"); }
};

string JGetStr(const string js, const string key, const string def = "")
{
   string needle = "\"" + key + "\"";
   int p = StringFind(js, needle);
   if(p < 0) return def;
   p += StringLen(needle);
   int n = StringLen(js);
   while(p < n)
   {
      ushort c = StringGetCharacter(js, p);
      if(c == ' ' || c == '\t' || c == '\r' || c == '\n' || c == ':') p++;
      else break;
   }
   if(p >= n) return def;
   ushort q = StringGetCharacter(js, p);
   if(q == '"')
   {
      p++;
      string out = "";
      while(p < n)
      {
         ushort c = StringGetCharacter(js, p);
         if(c == '\\')
         {
            p++;
            ushort e = StringGetCharacter(js, p);
            if(e == 'n')       out += "\n";
            else if(e == 'r')  out += "\r";
            else if(e == 't')  out += "\t";
            else if(e == '"')  out += "\"";
            else if(e == '\\') out += "\\";
            else               out += ShortToString(e);
         }
         else if(c == '"') break;
         else out += ShortToString(c);
         p++;
      }
      return out;
   }
   int start = p;
   while(p < n)
   {
      ushort c = StringGetCharacter(js, p);
      if(c == ',' || c == '}' || c == ']' || c == ' ' || c == '\r' || c == '\n') break;
      p++;
   }
   return StringSubstr(js, start, p - start);
}

double JGetNum(const string js, const string key, const double def = 0.0)
{
   string raw = JGetStr(js, key, "");
   if(raw == "") return def;
   return StringToDouble(raw);
}

long JGetLong(const string js, const string key, const long def = 0)
{
   string raw = JGetStr(js, key, "");
   if(raw == "") return def;
   return StringToInteger(raw);
}

bool JGetBool(const string js, const string key, const bool def = false)
{
   string raw = JGetStr(js, key, def ? "true" : "false");
   StringToLower(raw);
   return (raw == "true" || raw == "1");
}

//+------------------------------------------------------------------+
//| Errors                                                           |
//+------------------------------------------------------------------+
void SetError(const string code, const string msg)
{
   g_errCode = code;
   g_errMsg  = msg;
   g_errors++;
   if(InpVerbose) PrintFormat("[trade-mcp] error %s: %s", code, msg);
}

//+------------------------------------------------------------------+
//| Socket plumbing                                                  |
//+------------------------------------------------------------------+
bool SendRaw(const string payload)
{
   if(g_socket == SOCK_NONE || !SocketIsConnected(g_socket)) return false;
   string data = payload;
   if(StringLen(data) == 0) return true;
   uchar bytes[];
   int len = StringToCharArray(data, bytes, 0, -1, CP_UTF8);
   if(len <= 0) return false;
   len -= 1; // drop trailing null terminator
   int sent = 0;
   while(sent < len)
   {
      int chunk = (int)MathMin(8192, len - sent);
      uchar part[];
      ArrayResize(part, chunk);
      for(int i = 0; i < chunk; i++) part[i] = bytes[sent + i];
      int n = SocketSend(g_socket, part, (uint)chunk);
      if(n <= 0)
      {
         Disconnect("SocketSend failed");
         return false;
      }
      sent += n;
   }
   return true;
}

void Disconnect(const string reason)
{
   if(g_socket != SOCK_NONE)
   {
      SocketClose(g_socket);
      g_socket = SOCK_NONE;
   }
   g_rx = "";
   g_connectedAt = 0;
   if(InpVerbose) PrintFormat("[trade-mcp] disconnected (%s)", reason);
}

bool Connect()
{
   g_lastAttempt = GetTickCount();
   if(g_socket != SOCK_NONE) Disconnect("reconnect");

   int s = SocketCreate();
   if(s == INVALID_HANDLE)
   {
      PrintFormat("[trade-mcp] SocketCreate failed, err=%d", GetLastError());
      return false;
   }
   ResetLastError();
   if(!SocketConnect(s, InpHost, InpPort, InpConnectTimeout))
   {
      PrintFormat("[trade-mcp] connect to %s:%d failed, err=%d (is 'trade-mcp serve' running?)",
                  InpHost, InpPort, GetLastError());
      SocketClose(s);
      return false;
   }
   g_socket = s;
   g_rx = "";
   g_connectedAt = TimeCurrent();
   PrintFormat("[trade-mcp] connected to %s:%d", InpHost, InpPort);
   SendHello();
   return true;
}

void SendHello()
{
   CJson term;
   term.Bool("connected", (bool)TerminalInfoInteger(TERMINAL_CONNECTED));
   term.Bool("trade_allowed", (bool)TerminalInfoInteger(TERMINAL_TRADE_ALLOWED));
   term.Bool("dll_allowed", (bool)TerminalInfoInteger(TERMINAL_DLLS_ALLOWED));
   term.Int("build", (long)TerminalInfoInteger(TERMINAL_BUILD));
   term.Str("company", TerminalInfoString(TERMINAL_COMPANY));
   term.Str("name", TerminalInfoString(TERMINAL_NAME));
   term.Str("data_path", TerminalInfoString(TERMINAL_DATA_PATH));

   CJson acct;
   acct.Int("login", (long)AccountInfoInteger(ACCOUNT_LOGIN));
   acct.Str("server", AccountInfoString(ACCOUNT_SERVER));
   acct.Str("currency", AccountInfoString(ACCOUNT_CURRENCY));
   acct.Str("company", AccountInfoString(ACCOUNT_COMPANY));

   CJson root;
   root.Str("type", "hello");
   root.Str("ea", "TradeMCPConnector");
   root.Str("version", EA_VERSION);
   root.Str("chart_symbol", _Symbol);
   root.Str("chart_period", EnumToString((ENUM_TIMEFRAMES)Period()));
   root.Val("\"allow_trading\":" + (InpAllowTrading ? "true" : "false"));
   root.Val("\"terminal\":" + term.ToString());
   root.Val("\"account\":" + acct.ToString());
   SendRaw(root.ToString() + "\n");
}

void ReadAvailable()
{
   uchar buf[];
   ArrayResize(buf, 16384);
   for(int guard = 0; guard < 16; guard++)
   {
      ResetLastError();
      int n = SocketRead(g_socket, buf, 16384, 0);
      if(n <= 0) break;
      g_rx += CharArrayToString(buf, 0, n, CP_UTF8);
   }
   if(StringLen(g_rx) > 8 * 1024 * 1024)
   {
      g_rx = "";
      Disconnect("rx buffer overflow");
      return;
   }
   ProcessLines();
}

void ProcessLines()
{
   while(true)
   {
      int idx = StringFind(g_rx, "\n");
      if(idx < 0) break;
      string line = StringSubstr(g_rx, 0, idx);
      g_rx = StringSubstr(g_rx, idx + 1);
      StringTrimLeft(line);
      StringTrimRight(line);
      if(StringLen(line) > 0) HandleLine(line);
   }
}

//+------------------------------------------------------------------+
//| Request dispatch                                                 |
//+------------------------------------------------------------------+
void HandleLine(const string line)
{
   string id  = JGetStr(line, "id");
   string cmd = JGetStr(line, "cmd");
   g_requests++;

   if(InpVerbose) PrintFormat("[trade-mcp] <- %d %s", g_requests, cmd);

   g_errCode = "";
   g_errMsg  = "";
   string payload = "";

   if(cmd == "ping")               payload = CmdPing();
   else if(cmd == "terminal_info") payload = CmdTerminalInfo();
   else if(cmd == "account_info")  payload = CmdAccountInfo();
   else if(cmd == "symbols_list")  payload = CmdSymbolsList(line);
   else if(cmd == "symbol_info")   payload = CmdSymbolInfo(line);
   else if(cmd == "symbol_select") payload = CmdSymbolSelect(line);
   else if(cmd == "rates")         payload = CmdRates(line);
   else if(cmd == "tick")          payload = CmdTick(line);
   else if(cmd == "positions")     payload = CmdPositions();
   else if(cmd == "orders")        payload = CmdOrders();
   else if(cmd == "history_deals") payload = CmdHistoryDeals(line);
   else if(cmd == "calc_order")    payload = CmdCalcOrder(line);
   else if(cmd == "trade_open")    payload = CmdTradeOpen(line);
   else if(cmd == "trade_modify")  payload = CmdTradeModify(line);
   else if(cmd == "trade_close")   payload = CmdTradeClose(line);
   else if(cmd == "trade_close_all") payload = CmdTradeCloseAll(line);
   else
   {
      SetError("unknown_command", "unsupported command: " + cmd);
   }

   string resp;
   if(g_errCode == "")
   {
      if(payload == "") payload = "{}";
      resp = "{\"id\":\"" + JEsc(id) + "\",\"ok\":true,\"data\":" + payload + "}";
   }
   else
   {
      resp = "{\"id\":\"" + JEsc(id) + "\",\"ok\":false,\"error\":{\"code\":\"" +
             JEsc(g_errCode) + "\",\"message\":\"" + JEsc(g_errMsg) + "\"}}";
   }
   SendRaw(resp + "\n");
}

//+------------------------------------------------------------------+
//| Commands                                                         |
//+------------------------------------------------------------------+
ENUM_TIMEFRAMES TfFromStr(const string raw)
{
   string s = raw;
   StringToUpper(s);
   if(s == "") return PERIOD_CURRENT;
   string want = "PERIOD_" + s;
   ENUM_TIMEFRAMES all[] = {PERIOD_M1, PERIOD_M2, PERIOD_M3, PERIOD_M4, PERIOD_M5, PERIOD_M6,
                            PERIOD_M10, PERIOD_M12, PERIOD_M15, PERIOD_M20, PERIOD_M30,
                            PERIOD_H1, PERIOD_H2, PERIOD_H3, PERIOD_H4, PERIOD_H6, PERIOD_H8,
                            PERIOD_H12, PERIOD_D1, PERIOD_W1, PERIOD_MN1};
   for(int i = 0; i < ArraySize(all); i++)
      if(EnumToString(all[i]) == want) return all[i];
   return PERIOD_CURRENT;
}

string CmdPing()
{
   CJson o;
   o.Bool("pong", true);
   o.Str("server_time", TimeToString(TimeCurrent(), TIME_DATE | TIME_SECONDS));
   o.Int("epoch", (long)TimeCurrent());
   o.Str("symbol", _Symbol);
   o.Int("requests", g_requests);
   o.Int("errors", g_errors);
   o.Str("ea_version", EA_VERSION);
   return o.ToString();
}

string CmdTerminalInfo()
{
   CJson o;
   o.Bool("connected", (bool)TerminalInfoInteger(TERMINAL_CONNECTED));
   o.Bool("trade_allowed", (bool)TerminalInfoInteger(TERMINAL_TRADE_ALLOWED));
   o.Bool("dll_allowed", (bool)TerminalInfoInteger(TERMINAL_DLLS_ALLOWED));
   o.Bool("community", (bool)TerminalInfoInteger(TERMINAL_COMMUNITY_ACCOUNT));
   o.Int("build", (long)TerminalInfoInteger(TERMINAL_BUILD));
   o.Str("company", TerminalInfoString(TERMINAL_COMPANY));
   o.Str("name", TerminalInfoString(TERMINAL_NAME));
   o.Str("path", TerminalInfoString(TERMINAL_PATH));
   o.Str("data_path", TerminalInfoString(TERMINAL_DATA_PATH));
   o.Str("language", TerminalInfoString(TERMINAL_LANGUAGE));
   o.Bool("ea_trading_allowed", (bool)MQLInfoInteger(MQL_TRADE_ALLOWED));
   o.Bool("ea_allow_trading_input", InpAllowTrading);
   o.Str("ea_version", EA_VERSION);
   return o.ToString();
}

string CmdAccountInfo()
{
   CJson o;
   o.Int("login", (long)AccountInfoInteger(ACCOUNT_LOGIN));
   o.Str("server", AccountInfoString(ACCOUNT_SERVER));
   o.Str("name", AccountInfoString(ACCOUNT_NAME));
   o.Str("company", AccountInfoString(ACCOUNT_COMPANY));
   o.Str("currency", AccountInfoString(ACCOUNT_CURRENCY));
   o.Int("leverage", (long)AccountInfoInteger(ACCOUNT_LEVERAGE));
   o.Num("balance", AccountInfoDouble(ACCOUNT_BALANCE), 2);
   o.Num("equity", AccountInfoDouble(ACCOUNT_EQUITY), 2);
   o.Num("margin", AccountInfoDouble(ACCOUNT_MARGIN), 2);
   o.Num("margin_free", AccountInfoDouble(ACCOUNT_MARGIN_FREE), 2);
   o.Num("margin_level", AccountInfoDouble(ACCOUNT_MARGIN_LEVEL), 2);
   o.Num("profit", AccountInfoDouble(ACCOUNT_PROFIT), 2);
   o.Num("credit", AccountInfoDouble(ACCOUNT_CREDIT), 2);
   o.Bool("trade_allowed", (bool)AccountInfoInteger(ACCOUNT_TRADE_ALLOWED));
   o.Bool("trade_expert", (bool)AccountInfoInteger(ACCOUNT_TRADE_EXPERT));
   long mode = (long)AccountInfoInteger(ACCOUNT_TRADE_MODE);
   o.Str("trade_mode", mode == ACCOUNT_TRADE_MODE_DEMO ? "demo" :
                       (mode == ACCOUNT_TRADE_MODE_CONTEST ? "contest" : "real"));
   return o.ToString();
}

string CmdSymbolsList(const string line)
{
   string pattern = JGetStr(line, "pattern", "");
   StringToUpper(pattern);
   long limit = JGetLong(line, "limit", 500);
   if(limit <= 0 || limit > InpMaxSymbols) limit = InpMaxSymbols;

   CJson arr(true);
   int total = SymbolsTotal(false);
   int added = 0;
   for(int i = 0; i < total && added < limit; i++)
   {
      string sym = SymbolName(i, false);
      if(sym == "") continue;
      if(pattern != "")
      {
         string up = sym;
         StringToUpper(up);
         string desc = SymbolInfoString(sym, SYMBOL_DESCRIPTION);
         StringToUpper(desc);
         if(StringFind(up, pattern) < 0 && StringFind(desc, pattern) < 0) continue;
      }
      CJson o;
      o.Str("symbol", sym);
      o.Str("description", SymbolInfoString(sym, SYMBOL_DESCRIPTION));
      o.Str("path", SymbolInfoString(sym, SYMBOL_PATH));
      o.Bool("visible", (bool)SymbolInfoInteger(sym, SYMBOL_VISIBLE));
      o.Bool("selected", (bool)SymbolInfoInteger(sym, SYMBOL_SELECT));
      arr.Raw(o.ToString());
      added++;
   }
   return arr.ToString();
}

string SymbolInfoJson(const string symbol)
{
   if(!SymbolSelect(symbol, true))
   {
      SetError("unknown_symbol", "symbol not found or cannot be selected: " + symbol);
      return "";
   }
   int digits = (int)SymbolInfoInteger(symbol, SYMBOL_DIGITS);
   CJson o;
   o.Str("name", symbol);
   o.Str("description", SymbolInfoString(symbol, SYMBOL_DESCRIPTION));
   o.Str("path", SymbolInfoString(symbol, SYMBOL_PATH));
   o.Int("digits", digits);
   o.Num("point", SymbolInfoDouble(symbol, SYMBOL_POINT), 8);
   o.Num("spread", (double)SymbolInfoInteger(symbol, SYMBOL_SPREAD), 2);
   o.Num("spread_points", (double)SymbolInfoInteger(symbol, SYMBOL_SPREAD), 2);
   o.Num("spread_float", (double)SymbolInfoInteger(symbol, SYMBOL_SPREAD_FLOAT), 0);
   o.Int("trade_stops_level", (long)SymbolInfoInteger(symbol, SYMBOL_TRADE_STOPS_LEVEL));
   o.Int("trade_freeze_level", (long)SymbolInfoInteger(symbol, SYMBOL_TRADE_FREEZE_LEVEL));
   o.Num("trade_tick_value", SymbolInfoDouble(symbol, SYMBOL_TRADE_TICK_VALUE), 5);
   o.Num("trade_tick_size", SymbolInfoDouble(symbol, SYMBOL_TRADE_TICK_SIZE), 8);
   o.Num("trade_contract_size", SymbolInfoDouble(symbol, SYMBOL_TRADE_CONTRACT_SIZE), 2);
   o.Num("volume_min", SymbolInfoDouble(symbol, SYMBOL_VOLUME_MIN), 4);
   o.Num("volume_max", SymbolInfoDouble(symbol, SYMBOL_VOLUME_MAX), 4);
   o.Num("volume_step", SymbolInfoDouble(symbol, SYMBOL_VOLUME_STEP), 4);
   o.Num("swap_long", SymbolInfoDouble(symbol, SYMBOL_SWAP_LONG), 4);
   o.Num("swap_short", SymbolInfoDouble(symbol, SYMBOL_SWAP_SHORT), 4);
   o.Num("bid", SymbolInfoDouble(symbol, SYMBOL_BID), digits);
   o.Num("ask", SymbolInfoDouble(symbol, SYMBOL_ASK), digits);
   o.Str("currency_base", SymbolInfoString(symbol, SYMBOL_CURRENCY_BASE));
   o.Str("currency_profit", SymbolInfoString(symbol, SYMBOL_CURRENCY_PROFIT));
   o.Str("currency_margin", SymbolInfoString(symbol, SYMBOL_CURRENCY_MARGIN));
   o.Bool("visible", (bool)SymbolInfoInteger(symbol, SYMBOL_VISIBLE));
   long mode = (long)SymbolInfoInteger(symbol, SYMBOL_TRADE_MODE);
   o.Str("trade_mode", mode == SYMBOL_TRADE_MODE_FULL ? "full" :
                       (mode == SYMBOL_TRADE_MODE_DISABLED ? "disabled" :
                        (mode == SYMBOL_TRADE_MODE_CLOSEONLY ? "close_only" : "long_only")));
   long filling = (long)SymbolInfoInteger(symbol, SYMBOL_FILLING_MODE);
   o.Str("filling_mode", filling == SYMBOL_FILLING_FOK ? "fok" :
                         (filling == SYMBOL_FILLING_IOC ? "ioc" : "both"));
   return o.ToString();
}

string CmdSymbolInfo(const string line)
{
   string symbol = JGetStr(line, "symbol", _Symbol);
   return SymbolInfoJson(symbol);
}

string CmdSymbolSelect(const string line)
{
   string symbol = JGetStr(line, "symbol", _Symbol);
   bool enable = JGetBool(line, "enable", true);
   if(!SymbolSelect(symbol, enable))
   {
      SetError("unknown_symbol", "cannot select " + symbol);
      return "";
   }
   CJson o;
   o.Str("symbol", symbol);
   o.Bool("visible", (bool)SymbolInfoInteger(symbol, SYMBOL_VISIBLE));
   return o.ToString();
}

string CmdRates(const string line)
{
   string symbol = JGetStr(line, "symbol", _Symbol);
   string tfStr  = JGetStr(line, "timeframe", "M15");
   long count    = JGetLong(line, "count", 200);
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
               StringFormat("CopyRates(%s,%s,%d) failed, err=%d", symbol, tfStr, (int)count, GetLastError()));
      return "";
   }
   int digits = (int)SymbolInfoInteger(symbol, SYMBOL_DIGITS);

   CJson arr(true);
   for(int i = 0; i < n; i++)
   {
      CJson o;
      o.Int("time", (long)r[i].time);
      o.Num("open", r[i].open, digits);
      o.Num("high", r[i].high, digits);
      o.Num("low", r[i].low, digits);
      o.Num("close", r[i].close, digits);
      o.Num("tick_volume", (double)r[i].tick_volume, 0);
      o.Int("spread", (long)r[i].spread);
      o.Num("real_volume", (double)r[i].real_volume, 0);
      arr.Raw(o.ToString());
   }
   CJson root;
   root.Str("symbol", symbol);
   root.Str("timeframe", tfStr);
   root.Int("count", n);
   root.Val("\"rates\":" + arr.ToString());
   return root.ToString();
}

string CmdTick(const string line)
{
   string symbol = JGetStr(line, "symbol", _Symbol);
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
   CJson o;
   o.Int("time", (long)t.time);
   o.Str("time_str", TimeToString(t.time, TIME_DATE | TIME_SECONDS));
   o.Num("bid", t.bid, digits);
   o.Num("ask", t.ask, digits);
   o.Num("last", t.last, digits);
   o.Num("volume", (double)t.volume, 0);
   o.Num("spread_points", point > 0 ? (t.ask - t.bid) / point : 0, 1);
   return o.ToString();
}

string CmdPositions()
{
   int total = PositionsTotal();
   CJson arr(true);
   for(int i = 0; i < total; i++)
   {
      ulong ticket = PositionGetTicket(i);
      if(ticket == 0) continue;
      string symbol = PositionGetString(POSITION_SYMBOL);
      int digits = (int)SymbolInfoInteger(symbol, SYMBOL_DIGITS);
      double point = SymbolInfoDouble(symbol, SYMBOL_POINT);
      long type = (long)PositionGetInteger(POSITION_TYPE);
      CJson o;
      o.Int("ticket", (long)ticket);
      o.Str("symbol", symbol);
      o.Str("type", type == POSITION_TYPE_BUY ? "buy" : "sell");
      o.Num("volume", PositionGetDouble(POSITION_VOLUME), 4);
      o.Num("price_open", PositionGetDouble(POSITION_PRICE_OPEN), digits);
      o.Num("sl", PositionGetDouble(POSITION_SL), digits);
      o.Num("tp", PositionGetDouble(POSITION_TP), digits);
      o.Num("price_current", PositionGetDouble(POSITION_PRICE_CURRENT), digits);
      o.Num("profit", PositionGetDouble(POSITION_PROFIT), 2);
      o.Num("swap", PositionGetDouble(POSITION_SWAP), 2);
      o.Int("magic", (long)PositionGetInteger(POSITION_MAGIC));
      o.Str("comment", PositionGetString(POSITION_COMMENT));
      o.Int("time", (long)PositionGetInteger(POSITION_TIME));
      arr.Raw(o.ToString());
   }
   return arr.ToString();
}

string CmdOrders()
{
   int total = OrdersTotal();
   CJson arr(true);
   for(int i = 0; i < total; i++)
   {
      ulong ticket = OrderGetTicket(i);
      if(ticket == 0) continue;
      string symbol = OrderGetString(ORDER_SYMBOL);
      int digits = (int)SymbolInfoInteger(symbol, SYMBOL_DIGITS);
      long type = (long)OrderGetInteger(ORDER_TYPE);
      CJson o;
      o.Int("ticket", (long)ticket);
      o.Str("symbol", symbol);
      o.Str("type", EnumToString((ENUM_ORDER_TYPE)type));
      o.Num("volume", OrderGetDouble(ORDER_VOLUME_CURRENT), 4);
      o.Num("price_open", OrderGetDouble(ORDER_PRICE_OPEN), digits);
      o.Num("sl", OrderGetDouble(ORDER_SL), digits);
      o.Num("tp", OrderGetDouble(ORDER_TP), digits);
      o.Int("magic", (long)OrderGetInteger(ORDER_MAGIC));
      o.Int("time_setup", (long)OrderGetInteger(ORDER_TIME_SETUP));
      o.Str("comment", OrderGetString(ORDER_COMMENT));
      arr.Raw(o.ToString());
   }
   return arr.ToString();
}

string CmdHistoryDeals(const string line)
{
   long from = JGetLong(line, "date_from", 0);
   long to   = JGetLong(line, "date_to", 0);
   datetime tFrom = (datetime)(from > 0 ? from : TimeCurrent() - 30 * 86400);
   datetime tTo   = (datetime)(to > 0 ? to : TimeCurrent() + 86400);

   if(!HistorySelect(tFrom, tTo))
   {
      SetError("history_unavailable", "HistorySelect failed, err=" + IntegerToString(GetLastError()));
      return "";
   }
   int total = HistoryDealsTotal();
   CJson arr(true);
   for(int i = 0; i < total; i++)
   {
      ulong ticket = HistoryDealGetTicket(i);
      if(ticket == 0) continue;
      long entry = (long)HistoryDealGetInteger(ticket, DEAL_ENTRY);
      long type  = (long)HistoryDealGetInteger(ticket, DEAL_TYPE);
      if(type != DEAL_TYPE_BUY && type != DEAL_TYPE_SELL) continue;
      string symbol = HistoryDealGetString(ticket, DEAL_SYMBOL);
      int digits = (int)SymbolInfoInteger(symbol, SYMBOL_DIGITS);
      CJson o;
      o.Int("ticket", (long)ticket);
      o.Int("position_id", (long)HistoryDealGetInteger(ticket, DEAL_POSITION_ID));
      o.Int("order_id", (long)HistoryDealGetInteger(ticket, DEAL_ORDER));
      o.Str("symbol", symbol);
      o.Str("type", type == DEAL_TYPE_BUY ? "buy" : "sell");
      o.Str("entry", entry == DEAL_ENTRY_IN ? "in" : (entry == DEAL_ENTRY_OUT ? "out" : "inout"));
      o.Num("volume", HistoryDealGetDouble(ticket, DEAL_VOLUME), 4);
      o.Num("price", HistoryDealGetDouble(ticket, DEAL_PRICE), digits);
      o.Num("profit", HistoryDealGetDouble(ticket, DEAL_PROFIT), 2);
      o.Num("swap", HistoryDealGetDouble(ticket, DEAL_SWAP), 2);
      o.Num("commission", HistoryDealGetDouble(ticket, DEAL_COMMISSION), 2);
      o.Int("magic", (long)HistoryDealGetInteger(ticket, DEAL_MAGIC));
      o.Int("time", (long)HistoryDealGetInteger(ticket, DEAL_TIME));
      o.Str("comment", HistoryDealGetString(ticket, DEAL_COMMENT));
      arr.Raw(o.ToString());
   }
   return arr.ToString();
}

bool TradingAllowed(string &reason)
{
   if(!InpAllowTrading)
   {
      reason = "InpAllowTrading is false in the EA inputs";
      return false;
   }
   if(!(bool)MQLInfoInteger(MQL_TRADE_ALLOWED))
   {
      reason = "AutoTrading is disabled (toolbar button / session permissions)";
      return false;
   }
   if(!(bool)TerminalInfoInteger(TERMINAL_TRADE_ALLOWED))
   {
      reason = "the terminal does not allow trading";
      return false;
   }
   if(!(bool)AccountInfoInteger(ACCOUNT_TRADE_ALLOWED))
   {
      reason = "trading is not allowed for this account";
      return false;
   }
   if(!(bool)AccountInfoInteger(ACCOUNT_TRADE_EXPERT))
   {
      reason = "algo trading is disabled for this account";
      return false;
   }
   return true;
}

string CmdCalcOrder(const string line)
{
   string symbol = JGetStr(line, "symbol", _Symbol);
   string sideIn = JGetStr(line, "side", "buy");
   double volume = JGetNum(line, "volume", 0.01);
   double price  = JGetNum(line, "price", 0.0);
   if(!SymbolSelect(symbol, true))
   {
      SetError("unknown_symbol", "symbol not found: " + symbol);
      return "";
   }
   ENUM_ORDER_TYPE type = (sideIn == "sell") ? ORDER_TYPE_SELL : ORDER_TYPE_BUY;
   if(price <= 0)
      price = SymbolInfoDouble(symbol, type == ORDER_TYPE_BUY ? SYMBOL_ASK : SYMBOL_BID);

   double margin = 0.0, profit = 0.0;
   if(!OrderCalcMargin(type, symbol, volume, price, margin))
   {
      SetError("calc_failed", "OrderCalcMargin failed, err=" + IntegerToString(GetLastError()));
      return "";
   }
   CJson o;
   o.Num("margin", margin, 2);
   o.Num("price", price, (int)SymbolInfoInteger(symbol, SYMBOL_DIGITS));
   o.Num("volume", volume, 4);
   o.Num("free_margin_after", AccountInfoDouble(ACCOUNT_MARGIN_FREE) - margin, 2);
   if(price > 0 && OrderCalcProfit(type, symbol, volume, price, price, profit))
      o.Num("profit_per_price_unit", profit, 2);
   return o.ToString();
}

string TradeResultJson(ulong ticket)
{
   CJson o;
   uint rc = trade.ResultRetcode();
   o.Int("retcode", (long)rc);
   o.Str("retcode_text", trade.ResultRetcodeDescription());
   o.Int("order", (long)trade.ResultOrder());
   o.Int("deal", (long)trade.ResultDeal());
   o.Int("ticket", (long)ticket);
   o.Num("volume", trade.ResultVolume(), 4);
   o.Num("price", trade.ResultPrice(), 8);
   o.Str("comment", trade.ResultComment());
   return o.ToString();
}

string CmdTradeOpen(const string line)
{
   string reason = "";
   if(!TradingAllowed(reason))
   {
      SetError("trading_disabled", reason);
      return "";
   }
   string symbol = JGetStr(line, "symbol", _Symbol);
   string side   = JGetStr(line, "side", "buy");
   double volume = JGetNum(line, "volume", 0.01);
   double sl     = JGetNum(line, "sl", 0.0);
   double tp     = JGetNum(line, "tp", 0.0);
   string comment = JGetStr(line, "comment", InpComment);
   long magic    = JGetLong(line, "magic", InpMagic);
   long dev      = JGetLong(line, "deviation", InpDeviation);

   if(!SymbolSelect(symbol, true))
   {
      SetError("unknown_symbol", "symbol not found: " + symbol);
      return "";
   }
   double vmin = SymbolInfoDouble(symbol, SYMBOL_VOLUME_MIN);
   double vmax = SymbolInfoDouble(symbol, SYMBOL_VOLUME_MAX);
   if(volume < vmin || volume > vmax)
   {
      SetError("bad_volume",
               StringFormat("volume %.4f outside broker range %.4f-%.4f", volume, vmin, vmax));
      return "";
   }

   trade.SetExpertMagicNumber((ulong)(magic == 0 ? InpMagic : magic));
   trade.SetDeviationInPoints((ulong)(dev <= 0 ? InpDeviation : dev));
   trade.SetTypeFillingBySymbol(symbol);
   trade.SetAsyncMode(false);

   int digits = (int)SymbolInfoInteger(symbol, SYMBOL_DIGITS);
   sl = NormalizeDouble(sl, digits);
   tp = NormalizeDouble(tp, digits);

   bool ok = false;
   if(side == "buy") ok = trade.Buy(volume, symbol, 0.0, sl, tp, comment);
   else if(side == "sell") ok = trade.Sell(volume, symbol, 0.0, sl, tp, comment);
   else
   {
      SetError("bad_request", "side must be 'buy' or 'sell'");
      return "";
   }

   string payload = TradeResultJson((ulong)trade.ResultDeal());
   if(!ok && trade.ResultRetcode() != TRADE_RETCODE_DONE &&
      trade.ResultRetcode() != TRADE_RETCODE_PLACED &&
      trade.ResultRetcode() != TRADE_RETCODE_DONE_PARTIAL)
      SetError("broker_rejected", trade.ResultRetcodeDescription());
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

string CmdTradeModify(const string line)
{
   string reason = "";
   if(!TradingAllowed(reason))
   {
      SetError("trading_disabled", reason);
      return "";
   }
   long ticket = JGetLong(line, "ticket", 0);
   double sl   = JGetNum(line, "sl", 0.0);
   double tp   = JGetNum(line, "tp", 0.0);
   if(FindPositionByTicket(ticket) == 0)
   {
      SetError("not_found", "position " + LongToString(ticket) + " not found");
      return "";
   }
   string symbol = PositionGetString(POSITION_SYMBOL);
   int digits = (int)SymbolInfoInteger(symbol, SYMBOL_DIGITS);
   trade.SetTypeFillingBySymbol(symbol);
   bool ok = trade.PositionModify((ulong)ticket, NormalizeDouble(sl, digits), NormalizeDouble(tp, digits));
   string payload = TradeResultJson((ulong)ticket);
   if(!ok && trade.ResultRetcode() != TRADE_RETCODE_DONE)
      SetError("broker_rejected", trade.ResultRetcodeDescription());
   return payload;
}

string CmdTradeClose(const string line)
{
   string reason = "";
   if(!TradingAllowed(reason))
   {
      SetError("trading_disabled", reason);
      return "";
   }
   long ticket = JGetLong(line, "ticket", 0);
   double volume = JGetNum(line, "volume", 0.0);
   long dev = JGetLong(line, "deviation", InpDeviation);

   if(FindPositionByTicket(ticket) == 0)
   {
      SetError("not_found", "position " + LongToString(ticket) + " not found");
      return "";
   }
   double posVolume = PositionGetDouble(POSITION_VOLUME);
   string symbol = PositionGetString(POSITION_SYMBOL);
   trade.SetTypeFillingBySymbol(symbol);
   trade.SetDeviationInPoints((ulong)(dev <= 0 ? InpDeviation : dev));

   bool ok = false;
   if(volume > 0 && volume < posVolume - 0.0001)
      ok = trade.PositionClosePartial((ulong)ticket, NormalizeDouble(volume, 4));
   else
      ok = trade.PositionClose((ulong)ticket);

   string payload = TradeResultJson((ulong)ticket);
   if(!ok && trade.ResultRetcode() != TRADE_RETCODE_DONE)
      SetError("broker_rejected", trade.ResultRetcodeDescription());
   return payload;
}

string CmdTradeCloseAll(const string line)
{
   string reason = "";
   if(!TradingAllowed(reason))
   {
      SetError("trading_disabled", reason);
      return "";
   }
   string symbol = JGetStr(line, "symbol", "");
   bool magicOnly = JGetBool(line, "magic_only", true);
   long magic = JGetLong(line, "magic", InpMagic);

   ulong tickets[];
   int found = 0;
   int total = PositionsTotal();
   for(int i = 0; i < total; i++)
   {
      ulong t = PositionGetTicket(i);
      if(t == 0) continue;
      string s = PositionGetString(POSITION_SYMBOL);
      if(symbol != "" && s != symbol) continue;
      if(magicOnly && (long)PositionGetInteger(POSITION_MAGIC) != magic) continue;
      ArrayResize(tickets, found + 1);
      tickets[found++] = t;
   }
   CJson arr(true);
   int closed = 0;
   for(int i = 0; i < found; i++)
   {
      string s = "";
      if(PositionSelectByTicket(tickets[i]))
      {
         s = PositionGetString(POSITION_SYMBOL);
         trade.SetTypeFillingBySymbol(s);
      }
      bool ok = trade.PositionClose(tickets[i]);
      CJson o;
      o.Int("ticket", (long)tickets[i]);
      o.Bool("ok", ok);
      o.Int("retcode", (long)trade.ResultRetcode());
      o.Str("retcode_text", trade.ResultRetcodeDescription());
      arr.Raw(o.ToString());
      if(ok) closed++;
   }
   CJson root;
   root.Int("found", found);
   root.Int("closed", closed);
   root.Val("\"results\":" + arr.ToString());
   return root.ToString();
}

//+------------------------------------------------------------------+
//| Lifecycle                                                        |
//+------------------------------------------------------------------+
int OnInit()
{
   trade.SetExpertMagicNumber(InpMagic);
   trade.SetDeviationInPoints(InpDeviation);
   trade.SetTypeFillingBySymbol(_Symbol);

   if(InpTimerMs < 20) Print("[trade-mcp] warning: InpTimerMs is very small, CPU load may rise");
   EventSetMillisecondTimer((int)MathMax(InpTimerMs, 20));

   PrintFormat("[trade-mcp] %s starting | target=%s:%d | trading=%s | chart=%s %s",
               EA_VERSION, InpHost, InpPort, InpAllowTrading ? "ENABLED" : "disabled",
               _Symbol, EnumToString((ENUM_TIMEFRAMES)Period()));

   Connect(); // blocking up to InpConnectTimeout; the timer retries if it fails
   return INIT_SUCCEEDED;
}

void OnDeinit(const int reason)
{
   EventKillTimer();
   if(g_socket != SOCK_NONE)
   {
      SendRaw("{\"type\":\"bye\",\"reason\":\"ea_stopped\"}\n");
      SocketClose(g_socket);
      g_socket = SOCK_NONE;
   }
   PrintFormat("[trade-mcp] stopped (reason=%d, requests=%d, errors=%d)", reason, g_requests, g_errors);
}

void OnTimer()
{
   if(g_socket == SOCK_NONE)
   {
      uint now = GetTickCount();
      if(now - g_lastAttempt >= (uint)MathMax(1, InpRetrySeconds) * 1000)
         Connect();
      return;
   }
   if(!SocketIsConnected(g_socket))
   {
      Disconnect("peer closed");
      return;
   }
   ReadAvailable();
}

void OnTradeTransaction(const MqlTradeTransaction &trans,
                        const MqlTradeRequest &request,
                        const MqlTradeResult &result)
{
   // Handy breadcrumb in the Experts log; no bridge traffic here.
   if(trans.type == TRADE_TRANSACTION_DEAL_ADD && InpVerbose)
      PrintFormat("[trade-mcp] deal added: %s %s vol=%.2f",
                  trans.symbol, EnumToString(trans.deal_type), trans.volume);
}
//+------------------------------------------------------------------+
