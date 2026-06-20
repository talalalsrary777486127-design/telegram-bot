import os
import io
import logging
from datetime import datetime

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, filters, ContextTypes
)
from telegram.constants import ParseMode

import database as db
import extractor as ex
import exporter as exp

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

ADMIN_USER_ID = int(os.environ.get("ADMIN_USER_ID", "0"))
CHUNK_SIZE = 100
DIVIDER = "━━━━━━━━━━━━━━"


def is_admin(user_id: int) -> bool:
    return ADMIN_USER_ID != 0 and user_id == ADMIN_USER_ID


async def ensure_user(update: Update):
    u = update.effective_user
    db.upsert_user(u.id, u.username, u.full_name)


def _result_keyboard() -> InlineKeyboardMarkup:
    """5-button keyboard shown after every extraction and in forum replies."""
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📢 Telegram Links", callback_data="show_telegram"),
            InlineKeyboardButton("💬 WhatsApp Links", callback_data="show_whatsapp"),
        ],
        [InlineKeyboardButton("🔗 Other Links",   callback_data="show_other")],
        [
            InlineKeyboardButton("📊 Statistics", callback_data="show_stats"),
            InlineKeyboardButton("📥 Export",     callback_data="show_export_latest"),
        ],
    ])


def chunks(lst: list, size: int):
    for i in range(0, len(lst), size):
        yield lst[i:i + size]


def escape_md(text: str) -> str:
    special = r"\_*[]()~`>#+-=|{}.!"
    return "".join(f"\\{c}" if c in special else c for c in str(text))


def format_link_block(title: str, emoji: str, links: list[str], offset: int = 0) -> str:
    header = f"{emoji} *{escape_md(title)}*\n{DIVIDER}"
    if not links:
        return f"{header}\n\n_No links found_"
    numbered = "\n".join(f"{offset + i + 1}\\. {escape_md(url)}" for i, url in enumerate(links))
    return f"{header}\n\n{numbered}"


TG_MSG_LIMIT = 4096


async def _send_category(message, emoji: str, title: str, links: list[str], txt_filename: str) -> None:
    """Send ALL links for one category in exactly ONE message.
    If the text would exceed Telegram's 4096-char limit, send a TXT file instead."""
    if not links:
        return
    links = sorted(set(links))          # deduplicate + sort alphabetically
    header = f"{emoji} <b>{title} ({len(links)})</b>"
    body_lines = [f"{i}. {url}" for i, url in enumerate(links, start=1)]
    text = header + "\n" + "\n".join(body_lines)

    if len(text) <= TG_MSG_LIMIT:
        await message.reply_text(text, parse_mode="HTML", disable_web_page_preview=True)
    else:
        plain = f"{emoji} {title} ({len(links)})\n\n" + "\n".join(body_lines)
        buf = io.BytesIO(plain.encode("utf-8"))
        await message.reply_document(
            buf,
            filename=txt_filename,
            caption=f"{emoji} <b>{title}</b> — {len(links)} links (sent as file, message too long)",
            parse_mode="HTML",
        )


async def send_category_messages(
    update: Update,
    category_name: str,
    emoji: str,
    links: list[str],
    keyboard: InlineKeyboardMarkup = None,
) -> None:
    if not links:
        return
    link_chunks = list(chunks(links, CHUNK_SIZE))
    for idx, chunk in enumerate(link_chunks):
        offset = idx * CHUNK_SIZE
        n = len(link_chunks)
        part = f" \\(part {idx+1}/{n}\\)" if n > 1 else ""
        text = format_link_block(f"{category_name} LINKS{part}", emoji, chunk, offset)
        is_last = idx == n - 1
        await update.message.reply_text(
            text,
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=keyboard if is_last else None,
            disable_web_page_preview=True,
        )


# _download_keyboard is now _result_keyboard — single unified keyboard
_download_keyboard = _result_keyboard


async def send_separate_output(update, tg, wa, other, new_total, dup_total, source_label=""):
    # Deduplicate + sort each list before display
    tg    = sorted(set(tg))
    wa    = sorted(set(wa))
    other = sorted(set(other))
    total = len(tg) + len(wa) + len(other)
    label = f" from <b>{source_label}</b>" if source_label else ""
    summary = (
        f"✅ <b>{total} link(s) found{label}</b>\n\n"
        f"📢 Telegram: <b>{len(tg)}</b>\n"
        f"💬 WhatsApp: <b>{len(wa)}</b>\n"
        f"🔗 Other: <b>{len(other)}</b>\n\n"
        f"🆕 New: <b>{new_total}</b> | ♻️ Duplicates: <b>{dup_total}</b>"
    )
    await update.message.reply_text(summary, parse_mode="HTML")

    await _send_category(update.message, "📢", "Telegram Links", tg,    "telegram_links.txt")
    await _send_category(update.message, "💬", "WhatsApp Links", wa,    "whatsapp_links.txt")
    await _send_category(update.message, "🔗", "Other Links",    other, "other_links.txt")

    await update.message.reply_text(
        "Download your links as Excel files:",
        reply_markup=_download_keyboard()
    )


async def send_merged_output(update, tg, wa, other, new_total, dup_total, source_label=""):
    label = f" in `{escape_md(source_label)}`" if source_label else ""
    total = len(tg) + len(wa) + len(other)
    summary = (
        f"✅ *{total} link\\(s\\) found{label}*\n\n"
        f"📱 Telegram: *{len(tg)}*\n"
        f"💬 WhatsApp: *{len(wa)}*\n"
        f"🔗 Other: *{len(other)}*\n\n"
        f"🆕 New: *{new_total}* \\| ♻️ Duplicates: *{dup_total}*\n\n"
        "_Generating files\\.\\.\\._"
    )
    await update.message.reply_text(summary, parse_mode=ParseMode.MARKDOWN_V2)

    tg_rows = [{"url": u, "source": "text", "first_seen_at": "", "subcategory": ex.get_subcategory(u, "telegram")} for u in tg]
    wa_rows = [{"url": u, "source": "text", "first_seen_at": "", "subcategory": ex.get_subcategory(u, "whatsapp")} for u in wa]
    ot_rows = [{"url": u, "source": "text", "first_seen_at": "", "subcategory": "other"} for u in other]

    txt_buf = exp.build_txt(tg, wa, other)
    tg_xlsx = exp.build_category_excel(tg_rows, "telegram", "Telegram")
    wa_xlsx = exp.build_category_excel(wa_rows, "whatsapp", "WhatsApp")
    other_xlsx = exp.build_category_excel(ot_rows, "other", "Other")

    await update.message.reply_document(txt_buf, filename="links.txt", caption="📄 All links (TXT)")
    await update.message.reply_document(tg_xlsx, filename="Telegram.xlsx", caption="📱 Telegram")
    await update.message.reply_document(wa_xlsx, filename="WhatsApp.xlsx", caption="💬 WhatsApp")
    await update.message.reply_document(other_xlsx, filename="Other.xlsx", caption="🔗 Other")
    await update.message.reply_text("Use the buttons below:", reply_markup=_result_keyboard())


