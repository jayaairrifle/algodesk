//+------------------------------------------------------------------+
//| AlgoDeskAgent.mq5                                                |
//| Terminal-side agent for the AlgoDesk dashboard.                  |
//|                                                                  |
//| 1. Daily protection: stores the start-of-day equity baseline and |
//|    blocks new entries once the loss limit is reached. Works with |
//|    no internet; EAs read the state through AlgoDeskGuard.mqh.    |
//| 2. Enforcement: optionally closes any position opened while      |
//|    entries are blocked/paused (for EAs that lack the guard).     |
//| 3. Telemetry: posts account, positions and Market Watch data to  |
//|    the backend, signed with HMAC-SHA256.                         |
//| 4. Commands: polls the backend for pause/resume/enable/disable/  |
//|    set_limit commands and acknowledges each one.                 |
//|                                                                  |
//| Attach to ONE chart per terminal/account.                        |
//+------------------------------------------------------------------+
#property copyright "AlgoDesk"
#property version   "0.10"
#property description "AlgoDesk MT5 Agent: proteksi rugi harian, telemetri, dan kanal perintah."

#include <Trade/Trade.mqh>
#include <AlgoDesk/AlgoDeskGuard.mqh>

//--- protection
input group "Proteksi harian"
input double InpLossLimitPct      = 3.0;    // Batas rugi harian (% dari equity awal hari)
input int    InpDayStartHour      = 0;      // Jam mulai hari (jam server broker, 0-23)
input bool   InpCloseBlockedEntry = true;   // Tutup posisi baru yang dibuka saat entry diblokir/dijeda
input bool   InpCloseAllOnLimit   = false;  // Tutup SEMUA posisi saat batas tercapai
input string InpWatchMagics       = "";     // Magic yang diawasi, pisahkan koma (kosong = semua kecuali manual)
input bool   InpIncludeManual     = false;  // Ikut awasi trade manual (magic 0)

//--- backend
input group "Backend"
input string InpBackendUrl        = "";     // URL backend, mis. https://algodesk.example.com/api (kosong = mode lokal)
input string InpAgentId           = "vps-01";
input string InpAgentSecret       = "";     // Rahasia HMAC agent (jangan dibagikan)
input int    InpTelemetrySec      = 2;      // Interval telemetri (detik)
input int    InpCommandPollSec    = 2;      // Interval cek perintah (detik)
input int    InpHttpTimeoutMs     = 3000;

//--- market watch
input group "Market Watch"
input ENUM_TIMEFRAMES InpSignalTf = PERIOD_H1;
input int    InpRsiPeriod         = 14;
input int    InpAtrPeriod         = 14;
input double InpRsiBuy            = 55.0;   // RSI minimal untuk sinyal BUY (dengan struktur HH/HL)
input double InpRsiSell           = 45.0;   // RSI maksimal untuk sinyal SELL (dengan struktur LH/LL)
input int    InpMaxSymbols        = 20;

#define AGENT_VERSION "0.12"

CTrade   g_trade;
long     g_watch[];
string   g_syms[];
int      g_rsi[];
int      g_atr[];
datetime g_lastTelemetry = 0;
datetime g_lastPoll      = 0;
datetime g_lastBackendOk = 0;
string   g_lastError     = "";
bool     g_limitLogged   = false;

//+------------------------------------------------------------------+
//| Day / baseline                                                   |
//+------------------------------------------------------------------+
datetime CurrentDayStart()
  {
   int h = (int)MathMax(0, MathMin(23, InpDayStartHour));
   datetime shifted = TimeTradeServer() - h * 3600;
   MqlDateTime t;
   TimeToStruct(shifted, t);
   t.hour = 0; t.min = 0; t.sec = 0;
   return StructToTime(t) + h * 3600;
  }

// Balance at the start of the trading day: current balance minus every deal since then
// (trades, deposits, withdrawals, credit). Used to check that a stored baseline belongs to this account.
double DayStartBalance()
  {
   double bal = AccountInfoDouble(ACCOUNT_BALANCE);
   if(!HistorySelect(CurrentDayStart(), TimeTradeServer() + 60))
      return bal;
   for(int i = 0; i < HistoryDealsTotal(); i++)
     {
      ulong t = HistoryDealGetTicket(i);
      if(t != 0)
         bal -= HistoryDealGetDouble(t, DEAL_PROFIT) + HistoryDealGetDouble(t, DEAL_SWAP)
                + HistoryDealGetDouble(t, DEAL_COMMISSION) + HistoryDealGetDouble(t, DEAL_FEE);
     }
   return bal;
  }

