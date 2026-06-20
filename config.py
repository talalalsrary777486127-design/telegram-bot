import os

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

ADMIN_USER_ID = int(
    os.getenv("ADMIN_USER_ID", "0")
)

MAX_FILE_SIZE = 50 * 1024 * 1024
