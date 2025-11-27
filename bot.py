import logging
import os
import json
import datetime
import re
import urllib.parse
import asyncio
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo, Chat
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters, CallbackQueryHandler
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
import uvicorn
from contextlib import asynccontextmanager

# --- CONFIGURATION ---
TOKEN = os.getenv("TOKEN")
WEB_APP_URL = os.getenv("WEB_APP_URL", "")
DATA_FILE = "storage.json"

logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

# --- PERSISTENCE ---
def load_data():
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r") as f:
            return json.load(f)
    return {}

def save_data(data):
    with open(DATA_FILE, "w") as f:
        json.dump(data, f)

events_db = load_data()

# --- BOT LOGIC ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not WEB_APP_URL:
        await update.message.reply_text("⚠️ **Config Error**: WEB_APP_URL missing.")
        return

    chat = update.effective_chat
    user = update.effective_user
    args = context.args or []
    
    # 1. GROUP CHAT: Send "Configure in Private" button
    if chat.type in [Chat.GROUP, Chat.SUPERGROUP]:
        # Need bot username for deep link
        bot_username = context.bot.username
        if not bot_username: # Fallback if not cached yet
            me = await context.bot.get_me()
            bot_username = me.username

        deep_link = f"https://t.me/{bot_username}?start=setup_{chat.id}"

        keyboard = [[InlineKeyboardButton("⚙️ Configure Event", url=deep_link)]]

        await update.message.reply_text(
            "👋 **Create Event**\n\nPlease configure the event in our private chat to avoid clutter.",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown"
        )
        return

    # 2. PRIVATE CHAT
    # Check for Deep Link Args
    if args and args[0].startswith("setup_"):
        # Setup Mode for a Group
        target_group_id = args[0].replace("setup_", "")

        setup_url = f"{WEB_APP_URL}?mode=setup&chatId={target_group_id}"
        web_app = WebAppInfo(url=setup_url)

        keyboard = [[InlineKeyboardButton("➕ Create Event", web_app=web_app)]]

        await update.message.reply_text(
            "📅 **Schedule New Event**\n\nTap below to set up your event details.",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown"
        )
        return

    if args and args[0].startswith("vote_"):
        # Voting Mode fallback (Deep Link)
        event_id = args[0].replace("vote_", "")

        safe_event_id = urllib.parse.quote(str(event_id))
        full_url = f"{WEB_APP_URL}?eventId={safe_event_id}&mode=time" # Defaulting mode if not known, but frontend will load it
        # Ideally we fetch event to get mode, but frontend handles it.
        web_app_vote = WebAppInfo(url=full_url)

        keyboard = [[InlineKeyboardButton("👉 Add Availability", web_app=web_app_vote)]]

        await update.message.reply_text(
            "📊 **Vote Now**\n\nTap below to add your availability.",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown"
        )
        return

    # 3. PRIVATE CHAT (No Args) - Main Menu
    setup_url = f"{WEB_APP_URL}?mode=setup&chatId={chat.id}"
    web_app = WebAppInfo(url=setup_url)

    keyboard = [
        [InlineKeyboardButton("➕ Create Event (Here)", web_app=web_app)],
        [InlineKeyboardButton("📅 Active Events", callback_data="list_active_events")],
        [InlineKeyboardButton("❓ Help", callback_data="show_help")]
    ]
    
    await update.message.reply_text(
        "👋 **When2Meet Bot**\n\nMain Menu:",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )

async def ask_event_mode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await start(update, context)

async def list_events_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await list_events_logic(query.message, update.effective_chat.id)

async def list_events_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await list_events_logic(update.message, update.effective_chat.id)

