import time
import requests
import json
import threading
from datetime import datetime, timezone, timedelta
from pathlib import Path

# ====================== 配置区 ======================
TELEGRAM_BOT_TOKEN = "8648405680:AAEVSH8reK5x1tXuyl_e4nfeA8-9Nz2gcPA"          # ← 填入你的 Bot Token
TELEGRAM_CHAT_ID   = "1085273843"          # ← 填入你的 Chat ID
HELIUS_API_KEY     = "5c1698fe-e1ed-495a-ac3f-11a28bf79145"          # ← 填入你的 Helius API Key

SCAN_INTERVAL = 90               # 横盘突破主循环间隔（秒）
WALLET_CHECK_INTERVAL = 120      # 钱包检查间隔（秒）= 2分钟

BREAKOUT_WATCH_FILE = "breakout_watch.json"
HISTORY_FILE = "price_history.json"
ALERT_COOLDOWN_FILE = "alert_cooldown.json"
FOLLOWED_WALLETS_FILE = "followed_wallets.json"
WALLET_BUY_RECORDS_FILE = "wallet_buy_records.json"

# ====================== 成功率优先参数 ======================
MIN_AGE_DAYS = 5
MAX_AGE_DAYS = 1000
HISTORY_KEEP_DAYS = 365

CONSOL_HOURS = 18
CONSOL_MAX_RANGE_PCT = 16
VOL_SPIKE_MULT = 2.8
MIN_VOL_H1 = 12000
PRICE_BREAK_PCT = 15

MIN_LIQ_BREAKOUT = 38000
MAX_FDV_LIQ_RATIO = 14
MIN_BUY_RATIO_M5 = 1.5

MIN_SCORE_BREAKOUT = 72
ALERT_COOLDOWN_HOURS = 4
# ====================================================

DEX_BASE = "https://api.dexscreener.com"
DEX_HEADERS = {
    "Accept": "application/json",
    "User-Agent": "Mozilla/5.0"
}

session = requests.Session()
session.headers.update(DEX_HEADERS)

last_update_id = 0

def load_json(path):
    if Path(path).exists():
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            print(f"读取 {path} 失败: {e}")
            return {}
    return {}

def save_json(path, data):
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"保存 {path} 失败: {e}")

def send_telegram(text, chat_id=None):
    target = chat_id or TELEGRAM_CHAT_ID
    if not target or not TELEGRAM_BOT_TOKEN:
        print("发送失败：TOKEN 或 CHAT_ID 为空")
        return False

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": target,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": False
    }
    try:
        r = session.post(url, json=payload, timeout=15)
        if r.status_code != 200:
            print(f"Telegram发送失败 status={r.status_code}: {r.text[:200]}")
            return False
        return True
    except Exception as e:
        print(f"Telegram发送异常: {e}")
        return False

def get_updates():
    global last_update_id
    if not TELEGRAM_BOT_TOKEN:
        return []
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"
    params = {"offset": last_update_id + 1, "timeout": 8}
    try:
        r = session.get(url, params=params, timeout=12)
        if r.status_code == 200:
            data = r.json()
            return data.get("result", [])
        elif r.status_code == 409:
            print("getUpdates 409 Conflict：可能有其他实例正在运行，或设置了Webhook")
        else:
            print(f"getUpdates 失败 status={r.status_code}")
    except Exception as e:
        print(f"getUpdates 异常: {e}")
    return []