void EnsureBaseline()
  {
   // Right after start-up or an account switch the terminal can still report the previous
   // account's equity; wait until it is connected to the logged-in account.
   if(!TerminalInfoInteger(TERMINAL_CONNECTED) || AccountInfoInteger(ACCOUNT_LOGIN) == 0
      || AccountInfoDouble(ACCOUNT_BALANCE) <= 0.0)
      return;
   datetime dayStart = CurrentDayStart();
   double equity = AccountInfoDouble(ACCOUNT_EQUITY);
   double base = AD_Get("BASE", 0.0);
   if((datetime)(long)AD_Get("DAY", 0.0) == dayStart && base > 0.0)
     {
      // A baseline far from this account's start-of-day balance was taken from stale data
      // (another account or currency): replace it and lift the block it caused.
      double ref = DayStartBalance();
      if(ref <= 0.0 || MathAbs(base - ref) <= 0.5 * ref)
         return;
      AD_Set("BASE", ref);
      AD_Set("BLOCK", 0.0);
      g_limitLogged = false;
      PrintFormat("AlgoDesk: baseline %.2f tidak cocok dengan akun %I64d, diganti %.2f",
                  base, AccountInfoInteger(ACCOUNT_LOGIN), ref);
      return;
     }
   AD_Set("DAY", (double)dayStart);
   AD_Set("BASE", equity);
   AD_Set("BLOCK", 0.0);
   g_limitLogged = false;
   PrintFormat("AlgoDesk: hari baru %s, baseline equity %.2f", TimeToString(dayStart), equity);
  }

double Baseline()   { return AD_Get("BASE", 0.0); }
double LimitPct()   { return AD_Get("LIMIT_PCT", InpLossLimitPct); }
double LossLimit()  { return Baseline() * LimitPct() / 100.0; }
double LossUsed()   { return MathMax(0.0, Baseline() - AccountInfoDouble(ACCOUNT_EQUITY)); }

// Closed-trade result since the start of the trading day, as in the MT5 History tab:
// profit + swap + commission + fee of buy/sell deals. Deposits and withdrawals are excluded.
double RealizedToday(int &deals)
  {
   deals = 0;
   double sum = 0.0;
   if(!HistorySelect(CurrentDayStart(), TimeTradeServer() + 60))
      return 0.0;
   for(int i = 0; i < HistoryDealsTotal(); i++)
     {
      ulong t = HistoryDealGetTicket(i);
      if(t == 0)
         continue;
      long type = HistoryDealGetInteger(t, DEAL_TYPE);
      if(type != DEAL_TYPE_BUY && type != DEAL_TYPE_SELL)
         continue;
      sum += HistoryDealGetDouble(t, DEAL_PROFIT) + HistoryDealGetDouble(t, DEAL_SWAP)
             + HistoryDealGetDouble(t, DEAL_COMMISSION) + HistoryDealGetDouble(t, DEAL_FEE);
      if(HistoryDealGetInteger(t, DEAL_ENTRY) != DEAL_ENTRY_IN)
         deals++;
     }
   return sum;
  }

// Open-position result, as the Profit column total in the MT5 Trade tab (profit + swap).
double FloatingPl()
  {
   double sum = 0.0;
   for(int i = 0; i < PositionsTotal(); i++)
      if(PositionGetTicket(i) != 0)
         sum += PositionGetDouble(POSITION_PROFIT) + PositionGetDouble(POSITION_SWAP);
   return sum;
  }

string EntryStateText()
  {
   if(AD_Get("BLOCK", 0.0) > 0.5) return "blocked";
   if(AD_Get("PAUSE", 0.0) > 0.5) return "paused";
   return "allowed";
  }

