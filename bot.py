import logging
import asyncio
import httpx
import math
import ujson as json  # ⚡ O'ta tezkor ujson
import os
import re
import time
from datetime import datetime, timedelta
from telegram import Update, BotCommand
from telegram.ext import ApplicationBuilder, ContextTypes, CommandHandler
from binance.client import Client
from binance.exceptions import BinanceAPIException

# ==========================================
# KONFIGURASIYA (VPN-siz, to'g'ridan-to'g'ri ulanish)
# ==========================================
TELEGRAM_BOT_TOKEN = "8845998489:AAHq7JqjYTfTjeAxAqowddVaOTnF8MHQKLg"
CHAT_ID = "143401417"

# Sizning Binance.bh API kalitlaringiz:
BINANCE_API_KEY = "slsc2BeHrBrrOpa1iIJbivQWEPXgAqADqFgY3Jzf6SRdYU8Ml1jwuiMsvkTjCJmf"
BINANCE_SECRET_KEY = "cW4Cx1PS38zpuuIhGbhu2eL0P1sfndmGMPxkmEjZplKrGIzEzifVOgkaO8AbhoRK"

PERCENT_TRIGGER = 0.8         # 🎯 Tebranish chegarasi 0.8%
LEVERAGE = 10                 # ⚡ 10x plecho
TAKE_PROFIT_USD = 3.5         # 🎯 Asosiy Take-Profit ($3.5)
STOP_LOSS_USD = 1.0           # 🛑 Har bir pozitsiya uchun zarar ($1.0)

# 📈 TRAILING PROFIT SOZLAMALARI
TRAILING_ACTIVATION = 2.0     # Foyda shu summadan oshsa trailing ishga tushadi ($2.0)
TRAILING_CALLBACK = 0.6       # Cho'qqidan shuncha dollar pastga tushsa foydani yopadi ($0.6)

COMMISSION_RATE = 0.0004      # Taker komissiya
MAX_POSITIONS = 2             # ⚖️ Bir vaqtning o'zida 2 ta pozitsiya (50/50 taqsimot)
STATE_FILE = "apex_sniper_bh_state.json"

# 🚫 QAT'IY BLOK QILINGAN TANGALAR
BLOCKED_COINS = ["TESTTESTUSDT", "TESTUSDT"]

total_capital = 100.0         
active_positions = {}          
hourly_stats = {}              
bot_running = False            
exchange_filters = {}          

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)

# 🌐 PROKSI YO'Q (VPN-siz, to'g'ridan-to'g'ri mijoz)
binance_client = Client(BINANCE_API_KEY, BINANCE_SECRET_KEY)

# 🔗 Binance.bh Futures API manzili
binance_client.API_URL = 'https://api.binance.bh/api'
binance_client.FUTURES_URL = 'https://fapi.binance.bh/fapi'

last_prices = {}

# ==========================================
# HOLATni SAQLASH VA YUKLASH
# ==========================================
def save_state():
    global total_capital, active_positions, hourly_stats, bot_running
    state = {
        "total_capital": total_capital,
        "active_positions": active_positions,
        "hourly_stats": hourly_stats,
        "bot_running": bot_running
    }
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(state, f)
    except Exception as e:
        logging.error(f"Holatni saqlashda xatolik: {e}")

def load_state():
    global total_capital, active_positions, hourly_stats, bot_running
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f:
                state = json.load(f)
                if "total_capital" in state:
                    total_capital = float(state["total_capital"])
                active_positions = state.get("active_positions", {})
                bot_running = state.get("bot_running", False)
                hourly_stats = state.get("hourly_stats", {})
        except Exception as e:
            logging.error(f"Holatni o'qishda xatolik: {e}")

def load_exchange_filters():
    global exchange_filters
    for attempt in range(3):
        try:
            info = binance_client.futures_exchange_info()
            for s in info['symbols']:
                symbol = s['symbol']
                for f in s['filters']:
                    if f['filterType'] == 'LOT_SIZE':
                        step_size = f['stepSize']
                        min_qty = float(f['minQty'])
                        max_qty = float(f['maxQty'])
                        decimals = len(step_size.rstrip('0').split('.')[1]) if '.' in step_size else 0
                        exchange_filters[symbol] = {'decimals': decimals, 'min_qty': min_qty, 'max_qty': max_qty}
            return
        except Exception as e:
            logging.error(f"Exchange info yuklashda xatolik (urinish {attempt+1}/3): {e}")
            time.sleep(2)

