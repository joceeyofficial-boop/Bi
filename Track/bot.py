import asyncio
import logging
import os
import sqlite3
import time
from datetime import datetime, timezone
from typing import Optional, Dict, Any, List

import aiohttp
from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import Message
from dotenv import load_dotenv

load_dotenv()

# ==================== 配置区域 ====================
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
HELIUS_API_KEY = os.getenv("HELIUS_API_KEY")

POLL_INTERVAL = 25  # 轮询间隔（秒）
# ==================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

bot = Bot(token=TELEGRAM_BOT_TOKEN)
dp = Dispatcher()
router = Router()
dp.include_router(router)

DB_PATH = "smart_money_bot.db"


def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    c.execute("""
        CREATE TABLE IF NOT EXISTS tracked_wallets (
            address TEXT PRIMARY KEY,
            added_at INTEGER,
            last_checked INTEGER DEFAULT 0,
            status TEXT DEFAULT 'normal'
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS notified (
            wallet TEXT,
            mint TEXT,
            day_number INTEGER,
            notified_at INTEGER,
            tx_signature TEXT,
            PRIMARY KEY (wallet, mint, day_number)
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS cost_basis (
            wallet TEXT,
            mint TEXT,
            total_tokens REAL DEFAULT 0,
            total_cost_usd REAL DEFAULT 0,
            PRIMARY KEY (wallet, mint)
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS token_info (
            mint TEXT PRIMARY KEY,
            symbol TEXT,
            name TEXT,
            launch_ts INTEGER
        )
    """)

    conn.commit()
    conn.close()


def get_db():
    return sqlite3.connect(DB_PATH)


def is_admin(user_id: int) -> bool:
    return user_id == ADMIN_ID


# ------------------ Helius & DexScreener ------------------

async def get_recent_parsed_transactions(wallet: str, limit: int = 20) -> list:
    """获取地址最近的解析交易（稳定方式）"""
    url = f"https://api.helius.xyz/v0/addresses/{wallet}/transactions/?api-key={HELIUS_API_KEY}&limit={limit}"

    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(url, timeout=20) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    logger.error(f"获取交易失败 {resp.status}: {text[:200]}")
                    return []
                data = await resp.json()
                return data if isinstance(data, list) else []
        except Exception as e:
            logger.error(f"获取交易异常: {e}")
            return []


async def get_token_balance(wallet: str, mint: str) -> float:
    url = f"https://mainnet.helius-rpc.com/?api-key={HELIUS_API_KEY}"
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "getTokenAccountsByOwner",
        "params": [
            wallet,
            {"mint": mint},
            {"encoding": "jsonParsed"}
        ]
    }
    async with aiohttp.ClientSession() as session:
        try:
            async with session.post(url, json=payload, timeout=15) as resp:
                data = await resp.json()
                result = data.get("result", {})
                values = result.get("value", [])
                if values:
                    amount = values[0]["account"]["data"]["parsed"]["info"]["tokenAmount"]["uiAmount"]
                    return float(amount or 0)
        except Exception as e:
            logger.error(f"获取余额失败: {e}")
    return 0.0


async def get_token_supply(mint: str) -> float:
    url = f"https://mainnet.helius-rpc.com/?api-key={HELIUS_API_KEY}"
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "getTokenSupply",
        "params": [mint]
    }
    async with aiohttp.ClientSession() as session:
        try:
            async with session.post(url, json=payload, timeout=15) as resp:
                data = await resp.json()
                value = data.get("result", {}).get("value", {})
                return float(value.get("uiAmount") or 0)
        except Exception as e:
            logger.error(f"获取供应量失败: {e}")
    return 0.0