# ====================== DexScreener ======================
def fetch_token_info(token_address: str):
    url = f"{DEX_BASE}/latest/dex/tokens/{token_address}"
    try:
        r = session.get(url, headers=DEX_HEADERS, timeout=12)
        if r.status_code != 200:
            return None
        data = r.json()
        pairs = data.get("pairs") or []
        if not pairs:
            return None

        sol_pairs = [p for p in pairs if p.get("chainId") == "solana"]
        if not sol_pairs:
            sol_pairs = pairs

        best = max(sol_pairs, key=lambda x: float(x.get("liquidity", {}).get("usd", 0) or 0))
        base = best.get("baseToken", {})
        created = best.get("pairCreatedAt")

        age_days = 999
        if created:
            ct = datetime.fromtimestamp(created / 1000, tz=timezone.utc)
            age_days = (datetime.now(timezone.utc) - ct).total_seconds() / 86400

        return {
            "name": f"{base.get('name', 'Unknown')} ({base.get('symbol', '')})",
            "symbol": base.get("symbol", ""),
            "price": float(best.get("priceUsd") or 0),
            "fdv": float(best.get("fdv") or 0),
            "liquidity": float(best.get("liquidity", {}).get("usd") or 0),
            "pool_address": best.get("pairAddress", ""),
            "url": best.get("url") or f"https://dexscreener.com/solana/{best.get('pairAddress', '')}",
            "volume_24h": float(best.get("volume", {}).get("h24") or 0),
            "price_change_24h": float(best.get("priceChange", {}).get("h24") or 0),
            "pairCreatedAt": created,
            "age_days": age_days,
            "token_address": base.get("address", token_address)
        }
    except Exception as e:
        print(f"fetch_token_info 失败: {e}")
    return None

def enrich_with_pair(token_address: str):
    url = f"{DEX_BASE}/latest/dex/tokens/{token_address}"
    try:
        r = session.get(url, headers=DEX_HEADERS, timeout=12)
        if r.status_code != 200:
            return None
        pairs = r.json().get("pairs") or []
        sol = [p for p in pairs if p.get("chainId") == "solana"]
        if not sol:
            return None
        best = max(sol, key=lambda x: float(x.get("liquidity", {}).get("usd", 0) or 0))
        return best
    except Exception as e:
        print(f"enrich_with_pair 失败: {e}")
        return None

def parse_pair(pair: dict):
    try:
        base = pair.get("baseToken", {})
        name = f"{base.get('name', 'Unknown')} ({base.get('symbol', '')})"
        token_address = base.get("address", "")
        pool_address = pair.get("pairAddress", "")

        liquidity = float(pair.get("liquidity", {}).get("usd") or 0)
        fdv = float(pair.get("fdv") or 0)
        price = float(pair.get("priceUsd") or 0)

        txns = pair.get("txns", {}) or {}
        m5 = txns.get("m5", {}) or {}
        buys = int(m5.get("buys") or 0)
        sells = int(m5.get("sells") or 0)

        vol = pair.get("volume", {}) or {}
        vol_m5 = float(vol.get("m5") or 0)
        vol_h1 = float(vol.get("h1") or 0)
        vol_h6 = float(vol.get("h6") or 0)
        vol_h24 = float(vol.get("h24") or 0)

        pc = pair.get("priceChange", {}) or {}
        pc_h1 = float(pc.get("h1") or 0)
        pc_h6 = float(pc.get("h6") or 0)
        pc_h24 = float(pc.get("h24") or 0)

        created = pair.get("pairCreatedAt")
        age_days = 999
        if created:
            ct = datetime.fromtimestamp(created / 1000, tz=timezone.utc)
            age_days = (datetime.now(timezone.utc) - ct).total_seconds() / 86400

        return {
            "name": name,
            "token_address": token_address,
            "pool_address": pool_address,
            "price": price,
            "liquidity": liquidity,
            "fdv": fdv,
            "vol_m5": vol_m5,
            "vol_h1": vol_h1,
            "vol_h6": vol_h6,
            "vol_h24": vol_h24,
            "buys_m5": buys,
            "sells_m5": sells,
            "price_change_h1": pc_h1,
            "price_change_h6": pc_h6,
            "price_change_h24": pc_h24,
            "age_days": age_days,
            "url": pair.get("url") or f"https://dexscreener.com/solana/{pool_address}",
            "pairCreatedAt": created
        }
    except Exception as e:
        print(f"parse_pair 失败: {e}")
        return None

# ====================== 历史快照 ======================
def update_price_history(breakout_watch: dict):
    history = load_json(HISTORY_FILE)
    now = datetime.now(timezone.utc)
    cutoff = (now - timedelta(days=HISTORY_KEEP_DAYS)).isoformat()

    for addr in list(breakout_watch.keys()):
        raw = enrich_with_pair(addr)
        if not raw:
            continue
        p = parse_pair(raw)
        if not p:
            continue

        if addr not in history:
            history[addr] = []

        entry = {
            "ts": now.isoformat(),
            "price": p["price"],
            "liq": p["liquidity"],
            "vol_m5": p["vol_m5"],
            "vol_h1": p["vol_h1"],
            "vol_h6": p["vol_h6"],
            "buys_m5": p["buys_m5"],
            "sells_m5": p["sells_m5"],
            "fdv": p["fdv"]
        }
        history[addr].append(entry)
        history[addr] = [h for h in history[addr] if h["ts"] > cutoff]

    save_json(HISTORY_FILE, history)
    return history

