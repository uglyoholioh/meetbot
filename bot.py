import logging
import os
import json
import datetime
import re
import urllib.parse
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

# --- HELPERS ---
def parse_command_input(text: str):
    """
    Extracts event name and optional date range (YYYY-MM-DD) from input.
    Returns (name, start_date, end_date)
    """
    if not text:
        return "", None, None
    parts = text.split(" ", 1)
    if len(parts) < 2:
        return "", None, None

    raw_args = parts[1].strip()

    # Regex for YYYY-MM-DD
    date_pattern = r"(\d{4}-\d{2}-\d{2})"
    dates = re.findall(date_pattern, raw_args)

    start_date = None
    end_date = None

    if len(dates) >= 2:
        start_date = dates[0]
        end_date = dates[1]
        # Remove dates from name (simple replace might be risky if name contains date, but acceptable for now)
        name = raw_args.replace(start_date, "").replace(end_date, "").strip()
    elif len(dates) == 1:
        start_date = dates[0]
        name = raw_args.replace(start_date, "").strip()
    else:
        name = raw_args

    # Clean up extra spaces/commas if user typed "Name, Date"
    name = name.strip(" ,")

    return name, start_date, end_date

# --- BOT LOGIC ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 **When2Meet Bot**\n\n"
        "To start, type:\n`/schedule <Event Name> [Start Date] [End Date]`\n\n"
        "Examples:\n"
        "`/schedule Team Dinner` (Defaults to next 35 days)\n"
        "`/schedule Trip 2023-12-01 2023-12-05` (Specific range)",
        parse_mode="Markdown"
    )

async def ask_event_mode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Step 1: User types /schedule, Bot asks for Mode (Time vs Date)"""
    text = update.message.text
    event_name, start_date, end_date = parse_command_input(text)
    
    if not event_name:
        await update.message.reply_text(
            "⚠️ **Missing Event Name**\n\n"
            "Usage: `/schedule Team Dinner`\n"
            "Try typing it again!", 
            parse_mode="Markdown"
        )
        return

    # Create a temp ID for this interaction
    setup_id = f"{update.effective_chat.id}_{update.message.message_id}"
    
    # Store the draft data
    events_db[f"draft_{setup_id}"] = {
        "name": event_name,
        "start_date": start_date,
        "end_date": end_date
    }
    save_data(events_db)

    keyboard = [
        [
            InlineKeyboardButton("🕒 Time Slots", callback_data=f"setmode_time_{setup_id}"),
            InlineKeyboardButton("📅 Whole Days", callback_data=f"setmode_date_{setup_id}")
        ]
    ]
    
    date_info = ""
    if start_date:
        date_info = f"\n(Range: {start_date}"
        if end_date:
            date_info += f" to {end_date}"
        date_info += ")"

    await update.message.reply_text(
        f"⚙️ Setup for **{event_name}**{date_info}:\n\n"
        "Do you want to schedule by specific **Time Slots** (e.g., 9:00 AM) or **Whole Days**?",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )

async def finalize_event_creation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Step 2: User clicks mode, Bot creates the final event"""
    query = update.callback_query
    await query.answer()
    
    data = query.data.split("_") # ["setmode", "time", "setup_ID"]
    mode = data[1]
    setup_id = "_".join(data[2:]) # Reconstruct ID
    
    draft_key = f"draft_{setup_id}"
    draft_data = events_db.get(draft_key)
    
    if not draft_data:
        # Fallback if draft is missing (unlikely unless restart)
        event_name = "New Event"
        start_date = None
        end_date = None
    else:
        # Handle if draft was old string format (backward compatibility) or new dict
        if isinstance(draft_data, str):
            event_name = draft_data
            start_date = None
            end_date = None
        else:
            event_name = draft_data.get("name", "New Event")
            start_date = draft_data.get("start_date")
            end_date = draft_data.get("end_date")

        del events_db[draft_key]
    
    # Create Real Event ID
    event_id = f"{query.message.chat.id}_{query.message.message_id}"
    
    events_db[event_id] = {
        "name": event_name, 
        "mode": mode,
        "start_date": start_date,
        "end_date": end_date,
        "votes": {}
    }
    save_data(events_db)

    # Generate Web App URL with Mode
    # URL Encode parameters to ensure they work in Group Chats
    safe_name = urllib.parse.quote(event_name)
    safe_event_id = urllib.parse.quote(event_id) # Should be safe but good practice

    full_url = f"{WEB_APP_URL}?eventId={safe_event_id}&eventName={safe_name}&mode={mode}"

    web_app = WebAppInfo(url=full_url)
    
    view_btn = InlineKeyboardButton("📊 View Results", callback_data=f"view_{event_id}")

    keyboard = [
        [InlineKeyboardButton("👉 Add Availability", web_app=web_app)],
        [view_btn]
    ]
    
    mode_text = "Hourly Slots" if mode == "time" else "Whole Dates"
    if start_date:
        mode_text += f" ({start_date}...)"
    
    await query.message.edit_text(
        f"🗓 **{event_name}**\n"
        f"Mode: {mode_text}\n\n"
        "Tap below to vote!",
        reply_markup=InlineKeyboardMarkup(keyboard), 
        parse_mode="Markdown"
    )

