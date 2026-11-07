//+------------------------------------------------------------------+
//|                                        TradeNotifierTelegram.mq5 |
//|  Envía un mensaje a Telegram por cada trade de la cuenta:        |
//|  aperturas, cierres (con ganancia/pérdida), cambios de SL/TP     |
//|  y depósitos/retiros. No abre ni cierra operaciones.             |
//+------------------------------------------------------------------+
#property copyright   "emorales24c"
#property version     "1.00"
#property description "Notifica a Telegram cada trade de la cuenta. No opera."

//--- Parámetros
input string InpBotToken      = "";    // Token del bot (de @BotFather)
input string InpChatId        = "";    // Tu chat ID (de @userinfobot)
input bool   InpNotifyOpen    = true;  // Avisar aperturas
input bool   InpNotifyClose   = true;  // Avisar cierres
input bool   InpNotifyModify  = true;  // Avisar cambios de SL/TP
input bool   InpNotifyBalance = true;  // Avisar depósitos / retiros
input bool   InpSendTest      = true;  // Mensaje de prueba al iniciar

#define MAX_ATTEMPTS 10   // reintentos por mensaje si falla el envío
#define MAX_PER_TIMER 5   // mensajes enviados como máximo por segundo

//--- Cola de mensajes (se envían desde OnTimer para no bloquear y poder reintentar)
string g_queue[];
int    g_attempts[];

//+------------------------------------------------------------------+
int OnInit()
  {
   if(StringLen(InpBotToken) == 0 || StringLen(InpChatId) == 0)
     {
      Alert("TradeNotifierTelegram: escribe el Token del bot y tu Chat ID en los parámetros.");
      return(INIT_PARAMETERS_INCORRECT);
     }
   if(MQLInfoInteger(MQL_TESTER))
      Print("TradeNotifierTelegram: WebRequest no funciona en el Probador de Estrategias.");

   EventSetTimer(1);

   if(InpSendTest)
      Enqueue("✅ <b>Notificador conectado</b>\n"
              + "Cuenta: " + IntegerToString(AccountInfoInteger(ACCOUNT_LOGIN)) + "\n"
              + "Servidor: " + Esc(AccountInfoString(ACCOUNT_SERVER)) + "\n"
              + "Balance: " + Money(AccountInfoDouble(ACCOUNT_BALANCE), false));
   return(INIT_SUCCEEDED);
  }

//+------------------------------------------------------------------+
void OnDeinit(const int reason)
  {
   EventKillTimer();
  }

//+------------------------------------------------------------------+
void OnTick()
  {
  }

//+------------------------------------------------------------------+
void OnTimer()
  {
   for(int i = 0; i < MAX_PER_TIMER && ArraySize(g_queue) > 0; i++)
     {
      int code = SendTelegram(g_queue[0]);
      if(code == 200)
        {
         RemoveFirst();
         continue;
        }
      g_attempts[0]++;
      if(g_attempts[0] >= MAX_ATTEMPTS)
        {
         Print("TradeNotifierTelegram: mensaje descartado tras ", MAX_ATTEMPTS, " intentos.");
         RemoveFirst();
        }
      break; // si falló, esperar al siguiente segundo
     }
  }

//+------------------------------------------------------------------+
void OnTradeTransaction(const MqlTradeTransaction &trans,
                        const MqlTradeRequest &request,
                        const MqlTradeResult &result)
  {
   if(trans.type == TRADE_TRANSACTION_DEAL_ADD)
      HandleDeal(trans.deal);
   else if(trans.type == TRADE_TRANSACTION_POSITION && InpNotifyModify)
      HandleModify(trans);
  }

