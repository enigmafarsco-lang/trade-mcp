//+------------------------------------------------------------------+
//| Bare.mq5 - compiler sanity check                                 |
//|                                                                  |
//| Nothing but the three lifecycle handlers: no file API, no timer,  |
//| no inputs, no strings.  If THIS does not compile, MetaEditor is    |
//| broken and no source file in the world will help.                  |
//+------------------------------------------------------------------+
#property copyright "trade-mcp"
#property version   "1.00"

int OnInit()
{
   return INIT_SUCCEEDED;
}

void OnTick()
{
}

void OnDeinit(const int reason)
{
}
//+------------------------------------------------------------------+
