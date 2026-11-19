// Read-only, one-shot cost snapshot. No trading or network API is used.
#property strict
#property script_show_inputs
input datetime From = D'2026.01.01 00:00:00';
input int MaxDeals = 10000;

bool Money(ulong ticket, ENUM_DEAL_PROPERTY_DOUBLE field, double &value)
{
   ResetLastError();
   return HistoryDealGetDouble(ticket, field, value) && MathIsValidNumber(value);
}
void OnStart()
{
   datetime until = TimeTradeServer();
   if(until <= 0 || From <= 0 || From >= until || MaxDeals < 1 || MaxDeals > 100000)
      return;
   if(!HistorySelect(From, until)) return;
   int count = HistoryDealsTotal();
   if(count > MaxDeals) return; // Never silently truncate history.
   string tmp = "trade-mcp-costs.tmp";
   int f = FileOpen(tmp, FILE_WRITE|FILE_CSV|FILE_ANSI, '\t', CP_UTF8);
   if(f == INVALID_HANDLE) return;
   FileWrite(f, "meta", "1", (string)AccountInfoInteger(ACCOUNT_LOGIN),
             AccountInfoString(ACCOUNT_SERVER), AccountInfoString(ACCOUNT_CURRENCY),
             (string)(long)TimeGMT(), (string)(long)From, (string)(long)until,
             (string)count);
   bool ok = true;
   for(int i=0; i<count; i++)
   {
      ulong ticket = HistoryDealGetTicket(i);
      double profit, swap, commission, fee;
      if(ticket == 0 || !Money(ticket, DEAL_PROFIT, profit) ||
         !Money(ticket, DEAL_SWAP, swap) || !Money(ticket, DEAL_COMMISSION, commission) ||
         !Money(ticket, DEAL_FEE, fee)) { ok=false; break; }
      long position, entry, type, timestamp;
      if(!HistoryDealGetInteger(ticket, DEAL_POSITION_ID, position) ||
         !HistoryDealGetInteger(ticket, DEAL_ENTRY, entry) ||
         !HistoryDealGetInteger(ticket, DEAL_TYPE, type) ||
         !HistoryDealGetInteger(ticket, DEAL_TIME, timestamp)) { ok=false; break; }
      FileWrite(f, "deal", (string)ticket, (string)position, (string)timestamp,
                (string)entry, (string)type, DoubleToString(profit, 8),
                DoubleToString(swap, 8), DoubleToString(commission, 8),
                DoubleToString(fee, 8));
   }
   FileFlush(f);
   FileClose(f);
   if(!ok) { FileDelete(tmp); return; }
   // Keep the previous snapshot if publication fails. Reader checks age/account.
   FileMove(tmp, 0, "trade-mcp-costs.tsv", FILE_REWRITE);
}
