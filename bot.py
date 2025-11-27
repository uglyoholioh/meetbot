import logging
import os
import json
import datetime
import re
import urllib.parse
import asyncio
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
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

    # Pass chat_id to Web App setup mode so backend knows where to send the result
    chat_id = update.effective_chat.id
    setup_url = f"{WEB_APP_URL}?mode=setup&chatId={chat_id}"
    web_app = WebAppInfo(url=setup_url)
    
    keyboard = [
        [InlineKeyboardButton("➕ Create Event", web_app=web_app)],
        [InlineKeyboardButton("📅 Active Events", callback_data="list_active_events")],
        [InlineKeyboardButton("❓ Help", callback_data="show_help")]
    ]
    
    await update.message.reply_text(
        "👋 **When2Meet Bot**\n\nWhat would you like to do?",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )

async def ask_event_mode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Backward compatibility for /schedule
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
        "• Click 'Create Event' to schedule.\n"
        "• Click 'Active Events' to see polls.\n"
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
        # Create bot instance
        application = Application.builder().token(TOKEN).build()
        app.state.bot_app = application # Store in app state

        application.add_handler(CommandHandler("start", start))
        application.add_handler(CommandHandler("schedule", ask_event_mode))
        application.add_handler(CommandHandler("events", list_events_command))
        application.add_handler(CommandHandler("attendance", check_attendance))

        application.add_handler(CallbackQueryHandler(list_events_callback, pattern="^list_active_events$"))
        application.add_handler(CallbackQueryHandler(help_callback, pattern="^show_help$"))
        application.add_handler(CallbackQueryHandler(view_results, pattern="^view_"))

        await application.initialize()
        await application.start()
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
    """
    Receives event data from Web App and triggers bot message to chat.
    """
    data = await request.json()

    event_name = data.get("name", "New Event")
    mode = data.get("mode", "time")
    start_date = data.get("start_date")
    end_date = data.get("end_date")
    chat_id = data.get("chat_id")

    if not chat_id:
        return {"status": "error", "message": "Missing chat_id"}

    # Create Event ID
    # Use timestamp + random or similar since we don't have message_id here
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

    # Send Message to Chat
    # Need to access the bot instance
    if hasattr(app.state, "bot_app"):
        bot = app.state.bot_app.bot

        safe_event_id = urllib.parse.quote(str(event_id))
        full_url = f"{WEB_APP_URL}?eventId={safe_event_id}&mode={mode}"
        web_app_vote = WebAppInfo(url=full_url)

        view_btn = InlineKeyboardButton("📊 View Results", callback_data=f"view_{event_id}")
        keyboard = [[InlineKeyboardButton("👉 Add Availability", web_app=web_app_vote)], [view_btn]]

        mode_text = "Hourly Slots" if mode == "time" else "Whole Dates"
        if start_date: mode_text += f" ({start_date}...)"

        try:
            await bot.send_message(
                chat_id=chat_id,
                text=f"🗓 **{event_name}**\nMode: {mode_text}\n\nTap below to vote!",
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode="Markdown"
            )
        except Exception as e:
            logger.error(f"Failed to send message: {e}")
            return {"status": "error", "message": str(e)}

    return {"status": "success", "event_id": event_id}