async def _send_to_thread(
    bot, chat_id: int, thread_id: int,
    emoji: str, title: str, links: list[str], filename: str,
    source_note: str = "",
):
    """Post a categorised link list to a forum topic; falls back to .txt if too long."""
    links = sorted(set(links))
    note = f"\n<i>Source: {source_note}</i>" if source_note else ""
    header = f"{emoji} <b>{title} ({len(links)} new link{'s' if len(links) != 1 else ''})</b>{note}"
    body_lines = [f"{i}. {url}" for i, url in enumerate(links, 1)]
    text = header + "\n" + "\n".join(body_lines)
    if len(text) <= 4096:
        await bot.send_message(
            chat_id=chat_id,
            message_thread_id=thread_id,
            text=text,
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
    else:
        plain = f"{emoji} {title} ({len(links)} new links)\n{source_note}\n\n" + "\n".join(body_lines)
        buf = io.BytesIO(plain.encode("utf-8"))
        buf.name = filename
        await bot.send_document(
            chat_id=chat_id,
            message_thread_id=thread_id,
            document=buf,
            filename=filename,
            caption=f"{emoji} <b>{title}</b> — {len(links)} new links",
            parse_mode="HTML",
        )


async def _publish_to_forum(
    bot,
    new_tg: list[str],
    new_wa: list[str],
    new_other: list[str],
    source_note: str = "",
):
    """Publish newly added links to the configured group forum topics (if set up)."""
    forum_chat_str = db.get_setting("forum_chat_id")
    if not forum_chat_str:
        return
    forum_chat_id = int(forum_chat_str)
    mapping = [
        ("forum_topic_tg",    new_tg,    "📢", "Telegram Links", "telegram_links.txt"),
        ("forum_topic_wa",    new_wa,    "💬", "WhatsApp Links",  "whatsapp_links.txt"),
        ("forum_topic_other", new_other, "🔗", "Other Links",     "other_links.txt"),
    ]
    for setting_key, links, emoji, title, fname in mapping:
        topic_str = db.get_setting(setting_key)
        if topic_str and links:
            try:
                await _send_to_thread(
                    bot, forum_chat_id, int(topic_str),
                    emoji, title, links, fname, source_note,
                )
            except Exception as exc:
                logger.warning(f"Forum publish failed ({title}): {exc}")


async def process_and_reply(update, context, extracted, source, source_label=""):
    user = update.effective_user
    total = ex.total_count(extracted)
    if total == 0:
        await update.message.reply_text("❌ No links found\\.", parse_mode=ParseMode.MARKDOWN_V2)
        return

    tg = extracted["telegram"]
    wa = extracted["whatsapp"]
    other = extracted["other"]

    new_total = dup_total = 0
    new_by_cat: dict[str, list[str]] = {}
    for category, urls in extracted.items():
        if urls:
            n, d, new_urls = db.add_links(urls, category, source, user.id)
            new_total += n
            dup_total += d
            new_by_cat[category] = new_urls
        else:
            new_by_cat[category] = []

    # Store sorted + deduped so /telegram_only and /whatsapp_only match what was shown
    context.user_data["last_tg"]    = sorted(set(tg))
    context.user_data["last_wa"]    = sorted(set(wa))
    context.user_data["last_other"] = sorted(set(other))

    mode = db.get_user_mode(user.id)
    if mode == "separate":
        await send_separate_output(update, tg, wa, other, new_total, dup_total, source_label)
    else:
        await send_merged_output(update, tg, wa, other, new_total, dup_total, source_label)

    # Publish only truly new (non-duplicate) links to the group forum
    await _publish_to_forum(
        context.bot,
        new_by_cat.get("telegram", []),
        new_by_cat.get("whatsapp", []),
        new_by_cat.get("other", []),
        source_note=source_label or source,
    )


# ── Commands ──────────────────────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await ensure_user(update)
    admin_note = "\n\n🔑 You have *admin access*\\. Use /admin for the admin panel\\." if is_admin(update.effective_user.id) else ""
    text = (
        "👋 *Welcome to Link Extractor Bot\\!*\n\n"
        "Send me text or a file — I extract, categorise, deduplicate, and report all links\\.\n\n"
        "*Supported files:* \\.txt · \\.pdf · \\.csv\n\n"
        "*Filter commands:*\n"
        "/telegram\\_only — show only Telegram links\n"
        "/whatsapp\\_only — show only WhatsApp links\n"
        "/latest — 50 most recent links in DB\n"
        "/topgroups — most\\-submitted groups\n"
        "/topchannels — most\\-submitted channels\n\n"
        "*Mode commands:*\n"
        "/separate — send each category as its own message \\(default\\)\n"
        "/merge — send combined Excel \\+ TXT files\n\n"
        "*Other commands:*\n"
        "/stats · /mystats · /search · /export · /help"
        + admin_note
    )
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN_V2)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await ensure_user(update)
    text = (
        "📖 *Help*\n\n"
        "*Filter commands \\(last extraction\\):*\n"
        "/telegram\\_only — show your last Telegram links\n"
        "/whatsapp\\_only — show your last WhatsApp links\n\n"
        "*Database commands:*\n"
        "/latest — last 50 links added to the DB\n"
        "/topgroups — top groups by submission count\n"
        "/topchannels — top Telegram channels by submission count\n"
        "/stats — global totals with subcategory breakdown\n"
        "/mystats — your personal totals\n"
        "/search `<query>` — find links in the DB\n"
        "/export — download all stored links \\(7 files\\)\n\n"
        "*Output mode:*\n"
        "/separate — each category in its own message \\(default\\)\n"
        "/merge — combined Excel \\+ TXT files\n\n"
        "*Link categories:*\n"
        "📱 Telegram channels: t\\.me/username · @username\n"
        "👥 Telegram groups: t\\.me/joinchat/ · t\\.me/\\+\n"
        "💬 WhatsApp groups: chat\\.whatsapp\\.com/\n"
        "💬 WhatsApp direct: wa\\.me/ · api\\.whatsapp\\.com/\n"
        "🔗 Other: all remaining URLs"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN_V2)


async def telegram_only_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await ensure_user(update)
    tg = sorted(set(context.user_data.get("last_tg", [])))
    if not tg:
        await update.message.reply_text(
            "📭 No Telegram links from your last extraction.\nSend me some text or a file first."
        )
        return
    await _send_category(update.message, "📢", "Telegram Links", tg, "telegram_links.txt")


async def whatsapp_only_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await ensure_user(update)
    wa = sorted(set(context.user_data.get("last_wa", [])))
    if not wa:
        await update.message.reply_text(
            "📭 No WhatsApp links from your last extraction.\nSend me some text or a file first."
        )
        return
    await _send_category(update.message, "💬", "WhatsApp Links", wa, "whatsapp_links.txt")


