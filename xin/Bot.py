import time
import requests
import json
from datetime import datetime, timezone, timedelta
from pathlib import Path

# ====================== 配置区 ======================
TELEGRAM_BOT_TOKEN = "8229035839:AAFfen2rHkpQB6hh1LW9cP8oYkQfoefcK_k"          # 必填
TELEGRAM_CHAT_ID = "1085273843"            # 必填

SCAN_INTERVAL = 90               # 主循环间隔（秒）
AUTO_SCAN_INTERVAL = 300         # 自动扫描间隔（秒），建议 5 分钟

BREAKOUT_WATCH_FILE = "breakout_watch.json"
HISTORY_FILE = "price_history.json"
ALERT_COOLDOWN_FILE = "alert_cooldown.json"
AUTO_SCANNED_FILE = "auto_scanned.json"   # 记录已经自动扫描过的币，避免重复

# ====================== 策略参数（稳当版） ======================
MIN_AGE_DAYS = 10
MAX_AGE_DAYS = 1000
HISTORY_KEEP_DAYS = 60

CONSOL_HOURS = 36
CONSOL_MAX_RANGE_PCT = 20
VOL_SPIKE_MULT = 2.4
MIN_VOL_H1 = 9000
PRICE_BREAK_PCT = 12

MIN_LIQ_BREAKOUT = 38000
MAX_FDV_LIQ_RATIO = 16
MIN_BUY_RATIO_M5 = 1.4

MIN_SCORE_BREAKOUT = 70
ALERT_COOLDOWN_HOURS = 5

# 自动扫描开关
ENABLE_AUTO_SCAN = True
AUTO_ADD_TO_WATCH = False        # True = 自动加入监控列表，False = 只推送不加入
# ====================================================

DEX_BASE = "https://api.dexscreener.com"
DEX_HEADERS = {
    "Accept": "application/json",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
}

session = requests.Session()
session.headers.update(DEX_HEADERS)

last_update_id = 0
last_auto_scan_time = 0

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
        "disable_web_page_preview": True
    }
    try:
        session.post(url, json=payload, timeout=15)
    except Exception as e:
        print(f"Telegram发送失败: {e}")

def get_updates():
    global last_update_id
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"
    params = {"offset": last_update_id + 1, "timeout": 8}
    try:
        r = session.get(url, params=params, timeout=12)
        if r.status_code == 200:
            return r.json().get("result", [])
    except:
        pass
    return []

# ====================== DexScreener 基础 ======================
def fetch_token_info(token_address: str):
    url = f"{DEX_BASE}/latest/dex/tokens/{token_address}"
    try:
        r = session.get(url, headers=DEX_HEADERS, timeout=12)
        if r.status_code != 200:
            return None
        pairs = r.json().get("pairs") or []
        if not pairs:
            return None
        sol_pairs = [p for p in pairs if p.get("chainId") == "solana"] or pairs
        best = max(sol_pairs, key=lambda x: float(x.get("liquidity", {}).get("usd", 0) or 0))
        return parse_pair(best)
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
        sol = [p for p in pairs if p.get("chainId") == "solana"] or pairs
        if not sol:
            return None
        return max(sol, key=lambda x: float(x.get("liquidity", {}).get("usd", 0) or 0))
    except:
        return None

