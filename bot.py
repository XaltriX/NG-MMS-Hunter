import os
import logging
from datetime import datetime, timedelta

from dotenv import load_dotenv
load_dotenv()  # loads variables from a local .env file if present (no-op on Heroku)

from bson import ObjectId
from pymongo import MongoClient, DESCENDING

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardRemove
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ContextTypes, filters
)
from telegram.error import TelegramError, Forbidden, TimedOut

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("bot")

# ---------------- CONFIG (from environment variables) ----------------
BOT_TOKEN = os.environ["BOT_TOKEN"]
CHANNEL_ID = int(os.environ["CHANNEL_ID"])  # e.g. -1001234567890
ADMIN_ID = int(os.environ.get("ADMIN_ID", "0"))  # your own Telegram user ID

MONGO_URI = os.environ["MONGO_URI"]
MONGO_DB_NAME = os.environ.get("MONGO_DB_NAME", "tgbot1")

# ---------------- TIMING SETTINGS ----------------
TOP_LIMIT = 10                     # only rank the top 10 rated posts
BROADCAST_DELETE_SECONDS = 3600    # channel posts auto-delete after 1 hour
IDLE_DELETE_SECONDS = 300          # browsed post auto-deletes after 5 min of no action
NOTICE_SELF_DELETE_SECONDS = 150   # the "post removed" notice deletes itself after 2.5 min
DELETE_SCAN_INTERVAL = 120         # how often the bot checks for posts due to be deleted

# ---------------- DATABASE SETUP ----------------
client = MongoClient(MONGO_URI)
db = client[MONGO_DB_NAME]

posts_col = db["posts"]
users_col = db["users"]
settings_col = db["settings"]
pending_deletes_col = db["pending_deletes"]

posts_col.create_index("channel_message_id", unique=True)

LINK_LABELS = {
    1: "👨‍💻 𝘿𝙚𝙫𝙚𝙡𝙤𝙥𝙚𝙧",
    2: "📢 𝙋𝙧𝙚𝙢𝙞𝙪𝙢 𝙈𝙈𝙎 𝘾𝙝𝙖𝙣𝙣𝙚𝙡",
    3: "🎬 𝙁𝙧𝙚𝙚 𝙈𝙈𝙎 𝙑𝙞𝙙𝙚𝙤𝙨",
}

# ---------------- TEXT (simple English, premium look) ----------------
WELCOME_TEXT = (
    "👑 <b>Welcome to NG Premium Bot</b> 👑\n\n"
    "✨ You will get every new post from our channel here, right away.\n"
    "🛡️ Even if something happens to the channel, you can still reach us using the buttons below.\n\n"
    "💎 Tap a button below to get started."
)

SECURITY_NOTICE_TEXT = (
    "🔒 <b>Security Notice</b>\n\n"
    "Your previously viewed post(s) were automatically removed for security reasons.\n\n"
    "🏠 Tap the button below anytime to see more."
)


# ---------------- DB HELPERS: posts ----------------
def save_post(channel_message_id: int, original_markup):
    buttons = original_markup.to_dict() if original_markup else None
    doc = {
        "channel_message_id": channel_message_id,
        "buttons": buttons,
        "hearts": 0,
        "score": 0,
        "hearted_by": [],
        "created_at": datetime.utcnow(),
    }
    result = posts_col.insert_one(doc)
    return posts_col.find_one({"_id": result.inserted_id})


def get_post(post_id):
    return posts_col.find_one({"_id": ObjectId(post_id)})


def get_latest_post():
    return posts_col.find_one(sort=[("_id", DESCENDING)])


def get_older_post(current_post_id):
    return posts_col.find_one(
        {"_id": {"$lt": ObjectId(current_post_id)}},
        sort=[("_id", DESCENDING)]
    )


def get_top_post():
    """Rank #1 — the single highest-rated post (must have at least 1 heart)."""
    return posts_col.find_one({"score": {"$gt": 0}}, sort=[("score", DESCENDING), ("_id", DESCENDING)])


def get_post_rank(post_doc):
    """1-indexed rank of this post among all rated posts."""
    higher_count = posts_col.count_documents({
        "score": {"$gt": 0},
        "$or": [
            {"score": {"$gt": post_doc["score"]}},
            {"score": post_doc["score"], "_id": {"$gt": post_doc["_id"]}},
        ]
    })
    return higher_count + 1


