//+------------------------------------------------------------------+
//| WriteTest.mq5 - only the WRITE half of the file bridge            |
//|                                                                  |
//| FolderCreate + FileOpen + StringToCharArray + FileWriteArray.      |
//| If Bare.mq5 compiles but this one crashes, the write path is the   |
//| trigger and we can replace it with FileWriteString.                |
//+------------------------------------------------------------------+
#property copyright "trade-mcp"
#property version   "1.00"

input string InpDir = "trade-mcp";

int OnInit()
{
   FolderCreate(InpDir);
   string text = "{\"ok\":true,\"probe\":\"write\"}";
   int h = FileOpen(InpDir + "\\diag-write.txt", FILE_WRITE | FILE_BIN);
   if(h == INVALID_HANDLE)
   {
      PrintFormat("WriteTest: FileOpen failed, err=%d", GetLastError());
      return INIT_FAILED;
   }
   uchar buf[];
   int n = StringToCharArray(text, buf, 0, WHOLE_ARRAY, CP_UTF8);
   if(n > 0) n = n - 1;
   if(n > 0) FileWriteArray(h, buf, 0, n);
   FileClose(h);
   Print("WriteTest: wrote MQL5\\Files\\" + InpDir + "\\diag-write.txt");
   return INIT_SUCCEEDED;
}

void OnDeinit(const int reason)
{
}
//+------------------------------------------------------------------+
