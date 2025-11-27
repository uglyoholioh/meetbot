import logging
import os
import json
import datetime
import re
import urllib.parse
import asyncio
import io
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import seaborn as sns
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo, Chat, InputFile
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

# --- HELPER FUNCTIONS ---

def generate_heatmap_image(event_data):
    """
    Generates a heatmap image for the event using matplotlib/seaborn.
    Returns bytes of the image.
    """
    votes = event_data.get("votes", {})
    mode = event_data.get("mode", "time")

    if not votes:
        return None

    # Aggregate scores
    slot_scores = {}

    for user_votes in votes.values():
        # Handle new format (dict) and legacy format (list)
        if isinstance(user_votes, list):
             for slot in user_votes:
                 slot_scores[slot] = slot_scores.get(slot, 0) + 1.0
        elif isinstance(user_votes, dict):
            # If it's the wrapper dict {slots: ..., username: ...}
            if "slots" in user_votes:
                user_votes = user_votes["slots"]

            if isinstance(user_votes, list):
                 for slot in user_votes:
                     slot_scores[slot] = slot_scores.get(slot, 0) + 1.0
            elif isinstance(user_votes, dict):
                for slot, type_val in user_votes.items():
                    weight = 1.0 if type_val == 'yes' else 0.5
                    slot_scores[slot] = slot_scores.get(slot, 0) + weight

    if not slot_scores:
        return None

    # Prepare data for plotting
    sorted_slots = sorted(slot_scores.keys())

    plt.figure(figsize=(10, 6))

    # Logic differs slightly for Time Grid vs Date Grid
    # For simplicity, we'll plot a bar chart or a simple grid depending on data structure.
    # A true heatmap requires mapping slots to X/Y coordinates.

    if mode == "date":
        # Sort by date
        try:
            dates = sorted(slot_scores.keys())
            scores = [slot_scores[d] for d in dates]

            sns.barplot(x=dates, y=scores, palette="viridis")
            plt.xticks(rotation=45)
            plt.title(f"Availability for {event_data.get('name')}")
            plt.ylabel("Score (Yes=1, Maybe=0.5)")
        except:
            return None
    else:
        # Time Grid: Try to parse Day/Time
        # Format: "YYYY-MM-DD-H"
        # We can group by Day (X) and Time (Y)
        try:
            data_points = []
            for slot, score in slot_scores.items():
                parts = slot.split('-')
                if len(parts) >= 2:
                    hour = int(parts[-1])
                    date_str = "-".join(parts[:-1])
                    data_points.append({"Date": date_str, "Hour": hour, "Score": score})

            if not data_points:
                return None

            import pandas as pd
            df = pd.DataFrame(data_points)
            pivot_table = df.pivot(index="Hour", columns="Date", values="Score")

            sns.heatmap(pivot_table, cmap="YlGnBu", annot=True, fmt=".1f")
            plt.title(f"Availability Heatmap: {event_data.get('name')}")
            plt.xlabel("Date")
            plt.ylabel("Hour")
        except Exception as e:
            logger.error(f"Heatmap generation error: {e}")
            # Fallback to simple bar
            keys = list(slot_scores.keys())
            vals = list(slot_scores.values())
            sns.barplot(x=keys, y=vals)

    buf = io.BytesIO()
    plt.savefig(buf, format='png', bbox_inches='tight')
    buf.seek(0)
    plt.close()
    return buf

# --- BOT LOGIC ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not WEB_APP_URL:
        await update.message.reply_text("⚠️ **Config Error**: WEB_APP_URL missing.")
        return

    chat = update.effective_chat
    args = context.args or []

    # Check for Deep Link Args (setup_ or vote_)
    if args:
        if args[0].startswith("setup_"):
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

        if args[0].startswith("vote_"):
            event_id = args[0].replace("vote_", "")
            safe_event_id = urllib.parse.quote(str(event_id))
            full_url = f"{WEB_APP_URL}?eventId={safe_event_id}&mode=time"
            web_app_vote = WebAppInfo(url=full_url)
            keyboard = [[InlineKeyboardButton("👉 Add Availability", web_app=web_app_vote)]]
            await update.message.reply_text(
                "📊 **Vote Now**\n\nTap below to add your availability.",
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode="Markdown"
            )
            return

    # Standard Menu

    # Logic: If Group Chat -> Use Deep Link to redirect to Private Chat for Web App
    # If Private Chat -> Use Web App Button directly

    if chat.type != Chat.PRIVATE:
        # Group Chat: Send URL Button
        bot_username = context.bot.username or (await context.bot.get_me()).username
        deep_link = f"https://t.me/{bot_username}?start=setup_{chat.id}"
        keyboard = [
            [InlineKeyboardButton("➕ Create Event", url=deep_link)],
            [InlineKeyboardButton("📅 Active Events", callback_data="list_active_events")],
            [InlineKeyboardButton("❓ Help", callback_data="show_help")]
        ]
    else:
        # Private Chat: Send Web App Button
        setup_url = f"{WEB_APP_URL}?mode=setup&chatId={chat.id}"
        web_app = WebAppInfo(url=setup_url)
        keyboard = [
            [InlineKeyboardButton("➕ Create Event", web_app=web_app)],
            [InlineKeyboardButton("📅 Active Events", callback_data="list_active_events")],
            [InlineKeyboardButton("❓ Help", callback_data="show_help")]
        ]

    await update.message.reply_text(
        "👋 **When2Meet Bot**\n\nMain Menu:",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )

