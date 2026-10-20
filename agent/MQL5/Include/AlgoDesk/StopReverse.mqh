//+------------------------------------------------------------------+
//| StopReverse.mqh                                                  |
//| XAU StopReverse (scalping M1, trailing stop-and-reverse) running |
//| inside AlgoDeskAgent, switched on/off and tuned from the         |
//| dashboard instead of EA inputs.                                  |
//|                                                                  |
//|  - Always 1 position (BUY/SELL), no SL, no TP.                   |
//|  - Always 1 opposite stop order: BUY guarded by SELL STOP        |
//|    (Bid - D), SELL guarded by BUY STOP (Ask + D).                |
//|  - The stop order only moves in the profit direction, by >= S.   |
//|  - Stop order hit -> old position closed, new one runs with a    |
//|    new stop order. First entry follows the last M1 candle colour.|
//|                                                                  |
//| Dashboard OFF, daily loss limit, pause, or the magic disabled:   |
//| no new entries and no reversal. The running position is not cut; |
//| it exits at its trailing level (netting: exit-only stop order,   |
//| hedging: trailing SL on the position).                           |
//|                                                                  |
//| Settings (on, symbol, lot, D, S) are stored per account login in |
//| MQL5\Files\AlgoDesk\sr_<login>.txt so they survive restarts.     |
//| Removing the agent leaves the stop order in place on purpose.    |
//+------------------------------------------------------------------+
#ifndef ALGODESK_STOPREVERSE_MQH
#define ALGODESK_STOPREVERSE_MQH

#include <Trade/Trade.mqh>
#include <AlgoDesk/AlgoDeskGuard.mqh>

struct SrSettings
  {
   bool              on;
   string            sym;
   double            lots;
   double            dist;
   double            step;
  };

CTrade     g_srTrade;
SrSettings g_sr;
long       g_srLogin    = -1;
ulong      g_srMagic    = 777001;
int        g_srSlippage = 30;
bool       g_srCsv      = true;
bool       g_srHedging  = true;
datetime   g_srNextTry  = 0;   // pause after an error
datetime   g_srLastExit = 0;   // time of an exit without reversal
int        g_srPrevN    = 0;
string     g_srStatus   = "";

//+------------------------------------------------------------------+
//| Settings                                                         |
//+------------------------------------------------------------------+
string SR_File() { return StringFormat("AlgoDesk\\sr_%I64d.txt", AccountInfoInteger(ACCOUNT_LOGIN)); }

// The chart symbol when it is gold, else the first gold symbol in Market Watch, else the chart symbol.
string SR_DefaultSymbol()
  {
   if(StringFind(_Symbol, "XAU") == 0)
      return _Symbol;
   for(int i = 0; i < SymbolsTotal(true); i++)
      if(StringFind(SymbolName(i, true), "XAUUSD") == 0)
         return SymbolName(i, true);
   return _Symbol;
  }

void SR_Save()
  {
   int h = FileOpen(SR_File(), FILE_WRITE | FILE_TXT | FILE_ANSI);
   if(h == INVALID_HANDLE)
     {
      PrintFormat("StopReverse: pengaturan tidak bisa disimpan (%d)", GetLastError());
      return;
     }
   FileWriteString(h, StringFormat("on=%d\nsymbol=%s\nlots=%.2f\ndist=%.5f\nstep=%.5f\n",
                                   g_sr.on ? 1 : 0, g_sr.sym, g_sr.lots, g_sr.dist, g_sr.step));
   FileClose(h);
  }

void SR_Load()
  {
   g_sr.on   = false;   // a new account starts switched off
   g_sr.sym  = SR_DefaultSymbol();
   g_sr.lots = 0.01;
   g_sr.dist = 0.60;
   g_sr.step = 0.05;
   g_srLogin = AccountInfoInteger(ACCOUNT_LOGIN);
   int h = FileOpen(SR_File(), FILE_READ | FILE_TXT | FILE_ANSI);
   if(h == INVALID_HANDLE)
      return;
   while(!FileIsEnding(h))
     {
      string line = FileReadString(h);
      int eq = StringFind(line, "=");
      if(eq <= 0)
         continue;
      string k = StringSubstr(line, 0, eq), v = StringSubstr(line, eq + 1);
      if(k == "on")          g_sr.on   = (StringToInteger(v) == 1);
      else if(k == "symbol") g_sr.sym  = v;
      else if(k == "lots")   g_sr.lots = StringToDouble(v);
      else if(k == "dist")   g_sr.dist = StringToDouble(v);
      else if(k == "step")   g_sr.step = StringToDouble(v);
     }
   FileClose(h);
  }

