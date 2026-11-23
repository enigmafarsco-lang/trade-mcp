// Read-only timer EA. Does not require permission to trade.
#property strict
#include "TradeMCPCostExport.mqh"
input datetime From = D'2026.01.01 00:00:00';
input int MaxDeals = 10000;
input int IntervalSeconds = 5;

int OnInit()
{
   if(IntervalSeconds < 2 || IntervalSeconds > 20 ||
      MaxDeals < 1 || MaxDeals > 100000 || From <= 0)
      return INIT_PARAMETERS_INCORRECT;
   // A single writer per terminal is required. Do not also run the script.
   if(!EventSetTimer(IntervalSeconds)) return INIT_FAILED;
   OnTimer();
   return INIT_SUCCEEDED;
}
void OnTimer()
{
   if(!ExportCostSnapshot(From, MaxDeals))
      Print("Cost snapshot publication failed; previous output is not refreshed");
}
void OnDeinit(const int reason)
{
   EventKillTimer();
}
