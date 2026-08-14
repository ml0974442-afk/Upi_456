import os
import asyncio
import logging
import sqlite3
from threading import Thread, Lock
from html import escape

from flask import Flask

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
)

from telegram.constants import ParseMode

from telegram.error import (
    RetryAfter,
    TelegramError,
)

from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)


# =========================================================
# CONFIG
# =========================================================

BOT_TOKEN = os.environ["BOT_TOKEN"]

CHANNEL = "@tigermark_et"
CHANNEL_2 = os.environ.get("CHANNEL_2", "@trust_zonw")
CHANNELS = (CHANNEL, CHANNEL_2)

# Telegram numeric admin ID
ADMIN_ID = int(
    os.environ.get("ADMIN_ID", "0")
)

# Admin's first balance
ADMIN_START_COINS = 500

# Referral reward
REFERRAL_REWARD = 2
# Cost of one  process
START_COST = 4
# Database file
DB_FILE = ".db"


# =========================================================
# LOGGING
# =========================================================

logging.basicConfig(
    format=(
        "%(asctime)s - "
        "%(name)s - "
        "%(levelname)s - "
        "%(message)s"
    ),
    level=logging.INFO,
)

logger = logging.getLogger(__name__)


# =========================================================
# DATABASE
# =========================================================

db = sqlite3.connect(
    DB_FILE,
    check_same_thread=False
)

db_lock = Lock()


def db_execute(
    query,
    params=(),
    fetchone=False,
    fetchall=False,
    commit=False
):
    with db_lock:

        cursor = db.execute(
            query,
            params
        )

        result = None

        if fetchone:
            result = cursor.fetchone()

        elif fetchall:
            result = cursor.fetchall()

        if commit:
            db.commit()

        return result


# =========================================================
# DATABASE SETUP / MIGRATION
# =========================================================