async def latest_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await ensure_user(update)
    rows = db.get_latest_links(50)
    if not rows:
        await update.message.reply_text("📭 No links in the database yet\\.", parse_mode=ParseMode.MARKDOWN_V2)
        return

    tg = [r for r in rows if r["category"] == "telegram"]
    wa = [r for r in rows if r["category"] == "whatsapp"]
    other = [r for r in rows if r["category"] == "other"]

    header = (
        f"🕐 *Latest {len(rows)} links added to DB*\n\n"
        f"📱 {len(tg)} · 💬 {len(wa)} · 🔗 {len(other)}"
    )
    await update.message.reply_text(header, parse_mode=ParseMode.MARKDOWN_V2)

    for cat_rows, name, emoji in [(tg, "TELEGRAM", "📱"), (wa, "WHATSAPP", "💬"), (other, "OTHER", "🔗")]:
        if cat_rows:
            urls = [r["url"] for r in cat_rows]
            for idx, chunk in enumerate(chunks(urls, CHUNK_SIZE)):
                text = format_link_block(name, emoji, chunk, idx * CHUNK_SIZE)
                await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN_V2, disable_web_page_preview=True)


async def topgroups_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await ensure_user(update)
    tg_groups = db.get_top_by_subcategory("group", category="telegram", limit=25)
    wa_groups = db.get_top_by_subcategory("group", category="whatsapp", limit=25)

    if not tg_groups and not wa_groups:
        await update.message.reply_text(
            "📭 No groups in the database yet\\.",
            parse_mode=ParseMode.MARKDOWN_V2
        )
        return

    await update.message.reply_text(
        f"🏆 *Top Groups*\n\n"
        f"👥 Telegram groups: *{len(tg_groups)}*\n"
        f"💬 WhatsApp groups: *{len(wa_groups)}*",
        parse_mode=ParseMode.MARKDOWN_V2
    )

    if tg_groups:
        lines = ["👥 *TOP TELEGRAM GROUPS*\n" + DIVIDER]
        for i, r in enumerate(tg_groups, start=1):
            count = r.get("submit_count", 0)
            lines.append(f"{i}\\. {escape_md(r['url'])} _{count}x_")
        await update.message.reply_text(
            "\n".join(lines), parse_mode=ParseMode.MARKDOWN_V2, disable_web_page_preview=True
        )

    if wa_groups:
        lines = ["💬 *TOP WHATSAPP GROUPS*\n" + DIVIDER]
        for i, r in enumerate(wa_groups, start=1):
            count = r.get("submit_count", 0)
            lines.append(f"{i}\\. {escape_md(r['url'])} _{count}x_")
        await update.message.reply_text(
            "\n".join(lines), parse_mode=ParseMode.MARKDOWN_V2, disable_web_page_preview=True
        )

    # Offer sub-category exports
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("📥 Export Group Files", callback_data="export_groups")
    ]])
    await update.message.reply_text(
        "_Export Telegram\\_Groups\\.xlsx and WhatsApp\\_Groups\\.xlsx:_",
        parse_mode=ParseMode.MARKDOWN_V2,
        reply_markup=keyboard
    )


async def topchannels_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await ensure_user(update)
    channels = db.get_top_by_subcategory("channel", category="telegram", limit=25)

    if not channels:
        await update.message.reply_text(
            "📭 No Telegram channels in the database yet\\.",
            parse_mode=ParseMode.MARKDOWN_V2
        )
        return

    lines = [f"📢 *TOP TELEGRAM CHANNELS \\({len(channels)}\\)*\n" + DIVIDER]
    for i, r in enumerate(channels, start=1):
        count = r.get("submit_count", 0)
        lines.append(f"{i}\\. {escape_md(r['url'])} _{count}x_")

    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("📥 Export Telegram_Channels.xlsx", callback_data="export_channels")
    ]])
    await update.message.reply_text(
        "\n".join(lines),
        parse_mode=ParseMode.MARKDOWN_V2,
        disable_web_page_preview=True,
        reply_markup=keyboard
    )


async def separate_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await ensure_user(update)
    db.set_user_mode(update.effective_user.id, "separate")
    await update.message.reply_text(
        "✅ *Separate mode enabled*\n\nEach category sent as its own numbered message\\.",
        parse_mode=ParseMode.MARKDOWN_V2
    )


async def merge_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await ensure_user(update)
    db.set_user_mode(update.effective_user.id, "merge")
    await update.message.reply_text(
        "✅ *Merge mode enabled*\n\nResults sent as combined Excel \\+ TXT files\\.",
        parse_mode=ParseMode.MARKDOWN_V2
    )


async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await ensure_user(update)
    s = db.get_global_stats()
    by_cat = s.get("by_category", {})
    by_sub = s.get("by_subcategory", {})
    text = (
        "📊 *Global Database Statistics*\n\n"
        f"🔗 Total unique links: *{s['total']}*\n\n"
        f"📱 *Telegram:* *{by_cat.get('telegram', 0)}*\n"
        f"  ├ 📢 Channels: {by_sub.get('telegram_channel', 0)}\n"
        f"  └ 👥 Groups: {by_sub.get('telegram_group', 0)}\n\n"
        f"💬 *WhatsApp:* *{by_cat.get('whatsapp', 0)}*\n"
        f"  ├ 👥 Groups: {by_sub.get('whatsapp_group', 0)}\n"
        f"  └ 💬 Direct: {by_sub.get('whatsapp_direct', 0)}\n\n"
        f"🔗 *Other:* *{by_cat.get('other', 0)}*\n\n"
        f"👥 Total users: *{s['total_users']}*"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN_V2)


async def mystats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await ensure_user(update)
    user_id = update.effective_user.id
    s = db.get_user_stats(user_id)
    if not s:
        await update.message.reply_text("You haven't submitted any links yet\\.", parse_mode=ParseMode.MARKDOWN_V2)
        return
    by_cat = s.get("by_category", {})
    u = s["user"]
    last = escape_md(u.get("last_seen", "")[:16].replace("T", " "))
    first = escape_md(u.get("first_seen", "")[:16].replace("T", " "))
    mode = db.get_user_mode(user_id)
    text = (
        "👤 *Your Statistics*\n\n"
        f"📱 Telegram: *{by_cat.get('telegram', 0)}*\n"
        f"💬 WhatsApp: *{by_cat.get('whatsapp', 0)}*\n"
        f"🔗 Other: *{by_cat.get('other', 0)}*\n"
        f"📦 Total submissions: *{s['total_submissions']}*\n\n"
        f"⚙️ Output mode: *{mode}*\n"
        f"🗓 First seen: {first} UTC\n"
        f"🕐 Last seen: {last} UTC"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN_V2)


