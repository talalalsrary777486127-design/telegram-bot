from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from config import ADMIN_USER_ID


def is_admin(user_id):
    return user_id == ADMIN_USER_ID


def admin_keyboard():

    return InlineKeyboardMarkup([

        [
            InlineKeyboardButton(
                "📊 Statistics",
                callback_data="admin_stats"
            )
        ],

        [
            InlineKeyboardButton(
                "👥 Users",
                callback_data="admin_users"
            )
        ],

        [
            InlineKeyboardButton(
                "🏆 Top Users",
                callback_data="admin_top"
            )
        ],

        [
            InlineKeyboardButton(
                "📢 Broadcast",
                callback_data="admin_broadcast"
            )
        ],

        [
            InlineKeyboardButton(
                "💾 Backup",
                callback_data="admin_backup"
            )
        ],

        [
            InlineKeyboardButton(
                "📦 Export DB",
                callback_data="admin_export"
            )
        ]
    ])