def setup_database():

    # -----------------------------------------------------
    # USERS
    # -----------------------------------------------------

    db_execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            last_name TEXT,
            coins INTEGER DEFAULT 0,
            referred_by INTEGER,
            referral_rewarded INTEGER DEFAULT 0,
            blocked INTEGER DEFAULT 0,
            created_at TIMESTAMP
                DEFAULT CURRENT_TIMESTAMP
        )
        """,
        commit=True
    )

    columns = db_execute(
        "PRAGMA table_info(users)",
        fetchall=True
    )

    existing_columns = {
        column[1]
        for column in columns
    }

    migrations = {
        "username": (
            """
            ALTER TABLE users
            ADD COLUMN username TEXT
            """
        ),
        "first_name": (
            """
            ALTER TABLE users
            ADD COLUMN first_name TEXT
            """
        ),
        "last_name": (
            """
            ALTER TABLE users
            ADD COLUMN last_name TEXT
            """
        ),
        "referral_rewarded": (
            """
            ALTER TABLE users
            ADD COLUMN referral_rewarded
            INTEGER DEFAULT 0
            """
        ),
        "blocked": (
            """
            ALTER TABLE users
            ADD COLUMN blocked
            INTEGER DEFAULT 0
            """
        ),
        "created_at": (
            """
            ALTER TABLE users
            ADD COLUMN created_at TIMESTAMP
            """
        ),
    }

    for column_name, query in migrations.items():

        if column_name not in existing_columns:

            db_execute(
                query,
                commit=True
            )

    db_execute(
        """
        UPDATE users
        SET created_at = CURRENT_TIMESTAMP
        WHERE created_at IS NULL
        """,
        commit=True
    )

    # -----------------------------------------------------
    # REFERRAL HISTORY
    # -----------------------------------------------------

    db_execute(
        """
        CREATE TABLE IF NOT EXISTS referral_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            referrer_id INTEGER NOT NULL,
            referred_user_id INTEGER NOT NULL UNIQUE,
            reward INTEGER DEFAULT 0,
            created_at TIMESTAMP
                DEFAULT CURRENT_TIMESTAMP
        )
        """,
        commit=True
    )

    # -----------------------------------------------------
    # COIN HISTORY
    # -----------------------------------------------------

    db_execute(
        """
        CREATE TABLE IF NOT EXISTS coin_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            delta INTEGER NOT NULL,
            balance_after INTEGER NOT NULL,
            reason TEXT,
            created_at TIMESTAMP
                DEFAULT CURRENT_TIMESTAMP
        )
        """,
        commit=True
    )

    # -----------------------------------------------------
    # SETTINGS
    # -----------------------------------------------------

    db_execute(
        """
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )
        """,
        commit=True
    )

    db_execute(
        """
        INSERT OR IGNORE INTO settings(
            key,
            value
        )
        VALUES (
            'maintenance',
            '0'
        )
        """,
        commit=True
    )

    db_execute(
        """
        INSERT OR IGNORE INTO settings(
            key,
            value
        )
        VALUES (
            'admin_coin_initialized',
            '0'
        )
        """,
        commit=True
    )


setup_database()


# =========================================================
# USER FUNCTIONS
# =========================================================

def add_user(user_id):

    db_execute(
        """
        INSERT OR IGNORE INTO users(
            user_id
        )
        VALUES (?)
        """,
        (
            user_id,
        ),
        commit=True
    )


def sync_user(telegram_user):

    if not telegram_user:
        return

    user_id = telegram_user.id

    db_execute(
        """
        INSERT OR IGNORE INTO users(
            user_id
        )
        VALUES (?)
        """,
        (
            user_id,
        ),
        commit=True
    )

    db_execute(
        """
        UPDATE users
        SET
            username = ?,
            first_name = ?,
            last_name = ?
        WHERE user_id = ?
        """,
        (
            telegram_user.username,
            telegram_user.first_name,
            telegram_user.last_name,
            user_id
        ),
        commit=True
    )


def get_user(user_id):

    return db_execute(
        """
        SELECT
            user_id,
            username,
            first_name,
            last_name,
            coins,
            referred_by,
            referral_rewarded,
            blocked,
            created_at
        FROM users
        WHERE user_id = ?
        """,
        (
            user_id,
        ),
        fetchone=True
    )


def get_coins(user_id):

    row = db_execute(
        """
        SELECT coins
        FROM users
        WHERE user_id = ?
        """,
        (
            user_id,
        ),
        fetchone=True
    )

    return row[0] if row else 0


def add_coins(
    user_id,
    amount,
    reason="Manual credit"
):

    if amount <= 0:
        return False

    with db_lock:

        db.execute(
            """
            INSERT OR IGNORE INTO users(
                user_id
            )
            VALUES (?)
            """,
            (
                user_id,
            )
        )

        row = db.execute(
            """
            SELECT coins
            FROM users
            WHERE user_id = ?
            """,
            (
                user_id,
            )
        ).fetchone()

        current = row[0] if row else 0

        new_balance = current + amount

        db.execute(
            """
            UPDATE users
            SET coins = ?
            WHERE user_id = ?
            """,
            (
                new_balance,
                user_id
            )
        )

        db.execute(
            """
            INSERT INTO coin_history(
                user_id,
                delta,
                balance_after,
                reason
            )
            VALUES (?, ?, ?, ?)
            """,
            (
                user_id,
                amount,
                new_balance,
                reason
            )
        )

        db.commit()

    return True


def spend_coins(
    user_id,
    amount,
    reason="process"
):

    if amount <= 0:
        return False

    with db_lock:

        row = db.execute(
            """
            SELECT coins
            FROM users
            WHERE user_id = ?
            """,
            (
                user_id,
            )
        ).fetchone()

        if not row:
            return False

        current = row[0]

        if current < amount:
            return False

        new_balance = current - amount

        db.execute(
            """
            UPDATE users
            SET coins = ?
            WHERE user_id = ?
            """,
            (
                new_balance,
                user_id
            )
        )

        db.execute(
            """
            INSERT INTO coin_history(
                user_id,
                delta,
                balance_after,
                reason
            )
            VALUES (?, ?, ?, ?)
            """,
            (
                user_id,
                -amount,
                new_balance,
                reason
            )
        )

        db.commit()

    return True


def remove_coins(
    user_id,
    amount,
    reason="Admin removal"
):

    if amount <= 0:
        return False

    with db_lock:

        db.execute(
            """
            INSERT OR IGNORE INTO users(
                user_id
            )
            VALUES (?)
            """,
            (
                user_id,
            )
        )

        row = db.execute(
            """
            SELECT coins
            FROM users
            WHERE user_id = ?
            """,
            (
                user_id,
            )
        ).fetchone()

        current = row[0] if row else 0

        removed = min(
            current,
            amount
        )

        new_balance = current - removed

        db.execute(
            """
            UPDATE users
            SET coins = ?
            WHERE user_id = ?
            """,
            (
                new_balance,
                user_id
            )
        )

        if removed > 0:

            db.execute(
                """
                INSERT INTO coin_history(
                    user_id,
                    delta,
                    balance_after,
                    reason
                )
                VALUES (?, ?, ?, ?)
                """,
                (
                    user_id,
                    -removed,
                    new_balance,
                    reason
                )
            )

        db.commit()

    return True


def set_coins(
    user_id,
    amount,
    reason="Admin balance adjustment"
):

    if amount < 0:
        return False

    with db_lock:

        db.execute(
            """
            INSERT OR IGNORE INTO users(
                user_id
            )
            VALUES (?)
            """,
            (
                user_id,
            )
        )

        row = db.execute(
            """
            SELECT coins
            FROM users
            WHERE user_id = ?
            """,
            (
                user_id,
            )
        ).fetchone()

        current = row[0] if row else 0

        delta = amount - current

        db.execute(
            """
            UPDATE users
            SET coins = ?
            WHERE user_id = ?
            """,
            (
                amount,
                user_id
            )
        )

        if delta != 0:

            db.execute(
                """
                INSERT INTO coin_history(
                    user_id,
                    delta,
                    balance_after,
                    reason
                )
                VALUES (?, ?, ?, ?)
                """,
                (
                    user_id,
                    delta,
                    amount,
                    reason
                )
            )

        db.commit()

    return True


def get_referrals(user_id):

    row = db_execute(
        """
        SELECT COUNT(*)
        FROM referral_history
        WHERE referrer_id = ?
        """,
        (
            user_id,
        ),
        fetchone=True
    )

    return row[0] if row else 0


def get_referral_history(user_id, limit=20):

    return db_execute(
        """
        SELECT
            u.user_id,
            u.username,
            u.first_name,
            u.last_name,
            r.reward,
            r.created_at
        FROM referral_history r
        JOIN users u
            ON u.user_id = r.referred_user_id
        WHERE r.referrer_id = ?
        ORDER BY r.id DESC
        LIMIT ?
        """,
        (user_id, limit),
        fetchall=True
    ) or []


def user_name_from_row(row, user_id_index=0, username_index=1, first_index=2, last_index=3):

    if not row:
        return "User"

    username = row[username_index]
    first_name = row[first_index]
    last_name = row[last_index]

    if username:
        return f"@{escape(username)}"

    full_name = " ".join(
        part for part in (first_name, last_name) if part
    ).strip()

    return escape(full_name) if full_name else f"ID {row[user_id_index]}"


def get_positive_earnings(user_id):

    row = db_execute(
        """
        SELECT COALESCE(
            SUM(
                CASE
                    WHEN delta > 0
                    THEN delta
                    ELSE 0
                END
            ),
            0
        )
        FROM coin_history
        WHERE user_id = ?
        """,
        (
            user_id,
        ),
        fetchone=True
    )

    return row[0] if row else 0


def get_total_spent(user_id):

    row = db_execute(
        """
        SELECT COALESCE(
            SUM(
                CASE
                    WHEN delta < 0
                    THEN ABS(delta)
                    ELSE 0
                END
            ),
            0
        )
        FROM coin_history
        WHERE user_id = ?
        """,
        (
            user_id,
        ),
        fetchone=True
    )

    return row[0] if row else 0


def get_coin_history(
    user_id,
    limit=10
):

    return db_execute(
        """
        SELECT
            delta,
            balance_after,
            reason,
            created_at
        FROM coin_history
        WHERE user_id = ?
        ORDER BY id DESC
        LIMIT ?
        """,
        (
            user_id,
            limit
        ),
        fetchall=True
    )


def is_blocked(user_id):

    row = get_user(
        user_id
    )

    return bool(
        row and row[7]
    )


def set_block(
    user_id,
    value
):

    add_user(
        user_id
    )

    db_execute(
        """
        UPDATE users
        SET blocked = ?
        WHERE user_id = ?
        """,
        (
            1 if value else 0,
            user_id
        ),
        commit=True
    )


# =========================================================
# SETTINGS
# =========================================================

def get_setting(
    key,
    default="0"
):

    row = db_execute(
        """
        SELECT value
        FROM settings
        WHERE key = ?
        """,
        (
            key,
        ),
        fetchone=True
    )

    return (
        row[0]
        if row
        else default
    )


def set_setting(
    key,
    value
):

    db_execute(
        """
        INSERT OR REPLACE INTO settings(
            key,
            value
        )
        VALUES (?, ?)
        """,
        (
            key,
            str(value)
        ),
        commit=True
    )


def maintenance_enabled():

    return (
        get_setting(
            "maintenance",
            "0"
        ) == "1"
    )


# =========================================================
# ADMIN INITIAL COINS
# =========================================================

def ensure_admin_coins():

    if ADMIN_ID == 0:
        logger.warning(
            "ADMIN_ID is not configured."
        )
        return

    add_user(
        ADMIN_ID
    )

    initialized = get_setting(
        "admin_coin_initialized",
        "0"
    )

    if initialized == "1":
        return

    current = get_coins(
        ADMIN_ID
    )

    # If old database has 0, give the admin 500 once.
    # After initialization, restart will NEVER reset it.
    if current == 0:

        add_coins(
            ADMIN_ID,
            ADMIN_START_COINS,
            "Initial admin balance"
        )

        logger.info(
            "Admin %s received initial %s coins.",
            ADMIN_ID,
            ADMIN_START_COINS
        )

    set_setting(
        "admin_coin_initialized",
        "1"
    )


ensure_admin_coins()


# =========================================================
# STATISTICS
# =========================================================

def get_total_users():

    row = db_execute(
        """
        SELECT COUNT(*)
        FROM users
        """,
        fetchone=True
    )

    return row[0] if row else 0


def get_total_coins():

    row = db_execute(
        """
        SELECT COALESCE(
            SUM(coins),
            0
        )
        FROM users
        """,
        fetchone=True
    )

    return row[0] if row else 0


def get_total_referrals():

    row = db_execute(
        """
        SELECT COUNT(*)
        FROM referral_history
        """,
        fetchone=True
    )

    return row[0] if row else 0


def get_blocked_users():

    row = db_execute(
        """
        SELECT COUNT(*)
        FROM users
        WHERE blocked = 1
        """,
        fetchone=True
    )

    return row[0] if row else 0


# =========================================================
# WEB SERVER
# =========================================================

web = Flask(__name__)


@web.route("/")
def home():

    return "Telegram bot is running."


def run_web():

    port = int(
        os.environ.get(
            "PORT",
            10000
        )
    )

    web.run(
        host="0.0.0.0",
        port=port
    )


# =========================================================
# DISPLAY NAME
# =========================================================

def display_name(user):

    if not user:
        return "User"

    if user.username:
        return user.username

    if user.first_name:
        return user.first_name

    return "User"


def safe_name(user):

    return escape(
        display_name(user)
    )


# =========================================================
# KEYBOARDS
# =========================================================

def main_keyboard():

    keyboard = [
        [
            "🎯 𝗦𝗧𝗔𝗥𝗧",
            "🛑 𝗦𝗧𝗢𝗣",
        ],
        [
            "👤 𝗣𝗥𝗢𝗙𝗜𝗟𝗘",
            "👥 𝗥𝗘𝗙𝗘𝗥",
        ],
        [
            "🪙 𝗕𝗔𝗟𝗔𝗡𝗖𝗘",
            "📜 𝗛𝗜𝗦𝗧𝗢𝗥𝗬",
        ],
        [
        ],
    ]

    return ReplyKeyboardMarkup(
        keyboard,
        resize_keyboard=True
    )


def join_keyboard():

    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "📢 JOIN CHANNEL 1",
                    url=(
                        "https://t.me/"
                        f"{CHANNEL.lstrip('@')}"
                    )
                )
            ],
            [
                InlineKeyboardButton(
                    "📢 JOIN CHANNEL 2",
                    url=(
                        "https://t.me/"
                        f"{CHANNEL_2.lstrip('@')}"
                    )
                )
            ],
            [
                InlineKeyboardButton(
                    "✅ VERIFY JOIN",
                    callback_data="verify"
                )
            ]
        ]
    )

def admin_keyboard():

    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "📊 STATISTICS",
                    callback_data="admin_stats"
                ),
            ],
            [
                InlineKeyboardButton(
                    "🪙 SET COINS",
                    callback_data="admin_setcoins"
                ),
                InlineKeyboardButton(
                    "📢 BROADCAST",
                    callback_data="admin_broadcast"
                ),
            ],
            [
                InlineKeyboardButton(
                    "🟢 MAINTENANCE ON",
                    callback_data="maintenance_on"
                ),
                InlineKeyboardButton(
                    "🔴 MAINTENANCE OFF",
                    callback_data="maintenance_off"
                )
            ]
        ]
    )


# =========================================================
# ADMIN
# =========================================================

def is_admin(user_id):

    return (
        ADMIN_ID != 0
        and user_id == ADMIN_ID
    )


# =========================================================
# BASIC ACCESS
# =========================================================

async def check_basic_access(
    update,
    context
):

    user = update.effective_user

    if not user:
        return False

    sync_user(
        user
    )

    if is_blocked(
        user.id
    ):

        if update.message:

            await update.message.reply_text(
                "╔════════════════════╗\n"
                "       🚫 ACCESS BLOCKED\n"
                "╚════════════════════╝\n\n"
                "Your access to this bot is currently blocked.",
                parse_mode=ParseMode.HTML
            )

        return False

    if (
        maintenance_enabled()
        and not is_admin(user.id)
    ):

        if update.message:

            await update.message.reply_text(
                "╔════════════════════╗\n"
                "       🛠 MAINTENANCE\n"
                "╚════════════════════╝\n\n"
                "The system is temporarily under maintenance.\n"
                "Please try again later.",
                parse_mode=ParseMode.HTML
            )

        return False

    return True


# =========================================================
# CHANNEL MEMBERSHIP
# =========================================================

async def is_member(
    bot,
    user_id
):

    try:

        for channel in CHANNELS:

            member = await bot.get_chat_member(
                channel,
                user_id
            )

            if member.status in (
                "member",
                "administrator",
                "creator"
            ):
                continue

            if member.status == "restricted" and getattr(
                member,
                "is_member",
                False
            ):
                continue

            return False

        return True

    except TelegramError as error:

        logger.warning(
            "Membership check failed for %s: %s",
            user_id,
            error
        )

        return False

    except Exception as error:

        logger.exception(
            "Membership error: %s",
            error
        )

        return False


async def require_membership(
    update,
    context
):

    if not await check_basic_access(
        update,
        context
    ):

        return False

    user_id = (
        update.effective_user.id
    )

    if await is_member(
        context.bot,
        user_id
    ):

        return True

    if update.message:

        name = safe_name(
            update.effective_user
        )

        await update.message.reply_text(
            "╔════════════════════╗\n"
            f"     👋 HELLO, <b>{name}</b>\n"
            "╚════════════════════╝\n\n"
            "🔐 <b>CHANNEL VERIFICATION REQUIRED</b>\n\n"
            "Join both channels first, then press\n"
            "<b>VERIFY JOIN</b>.",
            parse_mode=ParseMode.HTML,
            reply_markup=join_keyboard()
        )

    return False


# =========================================================
# REFERRAL SYSTEM
# =========================================================

async def process_referral(
    user_id,
    context
):

    if not context.args:
        return

    try:

        referrer = int(
            context.args[0]
        )

    except ValueError:

        return

    if referrer == user_id:
        return

    with db_lock:

        db.execute(
            """
            INSERT OR IGNORE INTO users(
                user_id
            )
            VALUES (?)
            """,
            (
                user_id,
            )
        )

        referrer_row = db.execute(
            """
            SELECT user_id
            FROM users
            WHERE user_id = ?
            """,
            (
                referrer,
            )
        ).fetchone()

        if not referrer_row:
            return

        current = db.execute(
            """
            SELECT referred_by
            FROM users
            WHERE user_id = ?
            """,
            (
                user_id,
            )
        ).fetchone()

        if not current:
            return

        if current[0] is not None:
            return

        db.execute(
            """
            UPDATE users
            SET referred_by = ?
            WHERE user_id = ?
            AND referred_by IS NULL
            """,
            (
                referrer,
                user_id
            )
        )

        db.commit()

    logger.info(
        "Referral registered: %s -> %s",
        referrer,
        user_id
    )


async def reward_referrer_after_verification(
    user_id,
    bot=None
):

    with db_lock:

        row = db.execute(
            """
            SELECT
                referred_by,
                referral_rewarded
            FROM users
            WHERE user_id = ?
            """,
            (
                user_id,
            )
        ).fetchone()

        if not row:
            return

        referrer = row[0]
        rewarded = row[1]

        if not referrer or rewarded:
            return

        existing = db.execute(
            """
            SELECT id
            FROM referral_history
            WHERE referred_user_id = ?
            """,
            (
                user_id,
            )
        ).fetchone()

        if existing:

            db.execute(
                """
                UPDATE users
                SET referral_rewarded = 1
                WHERE user_id = ?
                """,
                (
                    user_id,
                )
            )

            db.commit()

            return

        referrer_row = db.execute(
            """
            SELECT coins, username, first_name, last_name
            FROM users
            WHERE user_id = ?
            """,
            (
                referrer,
            )
        ).fetchone()

        if not referrer_row:
            return

        current_balance = referrer_row[0]
        referrer_username = referrer_row[1]
        referrer_first_name = referrer_row[2]
        referrer_last_name = referrer_row[3]

        new_balance = (
            current_balance +
            REFERRAL_REWARD
        )

        db.execute(
            """
            UPDATE users
            SET coins = ?
            WHERE user_id = ?
            """,
            (
                new_balance,
                referrer
            )
        )

        db.execute(
            """
            INSERT INTO referral_history(
                referrer_id,
                referred_user_id,
                reward
            )
            VALUES (?, ?, ?)
            """,
            (
                referrer,
                user_id,
                REFERRAL_REWARD
            )
        )

        db.execute(
            """
            INSERT INTO coin_history(
                user_id,
                delta,
                balance_after,
                reason
            )
            VALUES (?, ?, ?, ?)
            """,
            (
                referrer,
                REFERRAL_REWARD,
                new_balance,
                f"Referral reward from {user_id}"
            )
        )

        db.execute(
            """
            UPDATE users
            SET referral_rewarded = 1
            WHERE user_id = ?
            """,
            (
                user_id,
            )
        )

        db.commit()

    logger.info(
        "Referral rewarded: %s +%s coins",
        referrer,
        REFERRAL_REWARD
    )

    if bot:
        referred_row = get_user(user_id)
        referred_name = user_name_from_row(
            referred_row, 0, 1, 2, 3
        ) if referred_row else f"ID {user_id}"

        if referrer_username:
            referrer_name = f"@{escape(referrer_username)}"
        else:
            referrer_name = escape(
                " ".join(
                    part for part in
                    (referrer_first_name, referrer_last_name)
                    if part
                ).strip() or f"ID {referrer}"
            )

        try:
            await bot.send_message(
                chat_id=referrer,
                text=(
                    "🔔 <b>REFERRAL VERIFIED</b>\n\n"
                    f"👤 <b>{referred_name}</b> has completed channel verification.\n"
                    f"🎁 Reward credited: <code>+{REFERRAL_REWARD}</code> coins\n"
                    f"💰 New balance: <code>{new_balance}</code>"
                ),
                parse_mode=ParseMode.HTML
            )
        except TelegramError as error:
            logger.warning(
                "Could not notify referrer %s: %s",
                referrer,
                error
            )

    return True


# =========================================================
# WELCOME ANIMATION
# =========================================================

async def welcome_animation(
    update,
    context,
    name
):

    message = await update.message.reply_text(
        "╔════════════════════╗\n"
        "       ⚡ INITIALIZING\n"
        "╚════════════════════╝\n\n"
        "🔄 Connecting...",
        parse_mode=ParseMode.HTML
    )

    frames = [
        (
            "╔════════════════════╗\n"
            "       ⚡ INITIALIZING\n"
            "╚════════════════════╝\n\n"
            "🔄 Connecting..."
        ),
        (
            "╔════════════════════╗\n"
            "       🔐 VERIFYING\n"
            "╚════════════════════╝\n\n"
            "🟢 User session detected..."
        ),
        (
            "╔════════════════════╗\n"
            f"       👋 WELCOME, {name}\n"
            "╚════════════════════╝\n\n"
            "⚡ Access granted..."
        ),
    ]

    for frame in frames:

        try:

            await message.edit_text(
                frame,
                parse_mode=ParseMode.HTML
            )

        except TelegramError:
            pass

        await asyncio.sleep(
            0.28
        )

    return message


# =========================================================
# /START
# =========================================================

async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    user = update.effective_user

    if not user:
        return

    sync_user(
        user
    )

    if not await check_basic_access(
        update,
        context
    ):

        return

    await process_referral(
        user.id,
        context
    )

    name = safe_name(
        user
    )

    member = await is_member(
        context.bot,
        user.id
    )

    if member:

        await reward_referrer_after_verification(
            user.id,
            context.bot
        )

        message = await welcome_animation(
            update,
            context,
            name
        )

        coins = get_coins(
            user.id
        )

        referrals = get_referrals(
            user.id
        )

        await asyncio.sleep(
            0.25
        )

        try:

            await message.edit_text(
                "╔══════════════════════════╗\n"
                f"       👋 <b>WELCOME, {name}</b>\n"
                "╚══════════════════════════╝\n\n"
                "🟢 <b>CHANNEL:</b> VERIFIED\n"
                "⚡ <b>ACCESS:</b> GRANTED\n\n"
                f"🪙 <b>BALANCE:</b> <code>{coins}</code>\n"
                f"👥 <b>REFERRALS:</b> <code>{referrals}</code>\n\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                "       <b>SELECT AN ACTION</b>\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━━━",
                parse_mode=ParseMode.HTML
            )

        except TelegramError:
            pass

        await update.message.reply_text(
            "✨ Your dashboard is ready.",
            reply_markup=main_keyboard()
        )

        return

    await update.message.reply_text(
        "╔══════════════════════════╗\n"
        f"       👋 <b>WELCOME, {name}</b>\n"
        "╚══════════════════════════╝\n\n"
        "🔐 <b>ACCESS VERIFICATION</b>\n\n"
        "1️⃣ Join both channels\n"
        "2️⃣ Press <b>VERIFY JOIN</b>\n"
        "3️⃣ Your dashboard will unlock\n\n"
        "⚡ Fast • Secure • Automated",
        parse_mode=ParseMode.HTML,
        reply_markup=join_keyboard()
    )


# =========================================================
# VERIFY
# =========================================================

async def verify(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    query = update.callback_query

    user = query.from_user

    sync_user(
        user
    )

    if is_blocked(
        user.id
    ):

        await query.answer(
            "Access blocked.",
            show_alert=True
        )

        return

    if await is_member(
        context.bot,
        user.id
    ):

        await reward_referrer_after_verification(
            user.id,
            context.bot
        )

        await query.answer(
            "✅ Verification successful!"
        )

        name = safe_name(
            user
        )

        try:

            await query.message.edit_text(
                "╔════════════════════╗\n"
                "      🟢 <b>VERIFIED</b>\n"
                "╚════════════════════╝\n\n"
                f"Welcome, <b>{name}</b>.\n"
                "Your access has been granted.",
                parse_mode=ParseMode.HTML
            )

        except TelegramError:
            pass

        await query.message.reply_text(
            "⚡ Dashboard unlocked.",
            reply_markup=main_keyboard()
        )

    else:

        await query.answer(
            "❌ Please join both channels first.",
            show_alert=True
        )


# =========================================================
# PROFILE
# =========================================================

async def profile(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if not await require_membership(
        update,
        context
    ):

        return

    user = update.effective_user

    sync_user(
        user
    )

    name = safe_name(
        user
    )

    loading = await update.message.reply_text(
        "╔════════════════════╗\n"
        "       ⏳ LOADING\n"
        "╚════════════════════╝\n\n"
        "Building your profile...",
        parse_mode=ParseMode.HTML
    )

    await asyncio.sleep(
        0.35
    )

    coins = get_coins(
        user.id
    )

    referrals = get_referrals(
        user.id
    )

    earned = get_positive_earnings(
        user.id
    )

    spent = get_total_spent(
        user.id
    )

    row = get_user(
        user.id
    )

    created_at = (
        row[8]
        if row
        else "Unknown"
    )

    username = (
        f"@{escape(user.username)}"
        if user.username
        else "Not set"
    )

    text = (
        "╔══════════════════════════╗\n"
        f"      👤 <b>{name}'S PROFILE</b>\n"
        "╚══════════════════════════╝\n\n"
        f"🆔 <b>Telegram ID:</b>\n"
        f"<code>{user.id}</code>\n\n"
        f"🏷 <b>Username:</b> {username}\n"
        f"🪙 <b>Coins:</b> <code>{coins}</code>\n"
        f"👥 <b>Referrals:</b> <code>{referrals}</code>\n"
        f"💎 <b>Total Earned:</b> <code>{earned}</code>\n"
        f"📉 <b>Total Spent:</b> <code>{spent}</code>\n"
        f"📅 <b>Joined:</b> <code>{created_at}</code>\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "🟢 <b>ACCOUNT STATUS:</b> ACTIVE"
    )

    try:

        await loading.edit_text(
            text,
            parse_mode=ParseMode.HTML
        )

    except TelegramError:

        await update.message.reply_text(
            text,
            parse_mode=ParseMode.HTML
        )


# =========================================================
# BALANCE
# =========================================================

async def balance(
    update,
    context
):

    if not await require_membership(
        update,
        context
    ):

        return

    user = update.effective_user

    coins = get_coins(
        user.id
    )

    await update.message.reply_text(
        "╔════════════════════╗\n"
        "       🪙 <b>BALANCE</b>\n"
        "╚════════════════════╝\n\n"
        f"👤 <b>{safe_name(user)}</b>\n\n"
        f"💰 Available Coins:\n"
        f"<code>{coins}</code>\n\n"
        f"🚀 Start Cost: <code>{START_COST}</code>\n"
        f"🎁 Referral Reward: <code>{REFERRAL_REWARD}</code>",
        parse_mode=ParseMode.HTML,
        reply_markup=main_keyboard()
    )


# =========================================================
# HISTORY
# =========================================================

async def history(
    update,
    context
):

    if not await require_membership(
        update,
        context
    ):

        return

    user = update.effective_user

    rows = get_coin_history(
        user.id,
        12
    )

    if not rows:

        await update.message.reply_text(
            "╔════════════════════╗\n"
            "       📜 <b>HISTORY</b>\n"
            "╚════════════════════╝\n\n"
            "No coin activity yet.",
            parse_mode=ParseMode.HTML,
            reply_markup=main_keyboard()
        )

        return

    lines = [
        "╔══════════════════════════════╗",
        "       📜 <b>COIN ACTIVITY</b>",
        "╚══════════════════════════════╝",
        "",
        "💡 <i>Every entry shows the date, coin change, balance and target.</i>",
        ""
    ]

    for delta, balance_after, reason, created_at in rows:

        sign = "+" if delta > 0 else ""
        icon = "🟢" if delta > 0 else "🔴"
        action = "CREDIT" if delta > 0 else "DEBIT"

        # Process entries are stored as: Process • <UPI ID>
        target = None
        if reason and "Process • " in reason:
            target = reason.split("Process • ", 1)[1].strip()
            reason_label = "Process"
        else:
            reason_label = reason or "Activity"

        lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
        lines.append(
            f"{icon} <b>{action}</b>   "
            f"<code>{sign}{delta} coins</code>"
        )
        lines.append(
            f"💰 <b>Balance:</b> <code>{balance_after} coins</code>"
        )
        lines.append(
            f"📅 <b>Date:</b> <code>{escape(str(created_at))}</code>"
        )
        lines.append(
            f"📝 <b>Reason:</b> {escape(reason_label)}"
        )
        if target:
            lines.append(
                f"🎯 <b>UPI Target:</b> <code>{escape(target)}</code>"
            )
        lines.append("")

    lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")

    await update.message.reply_text(
        "\n".join(lines),
        parse_mode=ParseMode.HTML,
        reply_markup=main_keyboard()
    )


async def referral_history(update, context):

    if not await require_membership(update, context):
        return

    user = update.effective_user
    rows = get_referral_history(user.id, 20)

    lines = [
        "╔══════════════════════════╗",
        "   👥 <b>REFERRAL HISTORY</b>",
        "╚══════════════════════════╝",
        ""
    ]

    if not rows:
        lines.extend([
            "🧪 <b>Demo</b> — No real referral data yet.",
            "Real referred users will appear here with their name/username."
        ])
    else:
        for index, row in enumerate(rows, start=1):
            referred_name = user_name_from_row(row)
            reward = row[4]
            created_at = escape(str(row[5]))
            lines.append(
                f"{index}. 👤 <b>{referred_name}</b>"
            )
            lines.append(
                f"   🎁 +<code>{reward}</code> coins • <i>{created_at}</i>"
            )
            lines.append("")

    await update.message.reply_text(
        "\n".join(lines),
        parse_mode=ParseMode.HTML,
        reply_markup=main_keyboard()
    )


# =========================================================
# REFER
# =========================================================

async def refer(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if not await require_membership(
        update,
        context
    ):

        return

    user = update.effective_user

    loading = await update.message.reply_text(
        "╔════════════════════╗\n"
        "       🔗 LOADING\n"
        "╚════════════════════╝\n\n"
        "Preparing your referral system...",
        parse_mode=ParseMode.HTML
    )

    await asyncio.sleep(
        0.35
    )

    bot_username = context.bot.username

    link = (
        f"https://t.me/"
        f"{bot_username}"
        f"?start={user.id}"
    )

    referrals = get_referrals(
        user.id
    )

    coins = get_coins(
        user.id
    )

    earned = get_positive_earnings(
        user.id
    )

    text = (
        "╔══════════════════════════╗\n"
        "       👥 <b>REFER & EARN</b>\n"
        "╚══════════════════════════╝\n\n"
        f"👤 <b>{safe_name(user)}</b>\n\n"
        f"🪙 <b>Current Coins:</b> <code>{coins}</code>\n"
        f"👥 <b>Successful Referrals:</b> <code>{referrals}</code>\n"
        f"💎 <b>Total Credits:</b> <code>{earned}</code>\n"
        f"🎁 <b>Per Referral:</b> <code>{REFERRAL_REWARD}</code>\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "🔗 <b>YOUR REFERRAL LINK</b>\n\n"
        f"<code>{escape(link)}</code>\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "⚡ Share your link with friends.\n"
        "Reward is credited after verification."
    )

    try:

        await loading.edit_text(
            text,
            parse_mode=ParseMode.HTML,
            reply_markup=main_keyboard()
        )

    except TelegramError:

        await update.message.reply_text(
            text,
            parse_mode=ParseMode.HTML,
            reply_markup=main_keyboard()
        )


# =========================================================
# START PROCESS
# =========================================================

async def start_process(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if not await require_membership(
        update,
        context
    ):

        return

    if context.user_data.get(
        "processing_active"
    ):

        await update.message.reply_text(
            "⚠️ A process is already running.\n"
            "Use STOP before starting another one.",
            reply_markup=main_keyboard()
        )

        return

    user_id = update.effective_user.id

    coins = get_coins(
        user_id
    )

    if coins < START_COST:

        await update.message.reply_text(
            "╔════════════════════╗\n"
            "       🪙 <b>COINS REQUIRED</b>\n"
            "╚════════════════════╝\n\n"
            f"💰 Available: <code>{coins}</code>\n"
            f"🎯 Required: <code>{START_COST}</code>\n\n"
            "❌ Insufficient coins.\n\n"
            f"👥 Refer friends to earn "
            f"<code>{REFERRAL_REWARD}</code> coins per verified referral.",
            parse_mode=ParseMode.HTML,
            reply_markup=main_keyboard()
        )

        return

    context.user_data[
        "waiting_for_id"
    ] = True

    context.user_data[
        "processing_stop_event"
    ] = None

    await update.message.reply_text(
        "╔══════════════════════════╗\n"
        "       💀 <b> START💀</b>\n"
        "╚══════════════════════════╝\n\n"
        f"🪙 <b>Balance:</b> <code>{coins}</code>\n"
        f"🎯 <b>Process Cost:</b> <code>{START_COST}</code>\n\n"
        "👻Send a UPI- ID.\n\n"
        "Example:\n"
        "<code>Rohan@UPI</code>\n\n"
        "⚠️ USE YOUR OWN RISK.",
        parse_mode=ParseMode.HTML,
        reply_markup=main_keyboard()
    )


# =========================================================
# STOP
# =========================================================

async def stop_process(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    context.user_data["waiting_for_id"] = False

    if not context.user_data.get("processing_active", False):
        await update.message.reply_text(
            "╔══════════════════════════╗\n"
            "      ⚠️ <b>NO PROCESSING</b>\n"
            "╚══════════════════════════╝\n\n"
            "🔎 <b>There is no processing running right now.</b>",
            parse_mode=ParseMode.HTML,
            reply_markup=main_keyboard()
        )
        return

    stop_event = context.user_data.get("processing_stop_event")
    if stop_event:
        stop_event.set()

    task = context.user_data.get("processing_task")
    processing_message = context.user_data.get("processing_message")

    # Stop/cancel the background task first so it cannot edit the
    # processing/timer message again after STOP is pressed.
    context.user_data["processing_active"] = False

    if task and not task.done():
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception as error:
            logger.warning("Processing task cancelled with error: %s", error)

    context.user_data["processing_stop_event"] = None
    context.user_data["processing_task"] = None
    context.user_data["processing_message"] = None

    # Delete the old processing/timer message completely.
    if processing_message:
        try:
            await processing_message.delete()
        except TelegramError:
            pass

    # Only the STOP SUCCESS message remains visible.
    await update.message.reply_text(
        "🛑 <b>Processing stopped successfully.</b>",
        parse_mode=ParseMode.HTML,
        reply_markup=main_keyboard()
    )



# =========================================================
#  ID VALIDATION
# =========================================================

def valid_id(text):
    """Validate UPI-ID syntax only.

    This checks the structure of a UPI ID, but cannot prove that the
    account actually exists. Real existence/ownership verification needs
    a payment-provider/bank verification API.
    """
    import re

    if not text:
        return False

    text = text.strip()

    # Practical UPI ID syntax: local-part@handle.
    # Keep it strict enough to reject arbitrary sentences/placeholders.
    if len(text) > 100 or text.count("@") != 1:
        return False

    left, right = text.split("@", 1)

    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{1,255}", left):
        return False

    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]{1,63}", right):
        return False

    return True


# =========================================================
# 9-MINUTE ACTIVE COUNTDOWN
# =========================================================

async def send_nine_minute_countdown(message, context):
    end = asyncio.get_running_loop().time() + 540
    while True:
        if not context.user_data.get("processing_active", False):
            return
        remaining = max(0, int(end - asyncio.get_running_loop().time()))
        minutes, seconds = divmod(remaining, 60)
        try:
            await message.edit_text(
                "╔══════════════════════════╗\n"
                "       ☠️ <b>PROCESSING COMPLETE☠️</b>\n"
                "╚══════════════════════════╝\n\n"
                "😈 <b>Bombing started successfully😈.</b>\n"
                "👽 <b>Active for 9 minutes</b>\n\n"
                f"🕐 Remaining: <code>{minutes:02d}:{seconds:02d}</code>",
                parse_mode=ParseMode.HTML
            )
        except TelegramError:
            pass
        if remaining <= 0:
            break
        await asyncio.sleep(1)
    context.user_data["processing_active"] = False
    context.user_data["processing_stop_event"] = None
    try:
        await message.edit_text(
            "✅ <b>9-minute session completed.</b>",
            parse_mode=ParseMode.HTML,
            reply_markup=main_keyboard()
        )
    except TelegramError:
        pass


# =========================================================
# PROCESSING ANIMATION
# =========================================================

async def processing_animation(
    message,
    target_id,
    stop_event,
    context
):

    progress_steps = [
        1,
        3,
        6,
        10,
        15,
        22,
        30,
        40,
        50,
        60,
        70,
        80,
        88,
        94,
        97,
        100
    ]

    flash = [
        "👽",
        "💀",
        "💥",
        "😈"
    ]

    for index, progress in enumerate(
        progress_steps
    ):

        if stop_event.is_set():

            try:

                await message.edit_text(
                    "╔════════════════════╗\n"
                    "       🛑 <b>CANCELLED</b>\n"
                    "╚════════════════════╝\n\n"
                    f"🆔 ID: <code>{escape(target_id)}</code>\n\n"
                    "🔴 Status: CANCELLED\n"
                    "⚠️ education purpose only.",
                    parse_mode=ParseMode.HTML,
                    reply_markup=main_keyboard()
                )

            except TelegramError:
                pass

            return False

        if progress <= 15:
            status = "⏳ INITIALIZING"

        elif progress <= 40:
            status = "⚙️ PROCESSING"

        elif progress <= 70:
            status = "⌛ ANALYZING"

        elif progress <= 90:
            status = "💫 FINALIZING"

        elif progress < 100:
            status = "🚀 COMPLETING"

        else:
            status = "✨ COMPLETE"

        total_blocks = 16

        filled = int(
            progress /
            100 *
            total_blocks
        )

        empty = (
            total_blocks -
            filled
        )

        bar = (
            "🟩" * filled +
            "⬜" * empty
        )

        icon = flash[
            index %
            len(flash)
        ]

        text = (
            "╔══════════════════════════╗\n"
            "       ⛄ <b> PROCESS</b>\n"
            "╚══════════════════════════╝\n\n"
            f"🆔 ID: <code>{escape(target_id)}</code>\n\n"
            f"{icon} <b>{status}</b> {icon}\n\n"
            f"<code>{progress}%</code>\n"
            f"{bar}\n\n"
            "⚠️ <i>EDUCATION PURPOSE ONLY</i>"
        )

        try:

            await message.edit_text(
                text,
                parse_mode=ParseMode.HTML
            )

        except RetryAfter as error:

            await asyncio.sleep(
                error.retry_after
            )

        except TelegramError as error:

            logger.warning(
                "Animation edit failed: %s",
                error
            )

        if progress <= 15:
            delay = 0.30

        elif progress <= 40:
            delay = 0.24

        elif progress <= 70:
            delay = 0.18

        elif progress <= 90:
            delay = 0.14

        else:
            delay = 0.12

        try:

            await asyncio.wait_for(
                stop_event.wait(),
                timeout=delay
            )

        except asyncio.TimeoutError:

            pass

    try:
        await message.edit_text(
            "╔══════════════════════════╗\n"
            "       💚 <b>PROCESSING COMPLETE</b>\n"
            "╚══════════════════════════╝\n\n"
            f"🆔 ID: <code>{escape(target_id)}</code>\n\n"
            "🟢 Status: <b>COMPLETED</b>\n"
            "📊 Progress: <code>100%</code>\n\n"
            "🚀 <b>Process started successfully.</b>\n"
            "⏳ Active for 9 minutes with countdown.",
            parse_mode=ParseMode.HTML
        )
        await send_nine_minute_countdown(message, context)
    except TelegramError:
        pass

    return True

# =========================================================
# TEXT HANDLER
# =========================================================

async def handle_text(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    user = update.effective_user

    if not user:
        return

    text = (
        update.message.text.strip()
    )

    if not await check_basic_access(
        update,
        context
    ):

        return

    # =====================================================
    # ADMIN ACTION STATE
    # =====================================================

    admin_action = context.user_data.get(
        "admin_action"
    )

    if admin_action and is_admin(
        user.id
    ):

        if text in (
            "🎯 𝗦𝗧𝗔𝗥𝗧",
            "👤 𝗣𝗥𝗢𝗙𝗜𝗟𝗘",
            "👥 𝗥𝗘𝗙𝗘𝗥",
            "🪙 𝗕𝗔𝗟𝗔𝗡𝗖𝗘",
            "📜 𝗛𝗜𝗦𝗧𝗢𝗥𝗬",
            "📜 𝗥𝗘𝗙𝗘𝗥𝗥𝗔𝗟 𝗛𝗜𝗦𝗧𝗢𝗥𝗬",
            "🛑 𝗦𝗧𝗢𝗣"
        ):

            context.user_data[
                "admin_action"
            ] = None

        else:

            # ---------------------------------------------
            # BROADCAST
            # ---------------------------------------------

            if admin_action == "broadcast":
                context.user_data["admin_action"] = None
                await run_broadcast(update, context, text)
                return

            # ---------------------------------------------
            # SET COINS
            # ---------------------------------------------

            if admin_action == "setcoins":

                parts = text.split()

                if len(parts) != 2:

                    await update.message.reply_text(
                        "❌ Format:\n"
                        "<code>USER_ID AMOUNT</code>",
                        parse_mode=ParseMode.HTML
                    )

                    return

                try:

                    target_id = int(
                        parts[0]
                    )

                    amount = int(
                        parts[1]
                    )

                    if amount < 0:
                        raise ValueError

                except ValueError:

                    await update.message.reply_text(
                        "❌ Invalid values."
                    )

                    return

                set_coins(
                    target_id,
                    amount,
                    f"Admin {user.id} set balance"
                )

                context.user_data[
                    "admin_action"
                ] = None

                await update.message.reply_text(
                    "╔════════════════════╗\n"
                    "       🪙 <b>UPDATED</b>\n"
                    "╚════════════════════╝\n\n"
                    f"👤 User: <code>{target_id}</code>\n"
                    f"💰 New Balance: <code>{amount}</code>",
                    parse_mode=ParseMode.HTML
                )

                return

    # =====================================================
    # STOP
    # =====================================================

    if text in (
        "🛑 𝗦𝗧𝗢𝗣",
        "🛑 Stop"
    ):

        await stop_process(
            update,
            context
        )

        return

    # =====================================================
    # WAITING FOR  ID
    # =====================================================

    if context.user_data.get(
        "waiting_for_id"
    ):

        if text in (
            "👤 𝗣𝗥𝗢𝗙𝗜𝗟𝗘",
            "👥 𝗥𝗘𝗙𝗘𝗥",
            "?? 𝗕𝗔𝗟𝗔𝗡𝗖𝗘",
            "📜 𝗛𝗜𝗦𝗧𝗢𝗥𝗬",
            "📜 𝗥𝗘𝗙𝗘𝗥𝗥𝗔𝗟 𝗛𝗜𝗦𝗧𝗢𝗥𝗬",
        ):

            context.user_data[
                "waiting_for_id"
            ] = False

        elif text == "🎯 𝗦𝗧𝗔𝗥𝗧":

            return

        else:

            if not await require_membership(
                update,
                context
            ):

                context.user_data[
                    "waiting_for_id"
                ] = False

                return

            if not valid_id(
                text
            ):

                await update.message.reply_text(
                    "❌ <b>INCORRECT UPI ID</b>\n\n"
                    "Please enter a properly formatted UPI ID, for example:\n"
                    "<code>name@bankhandle</code>\n\n"
                    "⚠️ The bot can check the format only. Actual UPI existence requires a verified payment-provider API.",
                    parse_mode=ParseMode.HTML,
                    reply_markup=main_keyboard()
                )

                return

            if not spend_coins(
                user.id,
                START_COST,
                f"Process • {text}"
            ):

                current_coins = get_coins(
                    user.id
                )

                context.user_data[
                    "waiting_for_id"
                ] = False

                await update.message.reply_text(
                    "╔════════════════════╗\n"
                    "       🪙 <b>INSUFFICIENT</b>\n"
                    "╚════════════════════╝\n\n"
                    f"Available: <code>{current_coins}</code>\n"
                    f"Required: <code>{START_COST}</code>",
                    parse_mode=ParseMode.HTML,
                    reply_markup=main_keyboard()
                )

                return

            context.user_data[
                "waiting_for_id"
            ] = False

            context.user_data[
                "processing_active"
            ] = True

            stop_event = asyncio.Event()

            context.user_data[
                "processing_stop_event"
            ] = stop_event

            processing_message = (
                await update.message.reply_text(
                    "╔══════════════════════════════╗\n"
                    "      😈 <b>𝙎𝙏𝘼𝙍𝙏𝙄𝙉𝙂...</b> ☠️\n"
                    "╚══════════════════════════════╝\n\n"
                    f"🆔 <b>ID:</b> <code>{escape(text)}</code>\n"
                    "🩸 <b>Progress:</b> <code>1%</code> 👿",
                    parse_mode=ParseMode.HTML
                )
            )

            context.user_data["processing_message"] = processing_message

            async def run_processing():
                try:
                    await processing_animation(
                        processing_message,
                        text,
                        stop_event,
                        context
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    logger.exception(
                        "Processing task failed: %s",
                        error
                    )
                finally:
                    context.user_data["processing_task"] = None
                    context.user_data["processing_message"] = None
                    if not context.user_data.get(
                        "processing_active",
                        False
                    ):
                        context.user_data["processing_stop_event"] = None

            context.user_data["processing_task"] = asyncio.create_task(
                run_processing()
            )

            return

    # =====================================================
    # MAIN BUTTONS
    # =====================================================

    if text == "🎯 𝗦𝗧𝗔𝗥𝗧":

        await start_process(
            update,
            context
        )

    elif text == "👤 𝗣𝗥𝗢𝗙𝗜𝗟𝗘":

        await profile(
            update,
            context
        )

    elif text == "👥 𝗥𝗘𝗙𝗘𝗥":

        await refer(
            update,
            context
        )

    elif text == "🪙 𝗕𝗔𝗟𝗔𝗡𝗖𝗘":

        await balance(
            update,
            context
        )

    elif text == "📜 𝗛𝗜𝗦𝗧𝗢𝗥𝗬":

        await history(
            update,
            context
        )

    elif text == "📜 𝗥𝗘𝗙𝗘𝗥𝗥𝗔𝗟 𝗛𝗜𝗦𝗧𝗢𝗥𝗬":

        await referral_history(
            update,
            context
        )


# =========================================================
# /ID
# =========================================================

async def my_id(
    update,
    context
):

    user = update.effective_user

    sync_user(
        user
    )

    await update.message.reply_text(
        "╔════════════════════╗\n"
        "        🆔 <b>YOUR ID</b>\n"
        "╚════════════════════╝\n\n"
        f"<code>{user.id}</code>\n\n"
        f"Username: "
        f"<code>{escape(user.username or 'Not set')}</code>",
        parse_mode=ParseMode.HTML
    )


# =========================================================
# /PROFILE
# =========================================================

async def profile_command(
    update,
    context
):

    await profile(
        update,
        context
    )


# =========================================================
# ADMIN PANEL
# =========================================================

async def admin_command(
    update,
    context
):

    if not is_admin(
        update.effective_user.id
    ):

        await update.message.reply_text(
            "🚫 Admin only."
        )

        return

    sync_user(
        update.effective_user
    )

    await update.message.reply_text(
        "╔══════════════════════════╗\n"
        "        👑 <b>ADMIN SYSTEM</b>\n"
        "╚══════════════════════════╝\n\n"
        f"🪙 Your Balance: "
        f"<code>{get_coins(update.effective_user.id)}</code>\n"
        f"👥 Users: <code>{get_total_users()}</code>\n"
        f"🔗 Referrals: <code>{get_total_referrals()}</code>\n\n"
        "Select an admin action:",
        parse_mode=ParseMode.HTML,
        reply_markup=admin_keyboard()
    )


# =========================================================
# ADMIN STATISTICS
# =========================================================

async def send_admin_stats(
    update,
    context,
    as_reply=True
):

    users = get_total_users()

    coins = get_total_coins()

    referrals = get_total_referrals()

    blocked = get_blocked_users()

    maintenance = (
        "ON 🟢"
        if maintenance_enabled()
        else "OFF 🔴"
    )

    lines = [
        "╔══════════════════════════╗",
        "        📊 <b>STATISTICS</b>",
        "╚══════════════════════════╝",
        "",
        f"👥 Users: <code>{users}</code>",
        f"🪙 Total Coins: <code>{coins}</code>",
        f"🔗 Referrals: <code>{referrals}</code>",
        f"🚫 Blocked: <code>{blocked}</code>",
        f"🎁 Referral Reward: <code>{REFERRAL_REWARD}</code>",
        f"🎬 Start Cost: <code>{START_COST}</code>",
        f"🛠 Maintenance: <code>{maintenance}</code>",
        "",
    ]



    text = "\n".join(
        lines
    )

    if as_reply:

        await update.message.reply_text(
            text,
            parse_mode=ParseMode.HTML
        )

    return text


async def admin_stats(
    update,
    context
):

    query = update.callback_query

    if not is_admin(
        query.from_user.id
    ):

        await query.answer(
            "Admin only.",
            show_alert=True
        )

        return

    await query.answer()

    text = await send_admin_stats(
        update,
        context,
        as_reply=False
    )

    await query.message.reply_text(
        text,
        parse_mode=ParseMode.HTML
    )


async def stats_command(
    update,
    context
):

    if not is_admin(
        update.effective_user.id
    ):

        await update.message.reply_text(
            "🚫 Admin only."
        )

        return

    await send_admin_stats(
        update,
        context
    )


# =========================================================
# ADMIN USER CARD
# =========================================================


# =========================================================
# ADD COINS
# =========================================================

async def add_coin_command(
    update,
    context
):

    if not is_admin(
        update.effective_user.id
    ):

        return

    if len(context.args) != 2:

        await update.message.reply_text(
            "Usage:\n"
            "<code>/addcoins USER_ID AMOUNT</code>",
            parse_mode=ParseMode.HTML
        )

        return

    try:

        user_id = int(
            context.args[0]
        )

        amount = int(
            context.args[1]
        )

        if amount <= 0:
            raise ValueError

    except ValueError:

        await update.message.reply_text(
            "❌ Invalid values."
        )

        return

    add_coins(
        user_id,
        amount,
        f"Admin {update.effective_user.id} added coins"
    )

    await update.message.reply_text(
        f"✅ Added <code>{amount}</code> coins "
        f"to <code>{user_id}</code>.",
        parse_mode=ParseMode.HTML
    )


# =========================================================
# REMOVE COINS
# =========================================================

async def remove_coin_command(
    update,
    context
):

    if not is_admin(
        update.effective_user.id
    ):

        return

    if len(context.args) != 2:

        await update.message.reply_text(
            "Usage:\n"
            "<code>/removecoins USER_ID AMOUNT</code>",
            parse_mode=ParseMode.HTML
        )

        return

    try:

        user_id = int(
            context.args[0]
        )

        amount = int(
            context.args[1]
        )

        if amount <= 0:
            raise ValueError

    except ValueError:

        await update.message.reply_text(
            "❌ Invalid values."
        )

        return

    remove_coins(
        user_id,
        amount,
        f"Admin {update.effective_user.id} removed coins"
    )

    await update.message.reply_text(
        f"✅ Removed <code>{amount}</code> coins "
        f"from <code>{user_id}</code>.",
        parse_mode=ParseMode.HTML
    )


# =========================================================
# SET COINS
# =========================================================

async def set_coins_command(
    update,
    context
):

    if not is_admin(
        update.effective_user.id
    ):

        return

    if len(context.args) != 2:

        await update.message.reply_text(
            "Usage:\n"
            "<code>/setcoins USER_ID AMOUNT</code>",
            parse_mode=ParseMode.HTML
        )

        return

    try:

        user_id = int(
            context.args[0]
        )

        amount = int(
            context.args[1]
        )

        if amount < 0:
            raise ValueError

    except ValueError:

        await update.message.reply_text(
            "❌ Invalid values."
        )

        return

    set_coins(
        user_id,
        amount,
        f"Admin {update.effective_user.id} set balance"
    )

    await update.message.reply_text(
        "╔════════════════════╗\n"
        "       🪙 <b>BALANCE SET</b>\n"
        "╚════════════════════╝\n\n"
        f"👤 User: <code>{user_id}</code>\n"
        f"💰 Balance: <code>{amount}</code>",
        parse_mode=ParseMode.HTML
    )


# =========================================================
# =========================================================
# BLOCK
# =========================================================

async def block_command(
    update,
    context
):

    if not is_admin(
        update.effective_user.id
    ):

        return

    if len(context.args) != 1:

        await update.message.reply_text(
            "Usage:\n"
            "<code>/block USER_ID</code>",
            parse_mode=ParseMode.HTML
        )

        return

    try:

        user_id = int(
            context.args[0]
        )

    except ValueError:

        await update.message.reply_text(
            "❌ Invalid User ID."
        )

        return

    set_block(
        user_id,
        True
    )

    await update.message.reply_text(
        f"🚫 User <code>{user_id}</code> blocked.",
        parse_mode=ParseMode.HTML
    )


# =========================================================
# UNBLOCK
# =========================================================

async def unblock_command(
    update,
    context
):

    if not is_admin(
        update.effective_user.id
    ):

        return

    if len(context.args) != 1:

        await update.message.reply_text(
            "Usage:\n"
            "<code>/unblock USER_ID</code>",
            parse_mode=ParseMode.HTML
        )

        return

    try:

        user_id = int(
            context.args[0]
        )

    except ValueError:

        await update.message.reply_text(
            "❌ Invalid User ID."
        )

        return

    set_block(
        user_id,
        False
    )

    await update.message.reply_text(
        f"🟢 User <code>{user_id}</code> unblocked.",
        parse_mode=ParseMode.HTML
    )


# =========================================================
# MAINTENANCE
# =========================================================

async def set_maintenance(
    update,
    context,
    value
):

    query = update.callback_query

    if not is_admin(
        query.from_user.id
    ):

        await query.answer(
            "Admin only.",
            show_alert=True
        )

        return

    set_setting(
        "maintenance",
        "1" if value else "0"
    )

    await query.answer(
        "Setting updated."
    )

    await query.message.reply_text(
        "🛠 Maintenance mode: "
        f"{'ON 🟢' if value else 'OFF 🔴'}"
    )


# =========================================================
# CALLBACK HANDLER
# =========================================================
# ADMIN BROADCAST
# =========================================================

async def admin_broadcast(update, context):
    query = update.callback_query
    if not is_admin(query.from_user.id):
        await query.answer("Admin only.", show_alert=True)
        return
    context.user_data["admin_action"] = "broadcast"
    await query.answer()
    await query.message.reply_text(
        "📢 <b>BROADCAST</b>\n\n"
        "✍️ Send the message to broadcast to all users.",
        parse_mode=ParseMode.HTML
    )


async def run_broadcast(update, context, broadcast_text):
    rows = db_execute(
        "SELECT user_id FROM users WHERE blocked = 0",
        fetchall=True
    ) or []
    sent = failed = 0
    for row in rows:
        try:
            await context.bot.send_message(
                chat_id=row[0],
                text=broadcast_text,
                parse_mode=ParseMode.HTML
            )
            sent += 1
        except TelegramError:
            failed += 1

    await update.message.reply_text(
        "╔══════════════════════════╗\n"
        "       📢 <b>BROADCAST DONE</b>\n"
        "╚══════════════════════════╝\n\n"
        f"✅ <b>Sent:</b> <code>{sent}</code>\n"
        f"❌ <b>Failed:</b> <code>{failed}</code>",
        parse_mode=ParseMode.HTML,
        reply_markup=main_keyboard()
    )


# =========================================================

async def button_handler(
    update,
    context
):

    query = update.callback_query

    data = query.data

    # -----------------------------------------------------
    # VERIFY
    # -----------------------------------------------------

    if data == "verify":

        await verify(
            update,
            context
        )

        return

    # -----------------------------------------------------
    # -----------------------------------------------------
    # -----------------------------------------------------
    # ADMIN BROADCAST
    # -----------------------------------------------------

    if data == "admin_broadcast":
        await admin_broadcast(update, context)
        return

    # ADMIN STATS
    # -----------------------------------------------------

    if data == "admin_stats":

        await admin_stats(
            update,
            context
        )

        return

    # -----------------------------------------------------
    # ADMIN SET COINS
    # -----------------------------------------------------

    if data == "admin_setcoins":

        if not is_admin(
            query.from_user.id
        ):

            await query.answer(
                "Admin only.",
                show_alert=True
            )

            return

        context.user_data[
            "admin_action"
        ] = "setcoins"

        await query.answer()

        await query.message.reply_text(
            "🪙 <b>SET COINS</b>\n\n"
            "Send:\n"
            "<code>USER_ID AMOUNT</code>\n\n"
            "Example:\n"
            "<code>123456789 500</code>",
            parse_mode=ParseMode.HTML
        )

        return

    # -----------------------------------------------------
    # MAINTENANCE ON
    # -----------------------------------------------------

    if data == "maintenance_on":

        await set_maintenance(
            update,
            context,
            True
        )

        return

    # -----------------------------------------------------
    # MAINTENANCE OFF
    # -----------------------------------------------------

    if data == "maintenance_off":

        await set_maintenance(
            update,
            context,
            False
        )

        return


# =========================================================
# ERROR HANDLER
# =========================================================

async def error_handler(
    update,
    context
):

    logger.exception(
        "Unhandled exception:",
        exc_info=context.error
    )


# =========================================================
# MAIN
# =========================================================

def main():

    # -----------------------------------------------------
    # WEB SERVER
    # -----------------------------------------------------

    Thread(
        target=run_web,
        daemon=True
    ).start()

    # -----------------------------------------------------
    # TELEGRAM APPLICATION
    # -----------------------------------------------------

    app = (
        Application
        .builder()
        .token(BOT_TOKEN)
        .build()
    )

    # -----------------------------------------------------
    # COMMANDS
    # -----------------------------------------------------

    app.add_handler(
        CommandHandler(
            "start",
            start
        )
    )

    app.add_handler(
        CommandHandler(
            "id",
            my_id
        )
    )

    app.add_handler(
        CommandHandler(
            "profile",
            profile_command
        )
    )

    app.add_handler(
        CommandHandler(
            "balance",
            balance
        )
    )

    app.add_handler(
        CommandHandler(
            "history",
            history
        )
    )

    app.add_handler(
        CommandHandler(
            "referralhistory",
            referral_history
        )
    )

    app.add_handler(
        CommandHandler(
            "admin",
            admin_command
        )
    )

    app.add_handler(
        CommandHandler(
            "stats",
            stats_command
        )
    )

    app.add_handler(
        CommandHandler(
            "addcoins",
            add_coin_command
        )
    )

    app.add_handler(
        CommandHandler(
            "removecoins",
            remove_coin_command
        )
    )

    app.add_handler(
        CommandHandler(
            "setcoins",
            set_coins_command
        )
    )

    app.add_handler(
        CommandHandler(
            "block",
            block_command
        )
    )

    app.add_handler(
        CommandHandler(
            "unblock",
            unblock_command
        )
    )



    # -----------------------------------------------------
    # INLINE BUTTONS
    # -----------------------------------------------------

    app.add_handler(
        CallbackQueryHandler(
            button_handler
        )
    )

    # -----------------------------------------------------
    # TEXT
    # -----------------------------------------------------

    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            handle_text
        )
    )

    # -----------------------------------------------------
    # ERRORS
    # -----------------------------------------------------

    app.add_error_handler(
        error_handler
    )

    logger.info(
        "Bot is starting..."
    )

    app.run_polling()


# =========================================================
# RUN
# =========================================================

if __name__ == "__main__":

    main()