def get_total_rated_count():
    return min(TOP_LIMIT, posts_col.count_documents({"score": {"$gt": 0}}))


def get_next_top_post(current_post_id):
    """Next post in the Top 10 ranking. Returns None if we've reached the cap (rank 10)."""
    current = get_post(current_post_id)
    if not current:
        return None
    if get_post_rank(current) >= TOP_LIMIT:
        return None
    return posts_col.find_one(
        {
            "score": {"$gt": 0},
            "$or": [
                {"score": {"$lt": current["score"]}},
                {"score": current["score"], "_id": {"$lt": current["_id"]}},
            ]
        },
        sort=[("score", DESCENDING), ("_id", DESCENDING)]
    )


# ---------------- DB HELPERS: users ----------------
def get_user(user_id: int):
    return users_col.find_one({"_id": user_id})


def add_user(user_id: int):
    users_col.update_one({"_id": user_id}, {"$setOnInsert": {"_id": user_id}}, upsert=True)


def remove_user(user_id: int):
    users_col.delete_one({"_id": user_id})


def get_all_user_ids():
    return [doc["_id"] for doc in users_col.find({}, {"_id": 1})]


def get_user_count():
    return users_col.count_documents({})


def get_post_count():
    return posts_col.count_documents({})


def get_browse_pointer(user_id: int):
    doc = users_col.find_one({"_id": user_id})
    return doc.get("browse_pointer") if doc else None


def set_browse_pointer(user_id: int, post_id):
    users_col.update_one({"_id": user_id}, {"$set": {"browse_pointer": post_id}}, upsert=True)


def get_top_pointer(user_id: int):
    doc = users_col.find_one({"_id": user_id})
    return doc.get("top_pointer") if doc else None


def set_top_pointer(user_id: int, post_id):
    users_col.update_one({"_id": user_id}, {"$set": {"top_pointer": post_id}}, upsert=True)


def already_reacted(post_id, user_id: int) -> bool:
    doc = posts_col.find_one({"_id": ObjectId(post_id), "hearted_by": user_id})
    return doc is not None


def add_reaction(post_id, user_id: int):
    posts_col.update_one(
        {"_id": ObjectId(post_id)},
        {"$addToSet": {"hearted_by": user_id}, "$inc": {"hearts": 1, "score": 1}}
    )


# ---------------- DB HELPERS: channel links ----------------
def set_channel_link(slot: int, url: str):
    settings_col.update_one({"_id": slot}, {"$set": {"url": url}}, upsert=True)


def build_menu_keyboard():
    rows = []
    dev = settings_col.find_one({"_id": 1})
    latest = settings_col.find_one({"_id": 2})
    free = settings_col.find_one({"_id": 3})

    if dev and dev.get("url"):
        rows.append([InlineKeyboardButton(LINK_LABELS[1], url=dev["url"])])

    row2 = []
    if latest and latest.get("url"):
        row2.append(InlineKeyboardButton(LINK_LABELS[2], url=latest["url"]))
    if free and free.get("url"):
        row2.append(InlineKeyboardButton(LINK_LABELS[3], url=free["url"]))
    if row2:
        rows.append(row2)

    rows.append([
        InlineKeyboardButton("📜 Get Old Post", callback_data="menu_old"),
        InlineKeyboardButton("🔥 Top Rated Post", callback_data="menu_top"),
    ])
    return InlineKeyboardMarkup(rows)


def build_post_buttons(post_doc, nav_prefix=None, nav_target_id=None):
    """Original channel buttons (untouched) + one row of bot buttons: Heart, Next, Menu."""
    kb_rows = []

    if post_doc.get("buttons"):
        try:
            for row in post_doc["buttons"].get("inline_keyboard", []):
                new_row = []
                for btn in row:
                    if "url" in btn:
                        new_row.append(InlineKeyboardButton(btn["text"], url=btn["url"]))
                if new_row:
                    kb_rows.append(new_row)
        except Exception:
            pass

    bot_row = [InlineKeyboardButton(f"❤️ {post_doc['hearts']}", callback_data=f"heart:{post_doc['_id']}")]
    if nav_prefix and nav_target_id is not None:
        bot_row.append(InlineKeyboardButton("➡️ Next", callback_data=f"{nav_prefix}:{nav_target_id}"))
    bot_row.append(InlineKeyboardButton("🏠 Menu", callback_data="menu"))
    kb_rows.append(bot_row)

    return InlineKeyboardMarkup(kb_rows)


