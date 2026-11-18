
import os
import json
import asyncio
import time
import requests
import websockets

# ---------- SETTINGS ----------
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
session.headers.update({"User-Agent": "PumpFun-Scanner/1.0"})

checked = set()


def get_json(url):
    response = session.get(url, timeout=15)
    response.raise_for_status()
    return response.json()


def send_telegram(message):
    if not TELEGRAM_TOKEN or not CHAT_ID:
        print("ERROR: Telegram secrets are missing")
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    response = requests.post(
        url,
        json={"chat_id": CHAT_ID, "text": message},
        timeout=15
    )
    response.raise_for_status()
    print("Telegram alert sent")


def check_coin(token):
    mint = token.get("mint")
    if not mint:
        return None

    try:
        # 1. DexScreener pool and market data
        pairs = get_json(DEX_URL.format(mint))
        if not isinstance(pairs, list) or not pairs:
            print("SKIP: No DEX pool:", mint)
            return None

        pairs = [
            p for p in pairs
            if p.get("chainId") == "solana"
            and p.get("baseToken", {}).get("address") == mint
        ]
        if not pairs:
            print("SKIP: No matching Solana pool:", mint)
            return None

        pair = max(
            pairs,
            key=lambda p: (p.get("liquidity") or {}).get("usd") or 0
        )

        market_cap = pair.get("marketCap")
        liquidity = (pair.get("liquidity") or {}).get("usd")
        created_at = pair.get("pairCreatedAt")

        if market_cap is None or liquidity is None or not created_at:
            print("SKIP: Missing market data:", mint)
            return None

        market_cap = float(market_cap)
        liquidity = float(liquidity)
        pool_age = (time.time() * 1000 - float(created_at)) / 60000

        if not MIN_MARKET_CAP <= market_cap <= MAX_MARKET_CAP:
            print("SKIP: Market cap:", mint, market_cap)
            return None

        if liquidity < MIN_LIQUIDITY:
            print("SKIP: Low liquidity:", mint, liquidity)
            return None

        if pool_age < 0 or pool_age > MAX_POOL_AGE_MINUTES:
            print("SKIP: Pool age:", mint, pool_age)
            return None

        # 2. RugCheck full report
        report = get_json(RUG_REPORT_URL.format(mint))
        summary = get_json(RUG_SUMMARY_URL.format(mint))

        if not isinstance(report, dict) or not isinstance(summary, dict):
            print("SKIP: Invalid RugCheck data:", mint)
            return None

        score = summary.get("score_normalised")
        if score is None or float(score) > MAX_RUGCHECK_SCORE:
            print("SKIP: RugCheck score:", mint, score)
            return None

        # 3. Mint and Freeze authority
        token_info = report.get("token") or {}

        mint_authority = report.get("mintAuthority")
        if mint_authority is None:
            mint_authority = token_info.get("mintAuthority")

        freeze_authority = report.get("freezeAuthority")
        if freeze_authority is None:
            freeze_authority = token_info.get("freezeAuthority")

        # Missing authority fields are NOT treated as revoked.
        if "mintAuthority" not in report and "mintAuthority" not in token_info:
            print("SKIP: Mint authority unknown:", mint)
            return None

        if "freezeAuthority" not in report and "freezeAuthority" not in token_info:
            print("SKIP: Freeze authority unknown:", mint)
            return None

        if mint_authority is not None or freeze_authority is not None:
            print("SKIP: Authority still active:", mint)
            return None

        # 4. Holder concentration
        holders = report.get("topHolders")
        if not isinstance(holders, list) or len(holders) < 10:
            print("SKIP: Holder data incomplete:", mint)
            return None

        percentages = []
        for holder in holders[:10]:
            pct = holder.get("pct")
            if pct is None:
                print("SKIP: Holder percentage missing:", mint)
                return None
            percentages.append(float(pct))

        biggest_holder = max(percentages)
        top_10_total = sum(percentages)

        if biggest_holder >= MAX_TOP_HOLDER_PCT:
            print("SKIP: Biggest holder too high:", mint, biggest_holder)
            return None

        if top_10_total >= MAX_TOP_10_PCT:
            print("SKIP: Top 10 concentration too high:", mint, top_10_total)
            return None

        # 5. Passed every check
        name = token.get("name") or pair.get("baseToken", {}).get("name") or "Unknown"
        symbol = token.get("symbol") or pair.get("baseToken", {}).get("symbol") or "Unknown"

        return (
            f"✅ COIN PASSED ALL FILTERS\n\n"
            f"Name: {name}\n"
            f"Symbol: {symbol}\n"
            f"Market Cap: ${market_cap:,.0f}\n"
            f"Liquidity: ${liquidity:,.0f}\n"
            f"Pool Age: {pool_age:.1f} minutes\n"
            f"RugCheck Score: {score}\n"
            f"Biggest Holder: {biggest_holder:.2f}%\n"
            f"Top 10 Holders: {top_10_total:.2f}%\n"
            f"Mint Authority: Revoked\n"
            f"Freeze Authority: Revoked\n\n"
            f"Contract:\n{mint}\n\n"
            f"https://pump.fun/coin/{mint}"
        )

    except Exception as error:
        print("SKIP: API or data error:", mint, str(error))
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
            print("Telegram error:", str(error))


async def main():
    print("Pump.fun scanner started")

    async with websockets.connect(
        PUMPPORTAL_WS,
        ping_interval=20,
        ping_timeout=20,
        close_timeout=10
    ) as ws:

        await ws.send(json.dumps({"method": "subscribeNewToken"}))
        print("Subscribed to new Pump.fun tokens")

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

    print("Scanner finished")


if __name__ == "__main__":
    if not TELEGRAM_TOKEN or not CHAT_ID:
        raise RuntimeError("Add TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in GitHub Secrets")

    asyncio.run(main())
