
import os
import json
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

# ==========================================
# DEX SCREENER V10 - SOLANA ONLY
# RUGCHECK SCORE DISPLAY ONLY
# ==========================================

DEX_API = "https://api.dexscreener.com"
RUGCHECK_API = "https://api.rugcheck.xyz/v1"
TELEGRAM_API = "https://api.telegram.org"

MIN_MC = 5000
MAX_MC = 50000
MIN_LIQUIDITY = 5000
MAX_AGE_HOURS = 10
MIN_VOLUME_5M = 500
MIN_TXNS_5M = 5

STATE_FILE = Path("seen_tokens.json")
MAX_SEEN = 5000
REQUEST_TIMEOUT = 15

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

session = requests.Session()
session.headers.update({
    "User-Agent": "DEX-Screener-V10/1.0"
})


def log(message):
    print(
        f"[{datetime.now(timezone.utc).isoformat()}] {message}",
        flush=True
    )


def get_json(url):
    try:
        response = session.get(url, timeout=REQUEST_TIMEOUT)
        if response.status_code == 429:
            log("API rate limit reached")
            return None
        response.raise_for_status()
        return response.json()
    except Exception as exc:
        log(f"API ERROR: {exc}")
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
    values = list(seen)[-MAX_SEEN:]
    STATE_FILE.write_text(
        json.dumps(values, indent=2),
        encoding="utf-8"
    )


def send_telegram(message):
    if not BOT_TOKEN or not CHAT_ID:
        log("TELEGRAM SECRETS MISSING")
        return False

    url = f"{TELEGRAM_API}/bot{BOT_TOKEN}/sendMessage"

    try:
        response = session.post(
            url,
            data={
                "chat_id": CHAT_ID,
                "text": message,
                "disable_web_page_preview": True
            },
            timeout=REQUEST_TIMEOUT
        )
        response.raise_for_status()
        return True
    except Exception as exc:
        log(f"TELEGRAM ERROR: {exc}")
        return False