void CheckLimit()
  {
   if(AD_Get("BLOCK", 0.0) > 0.5 || Baseline() <= 0.0)
      return;
   if(LossUsed() < LossLimit())
      return;
   AD_Set("BLOCK", 1.0);
   if(!g_limitLogged)
     {
      PrintFormat("AlgoDesk: batas rugi %.2f%% tercapai (rugi %.2f dari batas %.2f). Entry baru diblokir sampai reset harian.",
                  LimitPct(), LossUsed(), LossLimit());
      g_limitLogged = true;
     }
   if(InpCloseAllOnLimit)
      CloseWatchedPositions("batas rugi harian");
  }

//+------------------------------------------------------------------+
//| Magic filter                                                     |
//+------------------------------------------------------------------+
void ParseWatchMagics()
  {
   ArrayResize(g_watch, 0);
   string parts[];
   int n = StringSplit(InpWatchMagics, ',', parts);
   for(int i = 0; i < n; i++)
     {
      string p = parts[i];
      StringTrimLeft(p);
      StringTrimRight(p);
      if(p == "")
         continue;
      int k = ArraySize(g_watch);
      ArrayResize(g_watch, k + 1);
      g_watch[k] = StringToInteger(p);
     }
  }

bool IsWatched(const long magic)
  {
   if(magic == 0)
      return InpIncludeManual;
   if(ArraySize(g_watch) == 0)
      return true;
   for(int i = 0; i < ArraySize(g_watch); i++)
      if(g_watch[i] == magic)
         return true;
   return false;
  }

void CloseWatchedPositions(const string why)
  {
   for(int i = PositionsTotal() - 1; i >= 0; i--)
     {
      ulong ticket = PositionGetTicket(i);
      if(ticket == 0 || !IsWatched(PositionGetInteger(POSITION_MAGIC)))
         continue;
      if(g_trade.PositionClose(ticket))
         PrintFormat("AlgoDesk: posisi #%I64u ditutup (%s)", ticket, why);
      else
         PrintFormat("AlgoDesk: gagal menutup #%I64u (%s): %d", ticket, why, g_trade.ResultRetcode());
     }
  }

//+------------------------------------------------------------------+
//| Enforcement for EAs that do not use the guard                    |
//+------------------------------------------------------------------+
void OnTradeTransaction(const MqlTradeTransaction &trans,
                        const MqlTradeRequest &request,
                        const MqlTradeResult &result)
  {
   if(!InpCloseBlockedEntry || trans.type != TRADE_TRANSACTION_DEAL_ADD)
      return;
   if(!HistoryDealSelect(trans.deal))
      return;
   if(HistoryDealGetInteger(trans.deal, DEAL_ENTRY) != DEAL_ENTRY_IN)
      return;
   long magic = HistoryDealGetInteger(trans.deal, DEAL_MAGIC);
   if(!IsWatched(magic))
      return;
   ENUM_AD_ENTRY r = AlgoDesk_Check(magic);
   if(r == AD_ENTRY_OK)
      return;
   ulong posId = (ulong)HistoryDealGetInteger(trans.deal, DEAL_POSITION_ID);
   if(PositionSelectByTicket(posId) && g_trade.PositionClose(posId))
      PrintFormat("AlgoDesk: entry baru #%I64u (magic %I64d) ditutup: %s", posId, magic, AlgoDesk_ReasonText(r));
   else
      PrintFormat("AlgoDesk: gagal menutup entry terblokir #%I64u (magic %I64d)", posId, magic);
  }

//+------------------------------------------------------------------+
//| Market Watch indicators                                          |
//+------------------------------------------------------------------+
void ReleaseHandles()
  {
   for(int i = 0; i < ArraySize(g_rsi); i++)
      if(g_rsi[i] != INVALID_HANDLE) IndicatorRelease(g_rsi[i]);
   for(int i = 0; i < ArraySize(g_atr); i++)
      if(g_atr[i] != INVALID_HANDLE) IndicatorRelease(g_atr[i]);
   ArrayResize(g_syms, 0);
   ArrayResize(g_rsi, 0);
   ArrayResize(g_atr, 0);
  }

void BuildSymbols()
  {
   ReleaseHandles();
   int total = MathMin(SymbolsTotal(true), InpMaxSymbols);
   ArrayResize(g_syms, total);
   ArrayResize(g_rsi, total);
   ArrayResize(g_atr, total);
   for(int i = 0; i < total; i++)
     {
      g_syms[i] = SymbolName(i, true);
      g_rsi[i]  = iRSI(g_syms[i], InpSignalTf, InpRsiPeriod, PRICE_CLOSE);
      g_atr[i]  = iATR(g_syms[i], InpSignalTf, InpAtrPeriod);
     }
  }

