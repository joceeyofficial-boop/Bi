#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
市值提醒机器人 - 长期价值投资版（CoinPaprika）
支持：价格或市值添加、自动换算、提醒显示价格、详细列表
"""

import re
import time
import sqlite3
import logging
import asyncio
from datetime import datetime, timedelta
from typing import Optional, Tuple

import requests
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
    ConversationHandler,
)

# ==================== 配置区域 ====================
BOT_TOKEN = "8648405680:AAEVSH8reK5x1tXuyl_e4nfeA8-9Nz2gcPA"          # ← 必填
ADMIN_IDS = []                            # 留空则所有人可用

CHECK_INTERVAL_HOURS = 1
NOTIFY_TIMES = 3
NOTIFY_INTERVAL_SECONDS = 90
COOLDOWN_DAYS = 7

PAPRIKA_BASE = "https://api.coinpaprika.com/v1"
# ==================================================

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

WAITING_TARGET = 1
CONFIRM_REMOVEALL = 2


# ==================== 数据库 ====================
def init_db():
    conn = sqlite3.connect("mcap_watch.db")
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS watches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            symbol TEXT NOT NULL,
            cg_id TEXT NOT NULL,
            name TEXT,
            target_mcap REAL NOT NULL,
            added_at TEXT,
            cooldown_until TEXT,
            last_notified TEXT
        )
    """)
    c.execute("CREATE INDEX IF NOT EXISTS idx_user ON watches(user_id)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_cg ON watches(cg_id)")
    conn.commit()
    conn.close()


def db_execute(query, params=(), fetch=False):
    conn = sqlite3.connect("mcap_watch.db")
    c = conn.cursor()
    c.execute(query, params)
    if fetch:
        result = c.fetchall()
    else:
        conn.commit()
        result = c.rowcount
    conn.close()
    return result


# ==================== 工具函数 ====================
def parse_number(text: str) -> Optional[float]:
    """解析纯数字（支持 $ 和逗号）→ 当作价格"""
    text = text.strip().upper().replace(",", "").replace("$", "").replace(" ", "")
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def parse_mcap(text: str) -> Optional[float]:
    """解析带单位的市值（K/M/B/T）"""
    text = text.strip().upper().replace(",", "").replace(" ", "")
    if not text:
        return None
    match = re.match(r"^([\d.]+)([KMBT])$", text)
    if not match:
        return None
    num = float(match.group(1))
    unit = match.group(2)
    multipliers = {"K": 1_000, "M": 1_000_000, "B": 1_000_000_000, "T": 1_000_000_000_000}
    return num * multipliers.get(unit, 1)


def format_mcap(value: float) -> str:
    if value is None:
        return "N/A"
    if value >= 1_000_000_000_000:
        return f"{value / 1_000_000_000_000:.2f}T"
    if value >= 1_000_000_000:
        return f"{value / 1_000_000_000:.2f}B"
    if value >= 1_000_000:
        return f"{value / 1_000_000:.2f}M"
    if value >= 1_000:
        return f"{value / 1_000:.2f}K"
    return f"{value:,.0f}"


def is_allowed(user_id: int) -> bool:
    if not ADMIN_IDS:
        return True
    return user_id in ADMIN_IDS


def paprika_request(endpoint: str, params: dict = None):
    url = f"{PAPRIKA_BASE}{endpoint}"
    for attempt in range(3):
        try:
            r = requests.get(url, params=params, timeout=15)
            if r.status_code == 429:
                wait = 20 * (attempt + 1)
                logger.warning(f"CoinPaprika 限流，等待 {wait} 秒...")
                time.sleep(wait)
                continue
            r.raise_for_status()
            return r.json()
        except Exception as e:
            logger.error(f"Paprika request failed (attempt {attempt+1}): {e}")
            time.sleep(2)
    return None


def resolve_coin(query: str) -> Optional[Tuple[str, str, str]]:
    query = query.strip()
    if not query:
        return None

    q_lower = query.lower()
    data = paprika_request("/search", {"q": query})
    if not data or "currencies" not in data or not data["currencies"]:
        return None

    currencies = data["currencies"]
    exact_matches = [c for c in currencies if c.get("symbol", "").lower() == q_lower]
    if exact_matches:
        exact_matches.sort(key=lambda x: x.get("rank") or 99999)
        c = exact_matches[0]
        return c["id"], c["symbol"].upper(), c["name"]

    c = currencies[0]
    return c["id"], c.get("symbol", "").upper(), c.get("name", "")