void SR_Init(const ulong magic, const int slippage, const bool csv)
  {
   g_srMagic = magic;
   g_srSlippage = slippage;
   g_srCsv = csv;
   g_srTrade.SetExpertMagicNumber(magic);
   g_srTrade.SetDeviationInPoints(slippage);
   g_srTrade.SetAsyncMode(false);
   SR_Load();
  }

//+------------------------------------------------------------------+
//| Broker helpers                                                   |
//+------------------------------------------------------------------+
int    SR_Digits() { return (int)SymbolInfoInteger(g_sr.sym, SYMBOL_DIGITS); }
double SR_Point()  { return SymbolInfoDouble(g_sr.sym, SYMBOL_POINT); }

double SR_Px(double p)
  {
   double ts = SymbolInfoDouble(g_sr.sym, SYMBOL_TRADE_TICK_SIZE);
   if(ts > 0)
      p = MathRound(p / ts) * ts;
   return NormalizeDouble(p, SR_Digits());
  }

double SR_Lot(double v)
  {
   double st = SymbolInfoDouble(g_sr.sym, SYMBOL_VOLUME_STEP);
   double mn = SymbolInfoDouble(g_sr.sym, SYMBOL_VOLUME_MIN);
   double mx = SymbolInfoDouble(g_sr.sym, SYMBOL_VOLUME_MAX);
   if(st > 0)
      v = MathFloor(v / st + 0.5) * st;
   return NormalizeDouble(MathMax(mn, MathMin(mx, v)), 2);
  }

bool SR_TradingOpen(string &why)
  {
   if(!TerminalInfoInteger(TERMINAL_CONNECTED))
     { why = "Tidak terhubung ke server"; return false; }
   if(!TerminalInfoInteger(TERMINAL_TRADE_ALLOWED))
     { why = "Tombol Algo Trading MATI"; return false; }
   if(!MQLInfoInteger(MQL_TRADE_ALLOWED))
     { why = "Allow Algo Trading belum dicentang"; return false; }
   if(SymbolInfoInteger(g_sr.sym, SYMBOL_TRADE_MODE) != SYMBOL_TRADE_MODE_FULL)
     { why = "Simbol tidak bisa ditradingkan penuh saat ini"; return false; }
   return true;
  }

//+------------------------------------------------------------------+
//| Log                                                              |
//+------------------------------------------------------------------+
void SR_Log(string ev, string dir, double price, double vol, double profit, string info)
  {
   PrintFormat("StopReverse [%s] %s %s @ %s vol %.2f profit %.2f %s", ev, dir, g_sr.sym,
               DoubleToString(price, SR_Digits()), vol, profit, info);
   if(!g_srCsv)
      return;
   string f = StringFormat("AlgoDesk\\XSR_%s_%I64u.csv", g_sr.sym, g_srMagic);
   int h = FileOpen(f, FILE_READ | FILE_WRITE | FILE_CSV | FILE_ANSI | FILE_SHARE_READ, ';');
   if(h == INVALID_HANDLE)
      return;
   if(FileSize(h) == 0)
      FileWrite(h, "waktu", "event", "arah", "harga", "volume", "profit", "info");
   FileSeek(h, 0, SEEK_END);
   FileWrite(h, TimeToString(TimeCurrent(), TIME_DATE | TIME_SECONDS), ev, dir,
             DoubleToString(price, SR_Digits()), DoubleToString(vol, 2), DoubleToString(profit, 2), info);
   FileClose(h);
  }

bool SR_ResultOK()
  {
   uint rc = g_srTrade.ResultRetcode();
   return (rc == TRADE_RETCODE_DONE || rc == TRADE_RETCODE_PLACED || rc == TRADE_RETCODE_DONE_PARTIAL);
  }

void SR_Fail(string what)
  {
   uint rc = g_srTrade.ResultRetcode();
   SR_Log("ERROR", what, 0, 0, 0, StringFormat("%u %s", rc, g_srTrade.ResultRetcodeDescription()));
   int wait = 2;
   if(rc == TRADE_RETCODE_MARKET_CLOSED || rc == TRADE_RETCODE_TRADE_DISABLED)
      wait = 60;
   else if(rc == TRADE_RETCODE_NO_MONEY)
      wait = 300;
   g_srNextTry = TimeCurrent() + wait;
  }

