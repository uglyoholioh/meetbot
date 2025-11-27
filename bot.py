import logging
import os
import json
import re
import urllib.parse
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters, CallbackQueryHandler
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
import uvicorn
from contextlib import asynccontextmanager
from supabase import create_client, Client

# --- CONFIGURATION ---
TOKEN = os.getenv("TOKEN")
WEB_APP_URL = os.getenv("WEB_APP_URL", "")

# --- SUPABASE SETUP ---
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
supabase: Client = None

if SUPABASE_URL and SUPABASE_KEY:
    supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
else:
    print("⚠️ WARNING: Supabase credentials missing. Data will not be saved permanently!")

logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

# --- DATABASE HELPERS ---
def get_event(event_id):
    """Fetches event data from Supabase"""
    if not supabase: return {}
    try:
        response = supabase.table("events").select("event_data").eq("id", event_id).execute()
        if response.data and len(response.data) > 0:
            return response.data[0]["event_data"]
        return None
    except Exception as e:
        logger.error(f"DB Load Error: {e}")
        return None

def save_event(event_id, data):
    """Saves/Updates event data to Supabase"""
    if not supabase: return
    try:
        # 'upsert' means Update if exists, Insert if new
        supabase.table("events").upsert({"id": event_id, "event_data": data}).execute()
    except Exception as e:
        logger.error(f"DB Save Error: {e}")

# --- COMMAND PARSER ---
def parse_command_input(text: str):
    if not text: return "", None, None
    parts = text.split(" ", 1)
    if len(parts) < 2: return "", None, None
    raw_args = parts[1].strip()
    date_pattern = r"(\d{4}-\d{2}-\d{2})"
    dates = re.findall(date_pattern, raw_args)
    start_date, end_date = None, None
    if len(dates) >= 2:
        start_date, end_date = dates[0], dates[1]
        name = raw_args.replace(start_date, "").replace(end_date, "").strip()
    elif len(dates) == 1:
        start_date = dates[0]
        name = raw_args.replace(start_date, "").strip()
    else:
        name = raw_args
    return name.strip(" ,"), start_date, end_date

# --- BOT LOGIC ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 **When2Meet Bot (Persistent)**\n\n"
        "To start, type:\n`/schedule <Event Name>`",
        parse_mode="Markdown"
    )

async def ask_event_mode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    event_name, start, end = parse_command_input(text)
    
    if not event_name:
        await update.message.reply_text("⚠️ Usage: `/schedule Team Dinner`", parse_mode="Markdown")
        return

    # Use a temporary ID for the setup phase
    setup_id = f"setup_{update.effective_chat.id}_{update.message.message_id}"
    
    # Save draft to DB temporarily
    draft_data = {"name": event_name, "start_date": start, "end_date": end, "is_draft": True}
    save_event(setup_id, draft_data)

    keyboard = [
        [
            InlineKeyboardButton("🕒 Time Slots", callback_data=f"setmode_time_{setup_id}"),
            InlineKeyboardButton("📅 Whole Days", callback_data=f"setmode_date_{setup_id}")
        ]
    ]
    await update.message.reply_text(
        f"⚙️ Setup for **{event_name}**:\nChoose Mode:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def finalize_event_creation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    # Data: ["setmode", "time", "setup_..."]
    parts = query.data.split("_")
    mode = parts[1]
    setup_id = "_".join(parts[2:])
    
    # Retrieve draft
    draft = get_event(setup_id)
    if not draft:
        draft = {"name": "New Event"} # Fallback

    # Create Real Event ID
    event_id = f"{query.message.chat.id}_{query.message.message_id}"
    
    final_event = {
        "name": draft.get("name"),
        "mode": mode,
        "start_date": draft.get("start_date"),
        "end_date": draft.get("end_date"),
        "votes": {}
    }
    
    # Save to Real ID
    save_event(event_id, final_event)

    # Encode for URL
    safe_name = urllib.parse.quote(final_event["name"])
    safe_id = urllib.parse.quote(event_id)
    full_url = f"{WEB_APP_URL}?eventId={safe_id}&eventName={safe_name}&mode={mode}"

    keyboard = [
        [InlineKeyboardButton("👉 Add Availability", web_app=WebAppInfo(url=full_url))],
        [InlineKeyboardButton("📊 View Results", callback_data=f"view_{event_id}")]
    ]
    
    await query.message.edit_text(
        f"🗓 **{final_event['name']}** created!\nTap below to vote.",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )

async def view_results(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    event_id = query.data.replace("view_", "")
    event = get_event(event_id)

    if not event:
        await query.message.reply_text("❌ Event not found in database.")
        return

    votes = event.get("votes", {})
    if not votes:
        await query.message.reply_text("❌ No votes recorded yet!")
        return

    # Calculate Top Slots
    slot_counts = {}
    total_users = len(votes)
    for user_votes in votes.values():
        for slot in user_votes:
            slot_counts[slot] = slot_counts.get(slot, 0) + 1

    sorted_slots = sorted(slot_counts.items(), key=lambda x: x[1], reverse=True)
    
    msg = f"📊 **{event['name']}** ({total_users} voted)\n\n🏆 **Best Times:**\n"
    days = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']
    
    for slot, count in sorted_slots[:5]:
        if event.get("mode") == "date":
            msg += f"• {slot}: {count}/{total_users}\n"
        else:
            try:
                d_idx, h = map(int, slot.split("-"))
                d_name = days[d_idx] if d_idx < 7 else "Day"
                msg += f"• {d_name} {h}:00 - {count}/{total_users}\n"
            except: pass

    await query.message.reply_text(msg, parse_mode="Markdown")

# --- FASTAPI & LIFESPAN ---
@asynccontextmanager
async def lifespan(app: FastAPI):
    if TOKEN:
        app_bot = Application.builder().token(TOKEN).build()
        app_bot.add_handler(CommandHandler("start", start))
        app_bot.add_handler(CommandHandler("schedule", ask_event_mode))
        app_bot.add_handler(CallbackQueryHandler(finalize_event_creation, pattern="^setmode_"))
        app_bot.add_handler(CallbackQueryHandler(view_results, pattern="^view_"))
        
        await app_bot.initialize()
        await app_bot.start()
        await app_bot.updater.start_polling()
        try: yield
        finally:
            await app_bot.updater.stop()
            await app_bot.stop()
            await app_bot.shutdown()
    else:
        yield

app = FastAPI(lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
)

@app.get("/")
async def serve_frontend():
    try:
        with open("index.html", "r") as f: return HTMLResponse(content=f.read())
    except FileNotFoundError: return HTMLResponse(content="<h1>Error: index.html not found</h1>", status_code=404)

@app.get("/get_event_data")
async def get_event_data(eventId: str):
    data = get_event(eventId)
    return data if data else {"error": "Event not found"}

@app.post("/submit_availability")
async def submit_availability(request: Request):
    data = await request.json()
    event_id = data.get("eventId")
    user_id = str(data.get("userId"))
    slots = data.get("slots")
    
    # 1. Get current event
    event = get_event(event_id)
    if not event:
        return {"status": "error", "message": "Event not found"}
    
    # 2. Update votes
    if "votes" not in event: event["votes"] = {}
    event["votes"][user_id] = slots
    
    # 3. Save back to DB
    save_event(event_id, event)
    
    return {"status": "success"}
