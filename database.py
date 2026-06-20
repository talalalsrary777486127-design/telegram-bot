import sqlite3
import os
from datetime import datetime

DB_PATH = os.path.join(os.path.dirname(__file__), "links.db")


def get_connection():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db():
    with get_connection() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS links (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                url TEXT UNIQUE NOT NULL,
                category TEXT NOT NULL,
                subcategory TEXT NOT NULL DEFAULT 'other',
                source TEXT NOT NULL,
                first_seen_by INTEGER,
                first_seen_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                full_name TEXT,
                first_seen TEXT NOT NULL,
                last_seen TEXT NOT NULL,
                submission_count INTEGER DEFAULT 0,
                output_mode TEXT NOT NULL DEFAULT 'separate'
            );

            CREATE TABLE IF NOT EXISTS user_links (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                link_id INTEGER NOT NULL,
                submitted_at TEXT NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users(user_id),
                FOREIGN KEY (link_id) REFERENCES links(id)
            );

            CREATE TABLE IF NOT EXISTS settings (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_links_category ON links(category);
            CREATE INDEX IF NOT EXISTS idx_links_url ON links(url);
            CREATE INDEX IF NOT EXISTS idx_user_links_user ON user_links(user_id);
            CREATE INDEX IF NOT EXISTS idx_user_links_link ON user_links(link_id);
        """)
        # Migrate existing tables — ignore errors if columns already exist
        for stmt in [
            "ALTER TABLE links ADD COLUMN subcategory TEXT NOT NULL DEFAULT 'other'",
            "ALTER TABLE users ADD COLUMN output_mode TEXT NOT NULL DEFAULT 'separate'",
        ]:
            try:
                conn.execute(stmt)
            except Exception:
                pass
        # Create subcategory index after column is guaranteed to exist
        try:
            conn.execute("CREATE INDEX IF NOT EXISTS idx_links_subcategory ON links(subcategory)")
        except Exception:
            pass
        conn.commit()


def upsert_user(user_id: int, username: str, full_name: str):
    now = datetime.utcnow().isoformat()
    with get_connection() as conn:
        conn.execute("""
            INSERT INTO users (user_id, username, full_name, first_seen, last_seen, submission_count)
            VALUES (?, ?, ?, ?, ?, 0)
            ON CONFLICT(user_id) DO UPDATE SET
                username = excluded.username,
                full_name = excluded.full_name,
                last_seen = excluded.last_seen
        """, (user_id, username or "", full_name or "", now, now))
        conn.commit()


def add_links(urls: list[str], category: str, source: str, user_id: int) -> tuple[int, int, list[str]]:
    """Returns (new_count, duplicate_count, new_urls). Auto-detects subcategory."""
    from extractor import get_subcategory
    now = datetime.utcnow().isoformat()
    new_count = 0
    dup_count = 0
    new_urls: list[str] = []

    with get_connection() as conn:
        for url in urls:
            row = conn.execute("SELECT id FROM links WHERE url = ?", (url,)).fetchone()
            if row:
                dup_count += 1
                link_id = row["id"]
            else:
                subcat = get_subcategory(url, category)
                cur = conn.execute(
                    "INSERT INTO links (url, category, subcategory, source, first_seen_by, first_seen_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (url, category, subcat, source, user_id, now)
                )
                link_id = cur.lastrowid
                new_count += 1
                new_urls.append(url)

            conn.execute(
                "INSERT INTO user_links (user_id, link_id, submitted_at) VALUES (?, ?, ?)",
                (user_id, link_id, now)
            )

        conn.execute(
            "UPDATE users SET submission_count = submission_count + ?, last_seen = ? WHERE user_id = ?",
            (new_count, now, user_id)
        )
        conn.commit()

    return new_count, dup_count, new_urls


def get_setting(key: str) -> str | None:
    with get_connection() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def set_setting(key: str, value: str) -> None:
    with get_connection() as conn:
        conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value)
        )
        conn.commit()


def del_setting(key: str) -> None:
    with get_connection() as conn:
        conn.execute("DELETE FROM settings WHERE key = ?", (key,))
        conn.commit()


def get_all_links(category: str = None, subcategory: str = None) -> list[dict]:
    with get_connection() as conn:
        if category and subcategory:
            rows = conn.execute(
                "SELECT * FROM links WHERE category = ? AND subcategory = ? ORDER BY url",
                (category, subcategory)
            ).fetchall()
        elif category:
            rows = conn.execute(
                "SELECT * FROM links WHERE category = ? ORDER BY url", (category,)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM links ORDER BY category, subcategory, url"
            ).fetchall()
    return [dict(r) for r in rows]


def get_latest_links(limit: int = 50) -> list[dict]:
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM links ORDER BY first_seen_at DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]


def get_top_by_subcategory(subcategory: str, category: str = None, limit: int = 25) -> list[dict]:
    """Returns links sorted by how many times they were submitted."""
    with get_connection() as conn:
        if category:
            rows = conn.execute("""
                SELECT l.url, l.category, l.subcategory, l.first_seen_at,
                       COUNT(ul.id) as submit_count
                FROM links l
                LEFT JOIN user_links ul ON l.id = ul.link_id
                WHERE l.subcategory = ? AND l.category = ?
                GROUP BY l.id
                ORDER BY submit_count DESC, l.url ASC
                LIMIT ?
            """, (subcategory, category, limit)).fetchall()
        else:
            rows = conn.execute("""
                SELECT l.url, l.category, l.subcategory, l.first_seen_at,
                       COUNT(ul.id) as submit_count
                FROM links l
                LEFT JOIN user_links ul ON l.id = ul.link_id
                WHERE l.subcategory = ?
                GROUP BY l.id
                ORDER BY submit_count DESC, l.url ASC
                LIMIT ?
            """, (subcategory, limit)).fetchall()
    return [dict(r) for r in rows]


def search_links(query: str) -> list[dict]:
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM links WHERE url LIKE ? ORDER BY category, subcategory, url",
            (f"%{query}%",)
        ).fetchall()
    return [dict(r) for r in rows]


def get_global_stats() -> dict:
    with get_connection() as conn:
        total = conn.execute("SELECT COUNT(*) as c FROM links").fetchone()["c"]
        by_cat = conn.execute(
            "SELECT category, COUNT(*) as c FROM links GROUP BY category"
        ).fetchall()
        by_subcat = conn.execute(
            "SELECT category, subcategory, COUNT(*) as c FROM links GROUP BY category, subcategory"
        ).fetchall()
        total_users = conn.execute("SELECT COUNT(*) as c FROM users").fetchone()["c"]
    return {
        "total": total,
        "by_category": {r["category"]: r["c"] for r in by_cat},
        "by_subcategory": {f"{r['category']}_{r['subcategory']}": r["c"] for r in by_subcat},
        "total_users": total_users,
    }


def get_user_stats(user_id: int) -> dict:
    with get_connection() as conn:
        user = conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()
        if not user:
            return {}
        by_cat = conn.execute("""
            SELECT l.category, COUNT(*) as c
            FROM user_links ul
            JOIN links l ON ul.link_id = l.id
            WHERE ul.user_id = ?
            GROUP BY l.category
        """, (user_id,)).fetchall()
        total_submissions = conn.execute(
            "SELECT COUNT(*) as c FROM user_links WHERE user_id = ?", (user_id,)
        ).fetchone()["c"]
    return {
        "user": dict(user),
        "by_category": {r["category"]: r["c"] for r in by_cat},
        "total_submissions": total_submissions,
    }


def get_top_users(limit: int = 10) -> list[dict]:
    with get_connection() as conn:
        rows = conn.execute("""
            SELECT u.user_id, u.username, u.full_name, u.last_seen,
                   COUNT(ul.id) as submission_count
            FROM users u
            LEFT JOIN user_links ul ON u.user_id = ul.user_id
            GROUP BY u.user_id
            ORDER BY submission_count DESC
            LIMIT ?
        """, (limit,)).fetchall()
    return [dict(r) for r in rows]


def get_all_users() -> list[dict]:
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM users ORDER BY last_seen DESC"
        ).fetchall()
    return [dict(r) for r in rows]


def get_user_mode(user_id: int) -> str:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT output_mode FROM users WHERE user_id = ?", (user_id,)
        ).fetchone()
    return row["output_mode"] if row else "separate"


def set_user_mode(user_id: int, mode: str):
    with get_connection() as conn:
        conn.execute(
            "UPDATE users SET output_mode = ? WHERE user_id = ?", (mode, user_id)
        )
        conn.commit()