def format_quantity(symbol, raw_qty):
    if symbol not in exchange_filters:
        load_exchange_filters()

    f_info = exchange_filters.get(symbol)
    if not f_info: return None 

    decimals, min_qty, max_qty = f_info['decimals'], f_info['min_qty'], f_info['max_qty']
    safe_max = max_qty * 0.50 
    if raw_qty > safe_max: raw_qty = safe_max 
    if raw_qty < min_qty: return None 
    
    if decimals == 0: return int(raw_qty)
    formatted = math.floor(raw_qty * (10 ** decimals)) / (10 ** decimals)
    if formatted > max_qty:
        formatted = safe_max
        if decimals == 0: formatted = int(formatted)
        else: formatted = math.floor(formatted * (10 ** decimals)) / (10 ** decimals)
    return formatted

def get_all_usdt_prices():
    prices = {}
    for attempt in range(5):  # ⚡ 5 martagacha qayta urinish (Tarmoq barqarorligi uchun)
        try:
            for ticker in binance_client.futures_symbol_ticker():
                symbol = ticker['symbol']
                if symbol in BLOCKED_COINS: continue
                if symbol.endswith("USDT") and re.match("^[A-Z0-9]+USDT$", symbol):
                    if not any(x in symbol for x in ["UPUSDT", "DOWNUSDT", "BULLUSDT", "BEARUSDT"]):
                        prices[symbol] = float(ticker['price'])
            return prices
        except Exception as e:
            logging.warning(f"Narx olishda tarmoq xatoligi (urinish {attempt+1}/5): {e}. 3 soniyadan so'ng qayta uriniladi...")
            time.sleep(3)
    return {}

def get_global_market_trend(prices):
    btc_price = prices.get("BTCUSDT")
    btc_last = last_prices.get("BTCUSDT")
    if btc_price and btc_last:
        if btc_price > btc_last: return "BULLISH"
        elif btc_price < btc_last: return "BEARISH"
    return "NEUTRAL"

# ==========================================
# TELEGRAM BUYRUQLARI
# ==========================================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global last_prices, bot_running, total_capital
    load_state()  
    
    try:
        account_info = binance_client.futures_account()
        for asset in account_info.get('assets', []):
            if asset['asset'] == 'USDT':
                total_capital = float(asset['walletBalance'])
                break
    except Exception as e:
        logging.error(f"Balansni olishda xatolik: {e}")

    last_prices = get_all_usdt_prices()
    bot_running = True
    save_state()
    await update.message.reply_text(f"🎯 50/50 BINANCE.BH BOT (DIRECT) ISHGA TUSHDI!\n💰 Balans: {total_capital:.2f} USDT\n⚡ Plecho: {LEVERAGE}x\n⚖️ Max Pozitsiyalar: {MAX_POSITIONS} ta\n📈 Trailing Profit: Faol", parse_mode="Markdown")

async def stop_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global active_positions, total_capital, bot_running
    bot_running = False
    prices = get_all_usdt_prices()
    try:
        for sym, pos in list(active_positions.items()):
            side, cost, qty, entry, fee = pos['side'], pos['cost'], pos['qty'], pos['entry_price'], pos['open_fee']
            cur_p = prices.get(sym, entry)
            close_side = 'SELL' if side == 'BUY' else 'BUY'
            if side == 'BUY': pnl = cost * (((cur_p - entry) / entry) * LEVERAGE)
            else: pnl = cost * (((entry - cur_p) / entry) * LEVERAGE)
            close_fee = (cost * LEVERAGE) * COMMISSION_RATE
            net = pnl - (fee + close_fee)
            try: binance_client.futures_create_order(symbol=sym, side=close_side, type='MARKET', quantity=qty)
            except: pass
            total_capital = max(10.0, total_capital + net)
        active_positions.clear()
    except Exception as e:
        logging.error(f"Stop command error: {e}")
    save_state()
    await update.message.reply_text(f"🛑 BOT TO'XTATILDI VA BARCHA POZITSIYALAR YOPILDI!\n\n💰 Yakuniy Balans: {total_capital:.2f} USDT", parse_mode="Markdown")

async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    prices = get_all_usdt_prices()
    if not active_positions:
        pos_info = "Yo'q"
    else:
        lines = []
        for sym, pos in active_positions.items():
            entry = pos['entry_price']
            cost = pos['cost']
            side = pos['side']
            cur_p = prices.get(sym, entry)
            if side == 'BUY': pnl = cost * (((cur_p - entry) / entry) * LEVERAGE)
            else: pnl = cost * (((entry - cur_p) / entry) * LEVERAGE)
            highest = pos.get('highest_pnl', 0.0)
            lines.append(f"• {side} #{sym} (PnL: {pnl:+.2f}$ | Max: {highest:+.2f}$ | Margin: ${cost:.2f})")
        pos_info = "\n".join(lines)
    await update.message.reply_text(f"📊 BINANCE.BH STATUS ({LEVERAGE}x)\n\n📌 Ochiq Pozitsiyalar:\n{pos_info}\n\n💰 Jami Kapital: ~{total_capital:.2f}$", parse_mode="Markdown")

