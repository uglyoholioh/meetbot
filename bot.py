import logging
import os
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
import uvicorn
from contextlib import asynccontextmanager

# --- CONFIGURATION ---
TOKEN = os.getenv("TOKEN")
# This should match your Render Service URL (e.g., https://myapp.onrender.com)
WEB_APP_URL = os.getenv("WEB_APP_URL", "")

logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

# In-memory storage (Resets on restart)
events_db = {}

# --- BOT LOGIC ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("👋 Hi! Use /schedule <Name> to start a new poll.")

async def create_schedule(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Please provide a name. Usage: /schedule <Event Name>")
        return

    event_name = " ".join(context.args)
    # Create a unique ID for this event
    event_id = f"{update.effective_chat.id}_{update.message.message_id}"
    events_db[event_id] = {}

    # This URL opens the Mini App
    full_url = f"{WEB_APP_URL}?eventId={event_id}&eventName={event_name}"
    
    web_app = WebAppInfo(url=full_url)
    
    keyboard = [[InlineKeyboardButton("📅 Add Availability", web_app=web_app)]]
    await update.message.reply_text(
        f"🗓 **{event_name}**\nClick below to add your times!", 
        reply_markup=InlineKeyboardMarkup(keyboard), 
        parse_mode="Markdown"
    )

async def web_app_data_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles service messages (rarely used in this specific setup but good to have)"""
    if update.effective_message.web_app_data:
        await update.message.reply_text("Data received via Service Message!")

# --- FASTAPI SERVER ---
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: Run the bot
    application = Application.builder().token(TOKEN).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("schedule", create_schedule))
    application.add_handler(MessageHandler(filters.StatusUpdate.WEB_APP_DATA, web_app_data_handler))
    
    await application.initialize()
    await application.start()
    await application.updater.start_polling()
    yield
    # Shutdown: Stop the bot
    await application.updater.stop()
    await application.stop()
    await application.shutdown()

app = FastAPI(lifespan=lifespan)

# Allow the frontend to talk to the backend
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- THE NEW PART: SERVE THE HTML FILE ---
@app.get("/")
async def serve_frontend():
    """Reads index.html from the disk and sends it to the browser"""
    # Ensure index.html exists in the same folder
    try:
        with open("index.html", "r") as f:
            return HTMLResponse(content=f.read())
    except FileNotFoundError:
        return HTMLResponse(content="<h1>Error: index.html not found on server</h1>", status_code=404)

@app.post("/submit_availability")
async def submit_availability(request: Request):
    """API Endpoint that the HTML file talks to"""
    data = await request.json()
    event_id = data.get("eventId")
    user_id = data.get("userId")
    slots = data.get("slots")
    
    if event_id not in events_db:
        events_db[event_id] = {}
    
    events_db[event_id][user_id] = slots
    logger.info(f"Saved data for user {user_id} in event {event_id}")
    return {"status": "success", "count": len(events_db[event_id])}