# ====================== 四套策略 ======================
def strategy_A_volume_price_break(hist: list, current: dict) -> dict:
    if len(hist) < 10:
        return {"score": 0, "reason": "历史不足"}

    prices = [h["price"] for h in hist if h.get("price", 0) > 0]
    vols = [h.get("vol_h1", 0) or h.get("vol_m5", 0) * 12 for h in hist]

    if len(prices) < 8 or not vols:
        return {"score": 0, "reason": "数据不足"}

    recent_high = max(prices[-15:]) if len(prices) >= 15 else max(prices)
    avg_vol = sum(vols[:-3]) / max(len(vols) - 3, 1)
    curr_vol = vols[-1]
    curr_price = current.get("price", prices[-1])

    vol_mult = curr_vol / avg_vol if avg_vol > 0 else 0
    price_break = (curr_price - recent_high) / recent_high * 100 if recent_high > 0 else 0

    score = 0
    reasons = []

    if vol_mult >= VOL_SPIKE_MULT:
        score += 36
        reasons.append(f"量放大{vol_mult:.1f}x")
    elif vol_mult >= 2.0:
        score += 16
        reasons.append(f"量增{vol_mult:.1f}x")

    if price_break >= PRICE_BREAK_PCT:
        score += 42
        reasons.append(f"突破高点+{price_break:.1f}%")
    elif price_break > 8:
        score += 18
        reasons.append(f"接近突破+{price_break:.1f}%")

    if current.get("liquidity", 0) >= MIN_LIQ_BREAKOUT:
        score += 12

    return {
        "score": min(score, 100),
        "reason": " | ".join(reasons) or "无强信号",
        "vol_mult": round(vol_mult, 2),
        "price_break": round(price_break, 1)
    }

def strategy_B_consolidation_then_spike(hist: list, current: dict) -> dict:
    if len(hist) < 12:
        return {"score": 0, "reason": "历史不足"}

    now = datetime.now(timezone.utc)
    consol_cutoff = (now - timedelta(hours=CONSOL_HOURS)).isoformat()
    consol = [h for h in hist if h["ts"] >= consol_cutoff]

    if len(consol) < 8:
        return {"score": 0, "reason": "横盘窗口数据少"}

    prices = [h["price"] for h in consol if h.get("price", 0) > 0]
    if len(prices) < 6:
        return {"score": 0, "reason": "价格数据不足"}

    high, low = max(prices), min(prices)
    range_pct = (high - low) / low * 100 if low > 0 else 999

    vols = [h.get("vol_h1", 0) or h.get("vol_m5", 0) * 12 for h in consol]
    avg_vol = sum(vols[:-2]) / max(len(vols) - 2, 1) if len(vols) > 2 else 0
    curr_vol = sum(vols[-2:]) / 2 if len(vols) >= 2 else (vols[-1] if vols else 0)
    vol_mult = curr_vol / avg_vol if avg_vol > 0 else 0

    score = 0
    reasons = []

    if range_pct <= CONSOL_MAX_RANGE_PCT:
        score += 35
        reasons.append(f"横盘{range_pct:.1f}%")
    else:
        return {"score": 0, "reason": f"波动过大({range_pct:.1f}%)"}

    if vol_mult >= VOL_SPIKE_MULT:
        score += 45
        reasons.append(f"量突增{vol_mult:.1f}x")
    elif vol_mult >= 2.0:
        score += 20
        reasons.append(f"量增加{vol_mult:.1f}x")

    last_price = prices[-1]
    if last_price >= high * 0.97:
        score += 12
        reasons.append("接近/突破横盘高点")

    if current.get("liquidity", 0) >= MIN_LIQ_BREAKOUT:
        score += 8

    return {
        "score": min(score, 100),
        "reason": " | ".join(reasons),
        "range_pct": round(range_pct, 1),
        "vol_mult": round(vol_mult, 2)
    }

