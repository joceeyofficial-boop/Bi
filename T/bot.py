import asyncio
import logging
import os
import sqlite3
import time
import hmac
import hashlib
import base64
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, List
from urllib.parse import urlencode

import aiohttp
from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import Message
from dotenv import load_dotenv

load_dotenv()

# ==================== 配置区域 ====================
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))

OKX_API_KEY = os.getenv("OKX_API_KEY", "")
OKX_SECRET_KEY = os.getenv("OKX_SECRET_KEY", "")
OKX_PASSPHRASE = os.getenv("OKX_PASSPHRASE", "")

# Solana chainIndex
CHAIN_INDEX = "501"

POLL_INTERVAL = 180          # 3分钟检查一次，控制免费额度消耗
# ==================================================

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

bot = Bot(token=TELEGRAM_BOT_TOKEN)
dp = Dispatcher()
router = Router()
dp.include_router(router)

DB_PATH = "smart_money_bot_okx.db"


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
            buy_date TEXT,
            notified_at INTEGER,
            tx_signature TEXT,
            PRIMARY KEY (wallet, mint, buy_date)
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


def get_okx_headers(method: str, request_path: str, body: str = "") -> dict:
    """生成 OKX API 签名头"""
    timestamp = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3] + 'Z'
    prehash = timestamp + method.upper() + request_path + body
    signature = base64.b64encode(
        hmac.new(
            OKX_SECRET_KEY.encode('utf-8'),
            prehash.encode('utf-8'),
            hashlib.sha256
        ).digest()
    ).decode()

    return {
        "OK-ACCESS-KEY": OKX_API_KEY,
        "OK-ACCESS-SIGN": signature,
        "OK-ACCESS-TIMESTAMP": timestamp,
        "OK-ACCESS-PASSPHRASE": OKX_PASSPHRASE,
        "Content-Type": "application/json"
    }


async def okx_get(path: str, params: dict = None) -> dict:
    """发送 OKX GET 请求"""
    if params:
        query = urlencode(params)
        full_path = f"{path}?{query}"
    else:
        full_path = path

    url = f"https://web3.okx.com{full_path}"
    headers = get_okx_headers("GET", full_path)

    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(url, headers=headers, timeout=20) as resp:
                data = await resp.json()
                if data.get("code") != "0":
                    logger.error(f"OKX API错误: {data}")
                    return {}
                return data
        except Exception as e:
            logger.error(f"OKX请求异常: {e}")
            return {}


async def get_wallet_dex_history(wallet: str, begin_ts: int = None) -> List[dict]:
    """获取钱包最近的 DEX 交易历史（买入优先）"""
    now = int(time.time() * 1000)
    if begin_ts is None:
        begin_ts = now - 24 * 3600 * 1000  # 默认查最近24小时

    params = {
        "chainIndex": CHAIN_INDEX,
        "walletAddress": wallet,
        "begin": str(begin_ts),
        "end": str(now),
        "type": "1",          # 1 = BUY
        "limit": "50"
    }

    data = await okx_get("/api/v6/dex/market/portfolio/dex-history", params)
    if not data:
        return []

    result = data.get("data", {})
    return result.get("transactionList", []) or []


async def get_token_pnl(wallet: str, mint: str) -> dict:
    """获取该钱包对某个代币的最新盈亏信息（可选）"""
    # 这里可以调用 recent-pnl 或 token latest-pnl，先简化处理
    return {}


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
                launch_ts = int(pair["pairCreatedAt"] / 1000) if pair.get("pairCreatedAt") else None
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


def get_buy_dates(wallet: str, mint: str) -> List[str]:
    conn = get_db()
    c = conn.cursor()
    c.execute(
        "SELECT buy_date FROM notified WHERE wallet=? AND mint=? ORDER BY buy_date",
        (wallet, mint)
    )
    rows = c.fetchall()
    conn.close()
    return [r[0] for r in rows]


def calculate_consecutive_days(buy_dates: List[str]) -> int:
    if not buy_dates:
        return 0
    dates = sorted(set(buy_dates), reverse=True)
    consecutive = 1
    for i in range(len(dates) - 1):
        d1 = datetime.strptime(dates[i], "%Y-%m-%d")
        d2 = datetime.strptime(dates[i + 1], "%Y-%m-%d")
        if (d1 - d2).days == 1:
            consecutive += 1
        else:
            break
    return consecutive


