
import os
import json
import time
import requests
from datetime import datetime, timezone

# ==========================================
# DEX SCREENER V6 - SOLANA
# ==========================================

MIN_MARKET_CAP = 5000
MAX_MARKET_CAP = 50000
MIN_LIQUIDITY = 5000
MAX_TOKEN_AGE_HOURS = 10

MIN_VOLUME_5M = 500
MIN_TXNS_5M = 5

SEEN_FILE = "seen_tokens.json"

BOT_TOKEN = (
    os.getenv("TELEGRAM_BOT_TOKEN")
    or os.getenv("BOT_TOKEN")
)
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID") or os.getenv("CHAT_ID")

BASE_URL = "https://api.dexscreener.com"

session = requests.Session()
session.headers.update({"User-Agent": "SolanaScannerV6/1.0"})


def load_seen():
    try:
        with open(SEEN_FILE, "r") as f:
            return set(json.load(f))
    except (FileNotFoundError, json.JSONDecodeError):
        return set()


def save_seen(seen):
    with open(SEEN_FILE, "w") as f:
        json.dump(sorted(seen), f)


def get_latest_profiles():
    url = f"{BASE_URL}/token-profiles/latest/v1"
    response = session.get(url, timeout=20)
    response.raise_for_status()

    data = response.json()
    return [
        item for item in data
        if item.get("chainId") == "solana"
        and item.get("tokenAddress")
    ]


def get_token_pairs(address):
    url = f"{BASE_URL}/token-pairs/v1/solana/{address}"
    response = session.get(url, timeout=20)

    if response.status_code != 200:
        return []

    data = response.json()
    if not isinstance(data, list):
        return []

    return [
        pair for pair in data
        if pair.get("chainId") == "solana"
    ]


def check_pair(pair):
    created = pair.get("pairCreatedAt")
    if not created:
        return False

    age_ms = int(time.time() * 1000) - int(created)
    age_hours = age_ms / 3600000

    if age_hours < 0 or age_hours > MAX_TOKEN_AGE_HOURS:
        return False

    market_cap = pair.get("marketCap") or pair.get("fdv") or 0
    liquidity = (pair.get("liquidity") or {}).get("usd", 0)
    volume = pair.get("volume") or {}
    txns = pair.get("txns") or {}
    m5 = txns.get("m5") or {}

    buys = m5.get("buys", 0)
    sells = m5.get("sells", 0)
    volume_5m = volume.get("m5", 0)

    if not MIN_MARKET_CAP <= market_cap <= MAX_MARKET_CAP:
        return False

    if liquidity < MIN_LIQUIDITY:
        return False

    if volume_5m < MIN_VOLUME_5M:
        return False

    if buys + sells < MIN_TXNS_5M:
        return False

    # Avoid tokens with no recent buying activity
    if buys < sells:
        return False

    return True


def send_telegram(message):
    if not BOT_TOKEN or not CHAT_ID:
        print("ERROR: Telegram secrets missing")
        return False

    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"

    try:
        response = session.post(
            url,
            data={
                "chat_id": CHAT_ID,
                "text": message,
                "disable_web_page_preview": True
            },
            timeout=20
        )
        response.raise_for_status()
        return response.json().get("ok", False)
    except requests.RequestException as error:
        print("Telegram error:", error)
        return False


def format_message(pair):
    base = pair.get("baseToken") or {}
    address = base.get("address", "")
    name = base.get("name", "Unknown")
    symbol = base.get("symbol", "Unknown")

    market_cap = pair.get("marketCap") or pair.get("fdv") or 0
    liquidity = (pair.get("liquidity") or {}).get("usd", 0)
    volume = pair.get("volume") or {}
    txns = pair.get("txns") or {}
    m5 = txns.get("m5") or {}

    age_hours = (
        time.time() * 1000 - pair["pairCreatedAt"]
    ) / 3600000

    buys = m5.get("buys", 0)
    sells = m5.get("sells", 0)

    return (
        "🚀 DEX SCREENER V6 - NEW SOLANA TOKEN\n\n"
        f"Name: {name}\n"
        f"Symbol: {symbol}\n"
        f"Market Cap: ${market_cap:,.0f}\n"
        f"Liquidity: ${liquidity:,.0f}\n"
        f"Volume (5m): ${volume.get('m5', 0):,.0f}\n"
        f"Buys (5m): {buys}\n"
        f"Sells (5m): {sells}\n"
        f"Pair Age: {age_hours:.2f} hours\n\n"
        f"Token: {address}\n"
        f"DEX Screener: {pair.get('url', '')}\n\n"
        "⚠️ Filters passed. Not a guarantee of safety."
    )


def main():
    seen = load_seen()
    new_seen = set(seen)
    alerts = 0

    print("DEX SCREENER V6 SCAN STARTED")

    try:
        profiles = get_latest_profiles()
        print("Solana profiles found:", len(profiles))

        checked_addresses = set()

        for profile in profiles:
            address = profile["tokenAddress"]

            if address in checked_addresses:
                continue
            checked_addresses.add(address)

            try:
                pairs = get_token_pairs(address)

                # Use the pair with the highest available liquidity
                pairs.sort(
                    key=lambda p: (p.get("liquidity") or {}).get("usd", 0),
                    reverse=True
                )

                for pair in pairs:
                    pair_address = pair.get("pairAddress")
                    if not pair_address or pair_address in new_seen:
                        continue

                    if check_pair(pair):
                        message = format_message(pair)

                        if send_telegram(message):
                            new_seen.add(pair_address)
                            alerts += 1
                            print("ALERT SENT:", pair_address)

                        break

            except (requests.RequestException, ValueError, TypeError, KeyError) as error:
                print("Token scan error:", address, error)

            time.sleep(0.15)

    except requests.RequestException as error:
        print("DEX Screener API error:", error)

    save_seen(new_seen)
    print("ALERTS SENT:", alerts)
    print("DEX SCREENER V6 FINISHED")


if __name__ == "__main__":
    main()