async def scan_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    prices = get_all_usdt_prices()
    trend = get_global_market_trend(prices)
    await update.message.reply_text(f"🔍 SKANER FAOL (Binance.bh)\n\n🌐 Global Trend: {trend}\n🎯 Triger: {PERCENT_TRIGGER}%\n💰 Kapital: {total_capital:.2f}$ | Ochiq pozitsiyalar: {len(active_positions)}/{MAX_POSITIONS}", parse_mode="Markdown")

async def timestats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global hourly_stats
    if not hourly_stats:
        await update.message.reply_text("🕒 Hozircha soatlar bo'yicha savdo statistikasi yig'ilmagan.", parse_mode="Markdown")
        return
    
    report = "🕒 **24 Soatlik Vaqt Statistikasi (Foyda/Zarar):**\n\n"
    for hour in sorted(hourly_stats.keys(), key=int):
        data = hourly_stats[hour]
        profit = data.get("profit", 0.0)
        loss = data.get("loss", 0.0)
        net = profit - loss
        icon = "🟢" if net >= 0 else "🔴"
        report += f"▫️ **{hour}:00 - {hour}:59** | {icon} Sof: {net:+.2f}$ (Foyda: +{profit:.2f}$ | Zarar: -{loss:.2f}$)\n"
    
    await update.message.reply_text(report, parse_mode="Markdown")

