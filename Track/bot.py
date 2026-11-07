import asyncio
import logging
import os
import sqlite3
import time
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, List, Optional

import aiohttp
from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import Message
from dotenv import load_dotenv

load_dotenv()

# ==================== 配置区域 ====================
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))

# GMGN 相关（如果有官方 API Key 可填，没有则走公开接口）
GMGN_API_KEY = os.getenv("GMGN_API_KEY", "")

POLL_INTERVAL = 120              # 2分钟检查一次
MIN_BUY_USD = 300.0              # 单笔低于此金额不通知
HIGHLIGHT_BUY_USD = 500.0        # 大单高亮
EARLY_STAGE_MAX_COST = 2000      # 早期建仓判断阈值
# ==================================================

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

bot = Bot(token=TELEGRAM_BOT_TOKEN)
dp = Dispatcher()
router = Router()
dp.include_router(router)

DB_PATH = "smart_money_bot_gmgn.db"


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
    return sqlite3.connect(DB_PATH)


# ==================== GMGN 数据获取 ====================

async def gmgn_get(url: str, params: dict = None) -> dict:
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "application/json",
    }
    if GMGN_API_KEY:
        headers["Authorization"] = f"Bearer {GMGN_API_KEY}"
        headers["x-route-key"] = GMGN_API_KEY

    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(url, params=params, headers=headers, timeout=20) as resp:
                if resp.status != 200:
                    logger.warning(f"GMGN请求状态码: {resp.status} | {url}")
                    return {}
                data = await resp.json()
                return data
        except Exception as e:
            logger.error(f"GMGN请求异常: {e}")
            return {}


async def get_wallet_activity(wallet: str, limit: int = 50) -> List[dict]:
    """获取钱包最近交易活动（GMGN）"""
    # 优先使用公开接口
    url = f"https://gmgn.ai/vas/api/v1/wallet_activity/sol"
    params = {
        "wallet": wallet,
        "limit": limit,
        "type": "buy"
    }
    data = await gmgn_get(url, params)

    if not data:
        # 备用接口
        url2 = f"https://gmgn.ai/defi/quotation/v1/wallet/{wallet}/activity"
        data = await gmgn_get(url2, {"limit": limit})

    # 兼容不同返回结构
    if isinstance(data, dict):
        activities = data.get("data", {}).get("activities") or data.get("data") or data.get("activities") or []
        if isinstance(activities, dict):
            activities = activities.get("list") or activities.get("activities") or []
        return activities if isinstance(activities, list) else []
    return []


async def get_wallet_holdings(wallet: str) -> List[dict]:
    """获取钱包当前持仓（用于辅助判断成本）"""
    url = f"https://gmgn.ai/pf/api/v1/wallet/sol/{wallet}/holdings"
    params = {"limit": 50}
    data = await gmgn_get(url, params)

    if not data:
        url2 = f"https://gmgn.ai/defi/quotation/v1/smartmoney/sol/walletNew/{wallet}"
        data = await gmgn_get(url2)

    if isinstance(data, dict):
        holdings = data.get("data", {}).get("holdings") or data.get("data", {}).get("tokens") or data.get("holdings") or []
        return holdings if isinstance(holdings, list) else []
    return []


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


# ==================== 数据库辅助 ====================

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
        SELECT COALESCE(SUM(buy_usd), 0), COALESCE(SUM(sell_usd), 0),
               COALESCE(SUM(net_usd), 0), COALESCE(SUM(buy_count), 0), COALESCE(SUM(sell_count), 0)
        FROM daily_flow WHERE wallet=? AND mint=? AND flow_date >= ?
    """, (wallet, mint, since_date))
    row = c.fetchone()
    conn.close()
    buy_usd, sell_usd, net_usd, buy_count, sell_count = row or (0, 0, 0, 0, 0)
    return {"buy_usd": buy_usd, "sell_usd": sell_usd, "net_usd": net_usd,
            "buy_count": buy_count, "sell_count": sell_count, "days": days}


def get_consecutive_net_buy_days(wallet: str, mint: str) -> int:
    conn = get_db()
    c = conn.cursor()
    c.execute("""
        SELECT flow_date, net_usd FROM daily_flow
        WHERE wallet=? AND mint=? ORDER BY flow_date DESC LIMIT 30
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
        WHERE wallet=? AND mint=? ORDER BY buy_date DESC
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


