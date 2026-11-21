
import os
import json
import time
import requests
from datetime import datetime, timezone
from pathlib import Path

# ==========================================
# PUMP.FUN + DEX SCREENER V11
# SOLANA ONLY
# ==========================================

DEX_API = "https://api.dexscreener.com"
RUGCHECK_API = "https://api.rugcheck.xyz/v1"
TELEGRAM_API = "https://api.telegram.org"

MIN_MC = 5000
MAX_MC = 50000
MIN_LIQUIDITY = 5000
MAX_AGE_MINUTES = 10
MIN_VOLUME_5M = 500
MIN_TXNS_5M = 5
MIN_BUY_SELL_RATIO = 1.5

STATE_FILE = Path("seen_tokens.json")
MAX_SEEN = 5000

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

session = requests.Session()


def log(msg):
    print(
        f"[{datetime.now(timezone.utc).isoformat()}] {msg}",
        flush=True
    )


def get_json(url):
    try:
        r = session.get(url, timeout=15)

        if r.status_code == 429:
            log("API RATE LIMIT")
            return None

        r.raise_for_status()
        return r.json()

    except Exception as e:
        log(f"API ERROR: {e}")
        return None


def load_seen():
    try:
        data = json.loads(
            STATE_FILE.read_text(encoding="utf-8")
        )
        return set(data if isinstance(data, list) else [])
    except Exception:
        return set()


def save_seen(seen):
    STATE_FILE.write_text(
        json.dumps(list(seen)[-MAX_SEEN:], indent=2),
        encoding="utf-8"
    )


