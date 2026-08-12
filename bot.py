# demo_bot.py
# DEMO ONLY — no real UPI/payment requests are sent.

import asyncio
import sqlite3
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, ContextTypes, filters
)

BOT_TOKEN = "8679389121:AAGMOtJTy2xJ8EDJNomL0QyBtYlMb2KdoA4"
CHANNEL = "@tigermark_et"

db = sqlite3.connect("demo.db", check_same_thread=False)
db.execute("""
CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY,
    coins INTEGER DEFAULT 0,
    referred_by INTEGER
)
""")
db.commit()


def add_user(user_id, referred_by=None):
    db.execute(
        "INSERT OR IGNORE INTO users(user_id, referred_by) VALUES (?, ?)",
        (user_id, referred_by)
    )
    db.commit()


def add_coins(user_id, amount):
    db.execute(
        "UPDATE users SET coins = coins + ? WHERE user_id = ?",
        (amount, user_id)
    )
    db.commit()


def get_coins(user_id):
    row = db.execute(
        "SELECT coins FROM users WHERE user_id = ?", (user_id,)
    ).fetchone()
    return row[0] if row else 0


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    add_user(user.id)

    # Referral
    if context.args:
        try:
            referrer = int(context.args[0])
            if referrer != user.id:
                existing = db.execute(
                    "SELECT referred_by FROM users WHERE user_id = ?",
                    (user.id,)
                ).fetchone()

                if existing and existing[0] is None:
                    db.execute(
                        "UPDATE users SET referred_by = ? WHERE user_id = ?",
                        (referrer, user.id)
                    )
                    add_coins(referrer, 10)
                    db.commit()
        except ValueError:
            pass

    keyboard = [
        [InlineKeyboardButton("📢 Join Channel", url="https://t.me/tigermark_et")],
        [InlineKeyboardButton("✅ Verify Join", callback_data="verify")],
        [InlineKeyboardButton("👥 Refer & Earn", callback_data="refer")],
        [InlineKeyboardButton("💰 Balance", callback_data="balance")]
    ]

    await update.message.reply_text(
        "🤖 *UPI DEMO BOT*\n\n"
        "⚠️ SIMULATION ONLY\n"
        "No real payment or UPI request will be sent.\n\n"
        "Join the channel to continue.",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


async def verify(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    try:
        member = await context.bot.get_chat_member(
            CHANNEL, query.from_user.id
        )

        if member.status in ("member", "administrator", "creator"):
            keyboard = [[
                InlineKeyboardButton(
                    "💸 Start Demo",
                    callback_data="demo"
                )
            ]]

            await query.edit_message_text(
                "✅ Channel verified!\n\n"
                "You can now start the payment-processing simulation.",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
        else:
            await query.answer(
                "❌ Please join the channel first.",
                show_alert=True
            )

    except Exception:
        await query.answer(
            "⚠️ Bot cannot verify the channel. "
            "Make sure the bot is an admin.",
            show_alert=True
        )


async def refer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    uid = query.from_user.id
    link = f"https://t.me/{context.bot.username}?start={uid}"

    await query.edit_message_text(
        "👥 *REFER & EARN*\n\n"
        "Invite friends using your link.\n"
        "🎁 Reward: 10 coins per referral\n\n"
        f"Your referral link:\n`{link}`",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("🔙 Back", callback_data="back")
        ]])
    )


async def balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    coins = get_coins(query.from_user.id)

    await query.edit_message_text(
        f"💰 *Your Balance*\n\n"
        f"🪙 Coins: `{coins}`\n\n"
        "⚠️ Demo coins have no monetary value.",
        parse_mode="Markdown"
    )


async def demo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    context.user_data["waiting_upi"] = True

    await query.edit_message_text(
        "💳 *DEMO CLAIM*\n\n"
        "Enter any test UPI ID.\n\n"
        "⚠️ This is a simulation. No payment will be sent.",
        parse_mode="Markdown"
    )


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get("waiting_upi"):
        return

    upi = update.message.text.strip()
    context.user_data["waiting_upi"] = False

    msg = await update.message.reply_text(
        "🔄 *Starting simulation...*",
        parse_mode="Markdown"
    )

    stages = [
        "🔄 Processing demo request...",
        "🔍 Validating test UPI ID...",
        "⚡ Simulating payment gateway...",
        "📡 Simulating server response...",
        "⏳ Finalizing demo...",
    ]

    # Short visual animation, not a real transaction.
    for stage in stages:
        await asyncio.sleep(2)
        await msg.edit_text(
            f"{stage}\n\n"
            f"UPI: `{upi}`\n"
            "⚠️ SIMULATION ONLY",
            parse_mode="Markdown"
        )

    await msg.edit_text(
        "✅ *DEMO COMPLETED*\n\n"
        f"Test UPI: `{upi}`\n"
        "Status: `SIMULATED SUCCESS`\n\n"
        "⚠️ No UPI request, payment, SMS, or notification was sent.",
        parse_mode="Markdown"
    )


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query

    if query.data == "verify":
        await verify(update, context)
    elif query.data == "refer":
        await refer(update, context)
    elif query.data == "balance":
        await balance(update, context)
    elif query.data == "demo":
        await demo(update, context)


def main():
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text)
    )

    print("Demo bot running...")
    app.run_polling()


if __name__ == "__main__":
    main()