// USD/IDR rate from the broker, for the dashboard's currency conversion.
// Uses the first symbol named like USDIDR (suffixes such as USDIDR.m are fine); 0 when the broker has none.
double UsdIdrRate()
  {
   for(int i = 0; i < SymbolsTotal(false); i++)
     {
      string name = SymbolName(i, false);
      if(StringFind(name, "USDIDR") != 0)
         continue;
      double bid = SymbolInfoDouble(name, SYMBOL_BID);
      if(bid > 0.0)
         return bid;
     }
   return 0.0;
  }

double LastValue(const int handle)
  {
   if(handle == INVALID_HANDLE)
      return EMPTY_VALUE;
   double buf[];
   if(CopyBuffer(handle, 0, 0, 1, buf) != 1)
      return EMPTY_VALUE;
   return buf[0];
  }

// "bull" = higher high + higher low, "bear" = lower high + lower low, else "mixed"
string MarketStructure(const string sym)
  {
   double hi[], lo[];
   ArraySetAsSeries(hi, true);
   ArraySetAsSeries(lo, true);
   int n = 120;
   if(CopyHigh(sym, InpSignalTf, 0, n, hi) != n || CopyLow(sym, InpSignalTf, 0, n, lo) != n)
      return "mixed";
   double sh[2], sl[2];
   int nh = 0, nl = 0;
   for(int i = 2; i < n - 2 && (nh < 2 || nl < 2); i++)
     {
      if(nh < 2 && hi[i] > hi[i - 1] && hi[i] > hi[i - 2] && hi[i] >= hi[i + 1] && hi[i] >= hi[i + 2])
         sh[nh++] = hi[i];
      if(nl < 2 && lo[i] < lo[i - 1] && lo[i] < lo[i - 2] && lo[i] <= lo[i + 1] && lo[i] <= lo[i + 2])
         sl[nl++] = lo[i];
     }
   if(nh < 2 || nl < 2)
      return "mixed";
   // index 0 = most recent swing
   if(sh[0] > sh[1] && sl[0] > sl[1]) return "bull";
   if(sh[0] < sh[1] && sl[0] < sl[1]) return "bear";
   return "mixed";
  }

//+------------------------------------------------------------------+
//| JSON helpers                                                     |
//+------------------------------------------------------------------+
string J(const string s)
  {
   string r = s;
   StringReplace(r, "\\", "\\\\");
   StringReplace(r, "\"", "\\\"");
   StringReplace(r, "\n", "\\n");
   StringReplace(r, "\r", "\\r");
   return "\"" + r + "\"";
  }

string N(const double v, const int digits = 2)
  {
   if(v == EMPTY_VALUE || !MathIsValidNumber(v))
      return "null";
   return DoubleToString(v, digits);
  }

// Value of "key" inside a flat JSON object; strings are returned unquoted.
string JsonValue(const string obj, const string key)
  {
   int k = StringFind(obj, "\"" + key + "\"");
   if(k < 0)
      return "";
   int c = StringFind(obj, ":", k);
   if(c < 0)
      return "";
   int i = c + 1;
   int len = StringLen(obj);
   while(i < len && (StringGetCharacter(obj, i) == ' ' || StringGetCharacter(obj, i) == '\t'))
      i++;
   if(i >= len)
      return "";
   if(StringGetCharacter(obj, i) == '"')
     {
      int e = i + 1;
      while(e < len && !(StringGetCharacter(obj, e) == '"' && StringGetCharacter(obj, e - 1) != '\\'))
         e++;
      return StringSubstr(obj, i + 1, e - i - 1);
     }
   int e = i;
   while(e < len)
     {
      ushort ch = StringGetCharacter(obj, e);
      if(ch == ',' || ch == '}' || ch == ']' || ch == ' ')
         break;
      e++;
     }
   return StringSubstr(obj, i, e - i);
  }