async def get_dexscreener_info(mint: str) -> Dict[str, Any]:
    url = f"https://api.dexscreener.com/tokens/v1/solana/{mint}"
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(url, timeout=10) as resp:
                data = await resp.json()
                pairs = data if isinstance(data, list) else data.get("pairs", [])
                if not pairs:
                    return {}
                pair = max(pairs, key=lambda x: (x.get("liquidity") or {}).get("usd") or 0)

                launch_ts = None
                if pair.get("pairCreatedAt"):
                    launch_ts = int(pair["pairCreatedAt"] / 1000)

                return {
                    "symbol": pair.get("baseToken", {}).get("symbol", "UNKNOWN"),
                    "name": pair.get("baseToken", {}).get("name", "Unknown"),
                    "price_usd": float(pair.get("priceUsd") or 0),
                    "market_cap": pair.get("marketCap") or pair.get("fdv") or 0,
                    "liquidity": (pair.get("liquidity") or {}).get("usd") or 0,
                    "launch_ts": launch_ts,
                }
        except Exception as e:
            logger.error(f"DexScreener 错误: {e}")
    return {}


# ------------------ 核心业务逻辑 ------------------

async def process_new_buys(wallet: str):
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT last_checked FROM tracked_wallets WHERE address = ?", (wallet,))
    row = c.fetchone()
    last_checked = row[0] if row else 0
    conn.close()

    txs = await get_recent_parsed_transactions(wallet, limit=20)
    if not txs:
        return

    new_txs = [tx for tx in txs if tx.get("timestamp", 0) > last_checked]
    new_txs.sort(key=lambda x: x.get("timestamp", 0))

    if new_txs:
        logger.info(f"钱包 {wallet[:8]}... 发现 {len(new_txs)} 笔新交易")

    for tx in new_txs:
        signature = tx.get("signature")
        timestamp = tx.get("timestamp", int(time.time()))

        token_transfers = tx.get("tokenTransfers", []) or []
        for transfer in token_transfers:
            to_user = transfer.get("toUserAccount")
            mint = transfer.get("mint")
            token_amount = float(transfer.get("tokenAmount") or 0)

            if (to_user == wallet and mint and token_amount > 0
                    and mint != "So11111111111111111111111111111111111111112"):
                await handle_buy(wallet, mint, token_amount, timestamp, signature)

    if txs:
        newest = max(tx.get("timestamp", 0) for tx in txs)
        conn = get_db()
        c = conn.cursor()
        c.execute(
            "UPDATE tracked_wallets SET last_checked = ?, status = 'normal' WHERE address = ?",
            (newest, wallet)
        )
        conn.commit()
        conn.close()


