import shutil
import datetime


def create_backup():

    now = datetime.datetime.now()

    backup_name = (
        f"backups/links_{now}.db"
    )

    shutil.copy(
        "links.db",
        backup_name
    )

    return backup_name