async def view_results(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    # Parse ID: view_ChatID_MsgID
    try:
        event_id = query.data.replace("view_", "")
        event = events_db.get(event_id)
    except:
        event = None

    if not event:
        await query.message.reply_text("❌ Event not found (it might be old).")
        return

    votes = event.get("votes", {})
    if not votes:
        await query.message.reply_text("❌ No votes recorded yet!")
        return

    # Tally Votes
    slot_counts = {}
    total_users = len(votes)
    
    for user, slots in votes.items():
        for slot in slots:
            slot_counts[slot] = slot_counts.get(slot, 0) + 1

    sorted_slots = sorted(slot_counts.items(), key=lambda x: x[1], reverse=True)
    
    # Format Message
    msg = f"📊 **{event['name']}** ({total_users} voted)\n\n"
    
    if event.get("mode") == "date":
        # Date Mode Display
        msg += "🏆 **Best Dates:**\n"
        for slot, count in sorted_slots[:5]:
            # slot is "YYYY-MM-DD"
            msg += f"• {slot}: {count}/{total_users} votes\n"
    else:
        # Time Mode Display
        days = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']
        msg += "🏆 **Best Times:**\n"
        for slot, count in sorted_slots[:5]:
            # slot is "dayIndex-hour" (e.g., "0-9")
            try:
                d_idx, hour = map(int, slot.split("-"))
                day_name = days[d_idx] if d_idx < 7 else "Day"
                msg += f"• {day_name} {hour}:00 : {count}/{total_users}\n"
            except:
                continue

    await query.message.reply_text(msg, parse_mode="Markdown")

# --- FASTAPI SERVER ---
@asynccontextmanager
async def lifespan(app: FastAPI):
    if TOKEN:
        application = Application.builder().token(TOKEN).build()

        application.add_handler(CommandHandler("start", start))
        application.add_handler(CommandHandler("schedule", ask_event_mode))
        application.add_handler(CallbackQueryHandler(finalize_event_creation, pattern="^setmode_"))
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
    """API for the Web App to fetch event config and votes"""
    event = events_db.get(eventId)
    if not event:
        return {"error": "Event not found"}
    return event

@app.post("/submit_availability")
async def submit_availability(request: Request):
    data = await request.json()
    event_id = data.get("eventId")
    user_id = str(data.get("userId"))
    slots = data.get("slots")
    
    if event_id not in events_db:
        return {"status": "error", "message": "Event not found"}
    
    events_db[event_id]["votes"][user_id] = slots
    save_data(events_db)
    return {"status": "success"}