async def handle_buy(wallet: str, mint: str, token_amount: float, buy_ts: int, signature: str):
    info = await get_dexscreener_info(mint)
    if not info:
        return

    symbol = info.get("symbol", "UNKNOWN")
    name = info.get("name", "Unknown")
    launch_ts = info.get("launch_ts")
    price_usd = info.get("price_usd", 0)

    if not launch_ts:
        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT launch_ts FROM token_info WHERE mint = ?", (mint,))
        row = c.fetchone()
        conn.close()
        launch_ts = row[0] if row else buy_ts

    day_number = max(1, (buy_ts - launch_ts) // 86400 + 1)

    # 去重
    conn = get_db()
    c = conn.cursor()
    c.execute(
        "SELECT 1 FROM notified WHERE wallet=? AND mint=? AND day_number=?",
        (wallet, mint, day_number)
    )
    if c.fetchone():
        conn.close()
        return

    # 更新成本
    cost_this_buy = token_amount * price_usd
    c.execute("""
        INSERT INTO cost_basis (wallet, mint, total_tokens, total_cost_usd)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(wallet, mint) DO UPDATE SET
            total_tokens = total_tokens + excluded.total_tokens,
            total_cost_usd = total_cost_usd + excluded.total_cost_usd
    """, (wallet, mint, token_amount, cost_this_buy))

    c.execute(
        "INSERT INTO notified (wallet, mint, day_number, notified_at, tx_signature) VALUES (?, ?, ?, ?, ?)",
        (wallet, mint, day_number, int(time.time()), signature)
    )

    c.execute(
        "INSERT OR REPLACE INTO token_info (mint, symbol, name, launch_ts) VALUES (?, ?, ?, ?)",
        (mint, symbol, name, launch_ts)
    )
    conn.commit()

    # 持仓占比 & 平均成本
    balance = await get_token_balance(wallet, mint)
    supply = await get_token_supply(mint)
    holding_pct = (balance / supply * 100) if supply > 0 else 0.0

    c.execute("SELECT total_tokens, total_cost_usd FROM cost_basis WHERE wallet=? AND mint=?", (wallet, mint))
    row = c.fetchone()
    avg_cost = (row[1] / row[0]) if row and row[0] > 0 else price_usd
    conn.close()

    short_wallet = wallet[:6] + "..." + wallet[-4:]
    age_seconds = buy_ts - launch_ts
    age_str = format_age(age_seconds)

    text = (
        f"聪明钱买入提醒\n\n"
        f"钱包: `{short_wallet}`\n"
        f"代币: {name} (${symbol})\n"
        f"**这是第 {day_number} 次买入该代币**\n\n"
        f"流动性: {format_number(info.get('liquidity', 0))}\n"
        f"市值: {format_number(info.get('market_cap', 0))}\n"
        f"年龄: {age_str}\n"
        f"当前持仓占比: {holding_pct:.2f}%\n"
        f"平均成本价: ${avg_cost:.8f}\n\n"
        f"时间: {datetime.fromtimestamp(buy_ts, tz=timezone.utc).strftime('%Y-%m-%d %H:%M')}\n"
        f"Tx: `{signature}`"
    )

    try:
        await bot.send_message(ADMIN_ID, text, parse_mode="Markdown")
        logger.info(f"通知成功: ${symbol} 第{day_number}次")
    except Exception as e:
        logger.error(f"发送通知失败: {e}")


def format_number(n: float) -> str:
    if n >= 1_000_000:
        return f"{n/1_000_000:.2f}M"
    if n >= 1_000:
        return f"{n/1_000:.1f}K"
    return f"{n:.0f}"


def format_age(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds}秒"
    if seconds < 3600:
        return f"{seconds // 60}分钟"
    if seconds < 86400:
        return f"{seconds // 3600}小时"
    return f"{seconds // 86400}天"


# ------------------ 命令 ------------------

@router.message(Command("start"), F.from_user.id == ADMIN_ID)
@router.message(Command("help"), F.from_user.id == ADMIN_ID)
async def cmd_help(message: Message):
    text = (
        "聪明钱跟踪 Bot\n\n"
        "/follow 钱包地址 - 开始跟踪\n"
        "/unfollow 钱包地址 - 取消跟踪\n"
        "/follows - 查看跟踪列表\n"
        "/followclear - 清空跟踪\n"
        "/list - 已通知代币总结\n"
        "/status - 运行状态\n"
        "/help - 帮助"
    )
    await message.answer(text)


@router.message(Command("follow"), F.from_user.id == ADMIN_ID)
async def cmd_follow(message: Message, command: CommandObject):
    if not command.args:
        await message.answer("用法: /follow 钱包地址")
        return
    address = command.args.strip()
    if len(address) < 32:
        await message.answer("地址格式不正确")
        return

    conn = get_db()
    c = conn.cursor()
    c.execute(
        "INSERT OR IGNORE INTO tracked_wallets (address, added_at, last_checked) VALUES (?, ?, ?)",
        (address, int(time.time()), int(time.time()) - 3600)
    )
    conn.commit()
    conn.close()
    await message.answer(f"已开始跟踪:\n`{address}`", parse_mode="Markdown")


@router.message(Command("unfollow"), F.from_user.id == ADMIN_ID)
async def cmd_unfollow(message: Message, command: CommandObject):
    if not command.args:
        await message.answer("用法: /unfollow 钱包地址")
        return
    address = command.args.strip()
    conn = get_db()
    c = conn.cursor()
    c.execute("DELETE FROM tracked_wallets WHERE address = ?", (address,))
    conn.commit()
    conn.close()
    await message.answer(f"已取消跟踪: `{address}`", parse_mode="Markdown")


@router.message(Command("follows"), F.from_user.id == ADMIN_ID)
async def cmd_follows(message: Message):
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT address, status, last_checked FROM tracked_wallets")
    rows = c.fetchall()
    conn.close()

    if not rows:
        await message.answer("当前没有跟踪任何地址")
        return

    text = "当前跟踪列表:\n\n"
    for addr, status, last in rows:
        short = addr[:6] + "..." + addr[-4:]
        last_str = datetime.fromtimestamp(last).strftime("%m-%d %H:%M") if last else "无"
        status_emoji = "✅" if status == "normal" else "⚠️"
        text += f"{status_emoji} `{short}`\n状态: {status} | 最后检查: {last_str}\n\n"
    await message.answer(text, parse_mode="Markdown")


@router.message(Command("followclear"), F.from_user.id == ADMIN_ID)
async def cmd_followclear(message: Message):
    conn = get_db()
    c = conn.cursor()
    c.execute("DELETE FROM tracked_wallets")
    conn.commit()
    conn.close()
    await message.answer("已清空所有跟踪地址")


@router.message(Command("list"), F.from_user.id == ADMIN_ID)
async def cmd_list(message: Message):
    conn = get_db()
    c = conn.cursor()
    c.execute("""
        SELECT n.mint, t.symbol, t.name, COUNT(*) as alert_count,
               cb.total_tokens, cb.total_cost_usd
        FROM notified n
        LEFT JOIN token_info t ON n.mint = t.mint
        LEFT JOIN cost_basis cb ON n.wallet = cb.wallet AND n.mint = cb.mint
        GROUP BY n.mint
        ORDER BY alert_count DESC
        LIMIT 30
    """)
    rows = c.fetchall()
    conn.close()

    if not rows:
        await message.answer("暂无已通知记录")
        return

    text = "已通知代币列表（按报警次数排序）\n\n"
    for i, (mint, symbol, name, count, total_tokens, total_cost) in enumerate(rows, 1):
        symbol = symbol or "UNKNOWN"
        avg_cost = (total_cost / total_tokens) if total_tokens and total_tokens > 0 else 0

        info = await get_dexscreener_info(mint)
        mcap = format_number(info.get("market_cap", 0))

        text += (
            f"{i}. ${symbol}\n"
            f"市值: {mcap} | 报警次数: {count}次\n"
            f"平均成本: ${avg_cost:.8f}\n\n"
        )
    await message.answer(text)


@router.message(Command("status"), F.from_user.id == ADMIN_ID)
async def cmd_status(message: Message):
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM tracked_wallets")
    wallet_count = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM notified")
    notify_count = c.fetchone()[0]
    conn.close()
    await message.answer(f"运行正常\n跟踪地址数: {wallet_count}\n累计通知次数: {notify_count}")


# ------------------ 后台轮询 ------------------

async def polling_loop():
    while True:
        try:
            conn = get_db()
            c = conn.cursor()
            c.execute("SELECT address FROM tracked_wallets")
            wallets = [row[0] for row in c.fetchall()]
            conn.close()

            for wallet in wallets:
                await process_new_buys(wallet)
                await asyncio.sleep(1.5)
        except Exception as e:
            logger.error(f"轮询错误: {e}")

        await asyncio.sleep(POLL_INTERVAL)


async def main():
    if not TELEGRAM_BOT_TOKEN or not HELIUS_API_KEY or ADMIN_ID == 0:
        logger.error("请检查环境变量 TELEGRAM_BOT_TOKEN / ADMIN_ID / HELIUS_API_KEY 是否设置正确")
        return

    init_db()
    logger.info("Bot 启动中...")
    asyncio.create_task(polling_loop())
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
