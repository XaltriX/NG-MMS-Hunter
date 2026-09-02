import os
import asyncio
import logging
from datetime import datetime

from dotenv import load_dotenv
load_dotenv()  # loads variables from a local .env file if present (no-op on Heroku)

from bson import ObjectId
from pymongo import MongoClient, DESCENDING

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ContextTypes, filters
)
from telegram.error import TelegramError, Forbidden

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("bot")

# ---------------- CONFIG (from environment variables) ----------------
BOT_TOKEN = os.environ["BOT_TOKEN"]
CHANNEL_ID = int(os.environ["CHANNEL_ID"])  # e.g. -1001234567890
PREMIUM_LINK = os.environ.get("PREMIUM_LINK", "https://t.me/NgPremiumX")
ADMIN_ID = int(os.environ.get("ADMIN_ID", "0"))  # your own Telegram user ID, for /stats command

MONGO_URI = os.environ["MONGO_URI"]          # mongodb+srv://... (from MongoDB Atlas)
MONGO_DB_NAME = os.environ.get("MONGO_DB_NAME", "tgbot1")

# ---------------- DATABASE SETUP ----------------
client = MongoClient(MONGO_URI)
db = client[MONGO_DB_NAME]

posts_col = db["posts"]      # { channel_message_id, buttons, likes, hearts, score, liked_by, hearted_by, created_at }
users_col = db["users"]      # { _id: user_id, browse_pointer, top_pointer }
settings_col = db["settings"]  # { _id: slot_number, url }

posts_col.create_index("channel_message_id", unique=True)

LINK_LABELS = {
    1: "👨‍💻 Developer",
    2: "📢 Latest Channel",
    3: "🎬 Free Videos",
}

MAIN_KEYBOARD = ReplyKeyboardMarkup(
    [["📜 Get Old Posts", "🔥 Top Rated Post"]],
    resize_keyboard=True
)


