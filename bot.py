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
        name = raw_args.replace(start_date, "").replace(end_date, "").strip()
    elif len(dates) == 1:
        start_date = dates[0]
        name = raw_args.replace(start_date, "").strip()
    else:
        name = raw_args

    name = name.strip(" ,")
    return name, start_date, end_date

# --- BOT LOGIC ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 **When2Meet Bot**\n\n"
        "Commands:\n"
        "`/schedule <Name> [Dates]` - Create new event\n"
        "`/events` - List active events\n"
        "`/attendance <Name>` - View details\n\n"
        "Examples:\n"
        "`/schedule Team Dinner`\n"
        "`/schedule Trip 2023-12-01 2023-12-05`",
        parse_mode="Markdown"
    )

async def ask_event_mode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    event_name, start_date, end_date = parse_command_input(text)
    
    if not event_name:
        await update.message.reply_text("⚠️ **Missing Event Name**\nUsage: `/schedule Dinner`")
        return

    setup_id = f"{update.effective_chat.id}_{update.message.message_id}"
    
    events_db[f"draft_{setup_id}"] = {
        "name": event_name,
        "start_date": start_date,
        "end_date": end_date,
        "chat_id": update.effective_chat.id # Store chat ID for /events listing
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
        "Do you want to schedule by specific **Time Slots** or **Whole Days**?",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )

async def finalize_event_creation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        query = update.callback_query
        await query.answer()

        # Robust ID reconstruction
        data = query.data.split("_") # ["setmode", "time", "ChatID", "MsgID"]
        # Join all parts after 'time'/'date' to support complex IDs if any
        mode = data[1]
        setup_id = "_".join(data[2:])

        draft_key = f"draft_{setup_id}"
        draft_data = events_db.get(draft_key)

        if not draft_data:
            logger.warning(f"Draft not found for key: {draft_key}")
            # Try to recover info from context or generic default
            event_name = "New Event"
            start_date = None
            end_date = None
            chat_id = query.message.chat.id
        else:
            if isinstance(draft_data, str): # Legacy support
                event_name = draft_data
                start_date = None
                end_date = None
                chat_id = query.message.chat.id
            else:
                event_name = draft_data.get("name", "New Event")
                start_date = draft_data.get("start_date")
                end_date = draft_data.get("end_date")
                chat_id = draft_data.get("chat_id", query.message.chat.id)

            del events_db[draft_key]

        # Create Real Event ID
        event_id = f"{query.message.chat.id}_{query.message.message_id}"

        events_db[event_id] = {
            "name": event_name,
            "mode": mode,
            "start_date": start_date,
            "end_date": end_date,
            "chat_id": chat_id,
            "votes": {}
        }
        save_data(events_db)

        safe_event_id = urllib.parse.quote(str(event_id))
        full_url = f"{WEB_APP_URL}?eventId={safe_event_id}&mode={mode}"
        web_app = WebAppInfo(url=full_url)

        view_btn = InlineKeyboardButton("📊 View Results", callback_data=f"view_{event_id}")
        keyboard = [[InlineKeyboardButton("👉 Add Availability", web_app=web_app)], [view_btn]]

        mode_text = "Hourly Slots" if mode == "time" else "Whole Dates"
        if start_date: mode_text += f" ({start_date}...)"

        await query.message.edit_text(
            f"🗓 **{event_name}**\nMode: {mode_text}\n\nTap below to vote!",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.error(f"Error in finalize_event_creation: {e}", exc_info=True)
        # Attempt to inform user if possible
        try:
            await query.message.reply_text("❌ An error occurred setting up the event. Please try again.")
        except:
            pass

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
            # slot is either "YYYY-MM-DD-HH" (new) or "d-h" (old)
            try:
                if "-" in slot and len(slot.split("-")) >= 3: # Date format
                    # 2023-11-27-09
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

async def list_events(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Command: /events - Lists active events in this chat"""
    chat_id = update.effective_chat.id
    active_events = []

    for eid, data in events_db.items():
        if not isinstance(data, dict): continue
        if str(data.get("chat_id")) == str(chat_id) or str(eid).startswith(str(chat_id)):
            active_events.append((eid, data.get("name", "Event")))

    if not active_events:
        await update.message.reply_text("No active events found in this chat.")
        return

    msg = "📅 **Active Events:**\n\n"
    keyboard = []
    for eid, name in active_events[-5:]: # Show last 5
        msg += f"• {name}\n"
        keyboard.append([InlineKeyboardButton(f"View {name}", callback_data=f"view_{eid}")])

    await update.message.reply_text(msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

async def check_attendance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Command: /attendance <EventName>"""
    args = context.args
    if not args:
        await update.message.reply_text("Usage: `/attendance <Event Name>`", parse_mode="Markdown")
        return

    target_name = " ".join(args).lower()
    chat_id = update.effective_chat.id
    found_event = None

    # Find event
    for eid, data in events_db.items():
        if not isinstance(data, dict): continue
        if str(data.get("chat_id")) == str(chat_id) or str(eid).startswith(str(chat_id)):
            if target_name in data.get("name", "").lower():
                found_event = data
                break

    if not found_event:
        await update.message.reply_text("❌ Event not found.")
        return

    votes = found_event.get("votes", {})
    if not votes:
        await update.message.reply_text(f"No votes for **{found_event['name']}** yet.", parse_mode="Markdown")
        return

    msg = f"📝 **Attendance for {found_event['name']}**\n\n"
    for user_id, slots in votes.items():
        # Ideally we'd resolve user_id to name, but bot API limits this without caching.
        # We'll just list the slots for now, or "User {id}"
        count = len(slots)
        msg += f"👤 User {user_id}: {count} slots selected\n"
        # Detailed listing might be too long, just summary?
        # User asked for "attendance of who picked what dates".
        # Let's try to group by dates?

    # Alternative: List by Date
    # "2023-11-01: User A, User B"
    slot_map = {}
    for user_id, slots in votes.items():
        for slot in slots:
            if slot not in slot_map: slot_map[slot] = []
            slot_map[slot].append(user_id)

    sorted_slots = sorted(slot_map.items())
    msg += "\n**Who is available when:**\n"
    for slot, users in sorted_slots[:15]: # Limit to avoid spam
        user_list = ", ".join([f"User {u[-4:]}" for u in users]) # Obfuscate ID
        msg += f"• {slot}: {len(users)} ppl ({user_list})\n"

    await update.message.reply_text(msg, parse_mode="Markdown")

# --- FASTAPI SERVER ---
@asynccontextmanager
async def lifespan(app: FastAPI):
    if TOKEN:
        application = Application.builder().token(TOKEN).build()

        application.add_handler(CommandHandler("start", start))
        application.add_handler(CommandHandler("schedule", ask_event_mode))
        application.add_handler(CommandHandler("events", list_events))
        application.add_handler(CommandHandler("attendance", check_attendance))

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
