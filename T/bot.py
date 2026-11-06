import asyncio
import logging
import os
import sqlite3
import time
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, List

import aiohttp
from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import Message
from dotenv import load_dotenv

load_dotenv()

# ==================== 配置区域 ====================
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))

# 公共 RPC（可换成其他免费节点）
RPC_URL = "https://api.mainnet-beta.solana.com"

POLL_INTERVAL = 120          # 建议 2 分钟，降低被限流概率
# ==================================================

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

bot = Bot(token=TELEGRAM_BOT_TOKEN)
dp = Dispatcher()
router = Router()
dp.include_router(router)

DB_PATH = "smart_money_bot_public.db"


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
    c.execute("""
        CREATE TABLE IF NOT EXISTS seen_signatures (
            signature TEXT PRIMARY KEY,
            wallet TEXT,
            checked_at INTEGER
        )
    """)
    conn.commit()
    conn.close()


def get_db():
    return sqlite3.connect(DB_PATH)


async def rpc_call(method: str, params: list) -> Any:
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": method,
        "params": params
    }
    async with aiohttp.ClientSession() as session:
        try:
            async with session.post(RPC_URL, json=payload, timeout=20) as resp:
                data = await resp.json()
                if "error" in data:
                    logger.error(f"RPC错误 {method}: {data['error']}")
                    return None
                return data.get("result")
        except Exception as e:
            logger.error(f"RPC请求异常 {method}: {e}")
            return None


async def get_recent_signatures(wallet: str, limit: int = 15) -> List[dict]:
    result = await rpc_call("getSignaturesForAddress", [wallet, {"limit": limit}])
    if not result:
        return []
    return result


async def get_parsed_transaction(signature: str) -> dict:
    result = await rpc_call("getTransaction", [
        signature,
        {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}
    ])
    return result or {}


async def get_token_balance(wallet: str, mint: str) -> float:
    result = await rpc_call("getTokenAccountsByOwner", [
        wallet,
        {"mint": mint},
        {"encoding": "jsonParsed"}
    ])
    if not result:
        return 0.0
    values = result.get("value", [])
    total = 0.0
    for acc in values:
        try:
            amount = acc["account"]["data"]["parsed"]["info"]["tokenAmount"]["uiAmount"]
            total += float(amount or 0)
        except:
            pass
    return total


async def get_token_supply(mint: str) -> float:
    result = await rpc_call("getTokenSupply", [mint])
    if not result:
        return 0.0
    try:
        return float(result.get("value", {}).get("uiAmount") or 0)
    except:
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