//+------------------------------------------------------------------+
//| Decisions                                                        |
//+------------------------------------------------------------------+
// Why new exposure is not allowed right now, or "" when it is.
string SR_EntryBlock()
  {
   if(!g_sr.on)
      return "dimatikan dari dashboard";
   ENUM_AD_ENTRY r = AlgoDesk_Check((long)g_srMagic);
   if(r != AD_ENTRY_OK)
      return AlgoDesk_ReasonText(r);
   return "";
  }

// First entry: colour of the last closed M1 candle. 1 = BUY, -1 = SELL.
int SR_EntryDirection()
  {
   return (iClose(g_sr.sym, PERIOD_M1, 1) >= iOpen(g_sr.sym, PERIOD_M1, 1)) ? 1 : -1;
  }

//+------------------------------------------------------------------+
//| Trade events (log)                                               |
//+------------------------------------------------------------------+
void SR_OnDeal(const ulong deal)
  {
   if(!HistoryDealSelect(deal))
      return;
   if(HistoryDealGetString(deal, DEAL_SYMBOL) != g_sr.sym || (ulong)HistoryDealGetInteger(deal, DEAL_MAGIC) != g_srMagic)
      return;
   long   entry = HistoryDealGetInteger(deal, DEAL_ENTRY);
   long   type  = HistoryDealGetInteger(deal, DEAL_TYPE);
   string dir   = (type == DEAL_TYPE_BUY) ? "BUY" : ((type == DEAL_TYPE_SELL) ? "SELL" : "-");
   double pnl   = HistoryDealGetDouble(deal, DEAL_PROFIT) + HistoryDealGetDouble(deal, DEAL_SWAP) +
                  HistoryDealGetDouble(deal, DEAL_COMMISSION);
   if(entry == DEAL_ENTRY_IN)
      SR_Log("MASUK", dir, HistoryDealGetDouble(deal, DEAL_PRICE), HistoryDealGetDouble(deal, DEAL_VOLUME), 0,
             HistoryDealGetString(deal, DEAL_COMMENT));
   else if(entry == DEAL_ENTRY_OUT || entry == DEAL_ENTRY_INOUT)
      SR_Log("KELUAR", dir, HistoryDealGetDouble(deal, DEAL_PRICE), HistoryDealGetDouble(deal, DEAL_VOLUME), pnl, "");
  }

//+------------------------------------------------------------------+
//| Own positions and orders                                         |
//+------------------------------------------------------------------+
int SR_Positions(ulong &tk[], long &tm[])
  {
   int n = 0;
   ArrayResize(tk, 0);
   ArrayResize(tm, 0);
   for(int i = PositionsTotal() - 1; i >= 0; i--)
     {
      ulong t = PositionGetTicket(i);
      if(t == 0 || PositionGetString(POSITION_SYMBOL) != g_sr.sym || (ulong)PositionGetInteger(POSITION_MAGIC) != g_srMagic)
         continue;
      ArrayResize(tk, n + 1);
      ArrayResize(tm, n + 1);
      tk[n] = t;
      tm[n] = PositionGetInteger(POSITION_TIME_MSC);
      n++;
     }
   return n;
  }

int SR_OrderCount()
  {
   int n = 0;
   for(int i = OrdersTotal() - 1; i >= 0; i--)
     {
      ulong t = OrderGetTicket(i);
      if(t != 0 && OrderGetString(ORDER_SYMBOL) == g_sr.sym && (ulong)OrderGetInteger(ORDER_MAGIC) == g_srMagic)
         n++;
     }
   return n;
  }

// Closes every position and deletes every order of the strategy; false if something is left.
bool SR_CloseAll()
  {
   bool ok = true;
   for(int i = OrdersTotal() - 1; i >= 0; i--)
     {
      ulong t = OrderGetTicket(i);
      if(t != 0 && OrderGetString(ORDER_SYMBOL) == g_sr.sym && (ulong)OrderGetInteger(ORDER_MAGIC) == g_srMagic)
         if(!g_srTrade.OrderDelete(t) || !SR_ResultOK())
            ok = false;
     }
   ulong tk[];
   long  tm[];
   int n = SR_Positions(tk, tm);
   for(int k = 0; k < n; k++)
      if(!g_srTrade.PositionClose(tk[k]) || !SR_ResultOK())
         ok = false;
   return ok;
  }

