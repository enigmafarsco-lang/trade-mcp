//+------------------------------------------------------------------+
//|                                             TradeMCPProbe.mq5    |
//|                     https://github.com/enigmafarsco-lang/trade-mcp
//|                                                                  |
//|  Smallest possible file bridge: it only proves that the          |
//|  MQL5\Files\trade-mcp handshake works.                           |
//|                                                                  |
//|  Use it when TradeMCPFile.mq5 refuses to compile: if this one    |
//|  compiles and doctor turns green, the plumbing is fine and the   |
//|  problem is somewhere in the command/trading code.               |
//|                                                                  |
//|  It answers every request with a fixed payload, so attach it on  |
//|  its own chart - do not run it together with TradeMCPFile.mq5.   |
//+------------------------------------------------------------------+
#property copyright "trade-mcp"
#property link      "https://github.com/enigmafarsco-lang/trade-mcp"
#property version   "1.00"
#property description "Minimal MQL5\\Files handshake probe for trade-mcp"

input string InpDir      = "trade-mcp";  // folder inside MQL5\Files
input int    InpPollMs   = 250;          // inbox scan interval (ms)
input int    InpStatusMs = 1000;         // status.json refresh (ms)

long g_requests   = 0;
uint g_lastStatus = 0;

string InFolder()
{
   return InpDir + "\\in";
}

string OutFolder()
{
   return InpDir + "\\out";
}

string ReadTextFile(const string name)
{
   int h = FileOpen(name, FILE_READ | FILE_BIN);
   if(h == INVALID_HANDLE) return "";
   ulong size = FileSize(h);
   if(size == 0 || size > 1048576)
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
   uchar buf[];
   int n = StringToCharArray(text, buf, 0, WHOLE_ARRAY, CP_UTF8);
   if(n > 0) n = n - 1;
   bool ok = true;
   if(n > 0) ok = ((int)FileWriteArray(h, buf, 0, n) == n);
   FileClose(h);
   return ok;
}

// We only need the "id" field: copy the value that follows "id":
string ExtractId(const string body)
{
   int p = StringFind(body, "\"id\"");
   if(p < 0) return "unknown";
   p = p + 4;
   int n = StringLen(body);
   while(p < n)
   {
      ushort c = StringGetCharacter(body, p);
      if(c == ' ' || c == ':' || c == '\t') p++;
      else break;
   }
   if(p >= n) return "unknown";
   if(StringGetCharacter(body, p) != '"') return "unknown";
   p++;
   string out = "";
   while(p < n)
   {
      ushort c = StringGetCharacter(body, p);
      if(c == '"') break;
      out += ShortToString(c);
      p++;
   }
   return out;
}

void WriteStatus(const bool alive)
{
   string text = "{\"alive\":" + (alive ? "true" : "false");
   text += ",\"ea\":\"TradeMCPProbe\"";
   text += ",\"version\":\"1.00\"";
   text += ",\"transport\":\"file\"";
   text += ",\"updated_at\":" + (string)(long)TimeGMT();
   text += ",\"updated_local\":" + (string)(long)TimeLocal();
   text += ",\"symbol\":\"" + _Symbol + "\"";
   text += ",\"login\":" + (string)(long)AccountInfoInteger(ACCOUNT_LOGIN);
   text += ",\"server\":\"" + AccountInfoString(ACCOUNT_SERVER) + "\"";
   text += ",\"build\":" + (string)(long)TerminalInfoInteger(TERMINAL_BUILD);
   text += ",\"requests\":" + (string)g_requests;
   text += "}";
   WriteTextFile(InpDir + "\\status.json", text);
}

void ProcessInbox()
{
   string name = "";
   long handle = FileFindFirst(InFolder() + "\\*.json", name);
   if(handle == INVALID_HANDLE) return;

   string names[];
   int count = 0;
   while(true)
   {
      if(StringLen(name) > 0)
      {
         ArrayResize(names, count + 1);
         names[count] = name;
         count++;
         if(count >= 16) break;
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
         FileDelete(reqName);
         continue;
      }
      g_requests++;
      string id = ExtractId(body);
      string resp = "{\"id\":\"" + id + "\",\"ok\":true,\"data\":{\"pong\":true,"
                    "\"probe\":true,\"ea\":\"TradeMCPProbe\",\"ea_version\":\"1.00\","
                    "\"symbol\":\"" + _Symbol + "\",\"requests\":" + (string)g_requests + "}}";
      PrintFormat("[trade-mcp] probe request %s", names[i]);
      WriteTextFile(OutFolder() + "\\" + names[i], resp);
      FileDelete(reqName);
   }
}

int OnInit()
{
   FolderCreate(InpDir);
   FolderCreate(InpDir + "\\in");
   FolderCreate(InpDir + "\\out");
   g_lastStatus = GetTickCount();
   WriteStatus(true);
   int poll = InpPollMs;
   if(poll < 20) poll = 20;
   EventSetMillisecondTimer(poll);
   PrintFormat("[trade-mcp] probe started | folder=MQL5\\Files\\%s", InpDir);
   return INIT_SUCCEEDED;
}

void OnDeinit(const int reason)
{
   EventKillTimer();
   WriteStatus(false);
   PrintFormat("[trade-mcp] probe stopped (reason=%d, requests=%d)", reason, g_requests);
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