def strategy_C_momentum_buys(current: dict) -> dict:
    buys = current.get("buys_m5", 0)
    sells = current.get("sells_m5", 0)
    br = buys / max(sells, 1)
    vol_m5 = current.get("vol_m5", 0)
    pc_h1 = current.get("price_change_h1", 0) or 0

    score = 0
    reasons = []

    if br >= 2.0 and buys >= 20:
        score += 32
        reasons.append(f"强买压{br:.1f}")
    elif br >= MIN_BUY_RATIO_M5:
        score += 18
        reasons.append(f"买压{br:.1f}")

    if vol_m5 >= 15000:
        score += 18
        reasons.append(f"5m量${vol_m5:,.0f}")
    elif vol_m5 >= 8000:
        score += 10

    if pc_h1 >= 8:
        score += 22
        reasons.append(f"1h涨{pc_h1:.1f}%")
    elif pc_h1 >= 4:
        score += 10

    if current.get("liquidity", 0) >= MIN_LIQ_BREAKOUT:
        score += 8

    return {
        "score": min(score, 100),
        "reason": " | ".join(reasons) or "无强信号"
    }

def strategy_D_combined(hist, current) -> dict:
    a = strategy_A_volume_price_break(hist, current)
    b = strategy_B_consolidation_then_spike(hist, current)
    c = strategy_C_momentum_buys(current)

    score = int(a["score"] * 0.25 + b["score"] * 0.50 + c["score"] * 0.25)

    return {
        "score": score,
        "reason": f"A:{a['score']}  B:{b['score']}  C:{c['score']}",
        "details": {"A": a, "B": b, "C": c}
    }

def hard_quality_filter(current: dict) -> bool:
    if current.get("liquidity", 0) < MIN_LIQ_BREAKOUT:
        return False
    if current.get("age_days", 999) < MIN_AGE_DAYS or current.get("age_days", 0) > MAX_AGE_DAYS:
        return False
    if current.get("fdv", 0) > 0 and current.get("liquidity", 0) > 0:
        if current["fdv"] / current["liquidity"] > MAX_FDV_LIQ_RATIO:
            return False
    if current.get("vol_h1", 0) < MIN_VOL_H1 and current.get("vol_m5", 0) * 12 < MIN_VOL_H1:
        return False
    buys = current.get("buys_m5", 0)
    sells = current.get("sells_m5", 0)
    if buys / max(sells, 1) < MIN_BUY_RATIO_M5 and buys < 15:
        return False
    if current.get("price_change_h6", 0) > 50:
        return False
    return True

# ====================== 聪明钱钱包跟踪（Helius）======================
def get_wallet_transactions(wallet: str, limit: int = 20):
    """获取钱包最近交易（Helius Enhanced Transactions）"""
    if not HELIUS_API_KEY:
        return []
    url = f"https://api.helius.xyz/v0/addresses/{wallet}/transactions"
    params = {
        "api-key": HELIUS_API_KEY,
        "limit": limit
    }
    try:
        r = session.get(url, params=params, timeout=15)
        if r.status_code == 200:
            return r.json()
        else:
            print(f"Helius交易获取失败 status={r.status_code}")
    except Exception as e:
        print(f"Helius交易异常: {e}")
    return []

def get_token_balance_and_supply(wallet: str, token_mint: str):
    """获取钱包持仓数量和代币总供应量（用于计算持仓占比）"""
    if not HELIUS_API_KEY:
        return None, None

    # 使用 Helius RPC
    rpc_url = f"https://mainnet.helius-rpc.com/?api-key={HELIUS_API_KEY}"

    # 1. 获取钱包该代币余额
    balance_payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "getTokenAccountsByOwner",
        "params": [
            wallet,
            {"mint": token_mint},
            {"encoding": "jsonParsed"}
        ]
    }
    try:
        r = session.post(rpc_url, json=balance_payload, timeout=12)
        data = r.json()
        accounts = data.get("result", {}).get("value", [])
        balance = 0
        if accounts:
            for acc in accounts:
                info = acc.get("account", {}).get("data", {}).get("parsed", {}).get("info", {})
                token_amount = info.get("tokenAmount", {})
                balance += float(token_amount.get("uiAmount") or 0)
    except Exception as e:
        print(f"获取余额失败: {e}")
        balance = 0

    # 2. 获取代币总供应量
    supply_payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "getTokenSupply",
        "params": [token_mint]
    }
    try:
        r = session.post(rpc_url, json=supply_payload, timeout=12)
        data = r.json()
        supply_info = data.get("result", {}).get("value", {})
        supply = float(supply_info.get("uiAmount") or 0)
    except Exception as e:
        print(f"获取供应量失败: {e}")
        supply = 0

    return balance, supply

