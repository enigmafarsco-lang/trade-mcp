// One-shot read-only cost export.
#property strict
#property script_show_inputs
#include "TradeMCPCostExport.mqh"
input datetime From = D'2026.01.01 00:00:00';
input int MaxDeals = 10000;
void OnStart()
{
   if(!ExportCostSnapshot(From, MaxDeals)) Print("Cost snapshot publication failed");
}