// Splits the objects of the first JSON array found after "key".
int JsonObjects(const string json, const string key, string &out[])
  {
   ArrayResize(out, 0);
   int k = StringFind(json, "\"" + key + "\"");
   if(k < 0)
      return 0;
   int i = StringFind(json, "[", k);
   if(i < 0)
      return 0;
   int len = StringLen(json), depth = 0, start = -1;
   bool inStr = false;
   for(int p = i + 1; p < len; p++)
     {
      ushort ch = StringGetCharacter(json, p);
      if(inStr)
        {
         if(ch == '"' && StringGetCharacter(json, p - 1) != '\\') inStr = false;
         continue;
        }
      if(ch == '"') { inStr = true; continue; }
      if(ch == '{') { if(depth == 0) start = p; depth++; }
      else if(ch == '}')
        {
         depth--;
         if(depth == 0 && start >= 0)
           {
            int n = ArraySize(out);
            ArrayResize(out, n + 1);
            out[n] = StringSubstr(json, start, p - start + 1);
            start = -1;
           }
        }
      else if(ch == ']' && depth == 0)
         break;
     }
   return ArraySize(out);
  }

//+------------------------------------------------------------------+
//| HMAC-SHA256 request signing                                      |
//+------------------------------------------------------------------+
int Utf8Bytes(const string s, uchar &out[])
  {
   int n = StringToCharArray(s, out, 0, WHOLE_ARRAY, CP_UTF8);
   n = MathMax(0, n - 1);           // drop terminating zero
   ArrayResize(out, n);
   return n;
  }

string HmacSha256Hex(const string secret, const string message)
  {
   uchar key[], msg[], none[], hashed[];
   Utf8Bytes(secret, key);
   int ml = Utf8Bytes(message, msg);
   if(ArraySize(key) > 64)
     {
      CryptEncode(CRYPT_HASH_SHA256, key, none, hashed);
      ArrayFree(key);
      ArrayCopy(key, hashed);
     }
   uchar block[64];
   ArrayInitialize(block, 0);
   ArrayCopy(block, key, 0, 0, ArraySize(key));

   uchar inner[], innerHash[], outer[], outerHash[];
   ArrayResize(inner, 64 + ml);
   for(int i = 0; i < 64; i++)
      inner[i] = (uchar)(block[i] ^ 0x36);
   if(ml > 0)
      ArrayCopy(inner, msg, 64, 0, ml);
   if(CryptEncode(CRYPT_HASH_SHA256, inner, none, innerHash) != 32)
      return "";

   ArrayResize(outer, 64 + 32);
   for(int i = 0; i < 64; i++)
      outer[i] = (uchar)(block[i] ^ 0x5c);
   ArrayCopy(outer, innerHash, 64, 0, 32);
   if(CryptEncode(CRYPT_HASH_SHA256, outer, none, outerHash) != 32)
      return "";

   string hex = "";
   for(int i = 0; i < 32; i++)
      hex += StringFormat("%02x", outerHash[i]);
   return hex;
  }

//+------------------------------------------------------------------+
//| HTTP                                                             |
//+------------------------------------------------------------------+
bool BackendEnabled() { return StringLen(InpBackendUrl) > 0; }

// Signature covers "<timestamp>\n<METHOD>\n<path>\n<body>" so a captured
// request cannot be replayed to another endpoint or after the time window.
int Http(const string method, const string path, const string body, string &response)
  {
   response = "";
   string ts  = IntegerToString((long)TimeGMT());
   string sig = HmacSha256Hex(InpAgentSecret, ts + "\n" + method + "\n" + path + "\n" + body);
   string headers = "Content-Type: application/json\r\n" +
                    "X-AlgoDesk-Agent: " + InpAgentId + "\r\n" +
                    "X-AlgoDesk-Timestamp: " + ts + "\r\n" +
                    "X-AlgoDesk-Signature: " + sig + "\r\n";
   char data[], result[];
   if(StringLen(body) > 0)
     {
      int n = StringToCharArray(body, data, 0, WHOLE_ARRAY, CP_UTF8);
      ArrayResize(data, MathMax(0, n - 1));
     }
   string resultHeaders;
   ResetLastError();
   int code = WebRequest(method, InpBackendUrl + path, headers, InpHttpTimeoutMs, data, result, resultHeaders);
   if(code == -1)
     {
      int err = GetLastError();
      g_lastError = (err == 4014)
                    ? "URL belum diizinkan: Tools > Options > Expert Advisors > Allow WebRequest"
                    : "WebRequest gagal, kode " + IntegerToString(err);
      return -1;
     }
   response = CharArrayToString(result, 0, WHOLE_ARRAY, CP_UTF8);
   if(code >= 200 && code < 300)
     {
      g_lastBackendOk = TimeLocal();
      g_lastError = "";
     }
   else
      g_lastError = "HTTP " + IntegerToString(code) + " pada " + path;
   return code;
  }