def parse_pair(pair: dict):
    try:
        base = pair.get("baseToken", {})
        name = f"{base.get('name', 'Unknown')} (${base.get('symbol', '')})"
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
        age_days = 999.0
        if created:
            ct = datetime.fromtimestamp(created / 1000, tz=timezone.utc)
            age_days = (datetime.now(timezone.utc) - ct).total_seconds() / 86400

        return {
            "name": name,
            "symbol": base.get("symbol", ""),
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
def update_price_history(watch_dict: dict):
    history = load_json(HISTORY_FILE)
    now = datetime.now(timezone.utc)
    cutoff = (now - timedelta(days=HISTORY_KEEP_DAYS)).isoformat()

    for addr in list(watch_dict.keys()):
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

# ====================== 策略 ======================
def strategy_A_volume_price_break(hist: list, current: dict) -> dict:
    if len(hist) < 8:
        return {"score": 0, "reason": "历史不足"}

    prices = [h["price"] for h in hist if h.get("price", 0) > 0]
    vols = [h.get("vol_h1", 0) or h.get("vol_m5", 0) * 12 for h in hist]

    if len(prices) < 6:
        return {"score": 0, "reason": "数据不足"}

    recent_high = max(prices[-20:]) if len(prices) >= 20 else max(prices)
    avg_vol = sum(vols[:-3]) / max(len(vols) - 3, 1)
    curr_vol = vols[-1] if vols else 0
    curr_price = current.get("price", prices[-1])

    vol_mult = curr_vol / avg_vol if avg_vol > 0 else 0
    price_break = (curr_price - recent_high) / recent_high * 100 if recent_high > 0 else 0

    score = 0
    reasons = []

    if vol_mult >= VOL_SPIKE_MULT:
        score += 34
        reasons.append(f"量放大{vol_mult:.1f}x")
    elif vol_mult >= 1.8:
        score += 14
        reasons.append(f"量增{vol_mult:.1f}x")

    if price_break >= PRICE_BREAK_PCT:
        score += 40
        reasons.append(f"突破高点+{price_break:.1f}%")
    elif price_break > 7:
        score += 16
        reasons.append(f"接近突破+{price_break:.1f}%")

    if current.get("liquidity", 0) >= MIN_LIQ_BREAKOUT:
        score += 12

    return {"score": min(score, 100), "reason": " | ".join(reasons) or "无强信号",
            "vol_mult": round(vol_mult, 2), "price_break": round(price_break, 1)}

def strategy_B_consolidation_then_spike(hist: list, current: dict) -> dict:
    if len(hist) < 10:
        return {"score": 0, "reason": "历史不足"}

    now = datetime.now(timezone.utc)
    consol_cutoff = (now - timedelta(hours=CONSOL_HOURS)).isoformat()
    consol = [h for h in hist if h["ts"] >= consol_cutoff]

    if len(consol) < 7:
        return {"score": 0, "reason": "横盘窗口数据少"}

    prices = [h["price"] for h in consol if h.get("price", 0) > 0]
    if len(prices) < 5:
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
        score += 36
        reasons.append(f"横盘{range_pct:.1f}%")
    else:
        return {"score": 0, "reason": f"波动过大({range_pct:.1f}%)"}

    if vol_mult >= VOL_SPIKE_MULT:
        score += 42
        reasons.append(f"量突增{vol_mult:.1f}x")
    elif vol_mult >= 1.8:
        score += 18
        reasons.append(f"量增加{vol_mult:.1f}x")

    last_price = prices[-1]
    if last_price >= high * 0.96:
        score += 12
        reasons.append("接近/突破横盘高点")

    if current.get("liquidity", 0) >= MIN_LIQ_BREAKOUT:
        score += 8

    return {"score": min(score, 100), "reason": " | ".join(reasons),
            "range_pct": round(range_pct, 1), "vol_mult": round(vol_mult, 2)}

def strategy_C_momentum_buys(current: dict) -> dict:
    buys = current.get("buys_m5", 0)
    sells = current.get("sells_m5", 0)
    br = buys / max(sells, 1)
    vol_m5 = current.get("vol_m5", 0)
    pc_h1 = current.get("price_change_h1", 0) or 0

    score = 0
    reasons = []

    if br >= 1.9 and buys >= 15:
        score += 28
        reasons.append(f"买压{br:.1f}")
    elif br >= MIN_BUY_RATIO_M5:
        score += 14
        reasons.append(f"买压{br:.1f}")

    if vol_m5 >= 10000:
        score += 16
        reasons.append(f"5m量${vol_m5:,.0f}")
    elif vol_m5 >= 5000:
        score += 8

    if pc_h1 >= 6:
        score += 18
        reasons.append(f"1h涨{pc_h1:.1f}%")
    elif pc_h1 >= 3:
        score += 8

    if current.get("liquidity", 0) >= MIN_LIQ_BREAKOUT:
        score += 8

    return {"score": min(score, 100), "reason": " | ".join(reasons) or "无强信号"}

def strategy_D_combined(hist, current) -> dict:
    a = strategy_A_volume_price_break(hist, current)
    b = strategy_B_consolidation_then_spike(hist, current)
    c = strategy_C_momentum_buys(current)
    score = int(a["score"] * 0.27 + b["score"] * 0.48 + c["score"] * 0.25)
    return {
        "score": score,
        "reason": f"A:{a['score']} B:{b['score']} C:{c['score']}",
        "details": {"A": a, "B": b, "C": c}
    }

def hard_quality_filter(current: dict) -> bool:
    if current.get("liquidity", 0) < MIN_LIQ_BREAKOUT:
        return False
    age = current.get("age_days", 999)
    if age < MIN_AGE_DAYS or age > MAX_AGE_DAYS:
        return False
    if current.get("fdv", 0) > 0 and current.get("liquidity", 0) > 0:
        if current["fdv"] / current["liquidity"] > MAX_FDV_LIQ_RATIO:
            return False
    if current.get("vol_h1", 0) < MIN_VOL_H1 and current.get("vol_m5", 0) * 12 < MIN_VOL_H1:
        return False
    buys = current.get("buys_m5", 0)
    sells = current.get("sells_m5", 0)
    if buys / max(sells, 1) < MIN_BUY_RATIO_M5 and buys < 10:
        return False
    if current.get("price_change_h6", 0) > 60:
        return False
    return True

# ====================== 详细警报格式 ======================
def format_alert(current: dict, results: dict, source: str = "监控"):
    a = results["A"]
    b = results["B"]
    c = results["C"]
    d = results["D"]
    best = max(r["score"] for r in results.values())

    buy_ratio = current["buys_m5"] / max(current["sells_m5"], 1)

    msg = (
        f"<b>🚀 底部启动信号</b>\n"
        f"<i>来源：{source} | 综合得分 {best}</i>\n\n"
        f"<b>代币：</b>{current['name']}\n"
        f"<b>年龄：</b>{current['age_days']:.1f} 天\n"
        f"<b>流动性：</b>${current['liquidity']:,.0f}\n"
        f"<b>市值：</b>${current['fdv']:,.0f}\n"
        f"<b>当前价格：</b>${current['price']:.8f}\n\n"
        f"<b>策略详情：</b>\n"
        f"A 量价突破：{a['score']} → {a['reason']}\n"
        f"B 横盘启动：{b['score']} → {b['reason']}\n"
        f"C 买压动量：{c['score']} → {c['reason']}\n"
        f"D 综合：{d['score']}\n\n"
        f"买压比：{buy_ratio:.2f} | 1h涨跌：{current.get('price_change_h1', 0):+.1f}%\n"
        f"时间：{datetime.now().strftime('%Y-%m-%d %H:%M')}\n"
        f"合约：\n<code>{current['token_address']}</code>\n\n"
        f"<a href='{current['url']}'>DexScreener</a>"
    )
    return msg

# ====================== 自动扫描 ======================
def fetch_latest_profiles():
    """获取最新 token profiles（自动扫描来源之一）"""
    url = f"{DEX_BASE}/token-profiles/latest/v1"
    try:
        r = session.get(url, headers=DEX_HEADERS, timeout=12)
        if r.status_code == 200:
            return r.json() or []
    except Exception as e:
        print(f"fetch_latest_profiles 失败: {e}")
    return []

def fetch_latest_boosts():
    """获取最新 boosted tokens"""
    url = f"{DEX_BASE}/token-boosts/latest/v1"
    try:
        r = session.get(url, headers=DEX_HEADERS, timeout=12)
        if r.status_code == 200:
            return r.json() or []
    except Exception as e:
        print(f"fetch_latest_boosts 失败: {e}")
    return []

def auto_scan():
    """自动扫描逻辑"""
    global last_auto_scan_time
    now_ts = time.time()
    if now_ts - last_auto_scan_time < AUTO_SCAN_INTERVAL:
        return
    last_auto_scan_time = now_ts

    print(f"[{datetime.now().strftime('%H:%M:%S')}] 开始自动扫描...")

    candidates = []
    # 来源1：最新 profiles
    for item in fetch_latest_profiles()[:30]:
        addr = item.get("tokenAddress")
        if addr:
            candidates.append(addr)

    # 来源2：最新 boosts
    for item in fetch_latest_boosts()[:20]:
        addr = item.get("tokenAddress")
        if addr:
            candidates.append(addr)

    candidates = list(set(candidates))
    print(f"自动扫描候选数量: {len(candidates)}")

    auto_scanned = load_json(AUTO_SCANNED_FILE)
    cooldown = load_json(ALERT_COOLDOWN_FILE)
    history = load_json(HISTORY_FILE)
    breakout_watch = load_json(BREAKOUT_WATCH_FILE)
    now = datetime.now(timezone.utc)

    for addr in candidates:
        # 避免短时间重复扫描同一币
        if addr in auto_scanned:
            last = datetime.fromisoformat(auto_scanned[addr])
            if (now - last).total_seconds() < 3600 * 6:  # 6小时内不重复
                continue

        raw = enrich_with_pair(addr)
        if not raw:
            continue
        current = parse_pair(raw)
        if not current or not hard_quality_filter(current):
            continue

        # 更新历史
        if addr not in history:
            history[addr] = []
        history[addr].append({
            "ts": now.isoformat(),
            "price": current["price"],
            "liq": current["liquidity"],
            "vol_m5": current["vol_m5"],
            "vol_h1": current["vol_h1"],
            "vol_h6": current["vol_h6"],
            "buys_m5": current["buys_m5"],
            "sells_m5": current["sells_m5"],
            "fdv": current["fdv"]
        })
        history[addr] = [h for h in history[addr] if h["ts"] > (now - timedelta(days=HISTORY_KEEP_DAYS)).isoformat()]

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

        # 冷却
        if addr in cooldown:
            last_dt = datetime.fromisoformat(cooldown[addr])
            if (now - last_dt).total_seconds() < ALERT_COOLDOWN_HOURS * 3600:
                continue

        # 发送警报
        msg = format_alert(current, results, source="自动扫描")
        send_telegram(msg)

        cooldown[addr] = now.isoformat()
        auto_scanned[addr] = now.isoformat()

        # 是否自动加入监控
        if AUTO_ADD_TO_WATCH and addr not in breakout_watch:
            breakout_watch[addr] = {
                "name": current["name"],
                "added_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
                "source": "auto"
            }

        print(f"自动扫描命中: {current['name']} 得分 {best_score}")

    save_json(HISTORY_FILE, history)
    save_json(ALERT_COOLDOWN_FILE, cooldown)
    save_json(AUTO_SCANNED_FILE, auto_scanned)
    if AUTO_ADD_TO_WATCH:
        save_json(BREAKOUT_WATCH_FILE, breakout_watch)

# ====================== 命令处理 ======================
def handle_command(text: str, chat_id: str):
    text = text.strip()
    breakout_watch = load_json(BREAKOUT_WATCH_FILE)

    if text.startswith("/start") or text.startswith("/help"):
        msg = (
            "<b>🤖 底部启动信号机器人</b>\n\n"
            "功能：手动监控 + 自动扫描\n"
            f"年龄范围：{MIN_AGE_DAYS} ~ {MAX_AGE_DAYS} 天\n"
            f"最低流动性：${MIN_LIQ_BREAKOUT:,}\n\n"
            "<b>命令：</b>\n"
            "/bwatch 合约 - 手动加入监控\n"
            "/bunwatch 合约 - 删除监控\n"
            "/blist - 查看监控列表\n"
            "/bclear - 清空监控\n"
            "/price 合约 - 查看数据\n"
            "/status - 运行状态\n"
            "/help"
        )
        send_telegram(msg, chat_id)

    elif text.startswith("/status"):
        history = load_json(HISTORY_FILE)
        cooldown = load_json(ALERT_COOLDOWN_FILE)
        auto_scanned = load_json(AUTO_SCANNED_FILE)
        msg = (
            f"<b>📊 运行状态</b>\n\n"
            f"手动监控：{len(breakout_watch)}\n"
            f"历史记录：{len(history)}\n"
            f"冷却中：{len(cooldown)}\n"
            f"已自动扫描：{len(auto_scanned)}\n"
            f"自动扫描：{'开启' if ENABLE_AUTO_SCAN else '关闭'}\n"
            f"年龄：{MIN_AGE_DAYS}~{MAX_AGE_DAYS}天\n"
            f"最低流动性：${MIN_LIQ_BREAKOUT:,}\n"
            f"时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        )
        send_telegram(msg, chat_id)

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

        breakout_watch[addr] = {
            "name": info["name"],
            "added_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "source": "manual"
        }
        save_json(BREAKOUT_WATCH_FILE, breakout_watch)

        tip = ""
        if info["age_days"] < MIN_AGE_DAYS:
            tip = f"\n⚠️ 年龄仅 {info['age_days']:.1f} 天，未达最低要求。"
        elif info["liquidity"] < MIN_LIQ_BREAKOUT:
            tip = f"\n⚠️ 流动性 ${info['liquidity']:,.0f} 低于门槛。"

        send_telegram(
            f"✅ 已加入监控\n\n"
            f"<b>{info['name']}</b>\n"
            f"年龄：{info['age_days']:.1f} 天\n"
            f"流动性：${info['liquidity']:,.0f}\n"
            f"市值：${info['fdv']:,.0f}\n"
            f"价格：${info['price']:.8f}{tip}",
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
            src = info.get("source", "manual")
            msg += f"{i}. <b>{info.get('name')}</b> ({src})\n<code>{addr}</code>\n添加：{info.get('added_at')}\n\n"
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
            change = info.get("price_change_h24", 0)
            change_str = f"+{change:.1f}%" if change >= 0 else f"{change:.1f}%"
            send_telegram(
                f"<b>{info['name']}</b>\n\n"
                f"价格：<b>${info['price']:.8f}</b>\n"
                f"市值：<b>${info['fdv']:,.0f}</b>\n"
                f"流动性：${info['liquidity']:,.0f}\n"
                f"24h涨跌：{change_str}\n"
                f"年龄：<b>{info['age_days']:.1f} 天</b>\n\n"
                f"<a href='{info['url']}'>DexScreener</a>",
                chat_id
            )
        else:
            send_telegram("❌ 获取失败", chat_id)

    else:
        send_telegram("未知命令，请输入 /help", chat_id)

def process_telegram_commands():
    global last_update_id
    updates = get_updates()
    for update in updates:
        last_update_id = update["update_id"]
        message = update.get("message", {})
        chat_id = str(message.get("chat", {}).get("id", ""))
        text = message.get("text", "")
        if chat_id == TELEGRAM_CHAT_ID and text.startswith("/"):
            handle_command(text, chat_id)

# ====================== 主循环 ======================
def main_loop():
    print("底部启动信号机器人启动中（手动监控 + 自动扫描）...")
    send_telegram(
        "🤖 机器人已启动\n"
        "模式：手动监控 + 自动扫描\n"
        f"年龄范围：{MIN_AGE_DAYS} ~ {MAX_AGE_DAYS} 天\n"
        f"最低流动性：${MIN_LIQ_BREAKOUT:,}\n"
        "警报标题：底部启动信号\n"
        "发送 /help 查看命令"
    )

    while True:
        try:
            process_telegram_commands()

            # 1. 处理手动监控列表
            breakout_watch = load_json(BREAKOUT_WATCH_FILE)
            if breakout_watch:
                history = update_price_history(breakout_watch)
                cooldown = load_json(ALERT_COOLDOWN_FILE)
                now = datetime.now(timezone.utc)

                for addr in list(breakout_watch.keys()):
                    raw = enrich_with_pair(addr)
                    if not raw:
                        continue
                    current = parse_pair(raw)
                    if not current or not hard_quality_filter(current):
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

                    if addr in cooldown:
                        last_dt = datetime.fromisoformat(cooldown[addr])
                        if (now - last_dt).total_seconds() < ALERT_COOLDOWN_HOURS * 3600:
                            continue

                    msg = format_alert(current, results, source="手动监控")
                    send_telegram(msg)
                    cooldown[addr] = now.isoformat()
                    save_json(ALERT_COOLDOWN_FILE, cooldown)

            # 2. 自动扫描
            if ENABLE_AUTO_SCAN:
                auto_scan()

            time.sleep(SCAN_INTERVAL)

        except Exception as e:
            print(f"主循环异常: {e}")
            time.sleep(30)

if __name__ == "__main__":
    main_loop()
