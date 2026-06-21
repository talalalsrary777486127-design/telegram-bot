import shutil
import os
from datetime import datetime


def create_backup():

    os.makedirs(
        "backups",
        exist_ok=True
    )

    timestamp = datetime.now().strftime(
        "%Y%m%d_%H%M%S"
    )

    backup_file = (
        f"backups/links_{timestamp}.db"
    )

    shutil.copy(
        "links.db",
        backup_file
    )

    return backup_file