async def search_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await ensure_user(update)
    query = " ".join(context.args).strip()
    if not query:
        await update.message.reply_text(
            "Usage: /search \\<query\\>\nExample: /search t\\.me",
            parse_mode=ParseMode.MARKDOWN_V2
        )
        return
    results = db.search_links(query)
    if not results:
        await update.message.reply_text(
            f"❌ No links matching *{escape_md(query)}*\\.", parse_mode=ParseMode.MARKDOWN_V2
        )
        return
    tg = [r["url"] for r in results if r["category"] == "telegram"]
    wa = [r["url"] for r in results if r["category"] == "whatsapp"]
    other = [r["url"] for r in results if r["category"] == "other"]
    await update.message.reply_text(
        f"🔍 *Search:* `{escape_md(query)}` — *{len(results)} result\\(s\\)*\n"
        f"📱 {len(tg)} · 💬 {len(wa)} · 🔗 {len(other)}",
        parse_mode=ParseMode.MARKDOWN_V2
    )
    for links, name, emoji in [(tg, "TELEGRAM", "📱"), (wa, "WHATSAPP", "💬"), (other, "OTHER", "🔗")]:
        if links:
            await send_category_messages(update, name, emoji, links)


async def export_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await ensure_user(update)
    msg = await update.message.reply_text("📦 Preparing export files\\.\\.\\.", parse_mode=ParseMode.MARKDOWN_V2)

    tg_rows    = db.get_all_links("telegram")
    wa_rows    = db.get_all_links("whatsapp")
    other_rows = db.get_all_links("other")
    tg_groups   = db.get_all_links("telegram", "group")
    tg_channels = db.get_all_links("telegram", "channel")
    wa_groups   = db.get_all_links("whatsapp", "group")

    total = len(tg_rows) + len(wa_rows) + len(other_rows)
    if total == 0:
        await msg.edit_text("❌ No links in the database yet\\.", parse_mode=ParseMode.MARKDOWN_V2)
        return

    tg_urls = [r["url"] for r in tg_rows]
    wa_urls = [r["url"] for r in wa_rows]
    other_urls = [r["url"] for r in other_rows]

    txt_buf    = exp.build_txt(tg_urls, wa_urls, other_urls)
    tg_xlsx    = exp.build_category_excel(tg_rows,    "telegram",         "Telegram")
    wa_xlsx    = exp.build_category_excel(wa_rows,    "whatsapp",         "WhatsApp")
    other_xlsx = exp.build_category_excel(other_rows, "other",            "Other")
    tg_grp_xlsx, tg_ch_xlsx, wa_grp_xlsx = exp.build_subcategory_excels(
        tg_groups, tg_channels, wa_groups
    )

    await msg.edit_text(
        f"📦 *Export ready — {total} unique links*\n\n"
        f"📱 Telegram: {len(tg_rows)} \\(👥 {len(tg_groups)} groups · 📢 {len(tg_channels)} channels\\)\n"
        f"💬 WhatsApp: {len(wa_rows)} \\(👥 {len(wa_groups)} groups\\)\n"
        f"🔗 Other: {len(other_rows)}\n\n"
        "_Sending 7 files\\.\\.\\._",
        parse_mode=ParseMode.MARKDOWN_V2
    )
    await update.message.reply_document(txt_buf,     filename="links.txt",              caption="📄 All links (TXT)")
    await update.message.reply_document(tg_xlsx,     filename="Telegram.xlsx",          caption="📱 All Telegram links")
    await update.message.reply_document(tg_grp_xlsx, filename="Telegram_Groups.xlsx",   caption="👥 Telegram Groups")
    await update.message.reply_document(tg_ch_xlsx,  filename="Telegram_Channels.xlsx", caption="📢 Telegram Channels")
    await update.message.reply_document(wa_xlsx,     filename="WhatsApp.xlsx",          caption="💬 All WhatsApp links")
    await update.message.reply_document(wa_grp_xlsx, filename="WhatsApp_Groups.xlsx",   caption="👥 WhatsApp Groups")
    await update.message.reply_document(other_xlsx,  filename="Other.xlsx",             caption="🔗 Other links")


async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await ensure_user(update)
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Admin access required\\.", parse_mode=ParseMode.MARKDOWN_V2)
        return
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 Global Stats",  callback_data="admin_stats"),
         InlineKeyboardButton("👥 User List",      callback_data="admin_users")],
        [InlineKeyboardButton("🏆 Top Users",      callback_data="admin_top"),
         InlineKeyboardButton("📦 Export All",     callback_data="admin_export")],
    ])
    await update.message.reply_text(
        "🔑 *Admin Panel*\n\nChoose an action:",
        parse_mode=ParseMode.MARKDOWN_V2,
        reply_markup=keyboard
    )