//+------------------------------------------------------------------+
//| Telemetry                                                        |
//+------------------------------------------------------------------+
string PositionsJson()
  {
   string s = "[";
   int added = 0;
   for(int i = 0; i < PositionsTotal(); i++)
     {
      ulong ticket = PositionGetTicket(i);
      if(ticket == 0)
         continue;
      string sym = PositionGetString(POSITION_SYMBOL);
      int d = (int)SymbolInfoInteger(sym, SYMBOL_DIGITS);
      if(added++ > 0) s += ",";
      s += "{\"ticket\":" + IntegerToString((long)ticket)
           + ",\"symbol\":" + J(sym)
           + ",\"digits\":" + IntegerToString(d)
           + ",\"side\":" + J(PositionGetInteger(POSITION_TYPE) == POSITION_TYPE_BUY ? "BUY" : "SELL")
           + ",\"volume\":" + N(PositionGetDouble(POSITION_VOLUME), 2)
           + ",\"price_open\":" + N(PositionGetDouble(POSITION_PRICE_OPEN), d)
           + ",\"sl\":" + N(PositionGetDouble(POSITION_SL), d)
           + ",\"tp\":" + N(PositionGetDouble(POSITION_TP), d)
           + ",\"profit\":" + N(PositionGetDouble(POSITION_PROFIT) + PositionGetDouble(POSITION_SWAP), 2)
           + ",\"magic\":" + IntegerToString(PositionGetInteger(POSITION_MAGIC))
           + ",\"comment\":" + J(PositionGetString(POSITION_COMMENT))
           + ",\"time\":" + IntegerToString(PositionGetInteger(POSITION_TIME))
           + "}";
     }
   return s + "]";
  }

string SymbolsJson()
  {
   string s = "[";
   for(int i = 0; i < ArraySize(g_syms); i++)
     {
      string sym = g_syms[i];
      double point = SymbolInfoDouble(sym, SYMBOL_POINT);
      int d = (int)SymbolInfoInteger(sym, SYMBOL_DIGITS);
      double rsi = LastValue(g_rsi[i]);
      double atr = LastValue(g_atr[i]);
      double atrPts = (atr != EMPTY_VALUE && point > 0) ? atr / point : EMPTY_VALUE;
      string st = MarketStructure(sym);
      string sig = "none";
      if(rsi != EMPTY_VALUE)
        {
         if(st == "bull" && rsi >= InpRsiBuy) sig = "BUY";
         else if(st == "bear" && rsi <= InpRsiSell) sig = "SELL";
        }
      if(i > 0) s += ",";
      s += "{\"symbol\":" + J(sym)
           + ",\"digits\":" + IntegerToString(d)
           + ",\"bid\":" + N(SymbolInfoDouble(sym, SYMBOL_BID), d)
           + ",\"ask\":" + N(SymbolInfoDouble(sym, SYMBOL_ASK), d)
           + ",\"spread\":" + IntegerToString(SymbolInfoInteger(sym, SYMBOL_SPREAD))
           + ",\"rsi\":" + N(rsi, 1)
           + ",\"atr_points\":" + N(atrPts, 1)
           + ",\"structure\":" + J(st)
           + ",\"signal\":" + J(sig)
           + "}";
     }
   return s + "]";
  }

string DisabledMagicsJson()
  {
   string s = "[";
   int n = 0;
   string prefix = AD_Key("M");
   for(int i = 0; i < GlobalVariablesTotal(); i++)
     {
      string name = GlobalVariableName(i);
      if(StringFind(name, prefix) != 0 || StringFind(name, "_OFF") < 0 || GlobalVariableGet(name) < 0.5)
         continue;
      string mid = StringSubstr(name, StringLen(prefix), StringLen(name) - StringLen(prefix) - 4);
      if(n++ > 0) s += ",";
      s += mid;
     }
   return s + "]";
  }

