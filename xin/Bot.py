import time
import requests
import json
import threading
from datetime import datetime, timezone, timedelta
from pathlib import Path

# ====================== 配置区 ======================
TELEGRAM_BOT_TOKEN = "8229035839:AAFfen2rHkpQB6hh1LW9cP8oYkQfoefcK_k"
TELEGRAM_CHAT_ID = "1085273843"

SCAN_INTERVAL = 90
SEEN_FILE = "seen_pools.json"
OBSERVE_FILE = "observe_pools.json"
WATCH_FILE = "watch_list.json"          # 个人监控列表

# 严格过滤参数
MIN_LIQUIDITY_USD = 25000
MIN_AGE_MINUTES = 8
MAX_AGE_MINUTES = 180
MIN_BUY_RATIO = 1.25
MAX_FDV_LIQ_RATIO = 18
MIN_BUYERS_M5 = 15
MIN_VOLUME_M5 = 8000
MIN_SCORE_TO_PUSH = 68
RECHECK_HOURS = [12, 24]
# ====================================================

BASE_URL = "https://api.geckoterminal.com/api/v2"
HEADERS = {
    "Accept": "application/json;version=20230203",
    "User-Agent": "Mozilla/5.0"
}
session = requests.Session()
session.headers.update(HEADERS)

# 全局变量
last_update_id = 0

def load_json(path):
    if Path(path).exists():
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except:
            return {}
    return {}

def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def send_telegram(text, chat_id=None):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": chat_id or TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": False
    }
    try:
        session.post(url, json=payload, timeout=15)
    except Exception as e:
        print(f"Telegram发送失败: {e}")

def get_updates():
    global last_update_id
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"
    params = {"offset": last_update_id + 1, "timeout": 10}
    try:
        r = session.get(url, params=params, timeout=15)
        if r.status_code == 200:
            data = r.json()
            return data.get("result", [])
    except:
        pass
    return []

def fetch_token_info(token_address: str):
    """通过代币地址获取价格和市值等信息"""
    # 先尝试搜索池子
    url = f"{BASE_URL}/networks/solana/tokens/{token_address}/pools"
    try:
        r = session.get(url, timeout=12)
        if r.status_code == 200:
            pools = r.json().get("data", [])
            if pools:
                # 取流动性最高的池
                best = max(pools, key=lambda x: float(x.get("attributes", {}).get("reserve_in_usd", 0) or 0))
                attr = best.get("attributes", {})
                return {
                    "name": attr.get("name", "Unknown"),
                    "price": float(attr.get("base_token_price_usd", 0) or 0),
                    "fdv": float(attr.get("fdv_usd", 0) or 0),
                    "liquidity": float(attr.get("reserve_in_usd", 0) or 0),
                    "pool_address": attr.get("address", ""),
                    "url": f"https://www.geckoterminal.com/solana/pools/{attr.get('address', '')}"
                }
    except Exception as e:
        print(f"获取代币信息失败: {e}")
    return None