# ---------------- HELPERS ----------------
def save_post(channel_message_id: int, original_markup):
    buttons = original_markup.to_dict() if original_markup else None
    doc = {
        "channel_message_id": channel_message_id,
        "buttons": buttons,
        "likes": 0,
        "hearts": 0,
        "score": 0,
        "liked_by": [],
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
    return posts_col.find_one(sort=[("score", DESCENDING), ("_id", DESCENDING)])


def get_next_top_post(current_post_id):
    """Find the next-highest-rated post after the given one (for Top Rated pagination)."""
    current = get_post(current_post_id)
    if not current:
        return None
    return posts_col.find_one(
        {
            "$or": [
                {"score": {"$lt": current["score"]}},
                {"score": current["score"], "_id": {"$lt": ObjectId(current_post_id)}},
            ]
        },
        sort=[("score", DESCENDING), ("_id", DESCENDING)]
    )


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


def set_channel_link(slot: int, url: str):
    settings_col.update_one({"_id": slot}, {"$set": {"url": url}}, upsert=True)


def get_channel_links_keyboard():
    """Build one button per row for each configured link slot, in slot order."""
    rows = []
    for slot in sorted(LINK_LABELS.keys()):
        doc = settings_col.find_one({"_id": slot})
        if doc and doc.get("url"):
            rows.append([InlineKeyboardButton(LINK_LABELS[slot], url=doc["url"])])
    return InlineKeyboardMarkup(rows) if rows else None


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


def already_reacted(post_id, user_id: int, reaction_type: str) -> bool:
    field = "liked_by" if reaction_type == "like" else "hearted_by"
    doc = posts_col.find_one({"_id": ObjectId(post_id), field: user_id})
    return doc is not None


def add_reaction(post_id, user_id: int, reaction_type: str):
    count_field = "likes" if reaction_type == "like" else "hearts"
    list_field = "liked_by" if reaction_type == "like" else "hearted_by"
    posts_col.update_one(
        {"_id": ObjectId(post_id)},
        {
            "$addToSet": {list_field: user_id},
            "$inc": {count_field: 1, "score": 1},
        }
    )


def build_keyboard(post_doc, nav_older_id=None, nav_top_id=None):
    """Rebuild inline keyboard: original buttons + heart row + premium row (+ optional nav row)."""
    kb_rows = []

    if post_doc.get("buttons"):
        try:
            for row in post_doc["buttons"].get("inline_keyboard", []):
                new_row = []
                for btn in row:
                    if "url" in btn:
                        new_row.append(InlineKeyboardButton(btn["text"], url=btn["url"]))
                    # callback_data buttons from the channel are skipped
                    # (they'd point to logic that doesn't exist in this bot's context)
                if new_row:
                    kb_rows.append(new_row)
        except Exception:
            pass

    kb_rows.append([
        InlineKeyboardButton(f"❤️ {post_doc['hearts']}", callback_data=f"heart:{post_doc['_id']}"),
    ])

    if nav_older_id is not None:
        kb_rows.append([
            InlineKeyboardButton("⬅️ Older Post", callback_data=f"older:{nav_older_id}")
        ])
    if nav_top_id is not None:
        kb_rows.append([
            InlineKeyboardButton("➡️ Next Top Post", callback_data=f"nexttop:{nav_top_id}")
        ])

    return InlineKeyboardMarkup(kb_rows)


# ---------------- HANDLERS ----------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    add_user(update.effective_user.id)
    await update.message.reply_text(
        "Welcome! Ab aapko channel ke saare naye posts yahan turant milenge.",
        reply_markup=MAIN_KEYBOARD
    )
    links_kb = get_channel_links_keyboard()
    if links_kb:
        await update.message.reply_text("👇 Check these out:", reply_markup=links_kb)


async def add_channel_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if ADMIN_ID and update.effective_user.id != ADMIN_ID:
        return  # silently ignore for non-admins

    command = update.message.text.split()[0].lstrip("/").split("@")[0]  # e.g. "addchnl1"
    slot = int(command.replace("addchnl", ""))

    if slot not in LINK_LABELS:
        await update.message.reply_text(f"Sirf slot 1-{len(LINK_LABELS)} available hai.")
        return

    if not context.args:
        await update.message.reply_text(f"Usage: /{command} <link>\nExample: /{command} https://t.me/yourlink")
        return

    url = context.args[0]
    set_channel_link(slot, url)
    await update.message.reply_text(f"✅ {LINK_LABELS[slot]} button set/updated:\n{url}")


async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if ADMIN_ID and update.effective_user.id != ADMIN_ID:
        return  # silently ignore for non-admins
    await update.message.reply_text(
        f"📊 Bot Stats\n\n"
        f"👥 Total Users: {get_user_count()}\n"
        f"📝 Total Posts: {get_post_count()}"
    )


async def handle_channel_post(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.channel_post
    if msg is None or msg.chat_id != CHANNEL_ID:
        return

    post_doc = save_post(msg.message_id, msg.reply_markup)
    keyboard = build_keyboard(post_doc)

    sent = 0
    removed = 0
    for user_id in get_all_user_ids():
        try:
            await context.bot.copy_message(
                chat_id=user_id,
                from_chat_id=CHANNEL_ID,
                message_id=msg.message_id,
                reply_markup=keyboard,
            )
            sent += 1
        except Forbidden:
            # user blocked the bot / deleted their account — clean up so future
            # broadcasts don't waste API calls and /stats stays accurate
            remove_user(user_id)
            removed += 1
        except TelegramError as e:
            log.warning(f"Failed to send to {user_id}: {e}")

        await asyncio.sleep(0.04)  # gentle pacing to stay under Telegram's rate limits

    log.info(f"Broadcast done. Sent: {sent}, Removed (blocked): {removed}")


async def handle_old_posts_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    pointer = get_browse_pointer(user_id)

    post_doc = None
    restarted = False
    if pointer is not None:
        post_doc = get_older_post(pointer)

    if post_doc is None:
        # either first time, or reached the oldest post — start over from the latest
        post_doc = get_latest_post()
        restarted = pointer is not None

    if not post_doc:
        await update.message.reply_text("Abhi tak koi post nahi hai.")
        return

    set_browse_pointer(user_id, post_doc["_id"])
    older = get_older_post(post_doc["_id"])
    keyboard = build_keyboard(post_doc, nav_older_id=(post_doc["_id"] if older else None))

    if restarted:
        await update.message.reply_text("Ye sabse purani post thi — ab latest se dobara shuru kar raha hoon 🔁")

    await context.bot.copy_message(
        chat_id=update.effective_chat.id,
        from_chat_id=CHANNEL_ID,
        message_id=post_doc["channel_message_id"],
        reply_markup=keyboard,
    )


async def handle_top_rated_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    pointer = get_top_pointer(user_id)

    post_doc = None
    restarted = False
    if pointer is not None:
        post_doc = get_next_top_post(pointer)

    if post_doc is None:
        post_doc = get_top_post()
        restarted = pointer is not None

    if not post_doc:
        await update.message.reply_text("Abhi tak koi post nahi hai.")
        return

    set_top_pointer(user_id, post_doc["_id"])
    next_top = get_next_top_post(post_doc["_id"])
    keyboard = build_keyboard(post_doc, nav_top_id=(post_doc["_id"] if next_top else None))

    if restarted:
        await update.message.reply_text("Ye list ki last post thi — ab #1 se dobara shuru kar raha hoon 🔁")

    await context.bot.copy_message(
        chat_id=update.effective_chat.id,
        from_chat_id=CHANNEL_ID,
        message_id=post_doc["channel_message_id"],
        reply_markup=keyboard,
    )


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data
    user_id = query.from_user.id

    if data.startswith("heart:"):
        reaction_type, post_id = data.split(":")

        if already_reacted(post_id, user_id, reaction_type):
            await query.answer("Aap pehle hi react kar chuke ho!", show_alert=False)
            return

        add_reaction(post_id, user_id, reaction_type)
        post_doc = get_post(post_id)
        new_keyboard = build_keyboard(post_doc)
        try:
            await query.edit_message_reply_markup(reply_markup=new_keyboard)
        except TelegramError:
            pass
        await query.answer("Thanks for your feedback!")

    elif data.startswith("older:"):
        current_post_id = data.split(":")[1]
        older_doc = get_older_post(current_post_id)
        if not older_doc:
            await query.answer("Aur purane posts nahi hai.", show_alert=True)
            return
        set_browse_pointer(user_id, older_doc["_id"])  # keep in sync with the reply-keyboard button
        even_older = get_older_post(older_doc["_id"])
        keyboard = build_keyboard(older_doc, nav_older_id=(older_doc["_id"] if even_older else None))
        await context.bot.copy_message(
            chat_id=update.effective_chat.id,
            from_chat_id=CHANNEL_ID,
            message_id=older_doc["channel_message_id"],
            reply_markup=keyboard,
        )
        await query.answer()

    elif data.startswith("nexttop:"):
        current_post_id = data.split(":")[1]
        next_doc = get_next_top_post(current_post_id)
        if not next_doc:
            await query.answer("Ye list ki last post hai.", show_alert=True)
            return
        set_top_pointer(user_id, next_doc["_id"])  # keep in sync with the reply-keyboard button
        even_next = get_next_top_post(next_doc["_id"])
        keyboard = build_keyboard(next_doc, nav_top_id=(next_doc["_id"] if even_next else None))
        await context.bot.copy_message(
            chat_id=update.effective_chat.id,
            from_chat_id=CHANNEL_ID,
            message_id=next_doc["channel_message_id"],
            reply_markup=keyboard,
        )
        await query.answer()


def main():
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(CommandHandler("addchnl1", add_channel_link))
    app.add_handler(CommandHandler("addchnl2", add_channel_link))
    app.add_handler(CommandHandler("addchnl3", add_channel_link))
    app.add_handler(MessageHandler(
        filters.ChatType.CHANNEL & filters.Chat(chat_id=CHANNEL_ID) & filters.UpdateType.CHANNEL_POST,
        handle_channel_post
    ))
    app.add_handler(MessageHandler(filters.Regex("^📜 Get Old Posts$"), handle_old_posts_button))
    app.add_handler(MessageHandler(filters.Regex("^🔥 Top Rated Post$"), handle_top_rated_button))
    app.add_handler(CallbackQueryHandler(handle_callback))

    log.info("Bot starting...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
