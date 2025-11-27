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
    # Deprecated but kept for backward compatibility if needed
    if not text: return "", None, None
    parts = text.split(" ", 1)
    if len(parts) < 2: return "", None, None
    raw_args = parts[1].strip()
    date_pattern = r"(\d{4}-\d{2}-\d{2})"
    dates = re.findall(date_pattern, raw_args)
    start_date, end_date = None, None
    if len(dates) >= 2: start_date, end_date = dates[0], dates[1]; name = raw_args.replace(start_date, "").replace(end_date, "").strip()
    elif len(dates) == 1: start_date = dates[0]; name = raw_args.replace(start_date, "").strip()
    else: name = raw_args
    return name.strip(" ,"), start_date, end_date

# --- BOT LOGIC ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 **When2Meet Bot**\n\n"
        "To create an event, tap `/schedule` and use the button!",
        parse_mode="Markdown"
    )

async def ask_event_mode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    New Flow: Sends a 'Create Event' button that opens the Web App in setup mode.
    """
    if not WEB_APP_URL:
        await update.message.reply_text("⚠️ **Configuration Error**: `WEB_APP_URL` is missing. Please set it in env vars.")
        return

    # Web App URL for Setup
    setup_url = f"{WEB_APP_URL}?mode=setup"
    web_app = WebAppInfo(url=setup_url)
    
    keyboard = [[InlineKeyboardButton("➕ Create Event", web_app=web_app)]]
    
    await update.message.reply_text(
        "📅 **Schedule New Event**\n\nTap below to set up your event details.",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )

async def handle_web_app_data(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Receives data from the Web App Setup Form (Name, Mode, Dates)
    and creates the event.
    """
    try:
        data = json.loads(update.effective_message.web_app_data.data)

        event_name = data.get("name", "New Event")
        mode = data.get("mode", "time")
        start_date = data.get("start_date")
        end_date = data.get("end_date")

        # Create Event ID
        # Using message_id might be tricky if multiple people click.
        # But `update.message` is the service message sent by the user.
        event_id = f"{update.effective_chat.id}_{update.effective_message.message_id}"

        events_db[event_id] = {
            "name": event_name,
            "mode": mode,
            "start_date": start_date,
            "end_date": end_date,
            "chat_id": update.effective_chat.id,
            "votes": {}
        }
        save_data(events_db)

        # Now send the Voting Message to the chat
        safe_event_id = urllib.parse.quote(str(event_id))
        full_url = f"{WEB_APP_URL}?eventId={safe_event_id}&mode={mode}"
        web_app_vote = WebAppInfo(url=full_url)

        view_btn = InlineKeyboardButton("📊 View Results", callback_data=f"view_{event_id}")
        keyboard = [[InlineKeyboardButton("👉 Add Availability", web_app=web_app_vote)], [view_btn]]

        mode_text = "Hourly Slots" if mode == "time" else "Whole Dates"
        if start_date: mode_text += f" ({start_date}...)"

        await update.message.reply_text(
            f"🗓 **{event_name}**\nMode: {mode_text}\n\nTap below to vote!",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown"
        )

    except Exception as e:
        logger.error(f"Error handling Web App data: {e}", exc_info=True)
        await update.message.reply_text("❌ Failed to create event. Invalid data received.")


async def finalize_event_creation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Kept for backward compatibility or if we reuse text-based setup,
    # but likely unused in new flow.
    try:
        query = update.callback_query
        await query.answer()
        # ... (Old logic, optional to keep) ...
        await query.message.edit_text("⚠️ This setup method is deprecated. Please use /schedule button.")
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

async def list_events(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
    for eid, name in active_events[-5:]:
        msg += f"• {name}\n"
        keyboard.append([InlineKeyboardButton(f"View {name}", callback_data=f"view_{eid}")])

    await update.message.reply_text(msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

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

        application.add_handler(CommandHandler("start", start))
        application.add_handler(CommandHandler("schedule", ask_event_mode))
        application.add_handler(CommandHandler("events", list_events))
        application.add_handler(CommandHandler("attendance", check_attendance))

        # Handler for Web App Data (Setup Form)
        application.add_handler(MessageHandler(filters.StatusUpdate.WEB_APP_DATA, handle_web_app_data))

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