async def list_events_logic(message_obj, chat_id):
    active_events = []
    for eid, data in events_db.items():
        if not isinstance(data, dict): continue
        if str(data.get("chat_id")) == str(chat_id) or str(eid).startswith(str(chat_id)):
            active_events.append((eid, data.get("name", "Event")))

    if not active_events:
        await message_obj.reply_text("No active events found in this chat.")
        return

    msg = "📅 **Active Events:**\n\n"
    keyboard = []
    for eid, name in active_events[-5:]:
        msg += f"• {name}\n"
        keyboard.append([InlineKeyboardButton(f"View {name}", callback_data=f"view_{eid}")])
    
    await message_obj.reply_text(msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

async def help_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.message.reply_text(
        "ℹ️ **Help**\n\n"
        "• Use `/start` to access the menu.\n"
        "• In groups, clicking 'Create Event' will ask you to configure it privately.\n"
        "• Use `/attendance <Name>` for details.",
        parse_mode="Markdown"
    )

async def view_results(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    try:
        event_id = query.data.replace("view_", "")
        event = events_db.get(event_id)
    except:
        event = None

    if not event:
        await query.message.reply_text("❌ Event not found.")
        return

    votes = event.get("votes", {})
    if not votes:
        await query.message.reply_text("❌ No votes recorded yet!")
        return

    slot_counts = {}
    total_users = len(votes)
    
    for user, slots in votes.items():
        for slot in slots:
            slot_counts[slot] = slot_counts.get(slot, 0) + 1

    sorted_slots = sorted(slot_counts.items(), key=lambda x: x[1], reverse=True)
    
    msg = f"📊 **{event['name']}** ({total_users} voted)\n\n"
    
    if event.get("mode") == "date":
        msg += "🏆 **Best Dates:**\n"
        for slot, count in sorted_slots[:5]:
            msg += f"• {slot}: {count}/{total_users}\n"
    else:
        msg += "🏆 **Best Times:**\n"
        for slot, count in sorted_slots[:5]:
            try:
                if "-" in slot and len(slot.split("-")) >= 3:
                    parts = slot.split("-")
                    hour = parts[-1]
                    date_str = "-".join(parts[:-1])
                    msg += f"• {date_str} {hour}:00 : {count}/{total_users}\n"
                else:
                    days = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']
                    d_idx, hour = map(int, slot.split("-"))
                    day_name = days[d_idx] if d_idx < 7 else "Day"
                    msg += f"• {day_name} {hour}:00 : {count}/{total_users}\n"
            except:
                continue

    await query.message.reply_text(msg, parse_mode="Markdown")

async def check_attendance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if not args:
        await update.message.reply_text("Usage: `/attendance <Event Name>`", parse_mode="Markdown")
        return
    target_name = " ".join(args).lower()
    chat_id = update.effective_chat.id
    found_event = None
    for eid, data in events_db.items():
        if not isinstance(data, dict): continue
        if str(data.get("chat_id")) == str(chat_id) or str(eid).startswith(str(chat_id)):
            if target_name in data.get("name", "").lower():
                found_event = data; break
    if not found_event:
        await update.message.reply_text("❌ Event not found.")
        return
    votes = found_event.get("votes", {})
    if not votes:
        await update.message.reply_text(f"No votes for **{found_event['name']}** yet.", parse_mode="Markdown")
        return
    msg = f"📝 **Attendance for {found_event['name']}**\n\n"
    slot_map = {}
    for user_id, slots in votes.items():
        for slot in slots:
            if slot not in slot_map: slot_map[slot] = []
            slot_map[slot].append(user_id)
    sorted_slots = sorted(slot_map.items())
    msg += "\n**Who is available when:**\n"
    for slot, users in sorted_slots[:15]:
        user_list = ", ".join([f"User {u[-4:]}" for u in users])
        msg += f"• {slot}: {len(users)} ppl ({user_list})\n"
    await update.message.reply_text(msg, parse_mode="Markdown")

# --- FASTAPI SERVER ---
@asynccontextmanager
async def lifespan(app: FastAPI):
    if TOKEN:
        application = Application.builder().token(TOKEN).build()

        # Initialize Bot so we can get username
        await application.initialize()
        await application.start()

        # Cache username
        try:
            me = await application.bot.get_me()
            application.bot.username = me.username # Manually attach if not there
        except:
            pass

        app.state.bot_app = application

        application.add_handler(CommandHandler("start", start))
        application.add_handler(CommandHandler("schedule", ask_event_mode))
        application.add_handler(CommandHandler("events", list_events_command))
        application.add_handler(CommandHandler("attendance", check_attendance))

        application.add_handler(CallbackQueryHandler(list_events_callback, pattern="^list_active_events$"))
        application.add_handler(CallbackQueryHandler(help_callback, pattern="^show_help$"))
        application.add_handler(CallbackQueryHandler(view_results, pattern="^view_"))

        await application.updater.start_polling()
        try:
            yield
        finally:
            await application.updater.stop()
            await application.stop()
            await application.shutdown()
    else:
        logger.warning("No TOKEN found. Bot functionality is disabled, but API is running.")
        yield

app = FastAPI(lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/")
async def serve_frontend():
    try:
        with open("index.html", "r") as f:
            return HTMLResponse(content=f.read())
    except FileNotFoundError:
        return HTMLResponse(content="<h1>Error: index.html not found</h1>", status_code=404)

@app.get("/get_event_data")
async def get_event_data(eventId: str):
    event = events_db.get(eventId)
    if not event: return {"error": "Event not found"}
    return event

@app.post("/submit_availability")
async def submit_availability(request: Request):
    data = await request.json()
    event_id = data.get("eventId")
    user_id = str(data.get("userId"))
    slots = data.get("slots")
    if event_id not in events_db: return {"status": "error", "message": "Event not found"}
    events_db[event_id]["votes"][user_id] = slots
    save_data(events_db)
    return {"status": "success"}

@app.post("/create_event")
async def create_event(request: Request):
    data = await request.json()
    event_name = data.get("name", "New Event")
    mode = data.get("mode", "time")
    start_date = data.get("start_date")
    end_date = data.get("end_date")
    chat_id = data.get("chat_id")

    if not chat_id: return {"status": "error", "message": "Missing chat_id"}

    import time, random
    event_id = f"{chat_id}_{int(time.time())}_{random.randint(100,999)}"

    events_db[event_id] = {
        "name": event_name,
        "mode": mode,
        "start_date": start_date,
        "end_date": end_date,
        "chat_id": chat_id,
        "votes": {}
    }
    save_data(events_db)

    if hasattr(app.state, "bot_app"):
        bot = app.state.bot_app.bot
        safe_event_id = urllib.parse.quote(str(event_id))
        full_url = f"{WEB_APP_URL}?eventId={safe_event_id}&mode={mode}"
        web_app_vote = WebAppInfo(url=full_url)

        view_btn = InlineKeyboardButton("📊 View Results", callback_data=f"view_{event_id}")

        mode_text = "Hourly Slots" if mode == "time" else "Whole Dates"
        if start_date: mode_text += f" ({start_date}...)"

        # Try sending Web App button. If fails (Group restriction?), fallback to Deep Link.
        try:
            keyboard = [[InlineKeyboardButton("👉 Add Availability", web_app=web_app_vote)], [view_btn]]
            await bot.send_message(
                chat_id=chat_id,
                text=f"🗓 **{event_name}**\nMode: {mode_text}\n\nTap below to vote!",
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode="Markdown"
            )
        except Exception as e:
            logger.warning(f"Failed to send Web App button: {e}. Falling back to Deep Link.")
            if "Button_type_invalid" in str(e) or "Bad Request" in str(e):
                # Fallback: Deep Link to Private
                # Need bot username
                me = await bot.get_me()
                username = me.username
                deep_link = f"https://t.me/{username}?start=vote_{event_id}"
                keyboard = [[InlineKeyboardButton("👉 Vote Here", url=deep_link)], [view_btn]]

                await bot.send_message(
                    chat_id=chat_id,
                    text=f"🗓 **{event_name}**\nMode: {mode_text}\n\nTap below to vote (opens in private chat)!",
                    reply_markup=InlineKeyboardMarkup(keyboard),
                    parse_mode="Markdown"
                )
            else:
                 return {"status": "error", "message": str(e)}

    return {"status": "success", "event_id": event_id}
