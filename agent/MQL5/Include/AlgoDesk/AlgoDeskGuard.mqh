//+------------------------------------------------------------------+
//| AlgoDeskGuard.mqh                                                |
//| Shared entry guard for every EA on an AlgoDesk-managed account.  |
//| Call AlgoDesk_EntryAllowed(magic) right before each OrderSend /  |
//| CTrade.Buy/Sell that OPENS a position. Closing / modifying       |
//| existing positions never needs the guard.                        |
//|                                                                  |
//| State lives in terminal Global Variables written by              |
//| AlgoDeskAgent.mq5, so it survives EA restarts and keeps working  |
//| when the backend or internet connection is down.                 |
//+------------------------------------------------------------------+
#ifndef ALGODESK_GUARD_MQH
#define ALGODESK_GUARD_MQH

// Define ALGODESK_FAIL_CLOSED before including this file to block
// entries when the agent has not written a baseline for the current day.
// #define ALGODESK_FAIL_CLOSED

enum ENUM_AD_ENTRY
  {
   AD_ENTRY_OK = 0,
   AD_ENTRY_LIMIT,        // daily loss limit reached
   AD_ENTRY_PAUSED,       // manual pause from dashboard
   AD_ENTRY_MAGIC_OFF,    // this magic number disabled from dashboard
   AD_ENTRY_NO_BASELINE,  // agent not running / no baseline today (fail-closed only)
   AD_ENTRY_SESSION       // outside the trading sessions allowed from the dashboard
  };

//--- Global Variable names are scoped per account login
string AD_Key(const string name)
  {
   return StringFormat("AD_%I64d_%s", AccountInfoInteger(ACCOUNT_LOGIN), name);
  }

double AD_Get(const string name, const double def = 0.0)
  {
   string k = AD_Key(name);
   if(!GlobalVariableCheck(k))
      return def;
   return GlobalVariableGet(k);
  }

void AD_Set(const string name, const double value)
  {
   GlobalVariableSet(AD_Key(name), value);
  }

string AD_MagicKey(const long magic)
  {
   return StringFormat("M%I64d_OFF", magic);
  }

//--- true when the stored baseline belongs to the current trading day
bool AD_BaselineFresh()
  {
   double dayStart = AD_Get("DAY", 0.0);
   if(dayStart <= 0.0 || AD_Get("BASE", 0.0) <= 0.0)
      return false;
   long age = (long)TimeTradeServer() - (long)dayStart;
   return (age >= 0 && age < 86400);
  }

ENUM_AD_ENTRY AlgoDesk_Check(const long magic)
  {
   if(AD_BaselineFresh())
     {
      // Recompute locally so the limit holds even between agent timer ticks.
      double baseline = AD_Get("BASE", 0.0);
      double limitPct = AD_Get("LIMIT_PCT", 0.0);
      double equity   = AccountInfoDouble(ACCOUNT_EQUITY);
      if(limitPct > 0.0 && baseline - equity >= baseline * limitPct / 100.0)
         return AD_ENTRY_LIMIT;
      if(AD_Get("BLOCK", 0.0) > 0.5)
         return AD_ENTRY_LIMIT;
     }
   else
     {
#ifdef ALGODESK_FAIL_CLOSED
      return AD_ENTRY_NO_BASELINE;
#endif
     }
   if(AD_Get("PAUSE", 0.0) > 0.5)
      return AD_ENTRY_PAUSED;
   if(magic != 0 && AD_Get(AD_MagicKey(magic), 0.0) > 0.5)
      return AD_ENTRY_MAGIC_OFF;
   if(AD_Get("SESS", 0.0) > 0.5)
      return AD_ENTRY_SESSION;
   return AD_ENTRY_OK;
  }

bool AlgoDesk_EntryAllowed(const long magic)
  {
   return AlgoDesk_Check(magic) == AD_ENTRY_OK;
  }

string AlgoDesk_ReasonText(const ENUM_AD_ENTRY r)
  {
   switch(r)
     {
      case AD_ENTRY_OK:          return "entry diizinkan";
      case AD_ENTRY_LIMIT:       return "batas rugi harian tercapai";
      case AD_ENTRY_PAUSED:      return "entry dijeda dari dashboard";
      case AD_ENTRY_MAGIC_OFF:   return "magic number dinonaktifkan dari dashboard";
      case AD_ENTRY_NO_BASELINE: return "agent belum menetapkan baseline hari ini";
      case AD_ENTRY_SESSION:     return "di luar sesi trading yang diizinkan";
     }
   return "tidak diketahui";
  }

#endif
//+------------------------------------------------------------------+