def number(value, default=0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def telegram(message):
    if not BOT_TOKEN or not CHAT_ID:
        log("TELEGRAM SECRETS MISSING")
        return False

    try:
        r = session.post(
            f"{TELEGRAM_API}/bot{BOT_TOKEN}/sendMessage",
            data={
                "chat_id": CHAT_ID,
                "text": message,
                "disable_web_page_preview": True
            },
            timeout=15
        )

        r.raise_for_status()
        return r.json().get("ok", False)

    except Exception as e:
        log(f"TELEGRAM ERROR: {e}")
        return False


# ==========================================
# PUMPFUN DISCOVERY
# ==========================================

def get_pumpfun_tokens():

    data = get_json(
        "https://frontend-api-v3.pump.fun/coins"
        "?offset=0&limit=100&sort=created_timestamp"
        "&order=DESC&includeNsfw=false"
    )

    result = []

    if not isinstance(data, list):
        return result

    for coin in data:
        if not isinstance(coin, dict):
            continue

        mint = coin.get("mint")

        if mint:
            result.append(mint)

    return result


# ==========================================
# DEXSCREENER DISCOVERY
# ==========================================

def get_dex_tokens():

    data = get_json(
        f"{DEX_API}/token-profiles/latest/v1"
    )

    if not isinstance(data, list):
        return []

    return [
        item["tokenAddress"]
        for item in data
        if isinstance(item, dict)
        and item.get("chainId") == "solana"
        and item.get("tokenAddress")
    ]


def get_pairs(mint):

    data = get_json(
        f"{DEX_API}/token-pairs/v1/solana/{mint}"
    )

    if not isinstance(data, list):
        return []

    return [
        p for p in data
        if isinstance(p, dict)
        and p.get("chainId") == "solana"
    ]


# ==========================================
# RUGCHECK
# ==========================================

def get_rugcheck(mint):

    data = get_json(
        f"{RUGCHECK_API}/tokens/{mint}/report"
    )

    return data if isinstance(data, dict) else {}


def extract_score(report):

    for key in (
        "score_normalised",
        "scoreNormalized",
        "score"
    ):
        if report.get(key) is not None:
            return report[key]

    return "N/A"


def holder_info(report):

    holders = (
        report.get("topHolders")
        or report.get("top_holders")
        or []
    )

    if not isinstance(holders, list):
        return None, None, None

    percentages = []

    for h in holders:
        if not isinstance(h, dict):
            continue

        pct = h.get("pct", h.get("percentage"))

        if pct is not None:
            percentages.append(number(pct))

    top10 = sum(percentages[:10]) if percentages else None
    largest = max(percentages) if percentages else None

    creator = (
        report.get("creator")
        or (report.get("token") or {}).get("creator")
        or (report.get("token") or {}).get("owner")
    )

    dev = None

    for h in holders:

        if not isinstance(h, dict):
            continue

        address = (
            h.get("address")
            or h.get("owner")
            or h.get("wallet")
        )

        if creator and address == creator:

            value = h.get("pct", h.get("percentage"))

            if value is not None:
                dev = number(value)

            break

    return top10, largest, dev


# ==========================================
# FILTERS
# ==========================================

def age_minutes(pair):

    created = pair.get("pairCreatedAt")

    if not created:
        return None

    try:
        return max(
            0,
            (time.time() - float(created) / 1000) / 60
        )
    except (TypeError, ValueError):
        return None


def passes_filters(pair):

    mc = number(
        pair.get("marketCap") or pair.get("fdv")
    )

    liquidity = number(
        (pair.get("liquidity") or {}).get("usd")
    )

    volume = number(
        (pair.get("volume") or {}).get("m5")
    )

    txns = (pair.get("txns") or {}).get("m5") or {}

    buys = int(number(txns.get("buys")))
    sells = int(number(txns.get("sells")))

    age = age_minutes(pair)

    if age is None or age > MAX_AGE_MINUTES:
        return False

    if not MIN_MC <= mc <= MAX_MC:
        return False

    if liquidity < MIN_LIQUIDITY:
        return False

    if volume < MIN_VOLUME_5M:
        return False

    if buys + sells < MIN_TXNS_5M:
        return False

    if sells == 0:
        ratio = float("inf") if buys > 0 else 0
    else:
        ratio = buys / sells

    if ratio < MIN_BUY_SELL_RATIO:
        return False

    return True


# ==========================================
# ALERT
# ==========================================

def make_alert(pair, mint, report, source):

    base = pair.get("baseToken") or {}

    name = base.get("name", "Unknown")
    symbol = base.get("symbol", "?")

    mc = number(
        pair.get("marketCap") or pair.get("fdv")
    )

    liq = number(
        (pair.get("liquidity") or {}).get("usd")
    )

    vol = number(
        (pair.get("volume") or {}).get("m5")
    )

    txns = (pair.get("txns") or {}).get("m5") or {}

    buys = int(number(txns.get("buys")))
    sells = int(number(txns.get("sells")))

    ratio = buys / sells if sells else "No sells"

    age = age_minutes(pair)

    score = extract_score(report)

    top10, largest, dev = holder_info(report)

    def pct(v):
        return f"{v:.2f}%" if v is not None else "N/A"

    dex = pair.get("url") or (
        f"https://dexscreener.com/solana/{mint}"
    )

    return (
        "🚀 PUMP.FUN + DEX V11\n\n"
        f"Name: {name}\n"
        f"Symbol: ${symbol}\n"
        f"Source: {source}\n\n"
        f"Market Cap: ${mc:,.0f}\n"
        f"Liquidity: ${liq:,.0f}\n"
        f"5m Volume: ${vol:,.0f}\n"
        f"Buys: {buys}\n"
        f"Sells: {sells}\n"
        f"Buy/Sell Ratio: {ratio}\n"
        f"Transactions: {buys+sells}\n"
        f"Pair Age: {age:.2f} minutes\n\n"
        "🛡️ RUGCHECK / HOLDERS\n"
        f"RugCheck Score: {score}\n"
        f"Top 10 Holders: {pct(top10)}\n"
        f"Largest Holder: {pct(largest)}\n"
        f"Dev Holding: {pct(dev)}\n\n"
        f"Contract: {mint}\n\n"
        f"DEX: {dex}\n"
        f"RugCheck: https://rugcheck.xyz/tokens/{mint}\n"
        f"Pump.fun: https://pump.fun/coin/{mint}"
    )


# ==========================================
# MAIN
# ==========================================

def main():

    log("PUMP.FUN + DEX V11 STARTED")

    if not BOT_TOKEN or not CHAT_ID:
        log("TELEGRAM SECRETS MISSING")
        return

    seen = load_seen()

    pump_tokens = get_pumpfun_tokens()
    dex_tokens = get_dex_tokens()

    tokens = list(dict.fromkeys(
        pump_tokens + dex_tokens
    ))

    log(f"Pump.fun tokens: {len(pump_tokens)}")
    log(f"DexScreener tokens: {len(dex_tokens)}")
    log(f"Total unique tokens: {len(tokens)}")

    alerts = 0

    for mint in tokens:

        if mint in seen:
            continue

        pairs = get_pairs(mint)

        if not pairs:
            continue

        pairs.sort(
            key=lambda p: number(
                (p.get("liquidity") or {}).get("usd")
            ),
            reverse=True
        )

        pair = pairs[0]

        if not passes_filters(pair):
            continue

        report = get_rugcheck(mint)

        # Determine discovery source
        source = (
            "Pump.fun + DexScreener"
            if mint in pump_tokens and mint in dex_tokens
            else "Pump.fun"
            if mint in pump_tokens
            else "DexScreener"
        )

        message = make_alert(
            pair,
            mint,
            report,
            source
        )

        if telegram(message):
            seen.add(mint)
            alerts += 1
            log(f"ALERT SENT: {mint}")

        time.sleep(0.3)

    save_seen(seen)

    log(
        f"V11 FINISHED | ALERTS: {alerts}"
    )


if __name__ == "__main__":
    main()
