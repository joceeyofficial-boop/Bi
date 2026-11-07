import asyncio
import logging
import os
import sqlite3
import time
import hmac
import hashlib
import base64
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, List, Optional
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

CHAIN_INDEX = "501"

POLL_INTERVAL = 180
MIN_BUY_USD = 300.0              # 单笔低于此金额不通知
HIGHLIGHT_BUY_USD = 500.0        # 单笔较大时额外高亮
EARLY_STAGE_MAX_COST = 2000      # 预估持仓成本低于此值视为早期建仓阶段
# ==================================================

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

bot = Bot(token=TELEGRAM_BOT_TOKEN)
dp = Dispatcher()
router = Router()
dp.include_router(router)

DB_PATH = "smart_money_bot_okx.db"


def init_db():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA busy_timeout=30000;")
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
        CREATE TABLE IF NOT EXISTS buy_records (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            wallet TEXT,
            mint TEXT,
            buy_date TEXT,
            buy_ts INTEGER,
            token_amount REAL,
            value_usd REAL,
            price_usd REAL,
            tx_signature TEXT,
            UNIQUE(wallet, mint, buy_ts, token_amount)
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS sell_records (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            wallet TEXT,
            mint TEXT,
            sell_date TEXT,
            sell_ts INTEGER,
            token_amount REAL,
            value_usd REAL,
            price_usd REAL,
            tx_signature TEXT,
            UNIQUE(wallet, mint, sell_ts, token_amount)
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

    c.execute("""
        CREATE TABLE IF NOT EXISTS daily_flow (
            wallet TEXT,
            mint TEXT,
            flow_date TEXT,
            buy_usd REAL DEFAULT 0,
            sell_usd REAL DEFAULT 0,
            net_usd REAL DEFAULT 0,
            buy_count INTEGER DEFAULT 0,
            sell_count INTEGER DEFAULT 0,
            PRIMARY KEY (wallet, mint, flow_date)
        )
    """)

    conn.commit()
    conn.close()


def get_db():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA busy_timeout=30000;")
    return conn


def get_okx_headers(method: str, request_path: str, body: str = "") -> dict:
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


async def get_wallet_dex_history(wallet: str, begin_ts: int = None, trade_type: str = "1") -> List[dict]:
    now = int(time.time() * 1000)
    if begin_ts is None:
        begin_ts = now - 24 * 3600 * 1000

    params = {
        "chainIndex": CHAIN_INDEX,
        "walletAddress": wallet,
        "begin": str(begin_ts),
        "end": str(now),
        "type": trade_type,
        "limit": "50"
    }

    data = await okx_get("/api/v6/dex/market/portfolio/dex-history", params)
    if not data:
        return []

    result = data.get("data", {})
    return result.get("transactionList", []) or []


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


def update_daily_flow(wallet: str, mint: str, flow_date: str, buy_usd: float = 0, sell_usd: float = 0):
    conn = get_db()
    c = conn.cursor()
    c.execute("""
        INSERT INTO daily_flow (wallet, mint, flow_date, buy_usd, sell_usd, net_usd, buy_count, sell_count)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(wallet, mint, flow_date) DO UPDATE SET
            buy_usd = buy_usd + excluded.buy_usd,
            sell_usd = sell_usd + excluded.sell_usd,
            net_usd = (buy_usd + excluded.buy_usd) - (sell_usd + excluded.sell_usd),
            buy_count = buy_count + excluded.buy_count,
            sell_count = sell_count + excluded.sell_count
    """, (
        wallet, mint, flow_date,
        buy_usd, sell_usd, buy_usd - sell_usd,
        1 if buy_usd > 0 else 0,
        1 if sell_usd > 0 else 0
    ))
    conn.commit()
    conn.close()


def get_net_flow_stats(wallet: str, mint: str, days: int = 7) -> Dict[str, Any]:
    conn = get_db()
    c = conn.cursor()
    since_date = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")

    c.execute("""
        SELECT 
            COALESCE(SUM(buy_usd), 0),
            COALESCE(SUM(sell_usd), 0),
            COALESCE(SUM(net_usd), 0),
            COALESCE(SUM(buy_count), 0),
            COALESCE(SUM(sell_count), 0)
        FROM daily_flow
        WHERE wallet=? AND mint=? AND flow_date >= ?
    """, (wallet, mint, since_date))
    row = c.fetchone()
    conn.close()

    buy_usd, sell_usd, net_usd, buy_count, sell_count = row or (0, 0, 0, 0, 0)
    return {
        "buy_usd": buy_usd,
        "sell_usd": sell_usd,
        "net_usd": net_usd,
        "buy_count": buy_count,
        "sell_count": sell_count,
        "days": days
    }


def get_consecutive_net_buy_days(wallet: str, mint: str) -> int:
    conn = get_db()
    c = conn.cursor()
    c.execute("""
        SELECT flow_date, net_usd FROM daily_flow
        WHERE wallet=? AND mint=?
        ORDER BY flow_date DESC
        LIMIT 30
    """, (wallet, mint))
    rows = c.fetchall()
    conn.close()

    if not rows:
        return 0

    consecutive = 0
    expected_date = datetime.now(timezone.utc).date()

    for flow_date_str, net_usd in rows:
        flow_date = datetime.strptime(flow_date_str, "%Y-%m-%d").date()
        if net_usd > 0 and flow_date == expected_date:
            consecutive += 1
            expected_date -= timedelta(days=1)
        elif net_usd > 0 and (expected_date - flow_date).days == 1:
            consecutive += 1
            expected_date = flow_date - timedelta(days=1)
        else:
            break

    return consecutive


def get_buy_dates(wallet: str, mint: str) -> List[str]:
    conn = get_db()
    c = conn.cursor()
    c.execute("""
        SELECT DISTINCT buy_date FROM buy_records
        WHERE wallet=? AND mint=?
        ORDER BY buy_date DESC
    """, (wallet, mint))
    rows = c.fetchall()
    conn.close()
    return [r[0] for r in rows]


def calculate_consecutive_buy_days(buy_dates: List[str]) -> int:
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


def get_cost_basis(wallet: str, mint: str) -> tuple:
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT total_tokens, total_cost_usd FROM cost_basis WHERE wallet=? AND mint=?", (wallet, mint))
    row = c.fetchone()
    conn.close()
    if row:
        return row[0] or 0, row[1] or 0
    return 0, 0


async def process_wallet(wallet: str):
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT last_checked FROM tracked_wallets WHERE address = ?", (wallet,))
    row = c.fetchone()
    last_checked = row[0] if row else 0
    conn.close()

    begin_ts = last_checked * 1000 if last_checked > 0 else int(time.time() * 1000) - 12 * 3600 * 1000

    buy_trades = await get_wallet_dex_history(wallet, begin_ts, trade_type="1")
    sell_trades = await get_wallet_dex_history(wallet, begin_ts, trade_type="2")

    newest_ts = last_checked

    for trade in buy_trades:
        try:
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
            tx_sig = trade.get("txHash") or trade.get("signature") or ""

            newest_ts = max(newest_ts, tx_time // 1000)

            await handle_trade(
                wallet=wallet,
                mint=mint,
                side="buy",
                token_amount=amount,
                trade_ts=tx_time // 1000,
                price_usd=price,
                value_usd=value_usd,
                symbol=symbol,
                tx_signature=tx_sig
            )
        except Exception as e:
            logger.error(f"处理买入失败: {e}")

    for trade in sell_trades:
        try:
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
            tx_sig = trade.get("txHash") or trade.get("signature") or ""

            newest_ts = max(newest_ts, tx_time // 1000)

            await handle_trade(
                wallet=wallet,
                mint=mint,
                side="sell",
                token_amount=amount,
                trade_ts=tx_time // 1000,
                price_usd=price,
                value_usd=value_usd,
                symbol=symbol,
                tx_signature=tx_sig
            )
        except Exception as e:
            logger.error(f"处理卖出失败: {e}")

    if newest_ts > last_checked:
        conn = get_db()
        c = conn.cursor()
        c.execute(
            "UPDATE tracked_wallets SET last_checked = ?, status = 'normal' WHERE address = ?",
            (newest_ts, wallet)
        )
        conn.commit()
        conn.close()


async def handle_trade(wallet: str, mint: str, side: str, token_amount: float,
                       trade_ts: int, price_usd: float = 0, value_usd: float = 0,
                       symbol: str = "UNKNOWN", tx_signature: str = ""):
    trade_date = datetime.fromtimestamp(trade_ts, tz=timezone.utc).strftime("%Y-%m-%d")

    if not price_usd and value_usd and token_amount > 0:
        price_usd = value_usd / token_amount
    if not value_usd and price_usd and token_amount > 0:
        value_usd = token_amount * price_usd

    conn = get_db()
    c = conn.cursor()

    if side == "buy":
        try:
            c.execute("""
                INSERT OR IGNORE INTO buy_records
                (wallet, mint, buy_date, buy_ts, token_amount, value_usd, price_usd, tx_signature)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (wallet, mint, trade_date, trade_ts, token_amount, value_usd, price_usd, tx_signature))
        except Exception:
            pass

        c.execute("""
            INSERT INTO cost_basis (wallet, mint, total_tokens, total_cost_usd)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(wallet, mint) DO UPDATE SET
                total_tokens = total_tokens + excluded.total_tokens,
                total_cost_usd = total_cost_usd + excluded.total_cost_usd
        """, (wallet, mint, token_amount, value_usd))

        # 先提交主事务，再更新 daily_flow（避免嵌套连接导致锁）
        c.execute(
            "INSERT OR IGNORE INTO token_info (mint, symbol, name, launch_ts) VALUES (?, ?, ?, 0)",
            (mint, symbol, symbol)
        )
        conn.commit()
        conn.close()

        update_daily_flow(wallet, mint, trade_date, buy_usd=value_usd, sell_usd=0)

    else:
        try:
            c.execute("""
                INSERT OR IGNORE INTO sell_records
                (wallet, mint, sell_date, sell_ts, token_amount, value_usd, price_usd, tx_signature)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (wallet, mint, trade_date, trade_ts, token_amount, value_usd, price_usd, tx_signature))
        except Exception:
            pass

        c.execute("""
            UPDATE cost_basis SET
                total_tokens = MAX(0, total_tokens - ?),
                total_cost_usd = CASE
                    WHEN total_tokens > 0 THEN total_cost_usd * (1 - ? / total_tokens)
                    ELSE 0
                END
            WHERE wallet=? AND mint=?
        """, (token_amount, token_amount, wallet, mint))

        # 先提交主事务，再更新 daily_flow（避免嵌套连接导致锁）
        c.execute(
            "INSERT OR IGNORE INTO token_info (mint, symbol, name, launch_ts) VALUES (?, ?, ?, 0)",
            (mint, symbol, symbol)
        )
        conn.commit()
        conn.close()

        update_daily_flow(wallet, mint, trade_date, buy_usd=0, sell_usd=value_usd)

    # 只有单笔买入 ≥ MIN_BUY_USD 才进入通知判断
    if side != "buy" or value_usd < MIN_BUY_USD:
        return

    # 同一天同一币只通知一次
    conn = get_db()
    c = conn.cursor()
    c.execute(
        "SELECT 1 FROM notified WHERE wallet=? AND mint=? AND buy_date=?",
        (wallet, mint, trade_date)
    )
    if c.fetchone():
        conn.close()
        return

    c.execute(
        "INSERT INTO notified (wallet, mint, buy_date, notified_at, tx_signature) VALUES (?, ?, ?, ?, ?)",
        (wallet, mint, trade_date, int(time.time()), tx_signature)
    )
    conn.commit()
    conn.close()

    await send_buy_alert(wallet, mint, token_amount, trade_ts, price_usd, value_usd, symbol)


async def send_buy_alert(wallet: str, mint: str, token_amount: float, buy_ts: int,
                         price_usd: float, value_usd: float, symbol: str):
    info = await get_dexscreener_info(mint)
    name = info.get("name", symbol) if info else symbol
    current_price = info.get("price_usd", price_usd) if info else price_usd
    liquidity = info.get("liquidity", 0) if info else 0
    market_cap = info.get("market_cap", 0) if info else 0
    launch_ts = info.get("launch_ts") if info else None

    total_tokens, total_cost = get_cost_basis(wallet, mint)
    avg_cost = (total_cost / total_tokens) if total_tokens > 0 else price_usd

    pnl_usd = (current_price - avg_cost) * total_tokens if avg_cost > 0 else 0
    pnl_pct = ((current_price / avg_cost) - 1) * 100 if avg_cost > 0 else 0
    if pnl_usd >= 0:
        pnl_text = f"+${pnl_usd:,.2f} (+{pnl_pct:.2f}%)"
    else:
        pnl_text = f"-${abs(pnl_usd):,.2f} ({pnl_pct:.2f}%)"

    buy_dates = get_buy_dates(wallet, mint)
    times = len(buy_dates)
    consecutive_buy_days = calculate_consecutive_buy_days(buy_dates)
    consecutive_net_days = get_consecutive_net_buy_days(wallet, mint)

    flow_1d = get_net_flow_stats(wallet, mint, days=1)
    flow_3d = get_net_flow_stats(wallet, mint, days=3)

    age_str = "未知"
    if launch_ts and launch_ts > 1600000000:
        age_str = format_age(buy_ts - launch_ts)

    short_wallet = wallet[:6] + "..." + wallet[-4:]

    # ========== 信号等级判断（核心） ==========
    is_consecutive = consecutive_buy_days >= 2
    is_net_inflow = consecutive_net_days >= 2 and flow_3d["net_usd"] > 0
    is_large_buy = value_usd >= HIGHLIGHT_BUY_USD
    is_early_stage = total_cost <= EARLY_STAGE_MAX_COST

    if is_consecutive and is_net_inflow and is_early_stage:
        level = "🔥 早期连续建仓"
        level_emoji = "🔥"
    elif is_consecutive and is_early_stage:
        level = "🟡 连续加仓（早期）"
        level_emoji = "🟡"
    elif is_consecutive and is_net_inflow:
        level = "🟢 连续加仓 + 净流入"
        level_emoji = "🟢"
    elif is_consecutive:
        level = "🟢 连续加仓"
        level_emoji = "🟢"
    elif is_large_buy and is_early_stage:
        level = "🟡 大单买入（早期）"
        level_emoji = "🟡"
    elif is_large_buy:
        level = "⚪ 大单买入"
        level_emoji = "⚪"
    else:
        level = "⚪ 普通加仓"
        level_emoji = "⚪"

    net_info = ""
    if consecutive_net_days >= 2:
        net_info = f" | 净流入连续{consecutive_net_days}天"
    elif flow_1d["net_usd"] > 0:
        net_info = f" | 今日净流入${flow_1d['net_usd']:+,.0f}"
    elif flow_1d["net_usd"] < 0:
        net_info = f" | 今日净流出${flow_1d['net_usd']:,.0f}"

    stage_info = ""
    if is_early_stage:
        stage_info = f" | 预估持仓成本 ${total_cost:,.0f}（早期）"
    else:
        stage_info = f" | 预估持仓成本 ${total_cost:,.0f}"

    text = (
        f"{level_emoji} <b>聪明钱加仓提醒</b>\n"
        f"<b>{level}</b>\n\n"
        f"<b>这是第 {times} 次买入该代币（连续 {consecutive_buy_days} 天加仓）{net_info}</b>\n"
        f"<b>{stage_info}</b>\n\n"
        f"钱包: <code>{short_wallet}</code>\n"
        f"代币: {name} (${symbol})\n\n"
        f"本次买入: <b>${value_usd:,.2f}</b>\n"
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
        logger.info(f"通知成功: ${symbol} | {level} | 第{times}次 | 连续{consecutive_buy_days}天")
    except Exception as e:
        logger.error(f"发送通知失败: {e}")


def format_number(n: float) -> str:
    if n is None:
        return "0"
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
        "聪明钱跟踪 Bot（早期信号版 v2）\n\n"
        "/follow 钱包地址 - 开始跟踪\n"
        "/unfollow 钱包地址 - 取消跟踪\n"
        "/follows - 查看跟踪列表\n"
        "/followclear - 清空跟踪\n"
        "/list - 已通知代币总结\n"
        "/flow 钱包地址 代币mint - 查看净流入详情\n"
        "/status - 运行状态\n"
        "/help - 帮助\n\n"
        f"通知门槛: 单笔 ≥ ${MIN_BUY_USD:.0f}\n"
        f"高亮大单: ≥ ${HIGHLIGHT_BUY_USD:.0f}\n"
        f"早期判断: 持仓成本 ≤ ${EARLY_STAGE_MAX_COST:,.0f}"
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

    text = "<b>已通知代币列表</b>\n\n"
    for i, (mint, symbol, count, total_tokens, total_cost) in enumerate(rows, 1):
        symbol = symbol or "UNKNOWN"
        avg_cost = (total_cost / total_tokens) if total_tokens and total_tokens > 0 else 0
        cost_str = f"${total_cost:,.0f}" if total_cost else "0"
        text += (
            f"{i}. <b>${symbol}</b>\n"
            f"报警: {count}次 | 成本: {cost_str} | 均价: ${avg_cost:.8f}\n\n"
        )
    await message.answer(text, parse_mode="HTML")


@router.message(Command("flow"), F.from_user.id == ADMIN_ID)
async def cmd_flow(message: Message, command: CommandObject):
    if not command.args:
        await message.answer("用法: /flow 钱包地址 代币mint")
        return
    parts = command.args.strip().split()
    if len(parts) < 2:
        await message.answer("用法: /flow 钱包地址 代币mint")
        return

    wallet, mint = parts[0], parts[1]
    flow_1d = get_net_flow_stats(wallet, mint, 1)
    flow_3d = get_net_flow_stats(wallet, mint, 3)
    flow_7d = get_net_flow_stats(wallet, mint, 7)
    consecutive = get_consecutive_net_buy_days(wallet, mint)
    total_tokens, total_cost = get_cost_basis(wallet, mint)

    text = (
        f"📊 <b>净流入详情</b>\n\n"
        f"钱包: <code>{wallet[:6]}...{wallet[-4:]}</code>\n"
        f"代币: <code>{mint[:10]}...</code>\n\n"
        f"预估持仓成本: <b>${total_cost:,.0f}</b>\n\n"
        f"今日净流入: <b>${flow_1d['net_usd']:+,.2f}</b>\n"
        f"  买入 {flow_1d['buy_count']} 笔 / ${flow_1d['buy_usd']:,.0f}\n"
        f"  卖出 {flow_1d['sell_count']} 笔 / ${flow_1d['sell_usd']:,.0f}\n\n"
        f"3日净流入: <b>${flow_3d['net_usd']:+,.2f}</b>\n"
        f"7日净流入: <b>${flow_7d['net_usd']:+,.2f}</b>\n"
        f"连续净流入天数: <b>{consecutive} 天</b>"
    )
    await message.answer(text, parse_mode="HTML")


@router.message(Command("status"), F.from_user.id == ADMIN_ID)
async def cmd_status(message: Message):
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM tracked_wallets")
    wallet_count = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM notified")
    notify_count = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM buy_records")
    buy_count = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM sell_records")
    sell_count = c.fetchone()[0]
    conn.close()
    await message.answer(
        f"运行正常（早期信号版 v2）\n"
        f"跟踪地址数: {wallet_count}\n"
        f"累计通知次数: {notify_count}\n"
        f"买入记录: {buy_count} | 卖出记录: {sell_count}\n"
        f"单笔通知阈值: ≥ ${MIN_BUY_USD:.0f}\n"
        f"早期成本阈值: ≤ ${EARLY_STAGE_MAX_COST:,.0f}"
    )


async def polling_loop():
    while True:
        try:
            conn = get_db()
            c = conn.cursor()
            c.execute("SELECT address FROM tracked_wallets")
            wallets = [row[0] for row in c.fetchall()]
            conn.close()
            for wallet in wallets:
                await process_wallet(wallet)
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
    logger.info("Bot 启动中（早期信号版 v2）...")
    asyncio.create_task(polling_loop())
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