def extract_received_tokens(tx: dict, wallet: str) -> List[dict]:
    """从解析后的交易中提取钱包收到的代币（简化买入检测）"""
    received = []
    try:
        meta = tx.get("meta") or {}
        if meta.get("err"):
            return []

        pre_balances = meta.get("preTokenBalances") or []
        post_balances = meta.get("postTokenBalances") or []

        # 建立账户索引
        pre_map = {}
        for b in pre_balances:
            if b.get("owner") == wallet:
                mint = b.get("mint")
                amount = float(b.get("uiTokenAmount", {}).get("uiAmount") or 0)
                pre_map[mint] = amount

        for b in post_balances:
            if b.get("owner") == wallet:
                mint = b.get("mint")
                if mint == "So11111111111111111111111111111111111111112":
                    continue
                post_amount = float(b.get("uiTokenAmount", {}).get("uiAmount") or 0)
                pre_amount = pre_map.get(mint, 0)
                delta = post_amount - pre_amount
                if delta > 0:
                    received.append({
                        "mint": mint,
                        "amount": delta
                    })
    except Exception as e:
        logger.error(f"解析交易代币失败: {e}")
    return received


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

    signatures = await get_recent_signatures(wallet, limit=12)
    if not signatures:
        return

    new_sigs = []
    for sig_info in signatures:
        sig = sig_info.get("signature")
        block_time = sig_info.get("blockTime") or 0
        if block_time <= last_checked:
            continue

        # 检查是否已处理过
        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT 1 FROM seen_signatures WHERE signature=?", (sig,))
        if c.fetchone():
            conn.close()
            continue
        conn.close()
        new_sigs.append(sig_info)

    if not new_sigs:
        return

    logger.info(f"钱包 {wallet[:8]}... 发现 {len(new_sigs)} 笔新签名")

    newest_time = last_checked
    for sig_info in sorted(new_sigs, key=lambda x: x.get("blockTime") or 0):
        sig = sig_info.get("signature")
        block_time = sig_info.get("blockTime") or int(time.time())
        newest_time = max(newest_time, block_time)

        tx = await get_parsed_transaction(sig)
        if not tx:
            continue

        received = extract_received_tokens(tx, wallet)
        for item in received:
            await handle_buy(wallet, item["mint"], item["amount"], block_time, sig)

        # 标记已处理
        conn = get_db()
        c = conn.cursor()
        c.execute(
            "INSERT OR IGNORE INTO seen_signatures (signature, wallet, checked_at) VALUES (?, ?, ?)",
            (sig, wallet, int(time.time()))
        )
        conn.commit()
        conn.close()

        await asyncio.sleep(0.4)  # 避免请求过快

    # 更新 last_checked
    conn = get_db()
    c = conn.cursor()
    c.execute(
        "UPDATE tracked_wallets SET last_checked = ?, status = 'normal' WHERE address = ?",
        (newest_time, wallet)
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

    cost_this_buy = token_amount * price_usd
    c.execute("""
        INSERT INTO cost_basis (wallet, mint, total_tokens, total_cost_usd)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(wallet, mint) DO UPDATE SET
            total_tokens = total_tokens + excluded.total_tokens,
            total_cost_usd = total_cost_usd + excluded.total_cost_usd
    """, (wallet, mint, token_amount, cost_this_buy))

    c.execute(
        "INSERT INTO notified (wallet, mint, buy_date, notified_at, tx_signature) VALUES (?, ?, ?, ?, ?)",
        (wallet, mint, buy_date, int(time.time()), signature)
    )
    c.execute(
        "INSERT OR REPLACE INTO token_info (mint, symbol, name, launch_ts) VALUES (?, ?, ?, ?)",
        (mint, symbol, name, launch_ts or 0)
    )
    conn.commit()

    buy_dates = get_buy_dates(wallet, mint)
    times = len(buy_dates)
    consecutive = calculate_consecutive_days(buy_dates)

    balance = await get_token_balance(wallet, mint)
    supply = await get_token_supply(mint)
    holding_pct = (balance / supply * 100) if supply > 0 else 0.0

    c.execute("SELECT total_tokens, total_cost_usd FROM cost_basis WHERE wallet=? AND mint=?", (wallet, mint))
    row = c.fetchone()
    avg_cost = (row[1] / row[0]) if row and row[0] > 0 else price_usd
    conn.close()

    age_str = "未知"
    if launch_ts and launch_ts > 1600000000:
        age_str = format_age(buy_ts - launch_ts)

    short_wallet = wallet[:6] + "..." + wallet[-4:]

    text = (
        f"🔔 <b>聪明钱加仓提醒</b>\n\n"
        f"<b>这是第 {times} 次买入该代币（连续 {consecutive} 天加仓）</b>\n\n"
        f"钱包: <code>{short_wallet}</code>\n"
        f"代币: {name} (${symbol})\n\n"
        f"当前价格: ${price_usd:.8f}\n"
        f"平均成本: <b>${avg_cost:.8f}</b>\n"
        f"持仓占比: {holding_pct:.2f}%\n"
        f"流动性: {format_number(info.get('liquidity', 0))}\n"
        f"市值: {format_number(info.get('market_cap', 0))}\n"
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
        "聪明钱跟踪 Bot（公共RPC版）\n\n"
        "/follow 钱包地址 - 开始跟踪\n"
        "/unfollow 钱包地址 - 取消跟踪\n"
        "/follows - 查看跟踪列表\n"
        "/followclear - 清空跟踪\n"
        "/list - 已通知代币总结\n"
        "/status - 运行状态\n"
        "/help - 帮助\n\n"
        "注意：不使用 Helius，可能漏掉部分交易"
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
        info = await get_dexscreener_info(mint)
        mcap = format_number(info.get("market_cap", 0))
        text += f"{i}. ${symbol}\n市值: {mcap} | 报警: {count}次\n平均成本: ${avg_cost:.8f}\n\n"
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
    await message.answer(f"运行正常（公共RPC版）\n跟踪地址数: {wallet_count}\n累计通知次数: {notify_count}")


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
        logger.error("请检查环境变量 TELEGRAM_BOT_TOKEN / ADMIN_ID")
        return
    init_db()
    logger.info("Bot 启动中（公共RPC版本，不依赖Helius）...")
    asyncio.create_task(polling_loop())
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())