# ── Callback handlers ─────────────────────────────────────────────────────────

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    action = query.data

    # ── Main inline buttons ──────────────────────────────────────────────────

    if action == "show_telegram":
        tg = sorted(set(context.user_data.get("last_tg", [])))
        if not tg:
            await query.message.reply_text("📭 No Telegram links from your last extraction.")
            return
        await _send_category(query.message, "📢", "Telegram Links", tg, "telegram_links.txt")

    elif action == "show_whatsapp":
        wa = sorted(set(context.user_data.get("last_wa", [])))
        if not wa:
            await query.message.reply_text("📭 No WhatsApp links from your last extraction.")
            return
        await _send_category(query.message, "💬", "WhatsApp Links", wa, "whatsapp_links.txt")

    elif action == "show_other":
        other = sorted(set(context.user_data.get("last_other", [])))
        if not other:
            await query.message.reply_text("📭 No other links from your last extraction.")
            return
        await _send_category(query.message, "🔗", "Other Links", other, "other_links.txt")

    elif action == "show_export_latest":
        tg    = sorted(set(context.user_data.get("last_tg",    [])))
        wa    = sorted(set(context.user_data.get("last_wa",    [])))
        other = sorted(set(context.user_data.get("last_other", [])))
        total = len(tg) + len(wa) + len(other)
        if total == 0:
            await query.message.reply_text("📭 No links from your last extraction.")
            return
        tg_rows    = [{"url": u, "source": "text", "first_seen_at": "", "subcategory": ex.get_subcategory(u, "telegram")} for u in tg]
        wa_rows    = [{"url": u, "source": "text", "first_seen_at": "", "subcategory": ex.get_subcategory(u, "whatsapp")} for u in wa]
        other_rows = [{"url": u, "source": "text", "first_seen_at": "", "subcategory": "other"} for u in other]
        tg_groups   = [r for r in tg_rows if r["subcategory"] == "group"]
        tg_channels = [r for r in tg_rows if r["subcategory"] == "channel"]
        wa_groups   = [r for r in wa_rows  if r["subcategory"] == "group"]
        txt_buf    = exp.build_txt(tg, wa, other)
        tg_xlsx    = exp.build_category_excel(tg_rows,    "telegram", "Telegram")
        wa_xlsx    = exp.build_category_excel(wa_rows,    "whatsapp", "WhatsApp")
        other_xlsx = exp.build_category_excel(other_rows, "other",    "Other")
        tg_grp_xlsx, tg_ch_xlsx, wa_grp_xlsx = exp.build_subcategory_excels(tg_groups, tg_channels, wa_groups)
        await query.message.reply_text(f"📦 Exporting {total} links (7 files)…")
        await query.message.reply_document(txt_buf,     filename="links.txt",              caption="📄 All links (TXT)")
        await query.message.reply_document(tg_xlsx,     filename="Telegram.xlsx",          caption="📢 All Telegram")
        await query.message.reply_document(tg_grp_xlsx, filename="Telegram_Groups.xlsx",   caption="👥 Telegram Groups")
        await query.message.reply_document(tg_ch_xlsx,  filename="Telegram_Channels.xlsx", caption="📣 Telegram Channels")
        await query.message.reply_document(wa_xlsx,     filename="WhatsApp.xlsx",          caption="💬 All WhatsApp")
        await query.message.reply_document(wa_grp_xlsx, filename="WhatsApp_Groups.xlsx",   caption="👥 WhatsApp Groups")
        if other:
            await query.message.reply_document(other_xlsx, filename="Other.xlsx", caption="🔗 Other")

    elif action == "dl_txt_latest":
        tg    = sorted(set(context.user_data.get("last_tg",    [])))
        wa    = sorted(set(context.user_data.get("last_wa",    [])))
        other = sorted(set(context.user_data.get("last_other", [])))
        total = len(tg) + len(wa) + len(other)
        if total == 0:
            await query.message.reply_text("📭 No links from your last extraction.")
            return
        buf = exp.build_txt(tg, wa, other)
        await query.message.reply_document(
            buf, filename="links.txt",
            caption=f"📄 All links — {total} total (📢 {len(tg)} Telegram · 💬 {len(wa)} WhatsApp · 🔗 {len(other)} Other)"
        )

    elif action == "dl_excel_latest":
        tg    = sorted(set(context.user_data.get("last_tg",    [])))
        wa    = sorted(set(context.user_data.get("last_wa",    [])))
        other = sorted(set(context.user_data.get("last_other", [])))
        total = len(tg) + len(wa) + len(other)
        if total == 0:
            await query.message.reply_text("📭 No links from your last extraction.")
            return
        tg_rows    = [{"url": u, "source": "text", "first_seen_at": "", "subcategory": ex.get_subcategory(u, "telegram")} for u in tg]
        wa_rows    = [{"url": u, "source": "text", "first_seen_at": "", "subcategory": ex.get_subcategory(u, "whatsapp")} for u in wa]
        other_rows = [{"url": u, "source": "text", "first_seen_at": "", "subcategory": "other"} for u in other]
        tg_groups   = [r for r in tg_rows if r["subcategory"] == "group"]
        tg_channels = [r for r in tg_rows if r["subcategory"] == "channel"]
        wa_groups   = [r for r in wa_rows if r["subcategory"] == "group"]
        tg_xlsx    = exp.build_category_excel(tg_rows,    "telegram", "Telegram")
        wa_xlsx    = exp.build_category_excel(wa_rows,    "whatsapp", "WhatsApp")
        other_xlsx = exp.build_category_excel(other_rows, "other",    "Other")
        tg_grp_xlsx, tg_ch_xlsx, wa_grp_xlsx = exp.build_subcategory_excels(tg_groups, tg_channels, wa_groups)
        await query.message.reply_text(f"📊 Sending Excel files for {total} links...")
        await query.message.reply_document(tg_xlsx,     filename="Telegram.xlsx",          caption="📢 All Telegram")
        await query.message.reply_document(tg_grp_xlsx, filename="Telegram_Groups.xlsx",   caption="👥 Telegram Groups")
        await query.message.reply_document(tg_ch_xlsx,  filename="Telegram_Channels.xlsx", caption="📣 Telegram Channels")
        await query.message.reply_document(wa_xlsx,     filename="WhatsApp.xlsx",          caption="💬 All WhatsApp")
        await query.message.reply_document(wa_grp_xlsx, filename="WhatsApp_Groups.xlsx",   caption="👥 WhatsApp Groups")
        if other:
            await query.message.reply_document(other_xlsx, filename="Other.xlsx", caption="🔗 Other")

    elif action == "show_stats":
        s = db.get_global_stats()
        by_cat = s.get("by_category", {})
        by_sub = s.get("by_subcategory", {})
        await query.message.reply_text(
            f"📊 *Global Stats*\n\n"
            f"🔗 Total: *{s['total']}*\n"
            f"📱 Telegram: *{by_cat.get('telegram', 0)}* "
            f"\\(📢 {by_sub.get('telegram_channel', 0)} ch · 👥 {by_sub.get('telegram_group', 0)} grp\\)\n"
            f"💬 WhatsApp: *{by_cat.get('whatsapp', 0)}* "
            f"\\(👥 {by_sub.get('whatsapp_group', 0)} grp\\)\n"
            f"🔗 Other: *{by_cat.get('other', 0)}*\n\n"
            f"👥 Users: *{s['total_users']}*",
            parse_mode=ParseMode.MARKDOWN_V2
        )

    elif action == "show_export":
        tg_rows    = db.get_all_links("telegram")
        wa_rows    = db.get_all_links("whatsapp")
        other_rows = db.get_all_links("other")
        tg_groups   = db.get_all_links("telegram", "group")
        tg_channels = db.get_all_links("telegram", "channel")
        wa_groups   = db.get_all_links("whatsapp", "group")
        total = len(tg_rows) + len(wa_rows) + len(other_rows)
        if total == 0:
            await query.message.reply_text("❌ No links yet\\.", parse_mode=ParseMode.MARKDOWN_V2)
            return
        tg_urls = [r["url"] for r in tg_rows]
        wa_urls = [r["url"] for r in wa_rows]
        other_urls = [r["url"] for r in other_rows]
        txt_buf    = exp.build_txt(tg_urls, wa_urls, other_urls)
        tg_xlsx    = exp.build_category_excel(tg_rows,    "telegram", "Telegram")
        wa_xlsx    = exp.build_category_excel(wa_rows,    "whatsapp", "WhatsApp")
        other_xlsx = exp.build_category_excel(other_rows, "other",    "Other")
        tg_grp_xlsx, tg_ch_xlsx, wa_grp_xlsx = exp.build_subcategory_excels(tg_groups, tg_channels, wa_groups)
        await query.message.reply_text(f"📦 *Exporting {total} links \\(7 files\\)\\.\\.\\.*", parse_mode=ParseMode.MARKDOWN_V2)
        await query.message.reply_document(txt_buf,     filename="links.txt",              caption="📄 All links (TXT)")
        await query.message.reply_document(tg_xlsx,     filename="Telegram.xlsx",          caption="📱 All Telegram")
        await query.message.reply_document(tg_grp_xlsx, filename="Telegram_Groups.xlsx",   caption="👥 Telegram Groups")
        await query.message.reply_document(tg_ch_xlsx,  filename="Telegram_Channels.xlsx", caption="📢 Telegram Channels")
        await query.message.reply_document(wa_xlsx,     filename="WhatsApp.xlsx",          caption="💬 All WhatsApp")
        await query.message.reply_document(wa_grp_xlsx, filename="WhatsApp_Groups.xlsx",   caption="👥 WhatsApp Groups")
        await query.message.reply_document(other_xlsx,  filename="Other.xlsx",             caption="🔗 Other")

    elif action == "dl_telegram":
        tg = context.user_data.get("last_tg", [])
        if not tg:
            await query.message.reply_text("📭 No Telegram links from your last extraction.")
            return
        tg_rows = [{"url": u, "source": "text", "first_seen_at": "", "subcategory": ex.get_subcategory(u, "telegram")} for u in tg]
        buf = exp.build_category_excel(tg_rows, "telegram", "Telegram")
        await query.message.reply_document(buf, filename="Telegram.xlsx", caption=f"📢 Telegram Links ({len(tg)})")

    elif action == "dl_whatsapp":
        wa = context.user_data.get("last_wa", [])
        if not wa:
            await query.message.reply_text("📭 No WhatsApp links from your last extraction.")
            return
        wa_rows = [{"url": u, "source": "text", "first_seen_at": "", "subcategory": ex.get_subcategory(u, "whatsapp")} for u in wa]
        buf = exp.build_category_excel(wa_rows, "whatsapp", "WhatsApp")
        await query.message.reply_document(buf, filename="WhatsApp.xlsx", caption=f"💬 WhatsApp Links ({len(wa)})")

    elif action == "dl_all":
        tg    = context.user_data.get("last_tg",    [])
        wa    = context.user_data.get("last_wa",    [])
        other = context.user_data.get("last_other", [])
        total = len(tg) + len(wa) + len(other)
        if total == 0:
            await query.message.reply_text("📭 No links from your last extraction.")
            return
        tg_rows    = [{"url": u, "source": "text", "first_seen_at": "", "subcategory": ex.get_subcategory(u, "telegram")} for u in tg]
        wa_rows    = [{"url": u, "source": "text", "first_seen_at": "", "subcategory": ex.get_subcategory(u, "whatsapp")} for u in wa]
        other_rows = [{"url": u, "source": "text", "first_seen_at": "", "subcategory": "other"} for u in other]
        tg_groups   = [r for r in tg_rows if r["subcategory"] == "group"]
        tg_channels = [r for r in tg_rows if r["subcategory"] == "channel"]
        wa_groups   = [r for r in wa_rows if r["subcategory"] == "group"]
        txt_buf    = exp.build_txt(tg, wa, other)
        tg_xlsx    = exp.build_category_excel(tg_rows,    "telegram", "Telegram")
        wa_xlsx    = exp.build_category_excel(wa_rows,    "whatsapp", "WhatsApp")
        other_xlsx = exp.build_category_excel(other_rows, "other",    "Other")
        tg_grp_xlsx, tg_ch_xlsx, wa_grp_xlsx = exp.build_subcategory_excels(tg_groups, tg_channels, wa_groups)
        await query.message.reply_text(f"📦 Sending {total} links in 7 files...")
        await query.message.reply_document(txt_buf,     filename="links.txt",              caption="📄 All links (TXT)")
        await query.message.reply_document(tg_xlsx,     filename="Telegram.xlsx",          caption="📢 All Telegram")
        await query.message.reply_document(tg_grp_xlsx, filename="Telegram_Groups.xlsx",   caption="👥 Telegram Groups")
        await query.message.reply_document(tg_ch_xlsx,  filename="Telegram_Channels.xlsx", caption="📣 Telegram Channels")
        await query.message.reply_document(wa_xlsx,     filename="WhatsApp.xlsx",          caption="💬 All WhatsApp")
        await query.message.reply_document(wa_grp_xlsx, filename="WhatsApp_Groups.xlsx",   caption="👥 WhatsApp Groups")
        await query.message.reply_document(other_xlsx,  filename="Other.xlsx",             caption="🔗 Other")

    elif action == "export_groups":
        tg_groups = db.get_all_links("telegram", "group")
        wa_groups = db.get_all_links("whatsapp",  "group")
        _, _, wa_grp_xlsx = exp.build_subcategory_excels(tg_groups, [], wa_groups)
        tg_grp_xlsx, _, _ = exp.build_subcategory_excels(tg_groups, [], [])
        await query.message.reply_text(f"📦 *Exporting group files\\.\\.\\.*", parse_mode=ParseMode.MARKDOWN_V2)
        await query.message.reply_document(tg_grp_xlsx, filename="Telegram_Groups.xlsx", caption="👥 Telegram Groups")
        await query.message.reply_document(wa_grp_xlsx, filename="WhatsApp_Groups.xlsx", caption="💬 WhatsApp Groups")

    elif action == "export_channels":
        tg_channels = db.get_all_links("telegram", "channel")
        _, tg_ch_xlsx, _ = exp.build_subcategory_excels([], tg_channels, [])
        await query.message.reply_text(f"📦 *Exporting channels\\.\\.\\.*", parse_mode=ParseMode.MARKDOWN_V2)
        await query.message.reply_document(tg_ch_xlsx, filename="Telegram_Channels.xlsx", caption="📢 Telegram Channels")

    # ── Admin panel buttons ──────────────────────────────────────────────────

    elif action == "admin_stats":
        if not is_admin(query.from_user.id): return
        s = db.get_global_stats()
        by_cat = s.get("by_category", {})
        by_sub = s.get("by_subcategory", {})
        text = (
            "📊 *Global Stats*\n\n"
            f"🔗 Total: *{s['total']}*\n"
            f"📱 Telegram: *{by_cat.get('telegram', 0)}*\n"
            f"  ├ 📢 Channels: {by_sub.get('telegram_channel', 0)}\n"
            f"  └ 👥 Groups: {by_sub.get('telegram_group', 0)}\n"
            f"💬 WhatsApp: *{by_cat.get('whatsapp', 0)}*\n"
            f"  └ 👥 Groups: {by_sub.get('whatsapp_group', 0)}\n"
            f"🔗 Other: *{by_cat.get('other', 0)}*\n\n"
            f"👥 Users: *{s['total_users']}*"
        )
        back = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="admin_back")]])
        await query.edit_message_text(text, parse_mode=ParseMode.MARKDOWN_V2, reply_markup=back)

    elif action == "admin_users":
        if not is_admin(query.from_user.id): return
        users = db.get_all_users()
        lines = [f"👥 *All Users \\({len(users)}\\)*\n"]
        for u in users[:30]:
            name  = escape_md(u.get("full_name") or "Unknown")
            uname = f"@{escape_md(u['username'])}" if u.get("username") else f"ID:{u['user_id']}"
            last  = escape_md((u.get("last_seen") or "")[:10])
            lines.append(f"• {name} {uname} — {last}")
        if len(users) > 30:
            lines.append(f"\n_\\.\\.\\. and {len(users)-30} more_")
        back = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="admin_back")]])
        await query.edit_message_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN_V2, reply_markup=back)

    elif action == "admin_top":
        if not is_admin(query.from_user.id): return
        top = db.get_top_users(10)
        medals = ["🥇","🥈","🥉"] + ["▪️"]*10
        lines = ["🏆 *Top Users*\n"]
        for i, u in enumerate(top):
            name  = escape_md(u.get("full_name") or "Unknown")
            uname = f"@{escape_md(u['username'])}" if u.get("username") else f"ID:{u['user_id']}"
            lines.append(f"{medals[i]} {name} {uname}: *{u.get('submission_count', 0)}*")
        back = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="admin_back")]])
        await query.edit_message_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN_V2, reply_markup=back)

    elif action == "admin_export":
        if not is_admin(query.from_user.id): return
        await query.edit_message_text("📦 Generating export files\\.\\.\\.", parse_mode=ParseMode.MARKDOWN_V2)
        tg_rows    = db.get_all_links("telegram")
        wa_rows    = db.get_all_links("whatsapp")
        other_rows = db.get_all_links("other")
        tg_groups   = db.get_all_links("telegram", "group")
        tg_channels = db.get_all_links("telegram", "channel")
        wa_groups   = db.get_all_links("whatsapp",  "group")
        total = len(tg_rows) + len(wa_rows) + len(other_rows)
        if total == 0:
            await query.edit_message_text("❌ No links yet\\.", parse_mode=ParseMode.MARKDOWN_V2)
            return
        tg_urls = [r["url"] for r in tg_rows]
        wa_urls = [r["url"] for r in wa_rows]
        other_urls = [r["url"] for r in other_rows]
        txt_buf    = exp.build_txt(tg_urls, wa_urls, other_urls)
        tg_xlsx    = exp.build_category_excel(tg_rows,    "telegram", "Telegram")
        wa_xlsx    = exp.build_category_excel(wa_rows,    "whatsapp", "WhatsApp")
        other_xlsx = exp.build_category_excel(other_rows, "other",    "Other")
        tg_grp_xlsx, tg_ch_xlsx, wa_grp_xlsx = exp.build_subcategory_excels(tg_groups, tg_channels, wa_groups)
        await query.edit_message_text(
            f"📦 *Admin Export — {total} links*\n"
            f"📱 {len(tg_rows)} · 💬 {len(wa_rows)} · 🔗 {len(other_rows)}",
            parse_mode=ParseMode.MARKDOWN_V2
        )
        cid = query.message.chat_id
        await context.bot.send_document(cid, txt_buf,     filename="links.txt",              caption="📄 All links (TXT)")
        await context.bot.send_document(cid, tg_xlsx,     filename="Telegram.xlsx",          caption="📱 All Telegram")
        await context.bot.send_document(cid, tg_grp_xlsx, filename="Telegram_Groups.xlsx",   caption="👥 Telegram Groups")
        await context.bot.send_document(cid, tg_ch_xlsx,  filename="Telegram_Channels.xlsx", caption="📢 Telegram Channels")
        await context.bot.send_document(cid, wa_xlsx,     filename="WhatsApp.xlsx",          caption="💬 All WhatsApp")
        await context.bot.send_document(cid, wa_grp_xlsx, filename="WhatsApp_Groups.xlsx",   caption="👥 WhatsApp Groups")
        await context.bot.send_document(cid, other_xlsx,  filename="Other.xlsx",             caption="🔗 Other")

    elif action == "admin_back":
        if not is_admin(query.from_user.id): return
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("📊 Global Stats", callback_data="admin_stats"),
             InlineKeyboardButton("👥 User List",    callback_data="admin_users")],
            [InlineKeyboardButton("🏆 Top Users",    callback_data="admin_top"),
             InlineKeyboardButton("📦 Export All",   callback_data="admin_export")],
        ])
        await query.edit_message_text(
            "🔑 *Admin Panel*\n\nChoose an action:",
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=keyboard
        )


