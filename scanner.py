
import os
import json
import asyncio
import time
import requests
import websockets

# ==================================
# FILTERED PUMP.FUN SCANNER V2
# ==================================

MIN_MARKET_CAP = 5000
MAX_MARKET_CAP = 50000
MIN_LIQUIDITY = 5000
MAX_POOL_AGE_MINUTES = 10
MAX_RUGCHECK_SCORE = 20
MAX_TOP_HOLDER_PCT = 10
MAX_TOP_10_PCT = 35

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

PUMPPORTAL_WS = "wss://pumpportal.fun/api/data"
DEX_URL = "https://api.dexscreener.com/token-pairs/v1/solana/{}"
RUG_REPORT_URL = "https://api.rugcheck.xyz/v1/tokens/{}/report"
RUG_SUMMARY_URL = "https://api.rugcheck.xyz/v1/tokens/{}/report/summary"

session = requests.Session()
session.headers.update({"User-Agent": "PumpFun-Filtered-Scanner-V2"})

checked = set()


def get_json(url):
    response = session.get(url, timeout=15)
    response.raise_for_status()
    return response.json()


def send_telegram(message):
    if not TELEGRAM_TOKEN or not CHAT_ID:
        raise RuntimeError("Telegram secrets missing")

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"

    response = requests.post(
        url,
        json={
            "chat_id": CHAT_ID,
            "text": message
        },
        timeout=15
    )

    response.raise_for_status()
    print("V2 Telegram alert sent")


def check_coin(token):
    mint = token.get("mint")

    if not mint:
        return None

    try:
        # 1. DEXSCREENER CHECK
        pairs = get_json(DEX_URL.format(mint))

        if not isinstance(pairs, list) or not pairs:
            print("V2 SKIP: No DEX pool", mint)
            return None

        pairs = [
            p for p in pairs
            if p.get("chainId") == "solana"
            and p.get("baseToken", {}).get("address") == mint
        ]

        if not pairs:
            print("V2 SKIP: No matching pool", mint)
            return None

        pair = max(
            pairs,
            key=lambda p: (p.get("liquidity") or {}).get("usd") or 0
        )

        market_cap = pair.get("marketCap")
        liquidity = (pair.get("liquidity") or {}).get("usd")
        created_at = pair.get("pairCreatedAt")

        if market_cap is None or liquidity is None or created_at is None:
            print("V2 SKIP: Missing DEX data", mint)
            return None

        market_cap = float(market_cap)
        liquidity = float(liquidity)

        pool_age = (
            time.time() * 1000 - float(created_at)
        ) / 60000

        if not MIN_MARKET_CAP <= market_cap <= MAX_MARKET_CAP:
            print("V2 SKIP: Market cap", mint)
            return None

        if liquidity < MIN_LIQUIDITY:
            print("V2 SKIP: Liquidity", mint)
            return None

        if pool_age < 0 or pool_age > MAX_POOL_AGE_MINUTES:
            print("V2 SKIP: Pool age", mint)
            return None

        # 2. RUGCHECK
        report = get_json(RUG_REPORT_URL.format(mint))
        summary = get_json(RUG_SUMMARY_URL.format(mint))

        if not isinstance(report, dict) or not isinstance(summary, dict):
            print("V2 SKIP: Invalid RugCheck data", mint)
            return None

        score = summary.get("score_normalised")

        if score is None or float(score) > MAX_RUGCHECK_SCORE:
            print("V2 SKIP: RugCheck score", mint)
            return None

        # 3. MINT AND FREEZE AUTHORITY
        token_info = report.get("token") or {}

        mint_authority = report.get("mintAuthority")
        if mint_authority is None:
            mint_authority = token_info.get("mintAuthority")

        freeze_authority = report.get("freezeAuthority")
        if freeze_authority is None:
            freeze_authority = token_info.get("freezeAuthority")

        if (
            "mintAuthority" not in report
            and "mintAuthority" not in token_info
        ):
            print("V2 SKIP: Mint authority unknown", mint)
            return None

        if (
            "freezeAuthority" not in report
            and "freezeAuthority" not in token_info
        ):
            print("V2 SKIP: Freeze authority unknown", mint)
            return None

        if mint_authority is not None or freeze_authority is not None:
            print("V2 SKIP: Active authority", mint)
            return None

        # 4. HOLDER CHECK
        holders = report.get("topHolders")

        if not isinstance(holders, list) or len(holders) < 10:
            print("V2 SKIP: Holder data incomplete", mint)
            return None

        percentages = []

        for holder in holders[:10]:
            pct = holder.get("pct")

            if pct is None:
                print("V2 SKIP: Holder percentage missing", mint)
                return None

            percentages.append(float(pct))

        biggest_holder = max(percentages)
        top_10_total = sum(percentages)

        if biggest_holder >= MAX_TOP_HOLDER_PCT:
            print("V2 SKIP: Biggest holder concentration", mint)
            return None

        if top_10_total >= MAX_TOP_10_PCT:
            print("V2 SKIP: Top 10 concentration", mint)
            return None

        # 5. PASSED FILTERS
        name = (
            token.get("name")
            or pair.get("baseToken", {}).get("name")
            or "Unknown"
        )

        symbol = (
            token.get("symbol")
            or pair.get("baseToken", {}).get("symbol")
            or "Unknown"
        )

        message = (
            "🟢 FILTERED SCANNER V2 - PASSED\n\n"
            f"Name: {name}\n"
            f"Symbol: {symbol}\n"
            f"Market Cap: ${market_cap:,.0f}\n"
            f"Liquidity: ${liquidity:,.0f}\n"
            f"Pool Age: {pool_age:.1f} minutes\n"
            f"RugCheck Score: {score}\n"
            f"Biggest Holder: {biggest_holder:.2f}%\n"
            f"Top 10 Holders: {top_10_total:.2f}%\n"
            "Mint Authority: Revoked\n"
            "Freeze Authority: Revoked\n\n"
            f"Contract:\n{mint}\n\n"
            f"https://pump.fun/coin/{mint}"
        )

        return message

    except Exception as error:
        print("V2 SKIP: API/data error", mint, str(error))
        return None


async def handle_token(token):
    mint = token.get("mint")

    if not mint or mint in checked:
        return

    checked.add(mint)

    message = await asyncio.to_thread(check_coin, token)

    if message:
        try:
            await asyncio.to_thread(send_telegram, message)
        except Exception as error:
            print("V2 Telegram error:", str(error))


async def main():
    print("FILTERED SCANNER V2 STARTED")

    async with websockets.connect(
        PUMPPORTAL_WS,
        ping_interval=20,
        ping_timeout=20,
        close_timeout=10
    ) as ws:

        await ws.send(
            json.dumps({"method": "subscribeNewToken"})
        )

        print("V2 subscribed to Pump.fun")

        tasks = set()
        end_time = time.time() + 190

        while time.time() < end_time:
            try:
                raw = await asyncio.wait_for(
                    ws.recv(),
                    timeout=max(1, end_time - time.time())
                )

                data = json.loads(raw)

                if data.get("txType") != "create":
                    continue

                task = asyncio.create_task(handle_token(data))
                tasks.add(task)
                task.add_done_callback(tasks.discard)

            except asyncio.TimeoutError:
                break

        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    print("FILTERED SCANNER V2 FINISHED")


if __name__ == "__main__":
    if not TELEGRAM_TOKEN or not CHAT_ID:
        raise RuntimeError("Telegram secrets missing")

    asyncio.run(main())

