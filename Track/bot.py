import asyncio
import json
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

# ==================== 配置区域（请填写） ====================
TELEGRAM_BOT_TOKEN = os.getenv("9214707700:AAHvpLbqfyXsoL2buizCEn6FwSQA8ECpP-w", "在这里填写你的Bot Token")
ADMIN_ID = int(os.getenv("1085273843", "在这里填写你的Telegram数字ID"))  # 例如 123456789
HELIUS_API_KEY = os.getenv("5c1698fe-e1ed-495a-ac3f-11a28bf79145", "在这里填写你的Helius API Key")

# 轮询间隔（秒）
POLL_INTERVAL = 20

# ==========================================================

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

bot = Bot(token=TELEGRAM_BOT_TOKEN)
dp = Dispatcher()
router = Router()
dp.include_router(router)

DB_PATH = "smart_money_bot.db"

# ------------------ 数据库初始化 ------------------
def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    
    # 跟踪的钱包
    c.execute("""
        CREATE TABLE IF NOT EXISTS tracked_wallets (
            address TEXT PRIMARY KEY,
            added_at INTEGER,
            last_checked INTEGER DEFAULT 0,
            status TEXT DEFAULT 'normal'
        )
    """)
    
    # 已通知记录（去重关键）：wallet + mint + day_number
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
    
    # 成本记录（完整版平均成本）
    c.execute("""
        CREATE TABLE IF NOT EXISTS cost_basis (
            wallet TEXT,
            mint TEXT,
            total_tokens REAL DEFAULT 0,
            total_cost_usd REAL DEFAULT 0,
            PRIMARY KEY (wallet, mint)
        )
    """)
    
    # 代币信息缓存
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

# ------------------ Helius & DexScreener 工具函数 ------------------
async def helius_rpc(method: str, params: list) -> Optional[dict]:
    url = f"https://mainnet.helius-rpc.com/?api-key={HELIUS_API_KEY}"
    payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    async with aiohttp.ClientSession() as session:
        try:
            async with session.post(url, json=payload, timeout=15) as resp:
                data = await resp.json()
                return data.get("result")
        except Exception as e:
            logger.error(f"Helius RPC error: {e}")
            return None

async def get_signatures(address: str, limit: int = 20, before: str = None) -> List[dict]:
    params = [address, {"limit": limit}]
    if before:
        params[1]["before"] = before
    result = await helius_rpc("getSignaturesForAddress", params)
    return result or []

async def get_parsed_transaction(signature: str) -> Optional[dict]:
    # 使用 Helius enhanced 解析
    url = f"https://api.helius.xyz/v0/transactions/?api-key={HELIUS_API_KEY}"
    async with aiohttp.ClientSession() as session:
        try:
            async with session.post(url, json={"transactions": [signature]}, timeout=15) as resp:
                data = await resp.json()
                if data and isinstance(data, list) and len(data) > 0:
                    return data[0]
        except Exception as e:
            logger.error(f"Parsed tx error: {e}")
    return None

async def get_token_balance(wallet: str, mint: str) -> float:
    """获取钱包对某代币的持仓数量"""
    result = await helius_rpc("getTokenAccountsByOwner", [
        wallet,
        {"mint": mint},
        {"encoding": "jsonParsed"}
    ])
    if not result or not result.get("value"):
        return 0.0
    try:
        amount = result["value"][0]["account"]["data"]["parsed"]["info"]["tokenAmount"]["uiAmount"]
        return float(amount or 0)
    except Exception:
        return 0.0

async def get_token_supply(mint: str) -> float:
    result = await helius_rpc("getTokenSupply", [mint])
    if result and result.get("value"):
        return float(result["value"].get("uiAmount") or 0)
    return 0.0

async def get_dexscreener_info(mint: str) -> Dict[str, Any]:
    """获取市值、流动性、年龄、价格等"""
    url = f"https://api.dexscreener.com/tokens/v1/solana/{mint}"
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(url, timeout=10) as resp:
                data = await resp.json()
                pairs = data if isinstance(data, list) else data.get("pairs", [])
                if not pairs:
                    return {}
                # 取流动性最高的 pair
                pair = max(pairs, key=lambda x: (x.get("liquidity", {}) or {}).get("usd") or 0)
                
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
                    "volume_5m": (pair.get("volume") or {}).get("m5") or 0,
                }
        except Exception as e:
            logger.error(f"DexScreener error: {e}")
    return {}

# ------------------ 业务逻辑 ------------------
def is_admin(user_id: int) -> bool:
    return user_id == ADMIN_ID

async def process_new_buys(wallet: str):
    """检查钱包新买入并触发通知"""
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT last_checked FROM tracked_wallets WHERE address = ?", (wallet,))
    row = c.fetchone()
    last_checked = row[0] if row else 0
    
    sigs = await get_signatures(wallet, limit=15)
    if not sigs:
        conn.close()
        return
    
    # 按时间倒序，处理比 last_checked 新的
    new_sigs = [s for s in sigs if s.get("blockTime", 0) > last_checked]
    new_sigs.sort(key=lambda x: x.get("blockTime", 0))  # 从旧到新处理
    
    for sig_info in new_sigs:
        signature = sig_info["signature"]
        block_time = sig_info.get("blockTime", int(time.time()))
        
        tx = await get_parsed_transaction(signature)
        if not tx:
            continue
        
        # 简单判断买入：tokenTransfers 中有转入该钱包的非 SOL 代币
        token_transfers = tx.get("tokenTransfers", []) or []
        for transfer in token_transfers:
            to_user = transfer.get("toUserAccount")
            mint = transfer.get("mint")
            token_amount = float(transfer.get("tokenAmount") or 0)
            
            if to_user == wallet and mint and token_amount > 0 and mint != "So11111111111111111111111111111111111111112":
                # 这是一次买入
                await handle_buy(wallet, mint, token_amount, block_time, signature)
    
    # 更新 last_checked
    if sigs:
        newest = max(s.get("blockTime", 0) for s in sigs)
        c.execute("UPDATE tracked_wallets SET last_checked = ?, status = 'normal' WHERE address = ?", (newest, wallet))
        conn.commit()
    conn.close()