# ── Message handlers ──────────────────────────────────────────────────────────

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await ensure_user(update)
    text = update.message.text or update.message.caption or ""
    if not text.strip():
        await update.message.reply_text("Please send a text message with links\\.", parse_mode=ParseMode.MARKDOWN_V2)
        return
    extracted = ex.extract_from_text(text)
    await process_and_reply(update, context, extracted, source="text")


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await ensure_user(update)
    doc  = update.message.document
    name = doc.file_name or ""
    ext  = name.lower().rsplit(".", 1)[-1] if "." in name else ""

    if ext not in ("txt", "pdf", "csv", "docx"):
        await update.message.reply_text(
            "⚠️ Unsupported file. Please send a <b>.txt</b>, <b>.pdf</b>, <b>.csv</b>, or <b>.docx</b> file.",
            parse_mode="HTML"
        )
        return
    if doc.file_size and doc.file_size > 10 * 1024 * 1024:
        await update.message.reply_text("⚠️ File too large \\(max 10 MB\\)\\.", parse_mode=ParseMode.MARKDOWN_V2)
        return

    await update.message.reply_text(f"📂 Reading *{escape_md(name)}*\\.\\.\\.", parse_mode=ParseMode.MARKDOWN_V2)

    file = await context.bot.get_file(doc.file_id)
    buf  = io.BytesIO()
    await file.download_to_memory(buf)
    content = buf.getvalue()

    pdf_stats = None
    try:
        if ext == "txt":
            extracted = ex.extract_from_txt(content)
        elif ext == "pdf":
            extracted, pdf_stats = ex.extract_from_pdf(content)
        elif ext == "csv":
            extracted = ex.extract_from_csv(content)
        elif ext == "docx":
            extracted = ex.extract_from_docx(content)
    except Exception as e:
        logger.error(f"File parse error ({ext}): {e}")
        await update.message.reply_text(
            f"❌ Could not read <b>{name}</b>.",
            parse_mode="HTML"
        )
        return

    if pdf_stats:
        pages      = pdf_stats["pages"]
        ocr_pages  = pdf_stats["ocr_pages"]
        method     = pdf_stats["method"]
        ocr_error  = pdf_stats.get("ocr_error")
        normal_pages = pages - ocr_pages

        if method == "normal":
            method_label = "📄 Normal text extraction"
        elif method == "ocr":
            method_label = "🔍 Full OCR (scanned PDF)"
        else:
            method_label = f"🔀 Mixed ({normal_pages} text + {ocr_pages} OCR)"

        stat_parts = [f"<b>📊 PDF Stats</b>: {pages} page(s) · {method_label}"]
        if ocr_error:
            stat_parts.append(f"⚠️ OCR error: {ocr_error[:80]}")
        await update.message.reply_text("\n".join(stat_parts), parse_mode="HTML")

    await process_and_reply(update, context, extracted, source=ext, source_label=name)