def extract_buy_tokens(tx: dict, wallet: str):
    """从交易中提取该钱包买入的代币（简化版，优先识别SWAP）"""
    buys = []
    try:
        # Helius 增强交易格式
        if tx.get("type") == "SWAP":
            # 检查 tokenTransfers
            for transfer in tx.get("tokenTransfers", []):
                if transfer.get("toUserAccount") == wallet:
                    mint = transfer.get("mint")
                    if mint and mint != "So11111111111111111111111111111111111111112":  # 排除SOL
                        buys.append({
                            "mint": mint,
                            "amount": float(transfer.get("tokenAmount") or 0),
                            "timestamp": tx.get("timestamp")
                        })
        # 也可以从 accountData 等进一步解析，这里先用增强字段
    except Exception as e:
        print(f"解析交易失败: {e}")
    return buys

def check_wallet_buys():
    """检查所有跟踪钱包的买入行为"""
    if not HELIUS_API_KEY:
        return

    followed = load_json(FOLLOWED_WALLETS_FILE)
    if not followed:
        return

    records = load_json(WALLET_BUY_RECORDS_FILE)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    for wallet, meta in list(followed.items()):
        try:
            txs = get_wallet_transactions(wallet, limit=15)
            if not txs:
                continue

            for tx in txs:
                buy_list = extract_buy_tokens(tx, wallet)
                for buy in buy_list:
                    mint = buy["mint"]
                    key = f"{wallet}|{mint}"

                    # 检查今天是否已经通知过
                    day_list = records.get(key, [])
                    if today in day_list:
                        continue

                    # 计算是第几次（按不同日期）
                    times = len(day_list) + 1

                    # 获取代币信息
                    token_info = fetch_token_info(mint)
                    name = token_info["name"] if token_info else mint[:8] + "..."

                    # 获取持仓占比
                    balance, supply = get_token_balance_and_supply(wallet, mint)
                    hold_pct = 0
                    if balance and supply and supply > 0:
                        hold_pct = (balance / supply) * 100

                    # 发送通知
                    short_wallet = wallet[:6] + "..." + wallet[-4:]
                    msg = (
                        f"<b>🔔 聪明钱买入提醒</b>\n\n"
                        f"钱包：<code>{short_wallet}</code>\n"
                        f"代币：<b>{name}</b>\n"
                        f"这是第 <b>{times}</b> 次买入该代币\n\n"
                        f"当前持仓占比：<b>{hold_pct:.2f}%</b>\n"
                        f"时间：{datetime.now().strftime('%Y-%m-%d %H:%M')}\n\n"
                        f"<code>{mint}</code>"
                    )
                    send_telegram(msg)

                    # 记录今天已通知
                    day_list.append(today)
                    records[key] = day_list
                    save_json(WALLET_BUY_RECORDS_FILE, records)

                    print(f"已通知聪明钱买入: {name} | 第{times}次 | 持仓{hold_pct:.2f}%")

        except Exception as e:
            print(f"检查钱包 {wallet[:8]} 异常: {e}")