def handle_command(text: str, chat_id: str):
    text = text.strip()
    watch_list = load_json(WATCH_FILE)

    if text.startswith("/start") or text.startswith("/help"):
        msg = (
            "<b>🤖 新B-扫 机器人</b>\n\n"
            "【自动扫描】\n"
            "后台持续扫描新池，严格过滤后推送\n"
            "并通过12h/24h复检二次提醒\n\n"
            "【个人监控命令】\n"
            "/watch 合约地址 - 添加监控\n"
            "/unwatch 合约地址 - 删除监控\n"
            "/list - 查看监控列表\n"
            "/price 合约地址 - 查看即时价格市值\n"
            "/clear - 清空所有监控\n"
            "/status - 运行状态\n"
            "/help - 显示帮助"
        )
        send_telegram(msg, chat_id)

    elif text.startswith("/status"):
        observe = load_json(OBSERVE_FILE)
        msg = (
            f"<b>📊 运行状态</b>\n\n"
            f"个人监控数量: {len(watch_list)}\n"
            f"自动观察中: {len(observe)}\n"
            f"当前时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        )
        send_telegram(msg, chat_id)

    elif text.startswith("/watch"):
        parts = text.split()
        if len(parts) < 2:
            send_telegram("请使用格式：\n<code>/watch 合约地址</code>", chat_id)
            return
        addr = parts[1].strip()
        info = fetch_token_info(addr)
        if info:
            watch_list[addr] = {
                "name": info["name"],
                "added_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
                "pool_address": info.get("pool_address", "")
            }
            save_json(WATCH_FILE, watch_list)
            send_telegram(
                f"✅ 已添加监控\n\n"
                f"<b>{info['name']}</b>\n"
                f"价格: ${info['price']:.8f}\n"
                f"市值: ${info['fdv']:,.0f}\n"
                f"流动性: ${info['liquidity']:,.0f}",
                chat_id
            )
        else:
            send_telegram("❌ 未找到该代币，请检查合约地址是否正确", chat_id)

    elif text.startswith("/unwatch"):
        parts = text.split()
        if len(parts) < 2:
            send_telegram("请使用格式：\n<code>/unwatch 合约地址</code>", chat_id)
            return
        addr = parts[1].strip()
        if addr in watch_list:
            name = watch_list[addr].get("name", addr)
            del watch_list[addr]
            save_json(WATCH_FILE, watch_list)
            send_telegram(f"✅ 已删除监控：{name}", chat_id)
        else:
            send_telegram("该地址不在监控列表中", chat_id)

    elif text.startswith("/list"):
        if not watch_list:
            send_telegram("当前没有监控任何代币", chat_id)
            return
        msg = "<b>📋 当前监控列表</b>\n\n"
        for i, (addr, info) in enumerate(watch_list.items(), 1):
            msg += f"{i}. <b>{info.get('name', 'Unknown')}</b>\n<code>{addr}</code>\n添加时间: {info.get('added_at', '-')}\n\n"
        send_telegram(msg, chat_id)

    elif text.startswith("/price"):
        parts = text.split()
        if len(parts) < 2:
            send_telegram("请使用格式：\n<code>/price 合约地址</code>", chat_id)
            return
        addr = parts[1].strip()
        info = fetch_token_info(addr)
        if info:
            send_telegram(
                f"<b>{info['name']}</b>\n\n"
                f"价格: <b>${info['price']:.8f}</b>\n"
                f"市值(FDV): <b>${info['fdv']:,.0f}</b>\n"
                f"流动性: ${info['liquidity']:,.0f}\n\n"
                f"<a href='{info['url']}'>GeckoTerminal 查看</a>",
                chat_id
            )
        else:
            send_telegram("❌ 获取失败，请检查合约地址", chat_id)

    elif text.startswith("/clear"):
        save_json(WATCH_FILE, {})
        send_telegram("✅ 已清空所有个人监控", chat_id)

    else:
        send_telegram("未知命令，请输入 /help 查看支持的命令", chat_id)

def process_telegram_commands():
    """处理用户发送的命令"""
    global last_update_id
    updates = get_updates()
    for update in updates:
        last_update_id = update["update_id"]
        message = update.get("message", {})
        chat_id = str(message.get("chat", {}).get("id", ""))
        text = message.get("text", "")
        if chat_id == TELEGRAM_CHAT_ID and text.startswith("/"):
            handle_command(text, chat_id)

# ------------------- 以下是原来的扫描逻辑（保持不变） -------------------
def fetch_new_pools(pages=2):
    all_pools = []
    for page in range(1, pages + 1):
        url = f"{BASE_URL}/networks/solana/new_pools"
        params = {"page": page}
        try:
            r = session.get(url, params=params, timeout=12)
            if r.status_code == 200:
                all_pools.extend(r.json().get("data", []))
        except:
            pass
        time.sleep(1.1)
    return all_pools

def fetch_pool_by_address(pool_address: str):
    url = f"{BASE_URL}/networks/solana/pools/{pool_address}"
    try:
        r = session.get(url, timeout=12)
        if r.status_code == 200:
            return r.json().get("data")
    except:
        pass
    return None

def parse_pool(pool):
    try:
        attr = pool.get("attributes", {})
        rel = pool.get("relationships", {})
        name = attr.get("name", "Unknown")
        pool_address = attr.get("address", "")
        base_token = ""
        if "base_token" in rel:
            base_token = rel["base_token"]["data"]["id"].split("_")[-1]

        liquidity = float(attr.get("reserve_in_usd", 0) or 0)
        fdv = float(attr.get("fdv_usd", 0) or 0)
        vol_m5 = float(attr.get("volume_usd", {}).get("m5", 0) or 0)
        m5 = attr.get("transactions", {}).get("m5", {})
        buys = int(m5.get("buys", 0) or 0)
        sells = int(m5.get("sells", 0) or 0)
        buyers = int(m5.get("buyers", 0) or 0)

        created = attr.get("pool_created_at")
        age = 999
        if created:
            ct = datetime.fromisoformat(created.replace("Z", "+00:00"))
            age = (datetime.now(timezone.utc) - ct).total_seconds() / 60

        return {
            "name": name, "pool_address": pool_address, "token_address": base_token,
            "liquidity": liquidity, "fdv": fdv, "vol_m5": vol_m5,
            "buys_m5": buys, "sells_m5": sells, "buyers_m5": buyers,
            "age_minutes": age,
            "url": f"https://www.geckoterminal.com/solana/pools/{pool_address}"
        }
    except:
        return None

def hard_filter(p):
    if not p or p["liquidity"] < MIN_LIQUIDITY_USD: return False
    if p["age_minutes"] < MIN_AGE_MINUTES or p["age_minutes"] > MAX_AGE_MINUTES: return False
    if p["buyers_m5"] < MIN_BUYERS_M5 or p["vol_m5"] < MIN_VOLUME_M5: return False
    if p["buys_m5"] / max(p["sells_m5"], 1) < MIN_BUY_RATIO: return False
    if p["fdv"] > 0 and p["liquidity"] > 0 and p["fdv"] / p["liquidity"] > MAX_FDV_LIQ_RATIO: return False
    return True

def score_pool(p):
    score = 0
    liq = p["liquidity"]
    if liq >= 80000: score += 22
    elif liq >= 50000: score += 18
    elif liq >= 30000: score += 14
    elif liq >= 25000: score += 10
    br = p["buys_m5"] / max(p["sells_m5"], 1)
    if br >= 2.0: score += 18
    elif br >= 1.5: score += 14
    elif br >= 1.25: score += 10
    if p["buyers_m5"] >= 40: score += 12
    elif p["buyers_m5"] >= 25: score += 9
    elif p["buyers_m5"] >= 15: score += 6
    if p["vol_m5"] >= 30000: score += 12
    elif p["vol_m5"] >= 15000: score += 9
    elif p["vol_m5"] >= 8000: score += 6
    if p["fdv"] > 0 and p["liquidity"] > 0:
        r = p["fdv"] / p["liquidity"]
        if r <= 8: score += 12
        elif r <= 12: score += 8
        elif r <= 18: score += 4
    return min(score, 90)

def is_still_healthy(old, new):
    if not new: return False
    if new["liquidity"] < old["liquidity"] * 0.7: return False
    if new["vol_m5"] < 3000: return False
    if new["buys_m5"] / max(new["sells_m5"], 1) < 0.8: return False
    return True

def format_primary(p, score):
    br = p["buys_m5"] / max(p["sells_m5"], 1)
    return (
        f"<b>🚀 初级提醒 | {score}/90</b>\n\n"
        f"<b>{p['name']}</b>\n"
        f"流动性: <b>${p['liquidity']:,.0f}</b>\n"
        f"FDV: ${p['fdv']:,.0f}\n"
        f"年龄: {p['age_minutes']:.1f}分钟\n"
        f"买/卖: {p['buys_m5']}/{p['sells_m5']} | 买家{p['buyers_m5']}\n"
        f"买/卖比: <b>{br:.2f}</b>\n"
        f"5m成交: ${p['vol_m5']:,.0f}\n\n"
        f"<a href='{p['url']}'>GeckoTerminal</a>\n"
        f"<code>{p['pool_address']}</code>"
    )

def format_recheck(p, hours, old_liq):
    chg = ((p["liquidity"] - old_liq) / old_liq * 100) if old_liq else 0
    return (
        f"<b>✅ 二次确认 | {hours}h复检通过</b>\n\n"
        f"<b>{p['name']}</b>\n"
        f"流动性: <b>${p['liquidity']:,.0f}</b> ({chg:+.1f}%)\n"
        f"FDV: ${p['fdv']:,.0f}\n"
        f"<a href='{p['url']}'>查看</a>"
    )

def main_loop():
    print("机器人启动中（自动扫描 + 个人监控）...")
    send_telegram("🤖 机器人已启动\n发送 /help 查看可用命令")

    seen = set(load_json(SEEN_FILE)) if isinstance(load_json(SEEN_FILE), list) else set()
    observe = load_json(OBSERVE_FILE)

    while True:
        try:
            # 1. 处理用户命令
            process_telegram_commands()

            # 2. 扫描新池
            pools = fetch_new_pools(pages=2)
            print(f"[{datetime.now().strftime('%H:%M:%S')}] 扫描到 {len(pools)} 个新池")

            for pool in pools:
                p = parse_pool(pool)
                if not p or p["pool_address"] in seen: continue
                if not hard_filter(p): continue
                score = score_pool(p)
                if score >= MIN_SCORE_TO_PUSH:
                    send_telegram(format_primary(p, score))
                    observe[p["pool_address"]] = {
                        "first_seen": datetime.now(timezone.utc).isoformat(),
                        "name": p["name"],
                        "liquidity": p["liquidity"],
                        "rechecks_done": []
                    }
                    seen.add(p["pool_address"])

            save_json(SEEN_FILE, list(seen))
            save_json(OBSERVE_FILE, observe)

            # 3. 复检观察列表
            now = datetime.now(timezone.utc)
            for addr, info in list(observe.items()):
                first = datetime.fromisoformat(info["first_seen"])
                hours = (now - first).total_seconds() / 3600
                for h in RECHECK_HOURS:
                    if h in info.get("rechecks_done", []): continue
                    if hours >= h:
                        raw = fetch_pool_by_address(addr)
                        if raw:
                            np = parse_pool(raw)
                            if np and is_still_healthy(info, np):
                                send_telegram(format_recheck(np, h, info["liquidity"]))
                        info.setdefault("rechecks_done", []).append(h)
                        observe[addr] = info
                if hours > max(RECHECK_HOURS) + 6:
                    observe.pop(addr, None)
            save_json(OBSERVE_FILE, observe)

            time.sleep(SCAN_INTERVAL)
        except Exception as e:
            print(f"异常: {e}")
            time.sleep(30)

if __name__ == "__main__":
    main_loop()