//+------------------------------------------------------------------+
//| Dashboard commands                                               |
//+------------------------------------------------------------------+
// sr_set: any of enabled / symbol / lots / dist / step. Values arrive already validated by the
// backend; they are checked again here because the agent is the last line before the broker.
bool SR_Set(string enabled, string sym, string lots, string dist, string step, string &msg)
  {
   // a JSON null means "leave as is", like a missing field
   if(enabled == "null") enabled = "";
   if(sym == "null")     sym = "";
   if(lots == "null")    lots = "";
   if(dist == "null")    dist = "";
   if(step == "null")    step = "";
   bool   on = g_sr.on;
   string sy = g_sr.sym;
   double lo = g_sr.lots, di = g_sr.dist, stp = g_sr.step;
   if(enabled != "")
      on = (enabled == "true" || enabled == "1");
   if(sym != "" && sym != g_sr.sym)
     {
      if(!SymbolSelect(sym, true) || SymbolInfoDouble(sym, SYMBOL_POINT) <= 0)
        { msg = "simbol " + sym + " tidak ada di broker ini"; return false; }
      ulong tk[];
      long  tm[];
      if(SR_Positions(tk, tm) > 0 || SR_OrderCount() > 0)
        { msg = "tutup posisi StopReverse di " + g_sr.sym + " dulu sebelum ganti simbol"; return false; }
      sy = sym;
     }
   if(lots != "")
     {
      double v = StringToDouble(lots);
      if(v <= 0 || v > 100) { msg = "lot harus lebih dari 0"; return false; }
      lo = v;
     }
   if(dist != "")
     {
      double v = StringToDouble(dist);
      if(v <= 0) { msg = "jarak D harus lebih dari 0"; return false; }
      di = v;
     }
   if(step != "")
     {
      double v = StringToDouble(step);
      if(v < 0) { msg = "geser minimal S tidak boleh negatif"; return false; }
      stp = v;
     }
   g_sr.on = on;
   g_sr.sym = sy;
   g_sr.lots = lo;
   g_sr.dist = di;
   g_sr.step = stp;
   SR_Save();
   g_srNextTry = 0;
   msg = StringFormat("StopReverse %s, %s lot %.2f D %.2f S %.2f", g_sr.on ? "ON" : "OFF", g_sr.sym, g_sr.lots, g_sr.dist, g_sr.step);
   return true;
  }

bool SR_CloseCommand(string &msg)
  {
   g_sr.on = false;
   SR_Save();
   bool ok = SR_CloseAll();
   msg = ok ? "StopReverse dimatikan, posisi dan stop order ditutup"
            : "StopReverse dimatikan, tetapi sebagian posisi/order gagal ditutup; cek tab Experts";
   return ok;
  }

//+------------------------------------------------------------------+
//| Telemetry                                                        |
//+------------------------------------------------------------------+
string SR_Q(string s)
  {
   StringReplace(s, "\\", "\\\\");
   StringReplace(s, "\"", "\\\"");
   return "\"" + s + "\"";
  }

string SR_Json()
  {
   ulong tk[];
   long  tm[];
   int n = SR_Positions(tk, tm);
   string block = SR_EntryBlock();
   return "{\"id\":\"stop_reverse\",\"name\":\"XAU StopReverse\""
          + ",\"enabled\":" + (g_sr.on ? "true" : "false")
          + ",\"symbol\":" + SR_Q(g_sr.sym)
          + ",\"lots\":" + DoubleToString(g_sr.lots, 2)
          + ",\"dist\":" + DoubleToString(g_sr.dist, 5)
          + ",\"step\":" + DoubleToString(g_sr.step, 5)
          + ",\"magic\":" + IntegerToString((long)g_srMagic)
          + ",\"hedging\":" + (g_srHedging ? "true" : "false")
          + ",\"positions\":" + IntegerToString(n)
          + ",\"orders\":" + IntegerToString(SR_OrderCount())
          + ",\"blocked\":" + SR_Q(block)
          + ",\"status\":" + SR_Q(g_srStatus) + "}";
  }