# ---------------- ZONE 2: browsing (sequential delete + idle delete) ----------------
def get_browse_jobs(context: ContextTypes.DEFAULT_TYPE):
    return context.application.bot_data.setdefault("browse_jobs", {})


async def cleanup_previous_browse(context: ContextTypes.DEFAULT_TYPE, user_id: int):
    browse_jobs = get_browse_jobs(context)
    prev = browse_jobs.pop(user_id, None)
    if not prev:
        return
    try:
        prev["job"].schedule_removal()
    except Exception:
        pass
    for message_id in prev["message_ids"]:
        try:
            await context.bot.delete_message(chat_id=prev["chat_id"], message_id=message_id)
        except TelegramError:
            pass


async def deliver_browsed_post(context, chat_id, user_id, post_doc, nav_prefix, nav_target_id, extra_message_id=None):
    await cleanup_previous_browse(context, user_id)

    keyboard = build_post_buttons(post_doc, nav_prefix, nav_target_id)
    try:
        msg = await context.bot.copy_message(
            chat_id=chat_id,
            from_chat_id=CHANNEL_ID,
            message_id=post_doc["channel_message_id"],
            reply_markup=keyboard,
        )
    except TimedOut:
        # Telegram was briefly slow — retry once before giving up
        msg = await context.bot.copy_message(
            chat_id=chat_id,
            from_chat_id=CHANNEL_ID,
            message_id=post_doc["channel_message_id"],
            reply_markup=keyboard,
        )

    message_ids = [msg.message_id]
    if extra_message_id:
        message_ids.append(extra_message_id)

    job = context.job_queue.run_once(
        idle_delete_job, IDLE_DELETE_SECONDS,
        data={"chat_id": chat_id, "message_ids": message_ids, "user_id": user_id, "post_message_id": msg.message_id},
        name=f"idle_{user_id}"
    )
    get_browse_jobs(context)[user_id] = {"chat_id": chat_id, "message_ids": message_ids, "job": job}
    return msg


async def idle_delete_job(context: ContextTypes.DEFAULT_TYPE):
    data = context.job.data
    user_id = data["user_id"]
    browse_jobs = get_browse_jobs(context)
    current = browse_jobs.get(user_id)
    if not current or current.get("message_ids") != data["message_ids"]:
        return  # a newer post already replaced this one, nothing to do

    for message_id in data["message_ids"]:
        try:
            await context.bot.delete_message(chat_id=data["chat_id"], message_id=message_id)
        except TelegramError:
            pass
    browse_jobs.pop(user_id, None)
    await send_and_self_delete_notice(context, data["chat_id"])


