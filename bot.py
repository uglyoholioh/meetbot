import logging
import os
from typing import Dict, List, Any
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters
from fastapi import FastAPI, Request
import uvicorn
from contextlib import asynccontextmanager

# --- CONFIGURATION ---
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")  # Set your bot token in environment variable
# The URL where your index.html is hosted (e.g., Vercel/GitHub Pages)
WEB_APP_URL = "https://meetbot-omega.vercel.app" 

# --- LOGGING ---
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- IN-MEMORY DATABASE (Replace with SQLite/Postgres for production) ---
# Structure: {event_id: {user_id: [selected_slots]}}
events_db: Dict[str, Dict[str, List[str]]] = {}

# --- BOT LOGIC ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Sends a welcome message with instructions."""
    await update.message.reply_text(
        "👋 Hi! I'm the When2Meet bot.\n\n"
        "To start a new scheduling event, use the command /schedule <Event Name>\n"
        "Example: /schedule Team Dinner"
    )

async def create_schedule(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Creates a new event and sends the Web App button."""
    if not context.args:
        await update.message.reply_text("Please provide an event name. Usage: /schedule <Name>")
        return

    event_name = " ".join(context.args)
    chat_id = update.effective_chat.id
    event_id = f"{chat_id}_{update.message.message_id}"  # Simple unique ID
    
    # Initialize event in DB
    events_db[event_id] = {}

    # Create the Web App Button
    # We pass the event_id as a query parameter so the frontend knows which event it is
    web_app = WebAppInfo(url=f"{WEB_APP_URL}?eventId={event_id}&eventName={event_name}")
    
    keyboard = [
        [InlineKeyboardButton("📅 Add My Availability", web_app=web_app)],
        [InlineKeyboardButton("📊 View Results", callback_data=f"view_{event_id}")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.message.reply_text(
        f"🗓 **New Event: {event_name}**\n\n"
        "Click the button below to mark your available times!",
        reply_markup=reply_markup,
        parse_mode="Markdown"
    )

async def web_app_data_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles data sent back from the Web App (The 'Save' button in the grid)."""
    # Note: Service messages with web_app_data are tricky; 
    # usually, the Mini App sends data via `sendData` which appears as a service message.
    if update.effective_message.web_app_data:
        data = update.effective_message.web_app_data.data
        # Parse logic here would normally handle the specific data format from the JS
        await update.message.reply_text(f"Received availability data! (Length: {len(data)})")

# --- FASTAPI SERVER (For handling Web App POST requests) ---
# Since the Mini App needs to send complex JSON data (availability arrays), 
# it's often better to POST to an API rather than using Telegram's sendData for large payloads.

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: Initialize Bot
    application = Application.builder().token(TOKEN).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("schedule", create_schedule))
    application.add_handler(MessageHandler(filters.StatusUpdate.WEB_APP_DATA, web_app_data_handler))
    
    await application.initialize()
    await application.start()
    await application.updater.start_polling() # Simplest for local dev, use webhook for Prod
    yield
    # Shutdown
    await application.updater.stop()
    await application.stop()
    await application.shutdown()

app = FastAPI(lifespan=lifespan)

@app.post("/submit_availability")
async def submit_availability(request: Request):
    """API Endpoint for the Mini App to save data."""
    data = await request.json()
    event_id = data.get("eventId")
    user_id = data.get("userId")
    slots = data.get("slots") # List of time slots e.g. ["Mon-09:00", "Mon-09:30"]

    if event_id not in events_db:
        events_db[event_id] = {}
    
    events_db[event_id][user_id] = slots
    
    logger.info(f"Updated availability for user {user_id} in event {event_id}")
    return {"status": "success", "participant_count": len(events_db[event_id])}

# To run: uvicorn bot:app --reload
