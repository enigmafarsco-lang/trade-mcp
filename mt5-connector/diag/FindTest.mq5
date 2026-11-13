//+------------------------------------------------------------------+
//| FindTest.mq5 - only the FIND + READ half of the file bridge       |
//|                                                                  |
//| FileFindFirst/FileFindNext + FileReadArray + CharArrayToString.    |
//| If WriteTest.mq5 compiles but this one crashes, the inbox scan is  |
//| the trigger and we switch to a fixed request file name.            |
//+------------------------------------------------------------------+
#property copyright "trade-mcp"
#property version   "1.00"

input string InpDir = "trade-mcp";

int OnInit()
{
   string name = "";
   long handle = FileFindFirst(InpDir + "\\*.json", name);
   if(handle == INVALID_HANDLE)
   {
      Print("FindTest: no *.json found, FileFindFirst returned INVALID_HANDLE");
      return INIT_SUCCEEDED;
   }
   string names[];
   int count = 0;
   while(true)
   {
      ArrayResize(names, count + 1);
      names[count] = name;
      count++;
      if(count >= 16) break;
      if(!FileFindNext(handle, name)) break;
   }
   FileFindClose(handle);

   for(int i = 0; i < count; i++)
   {
      int h = FileOpen(InpDir + "\\" + names[i], FILE_READ | FILE_BIN);
      if(h == INVALID_HANDLE) continue;
      ulong size = FileSize(h);
      if(size == 0 || size > 1048576)
      {
         FileClose(h);
         continue;
      }
      uchar buf[];
      ArrayResize(buf, (int)size);
      int got = (int)FileReadArray(h, buf);
      FileClose(h);
      if(got > 0)
      {
         string text = CharArrayToString(buf, 0, got, CP_UTF8);
         PrintFormat("FindTest: read %s (%d bytes) -> %s", names[i], got, text);
      }
   }
   return INIT_SUCCEEDED;
}

void OnDeinit(const int reason)
{
}
//+------------------------------------------------------------------+