async def setforum_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin: /setforum <chat_id> — link the bot to a group forum and auto-create 3 topics."""
    await ensure_user(update)
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Admin only.")
        return
    args = context.args
    if not args:
        existing = db.get_setting("forum_chat_id")
        if existing:
            tg_id    = db.get_setting("forum_topic_tg")    or "—"
            wa_id    = db.get_setting("forum_topic_wa")    or "—"
            other_id = db.get_setting("forum_topic_other") or "—"
            await update.message.reply_text(
                f"ℹ️ Forum already configured:\n"
                f"Chat ID: <code>{existing}</code>\n"
                f"📢 Telegram topic: <code>{tg_id}</code>\n"
                f"💬 WhatsApp topic: <code>{wa_id}</code>\n"
                f"🔗 Other topic:    <code>{other_id}</code>\n\n"
                f"Run /setforum &lt;chat_id&gt; again to reconfigure.",
                parse_mode="HTML",
            )
        else:
            await update.message.reply_text(
                "Usage: <code>/setforum -1001234567890</code>\n\n"
                "1. Create a Telegram group with Topics enabled.\n"
                "2. Add this bot as admin (allow posting messages).\n"
                "3. Copy the group chat ID and run the command above.\n\n"
                "The bot will auto-create 3 topics: Telegram Links, WhatsApp Links, Other Links.",
                parse_mode="HTML",
            )
        return

    chat_id_str = args[0]
    try:
        chat_id = int(chat_id_str)
    except ValueError:
        await update.message.reply_text("❌ Invalid chat ID. Must be an integer like <code>-1001234567890</code>.", parse_mode="HTML")
        return

    msg = await update.message.reply_text("⏳ Creating forum topics…")

    try:
        tg_topic    = await context.bot.create_forum_topic(chat_id, "📢 Telegram Links")
        wa_topic    = await context.bot.create_forum_topic(chat_id, "💬 WhatsApp Links")
        other_topic = await context.bot.create_forum_topic(chat_id, "🔗 Other Links")
    except Exception as exc:
        await msg.edit_text(
            f"❌ Failed to create topics: <code>{exc}</code>\n\n"
            "Make sure the bot is an admin in the group and the group has Topics enabled.",
            parse_mode="HTML",
        )
        return

    db.set_setting("forum_chat_id",     str(chat_id))
    db.set_setting("forum_topic_tg",    str(tg_topic.message_thread_id))
    db.set_setting("forum_topic_wa",    str(wa_topic.message_thread_id))
    db.set_setting("forum_topic_other", str(other_topic.message_thread_id))

    await msg.edit_text(
        f"✅ <b>Forum configured!</b>\n\n"
        f"Chat ID: <code>{chat_id}</code>\n"
        f"📢 Telegram topic ID: <code>{tg_topic.message_thread_id}</code>\n"
        f"💬 WhatsApp topic ID: <code>{wa_topic.message_thread_id}</code>\n"
        f"🔗 Other topic ID:    <code>{other_topic.message_thread_id}</code>\n\n"
        f"New unique links will now be automatically posted to the group forum after each extraction.",
        parse_mode="HTML",
    )


async def setforum_manual_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin: /setforum_manual <chat_id> <tg_thread_id> <wa_thread_id> <other_thread_id>
    Use this if the group already has topics created and you just need to link them."""
    await ensure_user(update)
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Admin only.")
        return
    args = context.args
    if len(args) != 4:
        await update.message.reply_text(
            "Usage: <code>/setforum_manual &lt;chat_id&gt; &lt;tg_topic_id&gt; &lt;wa_topic_id&gt; &lt;other_topic_id&gt;</code>",
            parse_mode="HTML",
        )
        return
    try:
        chat_id, tg_id, wa_id, other_id = [int(a) for a in args]
    except ValueError:
        await update.message.reply_text("❌ All four values must be integers.")
        return
    db.set_setting("forum_chat_id",     str(chat_id))
    db.set_setting("forum_topic_tg",    str(tg_id))
    db.set_setting("forum_topic_wa",    str(wa_id))
    db.set_setting("forum_topic_other", str(other_id))
    await update.message.reply_text(
        f"✅ Forum linked manually.\n"
        f"Chat: <code>{chat_id}</code> | TG: <code>{tg_id}</code> | WA: <code>{wa_id}</code> | Other: <code>{other_id}</code>",
        parse_mode="HTML",
    )


