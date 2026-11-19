// Shared read-only exporter. No trade or network functions.
bool Money(ulong ticket, ENUM_DEAL_PROPERTY_DOUBLE field, double &value)
{
   ResetLastError();
   return HistoryDealGetDouble(ticket, field, value) && MathIsValidNumber(value);
}
string CostCommentHex(string text)
{
   uchar data[];
   int size = StringToCharArray(text, data, 0, WHOLE_ARRAY, CP_UTF8);
   string hex = "";
   for(int i=0; i<size-1; i++) hex += StringFormat("%02X", (int)data[i]);
   return hex;
}
bool PositionMoney(ENUM_POSITION_PROPERTY_DOUBLE field, double &value)
{
   ResetLastError();
   return PositionGetDouble(field, value) && MathIsValidNumber(value);
}
bool ExportCostSnapshot(datetime history_from, int max_deals)
{
   if(!TerminalInfoInteger(TERMINAL_CONNECTED)) return false;
   long login = AccountInfoInteger(ACCOUNT_LOGIN);
   string server = AccountInfoString(ACCOUNT_SERVER);
   string currency = AccountInfoString(ACCOUNT_CURRENCY);
   datetime generated = TimeGMT();
   datetime until = TimeTradeServer();
   if(until <= 0 || history_from <= 0 || history_from >= until || max_deals < 1 || max_deals > 100000)
      return false;
   if(!HistorySelect(history_from, until)) return false;
   int count = HistoryDealsTotal();
   if(count > max_deals) return false; // Never silently truncate history.
   int position_count = PositionsTotal();
   if(position_count > 10000) return false;
   string tmp = "trade-mcp-costs.tmp";
   int f = FileOpen(tmp, FILE_WRITE|FILE_CSV|FILE_ANSI, '\t', CP_UTF8);
   if(f == INVALID_HANDLE) return false;
   ResetLastError();
   uint header_bytes = FileWrite(f, "meta", "2", (string)login,
             server, currency,
             (string)(long)generated, (string)(long)history_from, (string)(long)until,
             (string)count, (string)position_count);
   bool ok = (header_bytes > 0 && GetLastError() == 0);
   for(int i=0; i<count && ok; i++)
   {
      ulong ticket = HistoryDealGetTicket(i);
      double profit, swap, commission, fee, volume;
      if(ticket == 0 || !Money(ticket, DEAL_PROFIT, profit) ||
         !Money(ticket, DEAL_SWAP, swap) || !Money(ticket, DEAL_COMMISSION, commission) ||
         !Money(ticket, DEAL_FEE, fee) || !Money(ticket, DEAL_VOLUME, volume)) { ok=false; break; }
      long position, entry, type, timestamp;
      if(!HistoryDealGetInteger(ticket, DEAL_POSITION_ID, position) ||
         !HistoryDealGetInteger(ticket, DEAL_ENTRY, entry) ||
         !HistoryDealGetInteger(ticket, DEAL_TYPE, type) ||
         !HistoryDealGetInteger(ticket, DEAL_TIME, timestamp)) { ok=false; break; }
      string comment;
      if(!HistoryDealGetString(ticket, DEAL_COMMENT, comment)) { ok=false; break; }
      uint bytes = FileWrite(f, "deal", (string)ticket, (string)position, (string)timestamp,
                (string)entry, (string)type, DoubleToString(profit, 8),
                DoubleToString(swap, 8), DoubleToString(commission, 8),
                DoubleToString(fee, 8), DoubleToString(volume, 8), CostCommentHex(comment));
      if(bytes == 0 || GetLastError() != 0) ok = false;
   }
   for(int i=0; i<position_count && ok; i++)
   {
      ulong ticket = PositionGetTicket(i);
      long identifier, type, opened;
      double volume, profit, swap, sl, tp;
      string comment;
      if(ticket == 0 || !PositionGetInteger(POSITION_IDENTIFIER, identifier) ||
         !PositionGetInteger(POSITION_TYPE, type) ||
         !PositionGetInteger(POSITION_TIME, opened) ||
         !PositionGetString(POSITION_COMMENT, comment) ||
         !PositionMoney(POSITION_VOLUME, volume) ||
         !PositionMoney(POSITION_PROFIT, profit) || !PositionMoney(POSITION_SWAP, swap) ||
         !PositionMoney(POSITION_SL, sl) || !PositionMoney(POSITION_TP, tp))
      { ok=false; break; }
      uint bytes = FileWrite(f, "position", (string)ticket, (string)identifier,
                   (string)type, (string)opened, DoubleToString(volume, 8),
                   DoubleToString(profit, 8), DoubleToString(swap, 8),
                   DoubleToString(sl, 8), DoubleToString(tp, 8), CostCommentHex(comment));
      if(bytes == 0 || GetLastError() != 0) ok = false;
   }
   if(GetLastError() != 0) ok = false;
   if(!TerminalInfoInteger(TERMINAL_CONNECTED) ||
      AccountInfoInteger(ACCOUNT_LOGIN) != login ||
      AccountInfoString(ACCOUNT_SERVER) != server ||
      AccountInfoString(ACCOUNT_CURRENCY) != currency ||
      HistoryDealsTotal() != count || PositionsTotal() != position_count || TimeGMT() - generated > 25)
      ok = false;
   FileFlush(f);
   if(GetLastError() != 0) ok = false;
   FileClose(f);
   if(!ok) { FileDelete(tmp); return false; }
   // Keep the previous snapshot if publication fails. Reader checks age/account.
   return FileMove(tmp, 0, "trade-mcp-costs.tsv", FILE_REWRITE);
}