//+------------------------------------------------------------------+
//| Nuevo deal: apertura, cierre, reversa o balance                  |
//+------------------------------------------------------------------+
void HandleDeal(const ulong deal)
  {
   if(deal == 0)
      return;
   if(!HistoryDealSelect(deal))
     {
      HistorySelect(TimeCurrent() - 7 * 86400, TimeCurrent() + 86400);
      if(!HistoryDealSelect(deal))
        {
         Print("TradeNotifierTelegram: no se encontró el deal #", deal);
         return;
        }
     }

   ENUM_DEAL_TYPE  type  = (ENUM_DEAL_TYPE)HistoryDealGetInteger(deal, DEAL_TYPE);
   ENUM_DEAL_ENTRY entry = (ENUM_DEAL_ENTRY)HistoryDealGetInteger(deal, DEAL_ENTRY);
   string account = IntegerToString(AccountInfoInteger(ACCOUNT_LOGIN));
   string when    = TimeToString((datetime)HistoryDealGetInteger(deal, DEAL_TIME), TIME_DATE | TIME_MINUTES);

   //--- Depósitos / retiros
   if(type == DEAL_TYPE_BALANCE || type == DEAL_TYPE_CREDIT)
     {
      if(!InpNotifyBalance)
         return;
      double amount = HistoryDealGetDouble(deal, DEAL_PROFIT);
      string title  = (amount >= 0) ? "Depósito" : "Retiro";
      Enqueue("💵 <b>" + title + "</b> " + Money(amount, true) + "\n"
              + "Balance: " + Money(AccountInfoDouble(ACCOUNT_BALANCE), false) + "\n"
              + "Cuenta: " + account + "\n" + when);
      return;
     }

   if(type != DEAL_TYPE_BUY && type != DEAL_TYPE_SELL)
      return;

   string symbol = HistoryDealGetString(deal, DEAL_SYMBOL);
   int    digits = (int)SymbolInfoInteger(symbol, SYMBOL_DIGITS);
   double volume = HistoryDealGetDouble(deal, DEAL_VOLUME);
   double price  = HistoryDealGetDouble(deal, DEAL_PRICE);
   ulong  posId  = (ulong)HistoryDealGetInteger(deal, DEAL_POSITION_ID);
   long   reason = HistoryDealGetInteger(deal, DEAL_REASON);
   string side   = (type == DEAL_TYPE_BUY) ? "BUY" : "SELL";
   string arrow  = (type == DEAL_TYPE_BUY) ? "▲" : "▼";
   string vol    = DoubleToString(volume, 2);

   //--- Apertura
   if(entry == DEAL_ENTRY_IN)
     {
      if(!InpNotifyOpen)
         return;
      double sl = HistoryDealGetDouble(deal, DEAL_SL);
      double tp = HistoryDealGetDouble(deal, DEAL_TP);
      if(PositionSelectByTicket(posId))
        {
         if(sl == 0)
            sl = PositionGetDouble(POSITION_SL);
         if(tp == 0)
            tp = PositionGetDouble(POSITION_TP);
        }
      Enqueue("⚡ <b>Trade abierto</b>\n"
              + arrow + " <b>" + side + " " + vol + " " + Esc(symbol) + "</b>\n"
              + "Precio: " + Px(price, digits) + "\n"
              + "SL: " + Px(sl, digits) + " | TP: " + Px(tp, digits) + "\n"
              + "Origen: " + ReasonText(reason) + "\n"
              + "Cuenta: " + account + " | #" + IntegerToString((long)posId) + "\n"
              + when);
      return;
     }

   //--- Cierre (total o parcial), cierre por opuesta o reversa
   if(entry == DEAL_ENTRY_OUT || entry == DEAL_ENTRY_OUT_BY || entry == DEAL_ENTRY_INOUT)
     {
      if(!InpNotifyClose)
         return;
      double net = HistoryDealGetDouble(deal, DEAL_PROFIT)
                   + HistoryDealGetDouble(deal, DEAL_COMMISSION)
                   + HistoryDealGetDouble(deal, DEAL_SWAP)
                   + HistoryDealGetDouble(deal, DEAL_FEE);

      // El deal de cierre es contrario a la posición: un SELL cierra un BUY.
      string posSide   = (type == DEAL_TYPE_BUY) ? "SELL" : "BUY";
      double openPrice = OpenPriceOf(posId);

      string title = "Trade cerrado";
      string extra = "";
      if(entry == DEAL_ENTRY_INOUT)
         title = "Reversa de posición";
      else if(PositionSelectByTicket(posId))
        {
         title = "Cierre parcial";
         extra = "Queda abierto: " + DoubleToString(PositionGetDouble(POSITION_VOLUME), 2) + " lotes\n";
        }

      string icon = (net >= 0) ? "✅" : "❌";
      Enqueue(icon + " <b>" + title + "</b>  " + Money(net, true) + "\n"
              + "<b>" + posSide + " " + vol + " " + Esc(symbol) + "</b>\n"
              + "Entrada: " + Px(openPrice, digits) + " → Salida: " + Px(price, digits) + "\n"
              + "Motivo: " + ReasonText(reason) + "\n"
              + extra
              + "Balance: " + Money(AccountInfoDouble(ACCOUNT_BALANCE), false) + "\n"
              + "Cuenta: " + account + " | #" + IntegerToString((long)posId) + "\n"
              + when);
     }
  }

//+------------------------------------------------------------------+
//| Cambio de SL/TP de una posición abierta                           |
//+------------------------------------------------------------------+
void HandleModify(const MqlTradeTransaction &trans)
  {
   string symbol = trans.symbol;
   int    digits = (int)SymbolInfoInteger(symbol, SYMBOL_DIGITS);
   string desc   = Esc(symbol);
   if(PositionSelectByTicket(trans.position))
     {
      string side = (PositionGetInteger(POSITION_TYPE) == POSITION_TYPE_BUY) ? "BUY" : "SELL";
      desc = side + " " + DoubleToString(PositionGetDouble(POSITION_VOLUME), 2) + " " + Esc(symbol);
     }
   Enqueue("✏ <b>SL/TP modificado</b>\n"
           + "<b>" + desc + "</b> #" + IntegerToString((long)trans.position) + "\n"
           + "SL: " + Px(trans.price_sl, digits) + " | TP: " + Px(trans.price_tp, digits));
  }