async def process_new_buys(wallet: str):
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT last_checked FROM tracked_wallets WHERE address = ?", (wallet,))
    row = c.fetchone()
    last_checked = row[0] if row else 0
    conn.close()

    # 从上次检查时间开始查（毫秒）
    begin_ts = last_checked * 1000 if last_checked > 0 else int(time.time() * 1000) - 6 * 3600 * 1000

    trades = await get_wallet_dex_history(wallet, begin_ts)
    if not trades:
        return

    logger.info(f"钱包 {wallet[:8]}... 发现 {len(trades)} 笔买入记录")

    newest_ts = last_checked

    for trade in trades:
        try:
            # OKX 返回字段可能因版本略有差异，做兼容处理
            mint = trade.get("tokenContractAddress") or trade.get("tokenAddress")
            if not mint:
                continue

            tx_time = int(trade.get("time") or trade.get("txTime") or 0)
            if tx_time <= last_checked * 1000:
                continue

            amount = float(trade.get("amount") or 0)
            price = float(trade.get("price") or 0)
            value_usd = float(trade.get("valueUsd") or 0)
            symbol = trade.get("tokenSymbol") or "UNKNOWN"

            # 更新最新时间
            newest_ts = max(newest_ts, tx_time // 1000)

            await handle_buy(
                wallet=wallet,
                mint=mint,
                token_amount=amount,
                buy_ts=tx_time // 1000,
                price_usd=price,
                value_usd=value_usd,
                symbol=symbol
            )
        except Exception as e:
            logger.error(f"处理单笔交易失败: {e}")

    # 更新 last_checked
    if newest_ts > last_checked:
        conn = get_db()
        c = conn.cursor()
        c.execute(
            "UPDATE tracked_wallets SET last_checked = ?, status = 'normal' WHERE address = ?",
            (newest_ts, wallet)
        )
        conn.commit()
        conn.close()


async def handle_buy(wallet: str, mint: str, token_amount: float, buy_ts: int,
                     price_usd: float = 0, value_usd: float = 0, symbol: str = "UNKNOWN"):
    buy_date = datetime.fromtimestamp(buy_ts, tz=timezone.utc).strftime("%Y-%m-%d")

    # 同一天只通知一次
    conn = get_db()
    c = conn.cursor()
    c.execute(
        "SELECT 1 FROM notified WHERE wallet=? AND mint=? AND buy_date=?",
        (wallet, mint, buy_date)
    )
    if c.fetchone():
        conn.close()
        return

    # 补充 DexScreener 信息
    info = await get_dexscreener_info(mint)
    if info:
        name = info.get("name", symbol)
        if not price_usd:
            price_usd = info.get("price_usd", 0)
        liquidity = info.get("liquidity", 0)
        market_cap = info.get("market_cap", 0)
        launch_ts = info.get("launch_ts")
    else:
        name = symbol
        liquidity = 0
        market_cap = 0
        launch_ts = None

    if not price_usd and value_usd and token_amount > 0:
        price_usd = value_usd / token_amount

    cost_this_buy = token_amount * price_usd if price_usd else value_usd

    c.execute("""
        INSERT INTO cost_basis (wallet, mint, total_tokens, total_cost_usd)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(wallet, mint) DO UPDATE SET
            total_tokens = total_tokens + excluded.total_tokens,
            total_cost_usd = total_cost_usd + excluded.total_cost_usd
    """, (wallet, mint, token_amount, cost_this_buy))

    c.execute(
        "INSERT INTO notified (wallet, mint, buy_date, notified_at, tx_signature) VALUES (?, ?, ?, ?, ?)",
        (wallet, mint, buy_date, int(time.time()), "")
    )
    c.execute(
        "INSERT OR REPLACE INTO token_info (mint, symbol, name, launch_ts) VALUES (?, ?, ?, ?)",
        (mint, symbol, name, launch_ts or 0)
    )
    conn.commit()

    buy_dates = get_buy_dates(wallet, mint)
    times = len(buy_dates)
    consecutive = calculate_consecutive_days(buy_dates)

    c.execute("SELECT total_tokens, total_cost_usd FROM cost_basis WHERE wallet=? AND mint=?", (wallet, mint))
    row = c.fetchone()
    total_tokens = row[0] if row else 0
    total_cost = row[1] if row else 0
    avg_cost = (total_cost / total_tokens) if total_tokens > 0 else price_usd
    conn.close()

    # 简单计算未实现盈亏（用当前价格）
    current_price = info.get("price_usd", price_usd) if info else price_usd
    pnl_usd = (current_price - avg_cost) * total_tokens if avg_cost > 0 else 0
    pnl_pct = ((current_price / avg_cost) - 1) * 100 if avg_cost > 0 else 0

    if pnl_usd >= 0:
        pnl_text = f"+${pnl_usd:,.2f} (+{pnl_pct:.2f}%)"
    else:
        pnl_text = f"-${abs(pnl_usd):,.2f} ({pnl_pct:.2f}%)"

    age_str = "未知"
    if launch_ts and launch_ts > 1600000000:
        age_str = format_age(buy_ts - launch_ts)

    short_wallet = wallet[:6] + "..." + wallet[-4:]

    text = (
        f"🔔 <b>聪明钱加仓提醒</b>\n\n"
        f"<b>这是第 {times} 次买入该代币（连续 {consecutive} 天加仓）</b>\n\n"
        f"钱包: <code>{short_wallet}</code>\n"
        f"代币: {name} (${symbol})\n\n"
        f"当前价格: ${current_price:.8f}\n"
        f"平均成本（跟踪后）: <b>${avg_cost:.8f}</b>\n"
        f"总盈亏: <b>{pnl_text}</b>\n"
        f"流动性: {format_number(liquidity)}\n"
        f"市值: {format_number(market_cap)}\n"
        f"年龄: {age_str}\n\n"
        f"时间: {datetime.fromtimestamp(buy_ts, tz=timezone.utc).strftime('%Y-%m-%d %H:%M')}\n"
        f"合约: <code>{mint}</code>"
    )

    try:
        await bot.send_message(ADMIN_ID, text, parse_mode="HTML")
        logger.info(f"通知成功: ${symbol} 第{times}次 | 连续{consecutive}天")
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
    await message.answer(
        "聪明钱跟踪 Bot（OKX版）\n\n"
        "/follow 钱包地址 - 开始跟踪\n"
        "/unfollow 钱包地址 - 取消跟踪\n"
        "/follows - 查看跟踪列表\n"
        "/followclear - 清空跟踪\n"
        "/list - 已通知代币总结\n"
        "/status - 运行状态\n"
        "/help - 帮助"
    )


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
        (address, int(time.time()), int(time.time()))
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
        text += f"✅ `{short}`\n状态: {status} | 最后检查: {last_str}\n\n"
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
        SELECT n.mint, t.symbol, COUNT(DISTINCT n.buy_date) as alert_count, 
               cb.total_tokens, cb.total_cost_usd
        FROM notified n
        LEFT JOIN token_info t ON n.mint = t.mint
        LEFT JOIN cost_basis cb ON n.wallet = cb.wallet AND n.mint = cb.mint
        GROUP BY n.mint
        ORDER BY alert_count DESC LIMIT 30
    """)
    rows = c.fetchall()
    conn.close()
    if not rows:
        await message.answer("暂无已通知记录")
        return
    text = "已通知代币列表\n\n"
    for i, (mint, symbol, count, total_tokens, total_cost) in enumerate(rows, 1):
        symbol = symbol or "UNKNOWN"
        avg_cost = (total_cost / total_tokens) if total_tokens and total_tokens > 0 else 0
        text += f"{i}. ${symbol}\n报警: {count}次 | 平均成本: ${avg_cost:.8f}\n\n"
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
    await message.answer(f"运行正常（OKX版）\n跟踪地址数: {wallet_count}\n累计通知次数: {notify_count}")


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
                await asyncio.sleep(2)
        except Exception as e:
            logger.error(f"轮询错误: {e}")
        await asyncio.sleep(POLL_INTERVAL)


async def main():
    if not TELEGRAM_BOT_TOKEN or ADMIN_ID == 0:
        logger.error("请检查 TELEGRAM_BOT_TOKEN / ADMIN_ID")
        return
    if not OKX_API_KEY or not OKX_SECRET_KEY or not OKX_PASSPHRASE:
        logger.error("请检查 OKX_API_KEY / OKX_SECRET_KEY / OKX_PASSPHRASE")
        return

    init_db()
    logger.info("Bot 启动中（OKX Web3 API 版本）...")
    asyncio.create_task(polling_loop())
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
