from telegram import InlineKeyboardButton
from telegram import InlineKeyboardMarkup


def admin_keyboard():

    keyboard = [

        [
            InlineKeyboardButton(
                "📊 الإحصائيات",
                callback_data="stats"
            )
        ],

        [
            InlineKeyboardButton(
                "👥 المستخدمون",
                callback_data="users"
            )
        ],

        [
            InlineKeyboardButton(
                "📢 البث الجماعي",
                callback_data="broadcast"
            )
        ],

        [
            InlineKeyboardButton(
                "🚫 حظر مستخدم",
                callback_data="ban"
            )
        ]
    ]

    return InlineKeyboardMarkup(keyboard)