async def send_and_self_delete_notice(context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    try:
        notice = await context.bot.send_message(
            chat_id=chat_id, text=SECURITY_NOTICE_TEXT, parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Menu", callback_data="menu")]])
        )
        context.job_queue.run_once(
            delete_notice_job, NOTICE_SELF_DELETE_SECONDS,
            data={"chat_id": chat_id, "message_id": notice.message_id}
        )
    except TelegramError:
        pass


async def delete_notice_job(context: ContextTypes.DEFAULT_TYPE):
    data = context.job.data
    try:
        await context.bot.delete_message(chat_id=data["chat_id"], message_id=data["message_id"])
    except TelegramError:
        pass


async def show_old_post(context, chat_id, user_id):
    pointer = get_browse_pointer(user_id)
    post_doc = None
    restarted = False
    if pointer is not None:
        post_doc = get_older_post(pointer)
    if post_doc is None:
        post_doc = get_latest_post()
        restarted = pointer is not None

    if not post_doc:
        await context.bot.send_message(chat_id=chat_id, text="😔 No posts available yet.")
        return

    set_browse_pointer(user_id, post_doc["_id"])
    older = get_older_post(post_doc["_id"])
    nav_id = post_doc["_id"] if older else None

    if restarted:
        await context.bot.send_message(chat_id=chat_id, text="🔁 That was the oldest post — starting over from the latest.")

    await deliver_browsed_post(context, chat_id, user_id, post_doc, "older", nav_id)


async def show_top_post(context, chat_id, user_id):
    pointer = get_top_pointer(user_id)
    post_doc = None
    restarted = False
    if pointer is not None:
        post_doc = get_next_top_post(pointer)
    if post_doc is None:
        post_doc = get_top_post()
        restarted = pointer is not None

    if not post_doc:
        await context.bot.send_message(chat_id=chat_id, text="❤️ No posts have been rated yet. Be the first to rate one!")
        return

    set_top_pointer(user_id, post_doc["_id"])
    rank = get_post_rank(post_doc)
    next_top = get_next_top_post(post_doc["_id"])
    nav_id = post_doc["_id"] if next_top else None

    if restarted:
        await context.bot.send_message(chat_id=chat_id, text="🔁 That was the last one in the Top 10 — starting over from #1.")

    rank_msg = await context.bot.send_message(
        chat_id=chat_id,
        text=f"🏆 <b>Rank #{rank} of Top {get_total_rated_count()}</b>",
        parse_mode=ParseMode.HTML
    )
    await deliver_browsed_post(context, chat_id, user_id, post_doc, "nexttop", nav_id, extra_message_id=rank_msg.message_id)


# ---------------- ZONE 1: channel broadcast auto-delete (restart-safe) ----------------
def schedule_broadcast_delete(chat_id: int, message_id: int):
    pending_deletes_col.insert_one({
        "chat_id": chat_id,
        "message_id": message_id,
        "delete_at": datetime.utcnow() + timedelta(seconds=BROADCAST_DELETE_SECONDS),
    })


async def process_pending_deletes(context: ContextTypes.DEFAULT_TYPE):
    now = datetime.utcnow()
    due = list(pending_deletes_col.find({"delete_at": {"$lte": now}}))
    if not due:
        return

    by_chat = {}
    for doc in due:
        by_chat.setdefault(doc["chat_id"], []).append(doc)

    for chat_id, docs in by_chat.items():
        any_deleted = False
        for doc in docs:
            try:
                await context.bot.delete_message(chat_id=chat_id, message_id=doc["message_id"])
                any_deleted = True
            except TelegramError:
                pass
            pending_deletes_col.delete_one({"_id": doc["_id"]})
        if any_deleted:
            await send_and_self_delete_notice(context, chat_id)


# ---------------- HANDLERS ----------------
async def send_menu(context: ContextTypes.DEFAULT_TYPE, chat_id: int, user_id: int):
    """Sends the welcome/menu message, deleting the previous one for this user first
    so only one menu message ever sits in the chat at a time."""
    menu_messages = context.application.bot_data.setdefault("menu_messages", {})
    prev = menu_messages.get(user_id)
    if prev:
        try:
            await context.bot.delete_message(chat_id=prev["chat_id"], message_id=prev["message_id"])
        except TelegramError:
            pass

    msg = await context.bot.send_message(
        chat_id=chat_id, text=WELCOME_TEXT, parse_mode=ParseMode.HTML, reply_markup=build_menu_keyboard()
    )
    menu_messages[user_id] = {"chat_id": chat_id, "message_id": msg.message_id}
    return msg


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    is_returning = get_user(user_id) is not None
    add_user(user_id)

    if is_returning:
        # clears any old bottom keyboard from a previous version of the bot
        cleanup = await update.message.reply_text("⚙️ Updating your menu...", reply_markup=ReplyKeyboardRemove())
        try:
            await cleanup.delete()
        except TelegramError:
            pass

    await send_menu(context, update.effective_chat.id, user_id)


async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if ADMIN_ID and update.effective_user.id != ADMIN_ID:
        return
    await update.message.reply_text(
        f"📊 <b>Bot Stats</b>\n\n"
        f"👥 Total Users: {get_user_count()}\n"
        f"📝 Total Posts: {get_post_count()}",
        parse_mode=ParseMode.HTML
    )


async def add_channel_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if ADMIN_ID and update.effective_user.id != ADMIN_ID:
        return

    command = update.message.text.split()[0].lstrip("/").split("@")[0]
    slot = int(command.replace("addchnl", ""))

    if slot not in LINK_LABELS:
        await update.message.reply_text(f"Only slots 1-{len(LINK_LABELS)} are available.")
        return

    if not context.args:
        await update.message.reply_text(f"Usage: /{command} <link>\nExample: /{command} https://t.me/yourlink")
        return

    url = context.args[0]
    set_channel_link(slot, url)
    await update.message.reply_text(f"✅ {LINK_LABELS[slot]} button set/updated:\n{url}")


async def handle_channel_post(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.channel_post
    if msg is None or msg.chat_id != CHANNEL_ID:
        return

    post_doc = save_post(msg.message_id, msg.reply_markup)
    keyboard = build_post_buttons(post_doc)

    sent = 0
    removed = 0
    for user_id in get_all_user_ids():
        try:
            copied = await context.bot.copy_message(
                chat_id=user_id,
                from_chat_id=CHANNEL_ID,
                message_id=msg.message_id,
                reply_markup=keyboard,
            )
            schedule_broadcast_delete(user_id, copied.message_id)
            sent += 1
        except Forbidden:
            remove_user(user_id)
            removed += 1
        except TelegramError as e:
            log.warning(f"Failed to send to {user_id}: {e}")

    log.info(f"Broadcast done. Sent: {sent}, Removed (blocked): {removed}")


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data
    user_id = query.from_user.id
    chat_id = query.message.chat_id

    if data.startswith("heart:"):
        post_id = data.split(":")[1]

        if already_reacted(post_id, user_id):
            await query.answer("You already reacted to this! ❤️", show_alert=False)
            return

        add_reaction(post_id, user_id)
        post_doc = get_post(post_id)

        # only update the heart button's label — every other button (Next, Menu,
        # original channel buttons) stays exactly as it was
        old_markup = query.message.reply_markup
        new_rows = []
        for row in old_markup.inline_keyboard:
            new_row = []
            for btn in row:
                if btn.callback_data and btn.callback_data.startswith("heart:"):
                    new_row.append(InlineKeyboardButton(f"❤️ {post_doc['hearts']}", callback_data=btn.callback_data))
                else:
                    new_row.append(btn)
            new_rows.append(new_row)
        try:
            await query.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup(new_rows))
        except TelegramError:
            pass
        await query.answer("Thanks for your feedback! ❤️")

    elif data == "menu":
        await query.answer()
        await send_menu(context, chat_id, user_id)

    elif data == "menu_old":
        await query.answer()
        await show_old_post(context, chat_id, user_id)

    elif data == "menu_top":
        await query.answer()
        await show_top_post(context, chat_id, user_id)

    elif data.startswith("older:"):
        current_post_id = data.split(":")[1]
        older_doc = get_older_post(current_post_id)
        if not older_doc:
            await query.answer("No more old posts.", show_alert=True)
            set_browse_pointer(user_id, None)
            return
        set_browse_pointer(user_id, older_doc["_id"])
        even_older = get_older_post(older_doc["_id"])
        nav_id = older_doc["_id"] if even_older else None
        await query.answer()
        await deliver_browsed_post(context, chat_id, user_id, older_doc, "older", nav_id)

    elif data.startswith("nexttop:"):
        current_post_id = data.split(":")[1]
        next_doc = get_next_top_post(current_post_id)
        if not next_doc:
            await query.answer("That's the end of the Top 10!", show_alert=True)
            set_top_pointer(user_id, None)
            return
        set_top_pointer(user_id, next_doc["_id"])
        rank = get_post_rank(next_doc)
        even_next = get_next_top_post(next_doc["_id"])
        nav_id = next_doc["_id"] if even_next else None
        await query.answer()
        rank_msg = await context.bot.send_message(
            chat_id=chat_id,
            text=f"🏆 <b>Rank #{rank} of Top {get_total_rated_count()}</b>",
            parse_mode=ParseMode.HTML
        )
        await deliver_browsed_post(context, chat_id, user_id, next_doc, "nexttop", nav_id, extra_message_id=rank_msg.message_id)


async def error_handler(update, context: ContextTypes.DEFAULT_TYPE):
    log.warning(f"Update {update} caused error: {context.error}")


def main():
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .connect_timeout(30)
        .read_timeout(30)
        .write_timeout(30)
        .pool_timeout(30)
        .build()
    )

    app.add_error_handler(error_handler)

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(CommandHandler("addchnl1", add_channel_link))
    app.add_handler(CommandHandler("addchnl2", add_channel_link))
    app.add_handler(CommandHandler("addchnl3", add_channel_link))
    app.add_handler(MessageHandler(
        filters.ChatType.CHANNEL & filters.Chat(chat_id=CHANNEL_ID) & filters.UpdateType.CHANNEL_POST,
        handle_channel_post
    ))
    app.add_handler(CallbackQueryHandler(handle_callback))

    app.job_queue.run_repeating(process_pending_deletes, interval=DELETE_SCAN_INTERVAL, first=10)

    log.info("Bot starting...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