# ==================== 核心处理 ====================

async def process_wallet(wallet: str):
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT last_checked FROM tracked_wallets WHERE address = ?", (wallet,))
    row = c.fetchone()
    last_checked = row[0] if row else 0
    conn.close()

    activities = await get_wallet_activity(wallet, limit=50)
    if not activities:
        logger.info(f"钱包 {wallet[:8]}... 暂无新活动")
        return

    newest_ts = last_checked

    for act in activities:
        try:
            # 兼容多种字段名
            side = (act.get("event_type") or act.get("side") or act.get("type") or "").lower()
            if side not in ("buy", "sell"):
                continue

            mint = (act.get("token_address") or act.get("token", {}).get("address") or
                    act.get("mint") or act.get("token_address") or "")
            if not mint:
                continue

            # 时间
            ts = act.get("timestamp") or act.get("block_timestamp") or act.get("time") or 0
            if isinstance(ts, str):
                try:
                    ts = int(datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp())
                except Exception:
                    ts = 0
            ts = int(ts)
            if ts > 1e12:  # 毫秒转秒
                ts = ts // 1000

            if ts <= last_checked:
                continue

            # 金额
            value_usd = float(act.get("cost_usd") or act.get("amount_usd") or act.get("value_usd") or
                              act.get("quote_amount") or 0)
            token_amount = float(act.get("token_amount") or act.get("amount") or
                                 act.get("token", {}).get("amount") or 0)
            price_usd = float(act.get("price_usd") or act.get("token", {}).get("price_usd") or 0)
            symbol = (act.get("token", {}).get("symbol") or act.get("symbol") or "UNKNOWN")
            tx_sig = act.get("tx_hash") or act.get("signature") or act.get("transaction_hash") or ""

            newest_ts = max(newest_ts, ts)

            await handle_trade(
                wallet=wallet,
                mint=mint,
                side=side,
                token_amount=token_amount,
                trade_ts=ts,
                price_usd=price_usd,
                value_usd=value_usd,
                symbol=symbol,
                tx_signature=tx_sig
            )
        except Exception as e:
            logger.error(f"处理单条活动失败: {e}")

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

        update_daily_flow(wallet, mint, trade_date, buy_usd=0, sell_usd=value_usd)

    c.execute(
        "INSERT OR IGNORE INTO token_info (mint, symbol, name, launch_ts) VALUES (?, ?, ?, 0)",
        (mint, symbol, symbol)
    )
    conn.commit()
    conn.close()

    # 只有买入且金额达标才通知
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
    pnl_text = f"+${pnl_usd:,.2f} (+{pnl_pct:.2f}%)" if pnl_usd >= 0 else f"-${abs(pnl_usd):,.2f} ({pnl_pct:.2f}%)"

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

    # 信号等级
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

    stage_info = f" | 预估持仓成本 ${total_cost:,.0f}" + ("（早期）" if is_early_stage else "")

    text = (
        f"{level_emoji} <b>聪明钱加仓提醒</b>（GMGN）\n"
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


# ==================== 命令 ====================

@router.message(Command("start"), F.from_user.id == ADMIN_ID)
@router.message(Command("help"), F.from_user.id == ADMIN_ID)
async def cmd_help(message: Message):
    await message.answer(
        "聪明钱跟踪 Bot（GMGN 早期信号版）\n\n"
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
        f"运行正常（GMGN 早期信号版）\n"
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
                await asyncio.sleep(3)
        except Exception as e:
            logger.error(f"轮询错误: {e}")
        await asyncio.sleep(POLL_INTERVAL)


async def main():
    if not TELEGRAM_BOT_TOKEN or ADMIN_ID == 0:
        logger.error("请检查 TELEGRAM_BOT_TOKEN / ADMIN_ID")
        return

    init_db()
    logger.info("Bot 启动中（GMGN 早期信号版）...")
    asyncio.create_task(polling_loop())
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