# ====================== 命令处理 ======================
def handle_command(text: str, chat_id: str):
    text = text.strip()
    breakout_watch = load_json(BREAKOUT_WATCH_FILE)
    followed = load_json(FOLLOWED_WALLETS_FILE)

    if text.startswith("/start") or text.startswith("/help"):
        msg = (
            "<b>🤖 横盘突破 + 聪明钱监控机器人</b>\n\n"
            "【横盘突破监控】\n"
            "专注上线 5~1000 天代币\n"
            "捕捉横盘后放量突破（突破高点≥15%）\n\n"
            "<b>横盘命令：</b>\n"
            "/bwatch 合约地址 - 加入监控\n"
            "/bunwatch 合约地址 - 删除监控\n"
            "/blist - 查看监控列表\n"
            "/bclear - 清空全部\n"
            "/price 合约地址 - 查看数据\n\n"
            "【聪明钱跟踪】\n"
            "/follow 钱包地址 - 开始跟踪\n"
            "/unfollow 钱包地址 - 取消跟踪\n"
            "/follows - 查看跟踪列表\n"
            "/followclear - 清空跟踪\n\n"
            "/status - 运行状态\n"
            "/help"
        )
        send_telegram(msg, chat_id)

    elif text.startswith("/status"):
        history = load_json(HISTORY_FILE)
        cooldown = load_json(ALERT_COOLDOWN_FILE)
        msg = (
            f"<b>📊 运行状态</b>\n\n"
            f"横盘监控: {len(breakout_watch)}\n"
            f"历史币种: {len(history)}\n"
            f"冷却中: {len(cooldown)}\n"
            f"聪明钱跟踪: {len(followed)}\n"
            f"年龄范围: {MIN_AGE_DAYS}~{MAX_AGE_DAYS}天\n"
            f"最低流动性: ${MIN_LIQ_BREAKOUT:,}\n"
            f"突破要求: ≥{PRICE_BREAK_PCT}%\n"
            f"综合分阈值: {MIN_SCORE_BREAKOUT}\n"
            f"时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        )
        send_telegram(msg, chat_id)

    # ---------- 横盘突破命令 ----------
    elif text.startswith("/bwatch"):
        parts = text.split()
        if len(parts) < 2:
            send_telegram("格式：\n<code>/bwatch 合约地址</code>", chat_id)
            return
        addr = parts[1].strip()
        info = fetch_token_info(addr)
        if not info:
            send_telegram("❌ 未找到该代币", chat_id)
            return

        age = info.get("age_days", 999)
        breakout_watch[addr] = {
            "name": info["name"],
            "added_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "pool_address": info.get("pool_address", ""),
            "age_days_at_add": round(age, 1)
        }
        save_json(BREAKOUT_WATCH_FILE, breakout_watch)

        tip = ""
        if age < MIN_AGE_DAYS:
            tip = f"\n⚠️ 当前年龄仅 {age:.1f} 天，未达最低要求。"
        elif age > MAX_AGE_DAYS:
            tip = f"\n⚠️ 当前年龄 {age:.1f} 天，已超过最大关注范围。"
        elif info.get("liquidity", 0) < MIN_LIQ_BREAKOUT:
            tip = f"\n⚠️ 流动性 ${info['liquidity']:,.0f} 低于推荐门槛。"

        send_telegram(
            f"✅ 已加入监控\n\n"
            f"<b>{info['name']}</b>\n"
            f"年龄: <b>{age:.1f} 天</b>\n"
            f"流动性: ${info['liquidity']:,.0f}\n"
            f"市值: ${info['fdv']:,.0f}\n"
            f"价格: ${info['price']:.8f}{tip}",
            chat_id
        )

    elif text.startswith("/bunwatch"):
        parts = text.split()
        if len(parts) < 2:
            send_telegram("格式：\n<code>/bunwatch 合约地址</code>", chat_id)
            return
        addr = parts[1].strip()
        if addr in breakout_watch:
            name = breakout_watch[addr].get("name", addr)
            del breakout_watch[addr]
            save_json(BREAKOUT_WATCH_FILE, breakout_watch)
            send_telegram(f"✅ 已删除：{name}", chat_id)
        else:
            send_telegram("不在监控列表中", chat_id)

    elif text.startswith("/blist"):
        if not breakout_watch:
            send_telegram("当前没有监控任何代币", chat_id)
            return
        msg = "<b>📋 当前监控列表</b>\n\n"
        for i, (addr, info) in enumerate(breakout_watch.items(), 1):
            msg += f"{i}. <b>{info.get('name')}</b>\n<code>{addr}</code>\n添加: {info.get('added_at')}\n\n"
        send_telegram(msg, chat_id)

    elif text.startswith("/bclear"):
        save_json(BREAKOUT_WATCH_FILE, {})
        send_telegram("✅ 已清空所有监控", chat_id)

    elif text.startswith("/price"):
        parts = text.split()
        if len(parts) < 2:
            send_telegram("格式：\n<code>/price 合约地址</code>", chat_id)
            return
        addr = parts[1].strip()
        info = fetch_token_info(addr)
        if info:
            change = info.get("price_change_24h", 0)
            change_str = f"+{change:.1f}%" if change >= 0 else f"{change:.1f}%"
            send_telegram(
                f"<b>{info['name']}</b>\n\n"
                f"价格: <b>${info['price']:.8f}</b>\n"
                f"市值: <b>${info['fdv']:,.0f}</b>\n"
                f"流动性: ${info['liquidity']:,.0f}\n"
                f"24h涨跌: {change_str}\n"
                f"年龄: <b>{info.get('age_days', 0):.1f} 天</b>\n\n"
                f"<a href='{info['url']}'>DexScreener</a>",
                chat_id
            )
        else:
            send_telegram("❌ 获取失败", chat_id)

    # ---------- 聪明钱跟踪命令 ----------
    elif text.startswith("/follow"):
        parts = text.split()
        if len(parts) < 2:
            send_telegram("格式：\n<code>/follow 钱包地址</code>", chat_id)
            return
        wallet = parts[1].strip()
        if len(wallet) < 32:
            send_telegram("❌ 钱包地址格式不正确", chat_id)
            return

        followed[wallet] = {
            "added_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "note": ""
        }
        save_json(FOLLOWED_WALLETS_FILE, followed)
        short = wallet[:6] + "..." + wallet[-4:]
        send_telegram(f"✅ 已开始跟踪钱包\n<code>{short}</code>\n\n之后该钱包买入代币会通知你（每天每个币只通知一次）", chat_id)

    elif text.startswith("/unfollow"):
        parts = text.split()
        if len(parts) < 2:
            send_telegram("格式：\n<code>/unfollow 钱包地址</code>", chat_id)
            return
        wallet = parts[1].strip()
        if wallet in followed:
            del followed[wallet]
            save_json(FOLLOWED_WALLETS_FILE, followed)
            send_telegram("✅ 已取消跟踪", chat_id)
        else:
            send_telegram("该钱包不在跟踪列表中", chat_id)

    elif text.startswith("/follows"):
        if not followed:
            send_telegram("当前没有跟踪任何钱包", chat_id)
            return
        msg = "<b>👁 当前跟踪的钱包</b>\n\n"
        for i, (w, info) in enumerate(followed.items(), 1):
            short = w[:6] + "..." + w[-4:]
            msg += f"{i}. <code>{short}</code>\n添加: {info.get('added_at')}\n\n"
        send_telegram(msg, chat_id)

    elif text.startswith("/followclear"):
        save_json(FOLLOWED_WALLETS_FILE, {})
        send_telegram("✅ 已清空所有跟踪钱包", chat_id)

    else:
        send_telegram("未知命令，请输入 /help", chat_id)

def process_telegram_commands():
    global last_update_id
    updates = get_updates()
    for update in updates:
        last_update_id = update["update_id"]
        message = update.get("message") or update.get("edited_message") or {}
        chat = message.get("chat", {})
        chat_id = str(chat.get("id", ""))
        text = message.get("text") or ""

        if not text.startswith("/"):
            continue

        print(f"收到命令: {text}  from chat_id={chat_id}")

        try:
            handle_command(text, chat_id)
        except Exception as e:
            print(f"处理命令异常: {e}")
            send_telegram(f"处理命令时出错: {e}", chat_id)

def command_loop():
    while True:
        try:
            process_telegram_commands()
            time.sleep(2)
        except Exception as e:
            print(f"命令线程异常: {e}")
            time.sleep(5)

def wallet_loop():
    """独立线程检查聪明钱买入"""
    while True:
        try:
            check_wallet_buys()
            time.sleep(WALLET_CHECK_INTERVAL)
        except Exception as e:
            print(f"钱包检查线程异常: {e}")
            time.sleep(30)

# ====================== 主循环 ======================
def main_loop():
    print("=" * 55)
    print("横盘突破 + 聪明钱监控机器人启动")
    print(f"TOKEN 是否设置: {'是' if TELEGRAM_BOT_TOKEN else '否'}")
    print(f"CHAT_ID 是否设置: {'是' if TELEGRAM_CHAT_ID else '否'}")
    print(f"HELIUS 是否设置: {'是' if HELIUS_API_KEY else '否'}")
    print("=" * 55)

    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("警告：TELEGRAM_BOT_TOKEN 或 TELEGRAM_CHAT_ID 未设置！")
    if not HELIUS_API_KEY:
        print("警告：HELIUS_API_KEY 未设置，聪明钱跟踪功能将无法使用！")

    # 启动命令处理线程
    t1 = threading.Thread(target=command_loop, daemon=True)
    t1.start()

    # 启动钱包检查线程
    t2 = threading.Thread(target=wallet_loop, daemon=True)
    t2.start()

    send_telegram(
        "🤖 机器人已启动（横盘突破 + 聪明钱跟踪）\n"
        f"年龄范围: {MIN_AGE_DAYS}~{MAX_AGE_DAYS}天\n"
        f"最低流动性: ${MIN_LIQ_BREAKOUT:,}\n"
        f"横盘高点突破要求: ≥{PRICE_BREAK_PCT}%\n"
        "发送 /help 查看全部命令"
    )

    while True:
        try:
            breakout_watch = load_json(BREAKOUT_WATCH_FILE)
            if not breakout_watch:
                time.sleep(SCAN_INTERVAL)
                continue

            history = update_price_history(breakout_watch)
            cooldown = load_json(ALERT_COOLDOWN_FILE)
            now = datetime.now(timezone.utc)

            for addr, meta in list(breakout_watch.items()):
                raw = enrich_with_pair(addr)
                if not raw:
                    continue
                current = parse_pair(raw)
                if not current:
                    continue

                if not hard_quality_filter(current):
                    continue

                hist = history.get(addr, [])
                results = {
                    "A": strategy_A_volume_price_break(hist, current),
                    "B": strategy_B_consolidation_then_spike(hist, current),
                    "C": strategy_C_momentum_buys(current),
                    "D": strategy_D_combined(hist, current)
                }

                best_score = max(r["score"] for r in results.values())
                if best_score < MIN_SCORE_BREAKOUT:
                    continue

                last_alert = cooldown.get(addr)
                if last_alert:
                    last_dt = datetime.fromisoformat(last_alert)
                    if (now - last_dt).total_seconds() < ALERT_COOLDOWN_HOURS * 3600:
                        continue

                a, b, c, d = results["A"], results["B"], results["C"], results["D"]
                msg = (
                    f"<b>🚀 横盘突破信号 | 综合 {best_score}</b>\n\n"
                    f"<b>{current['name']}</b>\n"
                    f"年龄: {current['age_days']:.1f} 天\n"
                    f"流动性: <b>${current['liquidity']:,.0f}</b>\n"
                    f"FDV: ${current['fdv']:,.0f}\n"
                    f"价格: ${current['price']:.8f}\n\n"
                    f"<b>策略得分：</b>\n"
                    f"A (量价突破): {a['score']} → {a['reason']}\n"
                    f"B (横盘后放量): {b['score']} → {b['reason']}\n"
                    f"C (买压动量): {c['score']} → {c['reason']}\n"
                    f"D (综合): {d['score']}\n\n"
                    f"<a href='{current['url']}'>DexScreener</a>\n"
                    f"<code>{addr}</code>"
                )
                send_telegram(msg)

                cooldown[addr] = now.isoformat()
                save_json(ALERT_COOLDOWN_FILE, cooldown)

            for addr in list(cooldown.keys()):
                last_dt = datetime.fromisoformat(cooldown[addr])
                if (now - last_dt).total_seconds() > ALERT_COOLDOWN_HOURS * 3600 * 2:
                    del cooldown[addr]
            save_json(ALERT_COOLDOWN_FILE, cooldown)

            time.sleep(SCAN_INTERVAL)

        except Exception as e:
            print(f"主循环异常: {e}")
            time.sleep(30)

if __name__ == "__main__":
    main_loop()