async def clearforum_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin: /clearforum — stop publishing to the group forum."""
    await ensure_user(update)
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Admin only.")
        return
    for key in ("forum_chat_id", "forum_topic_tg", "forum_topic_wa", "forum_topic_other"):
        db.del_setting(key)
    await update.message.reply_text("✅ Forum configuration cleared. No more automatic publishing.")


def main():
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        raise ValueError("TELEGRAM_BOT_TOKEN not set")

    db.init_db()
    if ADMIN_USER_ID == 0:
        logger.warning("ADMIN_USER_ID not set — admin panel disabled")
    else:
        logger.info(f"Admin user ID: {ADMIN_USER_ID}")

    app = Application.builder().token(token).build()

    app.add_handler(CommandHandler("start",           start))
    app.add_handler(CommandHandler("help",            help_command))
    app.add_handler(CommandHandler("separate",        separate_command))
    app.add_handler(CommandHandler("merge",           merge_command))
    app.add_handler(CommandHandler("telegram_only",   telegram_only_command))
    app.add_handler(CommandHandler("whatsapp_only",   whatsapp_only_command))
    app.add_handler(CommandHandler("latest",          latest_command))
    app.add_handler(CommandHandler("topgroups",       topgroups_command))
    app.add_handler(CommandHandler("topchannels",     topchannels_command))
    app.add_handler(CommandHandler("stats",           stats_command))
    app.add_handler(CommandHandler("mystats",         mystats_command))
    app.add_handler(CommandHandler("search",          search_command))
    app.add_handler(CommandHandler("export",          export_command))
    app.add_handler(CommandHandler("admin",           admin_command))
    app.add_handler(CommandHandler("setforum",        setforum_command))
    app.add_handler(CommandHandler("setforum_manual", setforum_manual_command))
    app.add_handler(CommandHandler("clearforum",      clearforum_command))
    app.add_handler(CallbackQueryHandler(button_callback))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(MessageHandler(filters.CAPTION & ~filters.COMMAND, handle_message))

    logger.info("Bot starting...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