void SendTelemetry()
  {
   double equity = AccountInfoDouble(ACCOUNT_EQUITY);
   int closedDeals = 0;
   double realized = RealizedToday(closedDeals);
   string body = "{" +
      "\"agent_id\":" + J(InpAgentId)
      + ",\"agent_version\":" + J(AGENT_VERSION)
      + ",\"sent_at\":" + IntegerToString((long)TimeGMT())
      + ",\"server_time\":" + IntegerToString((long)TimeTradeServer())
      + ",\"terminal_connected\":" + (TerminalInfoInteger(TERMINAL_CONNECTED) ? "true" : "false")
      + ",\"account\":{" +
         "\"login\":" + IntegerToString(AccountInfoInteger(ACCOUNT_LOGIN))
         + ",\"server\":" + J(AccountInfoString(ACCOUNT_SERVER))
         + ",\"company\":" + J(AccountInfoString(ACCOUNT_COMPANY))
         + ",\"currency\":" + J(AccountInfoString(ACCOUNT_CURRENCY))
         + ",\"demo\":" + (AccountInfoInteger(ACCOUNT_TRADE_MODE) == ACCOUNT_TRADE_MODE_DEMO ? "true" : "false")
         + ",\"balance\":" + N(AccountInfoDouble(ACCOUNT_BALANCE))
         + ",\"equity\":" + N(equity)
         + ",\"margin\":" + N(AccountInfoDouble(ACCOUNT_MARGIN))
         + ",\"free_margin\":" + N(AccountInfoDouble(ACCOUNT_MARGIN_FREE))
         + ",\"margin_level\":" + N(AccountInfoDouble(ACCOUNT_MARGIN_LEVEL))
      + "}"
      + ",\"protection\":{" +
         "\"day_start\":" + IntegerToString((long)AD_Get("DAY", 0.0))
         + ",\"baseline\":" + N(Baseline())
         + ",\"limit_pct\":" + N(LimitPct())
         + ",\"limit_amount\":" + N(LossLimit())
         + ",\"loss_used\":" + N(LossUsed())
         + ",\"entry_state\":" + J(EntryStateText())
         + ",\"disabled_magics\":" + DisabledMagicsJson()
      + "}"
      + ",\"pl\":{" +
         "\"realized\":" + N(realized)
         + ",\"floating\":" + N(FloatingPl())
         + ",\"closed_deals\":" + IntegerToString(closedDeals)
      + "}"
      + ",\"fx\":{\"usd_idr\":" + N(UsdIdrRate(), 2) + "}"
      + ",\"positions\":" + PositionsJson()
      + ",\"symbols\":" + SymbolsJson()
      + ",\"timeframe\":" + J(StringSubstr(EnumToString(InpSignalTf), 7))
      + "}";
   string resp;
   Http("POST", "/agent/telemetry", body, resp);
  }

//+------------------------------------------------------------------+
//| Commands                                                         |
//+------------------------------------------------------------------+
bool ApplyCommand(const string type, const string obj, string &msg)
  {
   if(type == "pause_entry")
     {
      AD_Set("PAUSE", 1.0);
      msg = "entry dijeda";
      return true;
     }
   if(type == "resume_entry")
     {
      AD_Set("PAUSE", 0.0);
      msg = (AD_Get("BLOCK", 0.0) > 0.5) ? "jeda dicabut, tetapi batas rugi harian masih memblokir entry" : "entry dilanjutkan";
      return true;
     }
   if(type == "disable_magic" || type == "enable_magic")
     {
      long magic = StringToInteger(JsonValue(obj, "magic"));
      if(magic <= 0)
        {
         msg = "magic tidak valid";
         return false;
        }
      AD_Set(AD_MagicKey(magic), type == "disable_magic" ? 1.0 : 0.0);
      msg = StringFormat("magic %I64d %s", magic, type == "disable_magic" ? "dinonaktifkan" : "diaktifkan");
      return true;
     }
   if(type == "set_limit")
     {
      double v = StringToDouble(JsonValue(obj, "value"));
      if(v < 0.5 || v > 20.0)
        {
         msg = "batas harus 0.5 sampai 20 persen";
         return false;
        }
      AD_Set("LIMIT_PCT", v);
      msg = StringFormat("batas rugi diubah ke %.2f%%", v);
      // A higher limit does not lift a block already set today; only the daily reset does.
      CheckLimit();
      return true;
     }
   msg = "perintah tidak dikenal: " + type;
   return false;
  }