async def ask_event_mode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Handles /schedule. Parses mentions and starts setup.
    """
    args = context.args
    mentions = [w for w in args if w.startswith("@")]
    
    # Generate temporary setup session
    import time, random
    setup_id = f"{update.effective_chat.id}_{int(time.time())}_{random.randint(100,999)}"
    
    # Store pending participants if any
    if mentions:
        events_db[f"setup_{setup_id}"] = mentions
        save_data(events_db)
        msg_text = f"📅 **Schedule Event**\nParticipants: {', '.join(mentions)}\n\nClick below to configure:"
    else:
        msg_text = "📅 **Schedule Event**\n\nClick below to configure:"

    chat = update.effective_chat
    if chat.type != Chat.PRIVATE:
        bot_username = context.bot.username or (await context.bot.get_me()).username
        deep_link = f"https://t.me/{bot_username}?start=setup_{chat.id}"
        keyboard = [[InlineKeyboardButton("⚙️ Configure Event", url=deep_link)]]
    else:
        setup_url = f"{WEB_APP_URL}?mode=setup&chatId={chat.id}&setupId={setup_id}"
        web_app = WebAppInfo(url=setup_url)
        keyboard = [[InlineKeyboardButton("⚙️ Configure Event", web_app=web_app)]]
    
    await update.message.reply_text(
        msg_text,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )

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
        "• Use `/schedule @user` to start.\n"
        "• Click 'Create Event' to schedule.\n"
        "• Click 'Active Events' to see polls.\n",
        parse_mode="Markdown"
    )

async def view_results(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    # Don't answer yet, might take time to generate image

    try:
        event_id = query.data.replace("view_", "")
        event = events_db.get(event_id)
    except:
        event = None

    if not event:
        await query.answer("Event not found", show_alert=True)
        return

    # Generate Image
    img_buf = generate_heatmap_image(event)

    votes = event.get("votes", {})
    total_users = len(votes)
    
    msg = f"📊 **{event['name']}** ({total_users} voted)\n"

    # Check missing participants
    req_participants = event.get("required_participants", [])

    keyboard = []

    # Add Availability Button logic
    # Since View Results works in groups, we must check if we are in a group to decide button type.
    # However, 'view_results' is a callback. We can check query.message.chat.type
    chat = query.message.chat
    bot_username = context.bot.username or (await context.bot.get_me()).username

    # Logic: Always provide "Add Availability" button
    if chat.type != Chat.PRIVATE:
        deep_link = f"https://t.me/{bot_username}?start=vote_{event_id}"
        keyboard.append([InlineKeyboardButton("👉 Add Availability", url=deep_link)])
    else:
        safe_event_id = urllib.parse.quote(str(event_id))
        full_url = f"{WEB_APP_URL}?eventId={safe_event_id}&mode={event.get('mode', 'time')}"
        web_app_vote = WebAppInfo(url=full_url)
        keyboard.append([InlineKeyboardButton("👉 Add Availability", web_app=web_app_vote)])

    if req_participants:
        keyboard.append([InlineKeyboardButton("🔔 Nudge Missing", callback_data=f"nudge_{event_id}")])

    # Send Photo if generated, else text
    try:
        if img_buf:
            await query.message.reply_photo(
                photo=img_buf,
                caption=msg,
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode="Markdown"
            )
        else:
            await query.message.reply_text(msg + "\n(No data to visualize)", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
    except Exception as e:
        logger.error(f"Error sending results: {e}")
        await query.message.reply_text("Error generating results.", parse_mode="Markdown")

    await query.answer()

async def nudge_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    event_id = query.data.replace("nudge_", "")
    event = events_db.get(event_id)
    if not event: return

    req = set(event.get("required_participants", []))
    if not req:
        await query.message.reply_text("No specific participants required.")
        return

    # We need to filter out those who voted.
    # Currently votes key is UserID. We don't have Usernames.
    # To fix this, we need to store Username in the vote payload from frontend.
    # The frontend knows the user's username? tg.initDataUnsafe.user.username
    
    # We will implement logic assuming we can compare.
    # For now, since we can't perfectly map ID to Username without storing it,
    # we'll list ALL required participants and say "Waiting on..."
    # But that's annoying.
    # Better: Update /submit_availability to store username.

    voted_usernames = set()
    for uid, data in event.get("votes", {}).items():
        # Check if we stored username. If not, we can't filter.
        # We will update submit_availability to store it.
        if isinstance(data, dict) and "username" in data:
            voted_usernames.add(f"@{data['username']}")

    missing = [p for p in req if p not in voted_usernames]

    if missing:
        await query.message.reply_text(f"🔔 **Waiting on:**\n{' '.join(missing)}", parse_mode="Markdown")
    else:
        await query.message.reply_text("🎉 Everyone has voted!")

async def check_attendance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Same as before but updated for new structure
    # ... (simplified for brevity, main focus is on heatmap)
    await update.message.reply_text("Use 'View Results' for the heatmap!", parse_mode="Markdown")

# --- FASTAPI SERVER ---
@asynccontextmanager
async def lifespan(app: FastAPI):
    if TOKEN:
        application = Application.builder().token(TOKEN).build()
        app.state.bot_app = application

        application.add_handler(CommandHandler("start", start))
        application.add_handler(CommandHandler("schedule", ask_event_mode))
        application.add_handler(CommandHandler("events", list_events_command))
        application.add_handler(CommandHandler("attendance", check_attendance))

        application.add_handler(CallbackQueryHandler(list_events_callback, pattern="^list_active_events$"))
        application.add_handler(CallbackQueryHandler(help_callback, pattern="^show_help$"))
        application.add_handler(CallbackQueryHandler(view_results, pattern="^view_"))
        application.add_handler(CallbackQueryHandler(nudge_callback, pattern="^nudge_"))

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
    username = data.get("username") # NEW
    slots = data.get("slots") # Now a dict {slotId: type}

    if event_id not in events_db: return {"status": "error", "message": "Event not found"}
    
    # We store the slots AND the username in a wrapper dict if possible,
    # but the current structure is votes[user_id] = slots.
    # We should change structure to votes[user_id] = {slots: ..., username: ...}
    # OR just keep slots as the value if we only care about slots.
    # But for Nudge we need username.
    # Let's change votes structure?
    # events_db[eid]["votes"][uid] = {"slots": slots, "username": username}
    # This will break existing heatmap logic if not handled.
    
    # Updated logic to support this:
    events_db[event_id]["votes"][user_id] = {
        "slots": slots,
        "username": username
    }
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
    setup_id = data.get("setup_id")

    if not chat_id:
        return {"status": "error", "message": "Missing chat_id"}

    # Check for pending participants
    required_participants = []
    if setup_id:
        key = f"setup_{setup_id}"
        if key in events_db:
            required_participants = events_db[key]
            del events_db[key] # Cleanup

    import time, random
    event_id = f"{chat_id}_{int(time.time())}_{random.randint(100,999)}"

    events_db[event_id] = {
        "name": event_name,
        "mode": mode,
        "start_date": start_date,
        "end_date": end_date,
        "chat_id": chat_id,
        "required_participants": required_participants,
        "votes": {}
    }
    save_data(events_db)

    # Send Message
    if hasattr(app.state, "bot_app"):
        bot = app.state.bot_app.bot

        # Determine deep link vs web app button
        # create_event is sent to the Group where the event is created.
        # It's always a group? Not necessarily (could be private).
        # But we assume the goal is to share in a group.
        # Safest is to ALWAYS use Deep Link "Add Availability" in the announcement message.
        # This guarantees it works in groups.

        bot_username = bot.username or (await bot.get_me()).username
        deep_link = f"https://t.me/{bot_username}?start=vote_{event_id}"

        view_btn = InlineKeyboardButton("📊 View Results", callback_data=f"view_{event_id}")
        keyboard = [[InlineKeyboardButton("👉 Add Availability", url=deep_link)], [view_btn]]

        mode_text = "Hourly Slots" if mode == "time" else "Whole Dates"
        extra = ""
        if required_participants:
            extra = f"\nParticipants: {', '.join(required_participants)}"

        try:
            await bot.send_message(
                chat_id=chat_id,
                text=f"🗓 **{event_name}**\nMode: {mode_text}{extra}\n\nTap below to vote!",
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode="Markdown"
            )
        except Exception as e:
            logger.error(f"Failed to send message: {e}")
            return {"status": "error", "message": str(e)}

    return {"status": "success", "event_id": event_id}