# ==========================================
# ASOSIY FON TIZIMI (BACKGROUND LOOP)
# ==========================================
async def sniper_analyzer(application):
    global last_prices, total_capital, active_positions, hourly_stats, bot_running
    load_state() 
    load_exchange_filters()
    last_prices = get_all_usdt_prices()
    await asyncio.sleep(3)
    
    while True:
        if not bot_running:
            await asyncio.sleep(2)
            continue
            
        prices = get_all_usdt_prices()
        global_trend = get_global_market_trend(prices)
        
        # --- 1. OCHIQ POZITSIYALARNI TEKSHIRISH VA YOPISH ---
        if active_positions:
            closed_symbols = []
            for sym, pos in list(active_positions.items()):
                side, cost, qty, entry, fee = pos['side'], pos['cost'], pos['qty'], pos['entry_price'], pos['open_fee']
                cur_p = prices.get(sym, entry)
                
                if side == 'BUY':
                    pnl = cost * (((cur_p - entry) / entry) * LEVERAGE)
                    close_side = 'SELL'
                else:
                    pnl = cost * (((entry - cur_p) / entry) * LEVERAGE)
                    close_side = 'BUY'
                    
                close_fee = (cost * LEVERAGE) * COMMISSION_RATE
                total_comm = fee + close_fee
                net_pnl = pnl - total_comm  

                # 📈 Trailing Profit eng yuqori nuqtani yangilash
                highest_pnl = pos.get('highest_pnl', 0.0)
                if net_pnl > highest_pnl:
                    pos['highest_pnl'] = net_pnl
                    highest_pnl = net_pnl
                    save_state()

                is_hit_sl = net_pnl <= -STOP_LOSS_USD
                is_hit_tp = net_pnl >= TAKE_PROFIT_USD
                is_hit_trailing = (highest_pnl >= TRAILING_ACTIVATION) and (net_pnl <= (highest_pnl - TRAILING_CALLBACK))

                if is_hit_sl or is_hit_tp or is_hit_trailing:
                    try: binance_client.futures_create_order(symbol=sym, side=close_side, type='MARKET', quantity=qty)
                    except: pass
                    
                    current_hour = str(datetime.now().hour)
                    if current_hour not in hourly_stats:
                        hourly_stats[current_hour] = {"profit": 0.0, "loss": 0.0}
                    
                    if net_pnl > 0:
                        hourly_stats[current_hour]["profit"] += net_pnl
                    else:
                        hourly_stats[current_hour]["loss"] += abs(net_pnl)
                    
                    total_capital = max(10.0, total_capital + net_pnl)
                    closed_symbols.append(sym)
                    save_state()
                    
                    if is_hit_sl:
                        status_icon = "🛑 STOP-LOSSGA YETDI VA YOPILDI:"
                        net_label = "Sof zarar"
                    elif is_hit_trailing:
                        status_icon = "📈 TRAILING PROFIT BILAN UShLANDI VA YOPILDI:"
                        net_label = "Sof foyda (Trailing)"
                    else:
                        status_icon = "🟢 TAKE-PROFIT BILAN SOF FOYDA YOPILDI:"
                        net_label = "Sof foyda"
                    
                    msg = (f"{status_icon} #{sym}\n"
                           f"- Yalpi natija: {pnl:+.2f}$\n"
                           f"- Komissiya: -{total_comm:.2f}$\n\n"
                           f"🔴 {net_label}: {net_pnl:+.2f} USDT\n\n"
                           f"💰 Qolgan Jami Kapital: {total_capital:.2f} USDT")
                    await application.bot.send_message(chat_id=CHAT_ID, text=msg, parse_mode="Markdown")

            for sym in closed_symbols:
                del active_positions[sym]

        # --- 2. YANGI POZITSIYA OCHISH ---
        if prices and last_prices and len(active_positions) < MAX_POSITIONS:
            best_coin, target_side = None, None
            
            if global_trend == "BULLISH":
                for symbol, cp in prices.items():
                    if symbol in active_positions: continue
                    lp = last_prices.get(symbol)
                    if lp:
                        pct = ((cp - lp) / lp) * 100
                        if pct <= -PERCENT_TRIGGER:
                            try:
                                t24 = binance_client.futures_ticker(symbol=symbol)
                                if float(t24.get('quoteVolume', 0)) < 1000000: continue
                            except: continue
                            best_coin, target_side = symbol, 'BUY'
                            break
                            
            elif global_trend == "BEARISH":
                for symbol, cp in prices.items():
                    if symbol in active_positions: continue
                    lp = last_prices.get(symbol)
                    if lp:
                        pct = ((cp - lp) / lp) * 100
                        if pct >= PERCENT_TRIGGER:
                            try:
                                t24 = binance_client.futures_ticker(symbol=symbol)
                                if float(t24.get('quoteVolume', 0)) < 1000000: continue
                            except: continue
                            best_coin, target_side = symbol, 'SELL'
                            break
                            
            if best_coin:
                cp = prices[best_coin]
                spend = round((total_capital / MAX_POSITIONS) * 0.95, 2)
                if spend < 10.0: spend = 10.0
                
                fee = (spend * LEVERAGE) * COMMISSION_RATE
                qty = format_quantity(best_coin, (spend * LEVERAGE) / cp)
                
                if qty is not None:
                    try:
                        binance_client.futures_change_leverage(symbol=best_coin, leverage=LEVERAGE)
                        side_binance = Client.SIDE_BUY if target_side == 'BUY' else Client.SIDE_SELL
                        binance_client.futures_create_order(symbol=best_coin, side=side_binance, type='MARKET', quantity=qty)
                        
                        active_positions[best_coin] = {
                            "symbol": best_coin, "side": target_side, "cost": spend, 
                            "entry_price": cp, "open_fee": fee, "qty": qty, "highest_pnl": 0.0
                        }
                        save_state()
                        
                        await application.bot.send_message(chat_id=CHAT_ID, text=f"🎯 SNIPER {target_side} {LEVERAGE}X OCHILDI: #{best_coin}\n- Narx: {cp}\n- Hajm: ${spend:.2f}", parse_mode="Markdown")
                    except Exception as e:
                        logging.error(f"Sniper order error: {e}")

        last_prices = prices
        await asyncio.sleep(3)

async def post_init(application):
    commands = [
        BotCommand("start", "Botni ishga tushirish"),
        BotCommand("stop", "Botni to'xtatish"),
        BotCommand("status", "Holatni tekshirish"),
        BotCommand("scan", "Skaner holati"),
        BotCommand("timestats", "24 soatlik vaqt statistikasi")
    ]
    await application.bot.set_my_commands(commands)
    asyncio.create_task(sniper_analyzer(application))

def main():
    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("stop", stop_command))
    app.add_handler(CommandHandler("status", status_command))
    app.add_handler(CommandHandler("scan", scan_command))
    app.add_handler(CommandHandler("timestats", timestats_command))
    print(f"Binance.bh Direct Bot ishga tushmoqda ({LEVERAGE}x rejim, Proxy yo'q)...")
    
    while True:
        try:
            app.run_polling(drop_pending_updates=True, timeout=30, poll_interval=1.0)
        except Exception as e:
            print(f"Tarmoq xatoligi: {e}. 5 soniyadan keyin qayta ulanamiz...")
            time.sleep(5)

if __name__ == "__main__":
    main()