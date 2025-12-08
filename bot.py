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
import matplotlib.cm as cm
import seaborn as sns
import pandas as pd
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo, Chat, InputFile
from telegram.error import BadRequest
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

# --- PERSISTENCE MANAGER (Local Only) ---
class Persistence:
    def __init__(self):
        pass

    def load_all(self):
        """Loads all events from local file."""
        if os.path.exists(DATA_FILE):
            try:
                with open(DATA_FILE, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception as e:
                logger.error(f"File Load Error: {e}")
        return {}

    def upsert_event(self, event_id: str, data: dict):
        """Upserts a single event to memory and file."""
        events_db[event_id] = data
        self._save_file()

    def upsert_setup(self, setup_id: str, data: list):
        """Upserts setup data (list)."""
        events_db[setup_id] = data
        self._save_file()

    def delete_event(self, event_id: str):
        """Deletes an event."""
        if event_id in events_db:
            del events_db[event_id]
        self._save_file()

    def _save_file(self):
        """Helper for file persistence."""
        try:
            with open(DATA_FILE, "w", encoding="utf-8") as f:
                json.dump(events_db, f, indent=2)
        except Exception as e:
            logger.error(f"File Save Error: {e}")

# Initialize Persistence
db = Persistence()
events_db = db.load_all()

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

    # Calculate Total Users to determine Max Saturation
    total_users = len(votes)
    if total_users == 0:
        return None

    # Aggregate scores
    slot_scores = {}

    for user_votes in votes.values():
        if isinstance(user_votes, list):
             for slot in user_votes:
                 slot_scores[slot] = slot_scores.get(slot, 0) + 1.0
        elif isinstance(user_votes, dict):
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

    sorted_slots = sorted(slot_scores.keys())

    # Visual Polish
    plt.figure(figsize=(10, 6))
    sns.set_theme(style="whitegrid") # Cleaner style

    if mode == "date":
        try:
            dates = sorted(slot_scores.keys())
            scores = [slot_scores[d] for d in dates]

            # Normalize scores relative to total_users for coloring
            # Explicitly map score to color to ensure identical scores = identical colors
            norm_scores = [s / total_users for s in scores]
            colors = [cm.Greens(n) for n in norm_scores]

            # Use hue=dates to avoid FutureWarning, legend=False to hide it
            ax = sns.barplot(x=dates, y=scores, hue=dates, palette=colors, legend=False)

            plt.xticks(rotation=45, fontsize=10)
            plt.yticks(fontsize=10)
            plt.title(f"Availability: {event_data.get('name')}", fontsize=14)
            plt.ylabel("Score", fontsize=12)
            plt.xlabel("Date", fontsize=12)
            plt.ylim(0, total_users + 0.5) # Set Y-limit to max possible users
            plt.tight_layout()
        except Exception as e:
            logger.error(f"Barplot generation error: {e}")
            return None
    else:
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

            df = pd.DataFrame(data_points)
            pivot_table = df.pivot(index="Hour", columns="Date", values="Score")

            # Use 'Greens' colormap with fixed vmin/vmax
            # vmin=0, vmax=total_users ensures saturation is absolute % of turnout
            ax = sns.heatmap(pivot_table, cmap="Greens", annot=True, fmt=".1f",
                             cbar_kws={'label': 'Score'}, annot_kws={"size": 10},
                             vmin=0, vmax=total_users)

            plt.title(f"Availability: {event_data.get('name')}", fontsize=14)
            plt.xlabel("Date", fontsize=12)
            plt.ylabel("Hour", fontsize=12)
            plt.xticks(fontsize=10)
            plt.yticks(fontsize=10)
            plt.tight_layout()
        except Exception as e:
            logger.error(f"Heatmap generation error: {e}")
            return None

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
    
    # Handle Result Deep Link
    if args and args[0].startswith("result_"):
        event_id = args[0].replace("result_", "")
        safe_event_id = urllib.parse.quote(str(event_id))
        # Check event mode to load correct view
        event = events_db.get(event_id)
        mode = event.get("mode", "time") if event else "time"

        full_url = f"{WEB_APP_URL}?eventId={safe_event_id}&mode=result" # Force result mode
        web_app = WebAppInfo(url=full_url)
        keyboard = [[InlineKeyboardButton("📊 Open Results", web_app=web_app)]]

        await update.message.reply_text(
            "📊 **Event Results**\n\nTap below to view the interactive calendar.",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown"
        )
        return

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
            # Get event mode
            event = events_db.get(event_id)
            mode = event.get("mode", "time") if event else "time"

            full_url = f"{WEB_APP_URL}?eventId={safe_event_id}&mode={mode}"
            web_app_vote = WebAppInfo(url=full_url)
            keyboard = [[InlineKeyboardButton("👉 Add Availability", web_app=web_app_vote)]]
            await update.message.reply_text(
                "📊 **Vote Now**\n\nTap below to add your availability.",
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode="Markdown"
            )
            return

    # Standard Menu
    setup_url = f"{WEB_APP_URL}?mode=setup&chatId={chat.id}"
    web_app = WebAppInfo(url=setup_url)

    keyboard_webapp = [
        [InlineKeyboardButton("➕ Create Event", web_app=web_app)],
        [InlineKeyboardButton("📅 Active Events", callback_data="list_active_events")],
        [InlineKeyboardButton("❓ Help", callback_data="show_help")]
    ]
    
    try:
        await update.message.reply_text(
            "👋 **When2Meet Bot**\n\nMain Menu:",
            reply_markup=InlineKeyboardMarkup(keyboard_webapp),
            parse_mode="Markdown"
        )
    except BadRequest as e:
        if "Button_type_invalid" in str(e):
            bot_username = context.bot.username or (await context.bot.get_me()).username
            deep_link = f"https://t.me/{bot_username}?start=setup_{chat.id}"

            keyboard_fallback = [
                [InlineKeyboardButton("➕ Create Event", url=deep_link)],
                [InlineKeyboardButton("📅 Active Events", callback_data="list_active_events")],
                [InlineKeyboardButton("❓ Help", callback_data="show_help")]
            ]
            await update.message.reply_text(
                "👋 **When2Meet Bot**\n\nMain Menu:",
                reply_markup=InlineKeyboardMarkup(keyboard_fallback),
                parse_mode="Markdown"
            )
        else:
            raise e

async def ask_event_mode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    mentions = [w for w in args if w.startswith("@")]
    
    import time, random
    setup_id = f"{update.effective_chat.id}_{int(time.time())}_{random.randint(100,999)}"
    setup_key = f"setup_{setup_id}"
    
    if mentions:
        # PERSIST SETUP
        db.upsert_setup(setup_key, mentions)
        msg_text = f"📅 **Schedule Event**\nParticipants: {', '.join(mentions)}\n\nClick below to configure:"
    else:
        msg_text = "📅 **Schedule Event**\n\nClick below to configure:"

    chat = update.effective_chat
    
    setup_url = f"{WEB_APP_URL}?mode=setup&chatId={chat.id}&setupId={setup_id}"
    web_app = WebAppInfo(url=setup_url)
    keyboard_webapp = [[InlineKeyboardButton("⚙️ Configure Event", web_app=web_app)]]
    
    try:
        await update.message.reply_text(
            msg_text,
            reply_markup=InlineKeyboardMarkup(keyboard_webapp),
            parse_mode="Markdown"
        )
    except BadRequest as e:
        if "Button_type_invalid" in str(e):
            bot_username = context.bot.username or (await context.bot.get_me()).username
            deep_link = f"https://t.me/{bot_username}?start=setup_{chat.id}"
            keyboard_fallback = [[InlineKeyboardButton("⚙️ Configure Event", url=deep_link)]]
            await update.message.reply_text(
                msg_text,
                reply_markup=InlineKeyboardMarkup(keyboard_fallback),
                parse_mode="Markdown"
            )
        else:
            raise e

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

    # Collect usernames
    participants = []
    for v_data in votes.values():
        if isinstance(v_data, dict) and "username" in v_data:
            participants.append(f"{v_data['username']}")
        else:
            participants.append("User")

    msg = f"📊 **{event['name']}**\n"
    msg += f"👥 {total_users} responded\n\n"

    if participants:
        msg += "**Responded:** " + ", ".join(participants) + "\n"

    req_participants = event.get("required_participants", [])

    safe_event_id = urllib.parse.quote(str(event_id))
    full_url = f"{WEB_APP_URL}?eventId={safe_event_id}&mode={event.get('mode', 'time')}"
    web_app_vote = WebAppInfo(url=full_url)

    # New: View Results as Web App
    result_url = f"{WEB_APP_URL}?eventId={safe_event_id}&mode=result"
    web_app_result = WebAppInfo(url=result_url)

    btn_webapp = InlineKeyboardButton("👉 Add Availability", web_app=web_app_vote)
    btn_result = InlineKeyboardButton("📊 View Results", web_app=web_app_result)

    keyboard_webapp = [[btn_webapp], [btn_result]]

    # Fallback
    bot_username = context.bot.username or (await context.bot.get_me()).username
    link_vote = f"https://t.me/{bot_username}?start=vote_{event_id}"
    link_result = f"https://t.me/{bot_username}?start=result_{event_id}"

    btn_vote_fb = InlineKeyboardButton("👉 Add Availability", url=link_vote)
    btn_result_fb = InlineKeyboardButton("📊 View Results", url=link_result)
    keyboard_fallback = [[btn_vote_fb], [btn_result_fb]]

    extra_buttons = []
    if req_participants:
        extra_buttons.append(InlineKeyboardButton("🔔 Nudge Missing", callback_data=f"nudge_{event_id}"))

    if extra_buttons: keyboard_webapp.append(extra_buttons)
    if extra_buttons: keyboard_fallback.append(extra_buttons)

    try:
        if img_buf:
            await query.message.reply_photo(
                photo=img_buf,
                caption=msg,
                reply_markup=InlineKeyboardMarkup(keyboard_webapp),
                parse_mode="Markdown"
            )
        else:
            await query.message.reply_text(msg + "\n(No data to visualize)", reply_markup=InlineKeyboardMarkup(keyboard_webapp), parse_mode="Markdown")
    except BadRequest as e:
        if img_buf: img_buf.seek(0)

        if "Button_type_invalid" in str(e):
            if img_buf:
                await query.message.reply_photo(
                    photo=img_buf,
                    caption=msg,
                    reply_markup=InlineKeyboardMarkup(keyboard_fallback),
                    parse_mode="Markdown"
                )
            else:
                await query.message.reply_text(msg + "\n(No data to visualize)", reply_markup=InlineKeyboardMarkup(keyboard_fallback), parse_mode="Markdown")
        else:
            logger.error(f"Error sending results: {e}")

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

    voted_usernames = set()
    for uid, data in event.get("votes", {}).items():
        if isinstance(data, dict) and "username" in data:
            voted_usernames.add(f"@{data['username']}")

    missing = [p for p in req if p not in voted_usernames]

    if missing:
        await query.message.reply_text(f"🔔 **Waiting on:**\n{' '.join(missing)}", parse_mode="Markdown")
    else:
        await query.message.reply_text("🎉 Everyone has voted!")

async def check_attendance(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
        with open("index.html", "r", encoding="utf-8") as f:
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
    username = data.get("username")
    slots = data.get("slots")
    
    if event_id not in events_db: return {"status": "error", "message": "Event not found"}

    event = events_db[event_id]
    event["votes"][user_id] = {
        "slots": slots,
        "username": username
    }
    # PERSIST VOTE
    db.upsert_event(event_id, event)

    # Notify User (Private Chat)
    try:
        if hasattr(app.state, "bot_app"):
            bot = app.state.bot_app.bot
            event_name = event.get("name", "Event")

            await bot.send_message(
                chat_id=user_id,
                text=f"✅ Your availability for **{event_name}** has been saved.",
                parse_mode="Markdown"
            )
    except Exception as e:
        logger.error(f"Failed to notify user {user_id}: {e}")

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
            # CLEANUP SETUP
            db.delete_event(key)

    import time, random
    event_id = f"{chat_id}_{int(time.time())}_{random.randint(100,999)}"

    new_event = {
        "name": event_name,
        "mode": mode,
        "start_date": start_date,
        "end_date": end_date,
        "chat_id": chat_id,
        "required_participants": required_participants,
        "votes": {}
    }

    # PERSIST NEW EVENT
    db.upsert_event(event_id, new_event)

    # Send Message
    if hasattr(app.state, "bot_app"):
        bot = app.state.bot_app.bot

        safe_event_id = urllib.parse.quote(str(event_id))
        full_url = f"{WEB_APP_URL}?eventId={safe_event_id}&mode={mode}"
        web_app_vote = WebAppInfo(url=full_url)

        # New: View Results as Web App
        result_url = f"{WEB_APP_URL}?eventId={safe_event_id}&mode=result"
        web_app_result = WebAppInfo(url=result_url)

        btn_webapp = InlineKeyboardButton("👉 Add Availability", web_app=web_app_vote)
        btn_result = InlineKeyboardButton("📊 View Results", web_app=web_app_result)

        keyboard_webapp = [[btn_webapp], [btn_result]]

        # Fallback
        bot_username = bot.username or (await bot.get_me()).username
        link_vote = f"https://t.me/{bot_username}?start=vote_{event_id}"
        link_result = f"https://t.me/{bot_username}?start=result_{event_id}"

        btn_vote_fb = InlineKeyboardButton("👉 Add Availability", url=link_vote)
        btn_result_fb = InlineKeyboardButton("📊 View Results", url=link_result)
        keyboard_fallback = [[btn_vote_fb], [btn_result_fb]]

        mode_text = "Hourly Slots" if mode == "time" else "Whole Dates"
        extra = ""
        if required_participants:
            extra = f"\nParticipants: {', '.join(required_participants)}"

        msg_text = f"🗓 **{event_name}**\nMode: {mode_text}{extra}\n\nTap below to vote!"

        try:
            await bot.send_message(
                chat_id=chat_id,
                text=msg_text,
                reply_markup=InlineKeyboardMarkup(keyboard_webapp),
                parse_mode="Markdown"
            )
        except BadRequest as e:
            if "Button_type_invalid" in str(e):
                try:
                    await bot.send_message(
                        chat_id=chat_id,
                        text=msg_text,
                        reply_markup=InlineKeyboardMarkup(keyboard_fallback),
                        parse_mode="Markdown"
                    )
                except Exception as inner_e:
                     logger.error(f"Failed to send fallback message: {inner_e}")
                     return {"status": "error", "message": str(inner_e)}
            else:
                logger.error(f"Failed to send message: {e}")
                return {"status": "error", "message": str(e)}

    return {"status": "success", "event_id": event_id}

@app.post("/share_results")
async def share_results(request: Request):
    """
    Endpoint for Web App to request sending the result image to the group.
    """
    data = await request.json()
    event_id = data.get("eventId")

    if event_id not in events_db: return {"status": "error", "message": "Event not found"}
    event = events_db[event_id]

    img_buf = generate_heatmap_image(event)
    if not img_buf: return {"status": "error", "message": "No votes yet"}

    # We send the photo to the Chat ID associated with the event
    chat_id = event.get("chat_id")
    if hasattr(app.state, "bot_app"):
        bot = app.state.bot_app.bot
        try:
            img_buf.seek(0)
            await bot.send_photo(
                chat_id=chat_id,
                photo=img_buf,
                caption=f"📊 **Results Export: {event['name']}**",
                parse_mode="Markdown"
            )
            return {"status": "success"}
        except Exception as e:
            logger.error(f"Failed to share results: {e}")
            return {"status": "error", "message": str(e)}

    return {"status": "error", "message": "Bot instance not found"}