def get_ticker_info(paprika_id: str) -> Optional[dict]:
    data = paprika_request(f"/tickers/{paprika_id}")
    if not data:
        return None
    try:
        quotes = data["quotes"]["USD"]
        return {
            "price": float(quotes["price"]),
            "market_cap": float(quotes["market_cap"]),
            "circulating_supply": float(data.get("circulating_supply") or 0)
        }
    except (KeyError, TypeError, ValueError):
        return None


def get_market_cap(paprika_id: str) -> Optional[float]:
    info = get_ticker_info(paprika_id)
    if info:
        return info["market_cap"]
    return None


# ==================== 命令 ====================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id):
        await update.message.reply_text("无权限使用此机器人。")
        return
    await update.message.reply_text(
        "欢迎使用【市值提醒机器人】（CoinPaprika 版）\n\n"
        "📌 支持两种添加方式：\n"
        "• 市值：`BTC 1.58T`  `SOL 55B`\n"
        "• 价格：`BTC 78000`  `BTC $78000`\n\n"
        "输入 /help 查看完整命令",
        parse_mode="Markdown"
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = """
*命令列表*

*添加监控*
• 按市值：`BTC 1.58T`  `SOL 55B`
• 按价格：`BTC 78000`  `BTC $78000`（自动换算市值）

*管理*
/list - 查看监控列表（含当前价格、市值、提醒价格）
/edit 符号 新目标 - 修改目标市值
/remove 符号1 符号2 ... - 批量删除
/removeall - 清空所有监控（需确认）

*查询*
/mcap 符号 - 查看单个币当前真实市值
/mcap - 查看你所有监控币的当前市值
/status - 机器人状态

*其他*
/help - 显示本帮助
/cancel - 取消当前操作
"""
    await update.message.reply_text(text, parse_mode="Markdown")


async def list_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    rows = db_execute(
        "SELECT symbol, cg_id, name, target_mcap, cooldown_until FROM watches WHERE user_id=? ORDER BY symbol",
        (user_id,), fetch=True
    )
    if not rows:
        await update.message.reply_text("当前没有监控任何代币。\n发送 `符号 目标市值` 或 `符号 价格` 即可添加。")
        return

    await update.message.reply_text(f"正在查询 {len(rows)} 个代币的实时数据，请稍候...")

    now = datetime.utcnow()
    lines = [f"*当前监控列表*（共 {len(rows)} 个）\n"]

    for symbol, paprika_id, name, target, cooldown in rows:
        info = get_ticker_info(paprika_id)
        time.sleep(1.5)

        cooldown_text = ""
        if cooldown:
            try:
                cd = datetime.fromisoformat(cooldown)
                if cd > now:
                    cooldown_text = f"\n  ❄️冷却至 {cd.strftime('%m-%d')}"
            except:
                pass

        if info is None:
            lines.append(
                f"• *{symbol}* ({name or '-'})\n"
                f"  当前数据获取失败\n"
                f"  提醒市值：≤ {format_mcap(target)}{cooldown_text}"
            )
            continue

        current_price = info["price"]
        current_mcap = info["market_cap"]
        circ_supply = info["circulating_supply"]

        if circ_supply > 0:
            target_price = target / circ_supply
            target_price_str = f"${target_price:.4f}" if target_price < 1 else f"${target_price:.2f}"
        else:
            target_price_str = "无法估算"

        if current_price < 0.01:
            price_str = f"${current_price:.6f}"
        elif current_price < 1:
            price_str = f"${current_price:.4f}"
        else:
            price_str = f"${current_price:.2f}"

        status_icon = "🟢" if current_mcap <= target else "⚪"

        lines.append(
            f"• *{symbol}* ({name or '-'})\n"
            f"  当前价格：{price_str}\n"
            f"  当前市值：{format_mcap(current_mcap)} {status_icon}\n"
            f"  提醒市值：≤ {format_mcap(target)}\n"
            f"  提醒价格：约 {target_price_str}{cooldown_text}"
        )

    for i in range(0, len(lines), 6):
        chunk = "\n\n".join(lines[i:i+6])
        await update.message.reply_text(chunk, parse_mode="Markdown")


async def edit_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if len(context.args) < 2:
        await update.message.reply_text("用法：/edit 符号 新目标\n例：/edit BTC 1.5T  或  /edit BTC 75000")
        return

    symbol = context.args[0].upper()
    value_str = context.args[1]

    target_mcap = parse_mcap(value_str)
    if target_mcap is None:
        price = parse_number(value_str)
        if price is None:
            await update.message.reply_text("格式错误，请使用 1.5T 或 78000")
            return
        # 需要换算
        rows = db_execute(
            "SELECT cg_id FROM watches WHERE user_id=? AND symbol=?",
            (user_id, symbol), fetch=True
        )
        if not rows:
            await update.message.reply_text(f"你没有监控 {symbol}")
            return
        info = get_ticker_info(rows[0][0])
        if not info or info["circulating_supply"] <= 0:
            await update.message.reply_text("无法获取流通量，请直接用市值修改")
            return
        target_mcap = price * info["circulating_supply"]

    count = db_execute(
        "UPDATE watches SET target_mcap=?, cooldown_until=NULL WHERE user_id=? AND symbol=?",
        (target_mcap, user_id, symbol)
    )
    if count == 0:
        await update.message.reply_text(f"你没有监控 {symbol}")
        return

    await update.message.reply_text(
        f"✅ 已修改 *{symbol}* 目标市值 → ≤ *{format_mcap(target_mcap)}*",
        parse_mode="Markdown"
    )


async def remove_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not context.args:
        await update.message.reply_text("用法：/remove 符号1 符号2 ...\n例：/remove BTC ETH SOL")
        return

    removed = []
    not_found = []
    for sym in context.args:
        symbol = sym.upper()
        count = db_execute(
            "DELETE FROM watches WHERE user_id=? AND symbol=?",
            (user_id, symbol)
        )
        if count > 0:
            removed.append(symbol)
        else:
            not_found.append(symbol)

    msg = ""
    if removed:
        msg += f"✅ 已删除：{', '.join(removed)}"
    if not_found:
        msg += f"\n❌ 未找到：{', '.join(not_found)}"
    await update.message.reply_text(msg or "没有删除任何内容")


async def removeall_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    count = db_execute(
        "SELECT COUNT(*) FROM watches WHERE user_id=?", (user_id,), fetch=True
    )[0][0]
    if count == 0:
        await update.message.reply_text("你当前没有监控任何代币。")
        return

    context.user_data["confirm_removeall"] = True
    await update.message.reply_text(
        f"⚠️ 你确定要删除自己全部 *{count}* 个监控吗？\n\n请回复 `确认删除` 来确认。",
        parse_mode="Markdown"
    )
    return CONFIRM_REMOVEALL


async def confirm_removeall(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.text.strip() != "确认删除":
        context.user_data.pop("confirm_removeall", None)
        await update.message.reply_text("已取消清空操作。")
        return ConversationHandler.END

    user_id = update.effective_user.id
    count = db_execute("DELETE FROM watches WHERE user_id=?", (user_id,))
    context.user_data.pop("confirm_removeall", None)
    await update.message.reply_text(f"✅ 已清空你的全部监控（共删除 {count} 个）。")
    return ConversationHandler.END


async def mcap_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    if context.args:
        query = context.args[0]
        coin = resolve_coin(query)
        if not coin:
            await update.message.reply_text(f"找不到 {query}")
            return
        paprika_id, symbol, name = coin
        info = get_ticker_info(paprika_id)
        if not info:
            await update.message.reply_text(f"获取 {symbol} 数据失败")
            return

        rows = db_execute(
            "SELECT target_mcap FROM watches WHERE user_id=? AND cg_id=?",
            (user_id, paprika_id), fetch=True
        )
        extra = ""
        if rows:
            target = rows[0][0]
            status = "✅ 已低于目标" if info["market_cap"] <= target else "尚未达到目标"
            extra = f"\n你的目标：≤ {format_mcap(target)}\n状态：{status}"

        p = info["price"]
        price_str = f"${p:.6f}" if p < 0.01 else (f"${p:.4f}" if p < 1 else f"${p:.2f}")

        await update.message.reply_text(
            f"*{symbol}* ({name})\n"
            f"当前价格：{price_str}\n"
            f"当前市值：*{format_mcap(info['market_cap'])}*{extra}",
            parse_mode="Markdown"
        )
        return

    rows = db_execute(
        "SELECT symbol, cg_id, name, target_mcap FROM watches WHERE user_id=? ORDER BY symbol",
        (user_id,), fetch=True
    )
    if not rows:
        await update.message.reply_text("你没有监控任何代币。")
        return

    await update.message.reply_text(f"正在查询 {len(rows)} 个代币，请稍候...")
    lines = [f"*你的监控币当前市值*（共 {len(rows)} 个）\n"]
    for symbol, paprika_id, name, target in rows:
        info = get_ticker_info(paprika_id)
        if not info:
            status = "获取失败"
        else:
            mark = "🟢" if info["market_cap"] <= target else "⚪"
            status = f"{mark} {format_mcap(info['market_cap'])}  (目标 {format_mcap(target)})"
        lines.append(f"• *{symbol}*  {status}")
        time.sleep(1.5)

    for i in range(0, len(lines), 25):
        await update.message.reply_text("\n".join(lines[i:i+25]), parse_mode="Markdown")


async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    count = db_execute("SELECT COUNT(*) FROM watches WHERE user_id=?", (user_id,), fetch=True)[0][0]
    total = db_execute("SELECT COUNT(*) FROM watches", fetch=True)[0][0]
    await update.message.reply_text(
        f"*机器人状态*\n\n"
        f"• 你的监控数量：{count}\n"
        f"• 全局监控数量：{total}\n"
        f"• 数据源：CoinPaprika\n"
        f"• 检查频率：每 {CHECK_INTERVAL_HOURS} 小时\n"
        f"• 触发后通知次数：{NOTIFY_TIMES} 次\n"
        f"• 冷却期：{COOLDOWN_DAYS} 天",
        parse_mode="Markdown"
    )


# ==================== 添加逻辑 ====================
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id):
        return
    if context.user_data.get("confirm_removeall"):
        return

    text = update.message.text.strip()
    user_id = update.effective_user.id

    lines = re.split(r"[\n,]+", text)
    added = []
    failed = []

    for line in lines:
        line = line.strip()
        if not line:
            continue

        parts = line.split()
        if len(parts) == 1:
            context.user_data["pending_symbol"] = parts[0]
            await update.message.reply_text(
                f"请输入 *{parts[0].upper()}* 的目标（市值如 1.5T 或价格如 78000）：",
                parse_mode="Markdown"
            )
            return WAITING_TARGET

        if len(parts) >= 2:
            symbol_or_addr = parts[0]
            value_str = parts[1]

            target_mcap = parse_mcap(value_str)
            is_price = False
            price = None

            if target_mcap is None:
                price = parse_number(value_str)
                if price is None:
                    failed.append(f"{line}（格式错误，请用 1.5T 或 78000）")
                    continue
                is_price = True

            coin = resolve_coin(symbol_or_addr)
            if not coin:
                failed.append(f"{symbol_or_addr}（找不到该币）")
                continue

            paprika_id, symbol, name = coin

            if is_price:
                info = get_ticker_info(paprika_id)
                if not info or info["circulating_supply"] <= 0:
                    failed.append(f"{symbol}（无法获取流通量，请改用市值添加）")
                    continue
                target_mcap = price * info["circulating_supply"]
                added_msg = f"{symbol}（价格 ${price:,.2f} → 市值 {format_mcap(target_mcap)}）"
            else:
                added_msg = f"{symbol} ({name}) → ≤ {format_mcap(target_mcap)}"

            exists = db_execute(
                "SELECT id FROM watches WHERE user_id=? AND cg_id=?",
                (user_id, paprika_id), fetch=True
            )
            if exists:
                db_execute(
                    "UPDATE watches SET target_mcap=?, cooldown_until=NULL WHERE user_id=? AND cg_id=?",
                    (target_mcap, user_id, paprika_id)
                )
                added.append(f"{added_msg}（已更新）")
            else:
                db_execute(
                    "INSERT INTO watches (user_id, symbol, cg_id, name, target_mcap, added_at) VALUES (?,?,?,?,?,?)",
                    (user_id, symbol, paprika_id, name, target_mcap, datetime.utcnow().isoformat())
                )
                added.append(added_msg)

            time.sleep(1.8)

    msg = ""
    if added:
        msg += "✅ 已添加/更新：\n" + "\n".join(f"• {a}" for a in added)
    if failed:
        msg += "\n\n❌ 失败：\n" + "\n".join(f"• {f}" for f in failed)
    if not msg:
        msg = "没有识别到有效内容"
    await update.message.reply_text(msg)


async def receive_target(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    symbol_or_addr = context.user_data.get("pending_symbol")
    if not symbol_or_addr:
        await update.message.reply_text("请重新发送")
        return ConversationHandler.END

    value_str = update.message.text.strip()
    target_mcap = parse_mcap(value_str)
    is_price = False
    price = None

    if target_mcap is None:
        price = parse_number(value_str)
        if price is None:
            await update.message.reply_text("格式错误，请输入市值（1.5T）或价格（78000）")
            return WAITING_TARGET
        is_price = True

    coin = resolve_coin(symbol_or_addr)
    if not coin:
        await update.message.reply_text(f"找不到 {symbol_or_addr}")
        return ConversationHandler.END

    paprika_id, symbol, name = coin

    if is_price:
        info = get_ticker_info(paprika_id)
        if not info or info["circulating_supply"] <= 0:
            await update.message.reply_text("无法获取流通量，请改用市值添加")
            return ConversationHandler.END
        target_mcap = price * info["circulating_supply"]
        tip = f"（由价格 ${price:,.2f} 换算）"
    else:
        tip = ""

    exists = db_execute(
        "SELECT id FROM watches WHERE user_id=? AND cg_id=?",
        (user_id, paprika_id), fetch=True
    )
    if exists:
        db_execute(
            "UPDATE watches SET target_mcap=?, cooldown_until=NULL WHERE user_id=? AND cg_id=?",
            (target_mcap, user_id, paprika_id)
        )
        await update.message.reply_text(f"✅ 已更新 {symbol} 目标市值 ≤ {format_mcap(target_mcap)}{tip}")
    else:
        db_execute(
            "INSERT INTO watches (user_id, symbol, cg_id, name, target_mcap, added_at) VALUES (?,?,?,?,?,?)",
            (user_id, symbol, paprika_id, name, target_mcap, datetime.utcnow().isoformat())
        )
        await update.message.reply_text(f"✅ 已添加 {symbol} ({name})\n目标市值 ≤ {format_mcap(target_mcap)}{tip}")

    context.user_data.pop("pending_symbol", None)
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("pending_symbol", None)
    context.user_data.pop("confirm_removeall", None)
    await update.message.reply_text("已取消")
    return ConversationHandler.END


# ==================== 定时检查 ====================
async def check_all(context: ContextTypes.DEFAULT_TYPE):
    logger.info("开始执行市值检查任务...")
    rows = db_execute(
        "SELECT id, user_id, symbol, cg_id, name, target_mcap, cooldown_until FROM watches",
        fetch=True
    )
    if not rows:
        return

    now = datetime.utcnow()
    notified_count = 0

    for row in rows:
        watch_id, user_id, symbol, paprika_id, name, target, cooldown = row

        if cooldown:
            try:
                if datetime.fromisoformat(cooldown) > now:
                    continue
            except:
                pass

        info = get_ticker_info(paprika_id)
        if not info:
            time.sleep(1.5)
            continue

        mcap = info["market_cap"]
        price = info["price"]

        if mcap <= target:
            if price < 0.01:
                price_str = f"${price:.6f}"
            elif price < 1:
                price_str = f"${price:.4f}"
            else:
                price_str = f"${price:.2f}"

            msg = (
                f"🔔 *市值提醒*\n\n"
                f"*{symbol}* ({name})\n"
                f"当前价格：{price_str}\n"
                f"当前市值：*{format_mcap(mcap)}*\n"
                f"目标市值：*{format_mcap(target)}*\n"
                f"已低于目标，可考虑关注。"
            )
            for i in range(NOTIFY_TIMES):
                try:
                    await context.bot.send_message(
                        chat_id=user_id,
                        text=msg + f"\n\n（第 {i+1}/{NOTIFY_TIMES} 次通知）",
                        parse_mode="Markdown"
                    )
                except Exception as e:
                    logger.error(f"发送通知失败: {e}")
                if i < NOTIFY_TIMES - 1:
                    await asyncio.sleep(NOTIFY_INTERVAL_SECONDS)

            cooldown_until = (now + timedelta(days=COOLDOWN_DAYS)).isoformat()
            db_execute(
                "UPDATE watches SET cooldown_until=?, last_notified=? WHERE id=?",
                (cooldown_until, now.isoformat(), watch_id)
            )
            notified_count += 1

        time.sleep(1.6)

    logger.info(f"检查完成，共触发 {notified_count} 个提醒")


# ==================== 主程序 ====================
def main():
    init_db()
    app = Application.builder().token(BOT_TOKEN).build()

    conv_add = ConversationHandler(
        entry_points=[MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message)],
        states={
            WAITING_TARGET: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_target)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
    )

    conv_removeall = ConversationHandler(
        entry_points=[CommandHandler("removeall", removeall_cmd)],
        states={
            CONFIRM_REMOVEALL: [MessageHandler(filters.TEXT & ~filters.COMMAND, confirm_removeall)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("list", list_cmd))
    app.add_handler(CommandHandler("edit", edit_cmd))
    app.add_handler(CommandHandler("remove", remove_cmd))
    app.add_handler(CommandHandler("mcap", mcap_cmd))
    app.add_handler(CommandHandler("price", mcap_cmd))
    app.add_handler(CommandHandler("status", status_cmd))
    app.add_handler(conv_removeall)
    app.add_handler(conv_add)

    job_queue = app.job_queue
    job_queue.run_repeating(
        check_all,
        interval=CHECK_INTERVAL_HOURS * 3600,
        first=30
    )

    logger.info("机器人启动成功（CoinPaprika + 价格/市值双模式）")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