//+------------------------------------------------------------------+
//| Precio de entrada de una posición (primer deal IN)               |
//+------------------------------------------------------------------+
double OpenPriceOf(const ulong posId)
  {
   if(!HistorySelectByPosition(posId))
      return(0);
   int total = HistoryDealsTotal();
   for(int i = 0; i < total; i++)
     {
      ulong d = HistoryDealGetTicket(i);
      if(d == 0)
         continue;
      long e = HistoryDealGetInteger(d, DEAL_ENTRY);
      if(e == DEAL_ENTRY_IN || e == DEAL_ENTRY_INOUT)
         return(HistoryDealGetDouble(d, DEAL_PRICE));
     }
   return(0);
  }

//+------------------------------------------------------------------+
string ReasonText(const long reason)
  {
   switch((int)reason)
     {
      case DEAL_REASON_SL:       return("Stop Loss");
      case DEAL_REASON_TP:       return("Take Profit");
      case DEAL_REASON_SO:       return("Stop Out (margen)");
      case DEAL_REASON_CLIENT:   return("Manual (PC)");
      case DEAL_REASON_MOBILE:   return("Manual (móvil)");
      case DEAL_REASON_WEB:      return("Manual (web)");
      case DEAL_REASON_EXPERT:   return("Expert Advisor");
      case DEAL_REASON_ROLLOVER: return("Rollover");
      case DEAL_REASON_VMARGIN:  return("Margen variable");
      case DEAL_REASON_SPLIT:    return("Split");
     }
   return("Otro");
  }

//+------------------------------------------------------------------+
//| Formato                                                          |
//+------------------------------------------------------------------+
string Px(const double value, const int digits)
  {
   if(value == 0)
      return("—");
   return(DoubleToString(value, digits));
  }

string Money(const double value, const bool withSign)
  {
   string s = DoubleToString(value, 2);
   if(withSign && value > 0)
      s = "+" + s;
   return(s + " " + AccountInfoString(ACCOUNT_CURRENCY));
  }

// Escapa caracteres especiales para parse_mode=HTML de Telegram
string Esc(string s)
  {
   StringReplace(s, "&", "&amp;");
   StringReplace(s, "<", "&lt;");
   StringReplace(s, ">", "&gt;");
   return(s);
  }

//+------------------------------------------------------------------+
//| Cola                                                             |
//+------------------------------------------------------------------+
void Enqueue(const string text)
  {
   int n = ArraySize(g_queue);
   ArrayResize(g_queue, n + 1);
   ArrayResize(g_attempts, n + 1);
   g_queue[n]    = text;
   g_attempts[n] = 0;
  }

void RemoveFirst()
  {
   int n = ArraySize(g_queue);
   for(int i = 1; i < n; i++)
     {
      g_queue[i - 1]    = g_queue[i];
      g_attempts[i - 1] = g_attempts[i];
     }
   ArrayResize(g_queue, n - 1);
   ArrayResize(g_attempts, n - 1);
  }

//+------------------------------------------------------------------+
//| Envío a Telegram                                                 |
//+------------------------------------------------------------------+
int SendTelegram(const string text)
  {
   string url  = "https://api.telegram.org/bot" + InpBotToken + "/sendMessage";
   string body = "chat_id=" + UrlEncode(InpChatId)
                 + "&parse_mode=HTML&disable_web_page_preview=true"
                 + "&text=" + UrlEncode(text);

   char post[];
   int len = StringToCharArray(body, post, 0, WHOLE_ARRAY, CP_UTF8);
   if(len > 0)
      ArrayResize(post, len - 1); // quitar el 0 final

   char   result[];
   string resultHeaders;
   ResetLastError();
   int code = WebRequest("POST", url,
                         "Content-Type: application/x-www-form-urlencoded\r\n",
                         5000, post, result, resultHeaders);
   if(code == -1)
     {
      int err = GetLastError();
      if(err == 4014)
         Print("TradeNotifierTelegram: WebRequest no permitido. Ve a Herramientas > Opciones > "
               "Asesores Expertos, marca 'Permitir WebRequest' y agrega https://api.telegram.org");
      else
         Print("TradeNotifierTelegram: error de WebRequest ", err);
     }
   else if(code != 200)
      Print("TradeNotifierTelegram: Telegram respondió ", code, ": ",
            CharArrayToString(result, 0, WHOLE_ARRAY, CP_UTF8));
   return(code);
  }

string UrlEncode(const string s)
  {
   uchar bytes[];
   int n = StringToCharArray(s, bytes, 0, WHOLE_ARRAY, CP_UTF8);
   string out = "";
   for(int i = 0; i < n; i++)
     {
      uchar c = bytes[i];
      if(c == 0)
         break;
      if((c >= 'A' && c <= 'Z') || (c >= 'a' && c <= 'z') || (c >= '0' && c <= '9')
         || c == '-' || c == '_' || c == '.' || c == '~')
         out += CharToString(c);
      else
         out += StringFormat("%%%02X", c);
     }
   return(out);
  }
//+------------------------------------------------------------------+