async def handle_buy(wallet: str, mint: str, token_amount: float, buy_ts: int, signature: str):
    """处理单次买入逻辑"""
    # 获取代币信息
    info = await get_dexscreener_info(mint)
    if not info:
        return
    
    symbol = info.get("symbol", "UNKNOWN")
    name = info.get("name", "Unknown")
    launch_ts = info.get("launch_ts")
    price_usd = info.get("price_usd", 0)
    
    if not launch_ts:
        # 尝试从缓存取
        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT launch_ts FROM token_info WHERE mint = ?", (mint,))
        row = c.fetchone()
        conn.close()
        launch_ts = row[0] if row else buy_ts  # 没有就用买入时间近似
    
    # 计算第几天
    day_number = max(1, (buy_ts - launch_ts) // 86400 + 1)
    
    # 去重检查
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT 1 FROM notified WHERE wallet=? AND mint=? AND day_number=?", (wallet, mint, day_number))
    if c.fetchone():
        conn.close()
        return  # 已经通知过这一天
    
    # 更新成本基础（完整版）
    cost_this_buy = token_amount * price_usd
    c.execute("""
        INSERT INTO cost_basis (wallet, mint, total_tokens, total_cost_usd)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(wallet, mint) DO UPDATE SET
            total_tokens = total_tokens + excluded.total_tokens,
            total_cost_usd = total_cost_usd + excluded.total_cost_usd
    """, (wallet, mint, token_amount, cost_this_buy))
    
    # 记录通知
    c.execute("INSERT INTO notified (wallet, mint, day_number, notified_at, tx_signature) VALUES (?, ?, ?, ?, ?)",
              (wallet, mint, day_number, int(time.time()), signature))
    
    # 缓存代币信息
    c.execute("INSERT OR REPLACE INTO token_info (mint, symbol, name, launch_ts) VALUES (?, ?, ?, ?)",
              (mint, symbol, name, launch_ts))
    conn.commit()
    
    # 计算当前持仓占比和平均成本
    balance = await get_token_balance(wallet, mint)
    supply = await get_token_supply(mint)
    holding_pct = (balance / supply * 100) if supply > 0 else 0.0
    
    c.execute("SELECT total_tokens, total_cost_usd FROM cost_basis WHERE wallet=? AND mint=?", (wallet, mint))
    row = c.fetchone()
    avg_cost = (row[1] / row[0]) if row and row[0] > 0 else price_usd
    
    conn.close()
    
    # 发送通知
    short_wallet = wallet[:6] + "..." + wallet[-4:]
    age_str = format_age(buy_ts - launch_ts)
    
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
        logger.info(f"通知发送成功: {symbol} 第{day_number}次")
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
        return f"{seconds//60}分钟"
    if seconds < 86400:
        return f"{seconds//3600}小时"
    return f"{seconds//86400}天"

# ------------------ 命令处理 ------------------
@router.message(Command("start"), F.from_user.id == ADMIN_ID)
@router.message(Command("help"), F.from_user.id == ADMIN_ID)
async def cmd_help(message: Message):
    text = (
        "聪明钱跟踪 Bot\n\n"
        "/follow 钱包地址 - 开始跟踪\n"
        "/unfollow 钱包地址 - 取消跟踪\n"
        "/follows - 查看跟踪列表\n"
        "/followclear - 清空跟踪\n"
        "/list - 已通知代币总结列表\n"
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
    c.execute("INSERT OR IGNORE INTO tracked_wallets (address, added_at, last_checked) VALUES (?, ?, ?)",
              (address, int(time.time()), int(time.time()) - 3600))
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
    """已通知代币总结列表"""
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
        
        # 实时获取市值和持仓（取第一个跟踪钱包的持仓作为参考）
        info = await get_dexscreener_info(mint)
        mcap = format_number(info.get("market_cap", 0))
        
        # 简单取一个跟踪钱包的持仓占比
        conn2 = get_db()
        c2 = conn2.cursor()
        c2.execute("SELECT address FROM tracked_wallets LIMIT 1")
        wrow = c2.fetchone()
        conn2.close()
        holding_pct = 0.0
        if wrow:
            bal = await get_token_balance(wrow[0], mint)
            sup = await get_token_supply(mint)
            holding_pct = (bal / sup * 100) if sup > 0 else 0
        
        text += (
            f"{i}. ${symbol}\n"
            f"市值: {mcap} | 报警次数: {count}次\n"
            f"持仓占比: {holding_pct:.2f}% | 平均成本: ${avg_cost:.8f}\n\n"
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

# ------------------ 后台轮询任务 ------------------
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
                await asyncio.sleep(1)  # 避免请求过快
        except Exception as e:
            logger.error(f"Polling error: {e}")
        
        await asyncio.sleep(POLL_INTERVAL)

# ------------------ 启动 ------------------
async def main():
    init_db()
    logger.info("Bot 启动中...")
    asyncio.create_task(polling_loop())
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())