def number(value, default=0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def fmt_usd(value):
    return f"${number(value):,.0f}"


def get_solana_profiles():
    data = get_json(
        f"{DEX_API}/token-profiles/latest/v1"
    )

    if not isinstance(data, list):
        return []

    return [
        item for item in data
        if isinstance(item, dict)
        and item.get("chainId") == "solana"
        and item.get("tokenAddress")
    ]


def get_pairs(mint):
    data = get_json(
        f"{DEX_API}/token-pairs/v1/solana/{mint}"
    )
    return data if isinstance(data, list) else []


def get_rugcheck(mint):
    report = get_json(
        f"{RUGCHECK_API}/tokens/{mint}/report"
    )

    if isinstance(report, dict):
        return report

    summary = get_json(
        f"{RUGCHECK_API}/tokens/{mint}/report/summary"
    )

    return summary if isinstance(summary, dict) else {}


def extract_score(report):
    for key in (
        "score_normalised",
        "scoreNormalized",
        "score"
    ):
        if report.get(key) is not None:
            return report.get(key)

    return None


def extract_holder_info(report):
    holders = (
        report.get("topHolders")
        or report.get("top_holders")
        or []
    )

    percentages = []

    if isinstance(holders, list):
        for holder in holders:
            if not isinstance(holder, dict):
                continue

            pct = holder.get(
                "pct",
                holder.get("percentage")
            )

            if pct is not None:
                percentages.append(number(pct))

    top10 = (
        sum(percentages[:10])
        if percentages else None
    )

    largest = (
        max(percentages)
        if percentages else None
    )

    creator = (
        report.get("creator")
        or report.get("token", {}).get("creator")
        or report.get("token", {}).get("owner")
    )

    dev_pct = None

    if creator and isinstance(holders, list):
        for holder in holders:
            if not isinstance(holder, dict):
                continue

            address = (
                holder.get("address")
                or holder.get("owner")
                or holder.get("wallet")
            )

            if address == creator:
                pct = holder.get(
                    "pct",
                    holder.get("percentage")
                )

                if pct is not None:
                    dev_pct = number(pct)

                break

    return top10, largest, dev_pct, len(holders)


def age_hours(pair):
    created = pair.get("pairCreatedAt")

    if not created:
        return None

    try:
        return max(
            0,
            (time.time() - float(created) / 1000) / 3600
        )
    except (TypeError, ValueError):
        return None


def passes_filters(pair):

    if pair.get("chainId") != "solana":
        return False, "Network"

    mc = number(
        pair.get("marketCap") or pair.get("fdv")
    )

    liquidity = number(
        (pair.get("liquidity") or {}).get("usd")
    )

    volume5 = number(
        (pair.get("volume") or {}).get("m5")
    )

    txns = (pair.get("txns") or {}).get("m5") or {}

    buys = int(number(txns.get("buys")))
    sells = int(number(txns.get("sells")))

    age = age_hours(pair)

    if not MIN_MC <= mc <= MAX_MC:
        return False, "Market Cap"

    if liquidity < MIN_LIQUIDITY:
        return False, "Liquidity"

    if age is None or age > MAX_AGE_HOURS:
        return False, "Age"

    if volume5 < MIN_VOLUME_5M:
        return False, "Volume"

    if buys + sells < MIN_TXNS_5M:
        return False, "Transactions"

    if buys <= sells:
        return False, "Buy/Sell"

    # RugCheck score and holders are NOT rejection filters.

    return True, "PASSED"


def make_alert(pair, mint, report):

    base = pair.get("baseToken") or {}

    name = base.get("name") or "Unknown"
    symbol = base.get("symbol") or "?"

    mc = number(
        pair.get("marketCap") or pair.get("fdv")
    )

    liquidity = number(
        (pair.get("liquidity") or {}).get("usd")
    )

    volume5 = number(
        (pair.get("volume") or {}).get("m5")
    )

    txns = (pair.get("txns") or {}).get("m5") or {}

    buys = int(number(txns.get("buys")))
    sells = int(number(txns.get("sells")))

    age = age_hours(pair)

    score = extract_score(report)

    top10, largest, dev_pct, holder_count = (
        extract_holder_info(report)
    )

    def pct(value):
        return (
            f"{value:.2f}%"
            if value is not None
            else "Not available"
        )

    dex_url = pair.get("url") or (
        f"https://dexscreener.com/solana/{mint}"
    )

    return (
        "🚀 DEX SCREENER V10\n\n"
        f"Name: {name}\n"
        f"Symbol: ${symbol}\n\n"
        f"Market Cap: {fmt_usd(mc)}\n"
        f"Liquidity: {fmt_usd(liquidity)}\n"
        f"5m Volume: {fmt_usd(volume5)}\n"
        f"5m Transactions: {buys+sells}\n"
        f"Buys: {buys}\n"
        f"Sells: {sells}\n"
        f"Pair Age: {age:.2f} hours\n\n"
        "🛡️ RUGCHECK / HOLDERS\n"
        f"RugCheck Score: {score if score is not None else 'Not available'}\n"
        f"Top 10 Holders: {pct(top10)}\n"
        f"Largest Holder: {pct(largest)}\n"
        f"Dev Holding: {pct(dev_pct)}\n"
        f"Holder Entries: {holder_count}\n\n"
        f"Contract: {mint}\n\n"
        f"DEX: {dex_url}\n"
        f"RugCheck: https://rugcheck.xyz/tokens/{mint}\n"
        f"Pump.fun: https://pump.fun/coin/{mint}"
    )


def main():

    log("DEX SCREENER V10 STARTED")

    if not BOT_TOKEN or not CHAT_ID:
        log("ERROR: TELEGRAM SECRETS MISSING")
        return

    seen = load_seen()

    profiles = get_solana_profiles()

    log(f"Solana profiles: {len(profiles)}")

    checked = set()
    alerts = 0

    for profile in profiles:

        mint = profile.get("tokenAddress")

        if not mint or mint in checked:
            continue

        checked.add(mint)

        pairs = get_pairs(mint)

        if not pairs:
            continue

        pairs = sorted(
            [
                p for p in pairs
                if isinstance(p, dict)
                and p.get("chainId") == "solana"
            ],
            key=lambda p: number(
                (p.get("liquidity") or {}).get("usd")
            ),
            reverse=True
        )

        if not pairs:
            continue

        pair = pairs[0]

        pair_id = pair.get("pairAddress") or mint

        if pair_id in seen:
            continue

        passed, reason = passes_filters(pair)

        if not passed:
            continue

        report = get_rugcheck(mint)

        message = make_alert(
            pair,
            mint,
            report
        )

        if send_telegram(message):
            seen.add(pair_id)
            alerts += 1
            log(f"ALERT SENT: {mint}")
        else:
            log(f"ALERT FAILED: {mint}")

        time.sleep(0.25)

    save_seen(seen)

    log(
        f"DEX SCREENER V10 FINISHED | ALERTS: {alerts}"
    )


if __name__ == "__main__":
    main()
