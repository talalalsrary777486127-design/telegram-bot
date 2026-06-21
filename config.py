import os


BOT_TOKEN = os.getenv(
    "TELEGRAM_BOT_TOKEN"
)

ADMIN_USER_ID = int(
    os.getenv(
        "ADMIN_USER_ID",
        "0"
    )
)

DATABASE_PATH = os.getenv(
    "DATABASE_PATH",
    "links.db"
)

MAX_FILE_SIZE = int(
    os.getenv(
        "MAX_FILE_SIZE",
        50 * 1024 * 1024
    )
)

BACKUP_FOLDER = os.getenv(
    "BACKUP_FOLDER",
    "backups"
)