void PollCommands()
  {
   string resp;
   if(Http("GET", "/agent/commands", "", resp) != 200)
      return;
   string cmds[];
   int n = JsonObjects(resp, "commands", cmds);
   for(int i = 0; i < n; i++)
     {
      string id   = JsonValue(cmds[i], "id");
      string type = JsonValue(cmds[i], "type");
      if(id == "")
         continue;
      string msg;
      bool ok = ApplyCommand(type, cmds[i], msg);
      PrintFormat("AlgoDesk: perintah %s (%s) dari %s: %s", type, id, JsonValue(cmds[i], "issued_by"), msg);
      string ack = "{\"id\":" + J(id)
                   + ",\"ok\":" + (ok ? "true" : "false")
                   + ",\"message\":" + J(msg)
                   + ",\"entry_state\":" + J(EntryStateText())
                   + ",\"applied_at\":" + IntegerToString((long)TimeGMT()) + "}";
      string ackResp;
      Http("POST", "/agent/commands/ack", ack, ackResp);
     }
  }

//+------------------------------------------------------------------+
//| Chart status                                                     |
//+------------------------------------------------------------------+
void ShowStatus()
  {
   string backend = !BackendEnabled() ? "mode lokal (tanpa backend)"
                    : (g_lastError != "" ? "ERROR: " + g_lastError
                       : "terhubung, terakhir " + TimeToString(g_lastBackendOk, TIME_SECONDS));
   Comment(StringFormat("AlgoDesk Agent %s  |  akun %I64d\n" +
                        "Baseline %.2f  |  Batas %.2f%% (%.2f)\n" +
                        "Rugi tercatat %.2f  |  Entry: %s\n" +
                        "Backend: %s",
                        AGENT_VERSION, AccountInfoInteger(ACCOUNT_LOGIN),
                        Baseline(), LimitPct(), LossLimit(),
                        LossUsed(), EntryStateText(), backend));
  }

//+------------------------------------------------------------------+
//| Lifecycle                                                        |
//+------------------------------------------------------------------+
int OnInit()
  {
   if(InpLossLimitPct < 0.5 || InpLossLimitPct > 20.0)
     {
      Print("AlgoDesk: InpLossLimitPct harus 0.5 sampai 20");
      return INIT_PARAMETERS_INCORRECT;
     }
   if(BackendEnabled() && StringLen(InpAgentSecret) < 16)
     {
      Print("AlgoDesk: InpAgentSecret minimal 16 karakter saat backend dipakai");
      return INIT_PARAMETERS_INCORRECT;
     }
   if(BackendEnabled() && StringFind(InpBackendUrl, "https://") != 0)
      Print("AlgoDesk: PERINGATAN, backend tidak memakai HTTPS");

   // The input value wins at start-up; later changes come from set_limit commands.
   AD_Set("LIMIT_PCT", InpLossLimitPct);
   ParseWatchMagics();
   EnsureBaseline();
   BuildSymbols();
   g_trade.SetAsyncMode(false);
   EventSetTimer(1);
   PrintFormat("AlgoDesk: agent aktif, baseline %.2f, batas %.2f%%", Baseline(), LimitPct());
   return INIT_SUCCEEDED;
  }

void OnDeinit(const int reason)
  {
   EventKillTimer();
   ReleaseHandles();
   Comment("");
  }

void OnTimer()
  {
   EnsureBaseline();
   CheckLimit();
   AD_Set("HB", (double)TimeGMT());

   if(SymbolsTotal(true) != ArraySize(g_syms) && ArraySize(g_syms) < InpMaxSymbols)
      BuildSymbols();

   if(BackendEnabled())
     {
      datetime now = TimeLocal();
      if(now - g_lastPoll >= InpCommandPollSec)
        {
         g_lastPoll = now;
         PollCommands();
        }
      if(now - g_lastTelemetry >= InpTelemetrySec)
        {
         g_lastTelemetry = now;
         SendTelemetry();
        }
     }
   ShowStatus();
  }

void OnTick()
  {
   // Equity can move between timer ticks; check the limit on every tick of the chart symbol too.
   CheckLimit();
  }
//+------------------------------------------------------------------+