//+------------------------------------------------------------------+
//| Main loop: call on every timer tick and on ticks of the symbol   |
//+------------------------------------------------------------------+
void SR_Run()
  {
   if(AccountInfoInteger(ACCOUNT_LOGIN) != g_srLogin)
      SR_Load();   // another account logged in: its own settings
   g_srHedging = (AccountInfoInteger(ACCOUNT_MARGIN_MODE) == ACCOUNT_MARGIN_MODE_RETAIL_HEDGING);
   g_srTrade.SetTypeFillingBySymbol(g_sr.sym);

   ulong tk[];
   long  tm[];
   int n = SR_Positions(tk, tm);
   if(g_srPrevN > 0 && n == 0)
      g_srLastExit = TimeCurrent();
   g_srPrevN = n;

   // Switched off with nothing open: remove a leftover stop order and idle.
   if(!g_sr.on && n == 0)
     {
      if(SR_OrderCount() > 0)
         SR_CloseAll();
      g_srStatus = "OFF";
      return;
     }

   string why;
   if(!SR_TradingOpen(why))
     { g_srStatus = "Menunggu: " + why; return; }
   if(TimeCurrent() < g_srNextTry)
     { g_srStatus = "Jeda setelah error, mencoba lagi sebentar"; return; }

   string block = SR_EntryBlock();
   bool   allowNew = (block == "");

   //--- 2 positions = the stop order was hit (hedging)
   if(n > 1)
     {
      int nw = 0;
      for(int k = 1; k < n; k++)
         if(tm[k] > tm[nw])
            nw = k;
      for(int k = 0; k < n; k++)
         if(k != nw || !allowNew)
            if(!g_srTrade.PositionClose(tk[k]) || !SR_ResultOK())
               SR_Fail("tutup posisi");
      if(!allowNew)
         g_srLastExit = TimeCurrent();
      g_srStatus = allowNew ? "Balik arah" : "Keluar tanpa balik arah (" + block + ")";
      return;
     }

   //--- own stop order (at most one)
   ulong  od = 0;
   double odPx = 0, odVol = 0;
   long   odType = -1;
   for(int i = OrdersTotal() - 1; i >= 0; i--)
     {
      ulong t = OrderGetTicket(i);
      if(t == 0 || OrderGetString(ORDER_SYMBOL) != g_sr.sym || (ulong)OrderGetInteger(ORDER_MAGIC) != g_srMagic)
         continue;
      if(od != 0)
        {
         g_srTrade.OrderDelete(t);
         continue;
        }
      od     = t;
      odPx   = OrderGetDouble(ORDER_PRICE_OPEN);
      odVol  = OrderGetDouble(ORDER_VOLUME_CURRENT);
      odType = OrderGetInteger(ORDER_TYPE);
     }

   double bid = SymbolInfoDouble(g_sr.sym, SYMBOL_BID);
   double ask = SymbolInfoDouble(g_sr.sym, SYMBOL_ASK);
   if(bid <= 0 || ask <= 0)
      return;
   int    dg   = SR_Digits();
   double pt   = SR_Point();
   double stl  = (double)SymbolInfoInteger(g_sr.sym, SYMBOL_TRADE_STOPS_LEVEL) * pt;
   double frz  = (double)SymbolInfoInteger(g_sr.sym, SYMBOL_TRADE_FREEZE_LEVEL) * pt;
   double dist = MathMax(g_sr.dist, MathMax(stl, frz) + 2 * pt);
   double lots = SR_Lot(g_sr.lots);

   //--- no position
   if(n == 0)
     {
      if(od != 0)
         g_srTrade.OrderDelete(od);
      if(!allowNew)
        { g_srStatus = "Menunggu: " + block; return; }
      if(g_srLastExit > 0 && iTime(g_sr.sym, PERIOD_M1, 0) <= g_srLastExit)
        { g_srStatus = "Menunggu candle M1 baru setelah keluar"; return; }
      int dir = SR_EntryDirection();
      bool ok = (dir > 0) ? g_srTrade.Buy(lots, g_sr.sym, 0, 0, 0, "SR")
                          : g_srTrade.Sell(lots, g_sr.sym, 0, 0, 0, "SR");
      if(!ok || !SR_ResultOK())
         SR_Fail(dir > 0 ? "buka BUY" : "buka SELL");
      g_srStatus = dir > 0 ? "Membuka BUY" : "Membuka SELL";
      return;
     }

   //--- one position
   if(!PositionSelectByTicket(tk[0]))
      return;
   bool   isBuy  = (PositionGetInteger(POSITION_TYPE) == POSITION_TYPE_BUY);
   double vol    = PositionGetDouble(POSITION_VOLUME);
   double openPx = PositionGetDouble(POSITION_PRICE_OPEN);
   double posSl  = PositionGetDouble(POSITION_SL);
   double lvl    = isBuy ? SR_Px(bid - dist) : SR_Px(ask + dist);
   double ref    = isBuy ? bid : ask;   // price that triggers the stop
   double gain   = isBuy ? (bid - openPx) : (openPx - ask);

   // Hedging without reversal: a pending order would open a new position, so the position
   // trails with its own SL instead and any stop order goes.
   if(g_srHedging && !allowNew)
     {
      if(od != 0 && (!g_srTrade.OrderDelete(od) || !SR_ResultOK()))
        { SR_Fail("hapus stop order"); return; }
      // start from the stop order's level so switching off never loosens the exit
      double cur = (posSl > 0) ? posSl : (od != 0 ? odPx : 0);
      bool better = (cur == 0) || (isBuy ? (lvl >= cur + g_sr.step) : (lvl <= cur - g_sr.step));
      double want = better ? lvl : cur;
      if(want != posSl && MathAbs(ref - want) > frz && (isBuy ? want <= bid - stl : want >= ask + stl))
         if(!g_srTrade.PositionModify(tk[0], want, 0) || !SR_ResultOK())
            SR_Fail("geser SL");
      g_srStatus = StringFormat("%s %.2f @ %s (%+.2f) | SL %s = KELUAR saja (%s)", isBuy ? "BUY" : "SELL", vol,
                                DoubleToString(openPx, dg), gain, DoubleToString(want, dg), block);
      return;
     }

   // stop-and-reverse mode: no SL / TP on the position
   if(posSl != 0 || PositionGetDouble(POSITION_TP) != 0)
      if(!g_srTrade.PositionModify(tk[0], 0, 0) || !SR_ResultOK())
         SR_Fail("hapus SL/TP");

   long   wType = isBuy ? ORDER_TYPE_SELL_STOP : ORDER_TYPE_BUY_STOP;
   double wVol  = g_srHedging ? lots : (allowNew ? SR_Lot(vol + lots) : vol);
   if(posSl != 0 && od == 0)
      lvl = isBuy ? MathMax(lvl, posSl) : MathMin(lvl, posSl);   // switching back on keeps the tighter level

   // wrong type / volume -> place again, keeping the better level
   if(od != 0 && (odType != wType || MathAbs(odVol - wVol) > 1e-8))
     {
      if(odType == wType)
        {
         double keepPx = isBuy ? MathMax(odPx, lvl) : MathMin(odPx, lvl);
         if(isBuy ? (keepPx <= bid - stl) : (keepPx >= ask + stl))
            lvl = keepPx;
        }
      if(!g_srTrade.OrderDelete(od) || !SR_ResultOK())
        { SR_Fail("hapus stop order"); return; }
      od = 0;
     }

   if(od == 0)
     {
      bool ok = isBuy ? g_srTrade.SellStop(wVol, lvl, g_sr.sym, 0, 0, ORDER_TIME_GTC, 0, "SR")
                      : g_srTrade.BuyStop(wVol, lvl, g_sr.sym, 0, 0, ORDER_TIME_GTC, 0, "SR");
      if(!ok || !SR_ResultOK())
         SR_Fail("pasang stop order");
      else
         odPx = lvl;
     }
   else
     {
      bool better   = isBuy ? (lvl >= odPx + g_sr.step) : (lvl <= odPx - g_sr.step);
      bool unfrozen = MathAbs(ref - odPx) > frz;
      if(better && unfrozen)
        {
         if(!g_srTrade.OrderModify(od, lvl, 0, 0, ORDER_TIME_GTC, 0) || !SR_ResultOK())
            SR_Fail("geser stop order");
         else
            odPx = lvl;
        }
     }

   g_srStatus = StringFormat("%s %.2f @ %s (%+.2f) | %s %s = %s", isBuy ? "BUY" : "SELL", vol,
                             DoubleToString(openPx, dg), gain, isBuy ? "SELL STOP" : "BUY STOP",
                             DoubleToString(odPx, dg), allowNew ? "BALIK ARAH" : "KELUAR saja (" + block + ")");
  }

#endif
