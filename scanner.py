
import os
import json
import asyncio
import time
import requests
import websockets

# ==========================================
# PUMP.FUN FILTERED SCANNER V3
# ==========================================

MIN_MARKET_CAP = 5000
MAX_MARKET_CAP = 50000
MIN_LIQUIDITY = 5000
MAX_POOL_AGE_MINUTES = 10
MAX_RUGCHECK_SCORE = 20
MAX_TOP_HOLDER_PCT = 10
MAX_TOP_10_PCT = 35

RUN_SECONDS = 230
POOL_WAIT_SECONDS = 180
POLL_INTERVAL = 12
MAX_PENDING = 80

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

PUMPPORTAL_WS = "wss://pumpportal.fun/api/data"
DEX_URL = "https://api.dexscreener.com/token-pairs/v1/solana/{}"
RUG_REPORT_URL = "https://api.rugcheck.xyz/v1/tokens/{}/report"
RUG_SUMMARY_URL = "https://api.rugcheck.xyz/v1/tokens/{}/report/summary"

session = requests.Session()
session.headers.update({"User-Agent": "PumpFun-Filtered-Scanner-V3"})

checked = set()
active = set()
semaphore = asyncio.Semaphore(6)


def get_json(url):
    response = session.get(url, timeout=12)
    response.raise_for_status()
    return response.json()


def send_telegram(message):
    if not TELEGRAM_TOKEN or not CHAT_ID:
        raise RuntimeError("Telegram secrets missing")

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"

    response = requests.post(
        url,
        json={"chat_id": CHAT_ID, "text": message},
        timeout=15
    )
    response.raise_for_status()
    print("V3 TELEGRAM ALERT SENT")


def check_coin(token):
    mint = token.get("mint")

    if not mint:
        return "reject", None

    # 1. DEX POOL CHECK
    try:
        pairs = get_json(DEX_URL.format(mint))
    except Exception as error:
        print("V3 WAIT: DEX API error", mint, str(error))
        return "wait", None

    if not isinstance(pairs, list) or not pairs:
        print("V3 WAIT: Pool not created yet", mint)
        return "wait", None

    pairs = [
        p for p in pairs
        if p.get("chainId") == "solana"
        and p.get("baseToken", {}).get("address") == mint
    ]

    if not pairs:
        print("V3 WAIT: Matching pool not found", mint)
        return "wait", None

    pair = max(
        pairs,
        key=lambda p: (p.get("liquidity") or {}).get("usd") or 0
    )

    market_cap = pair.get("marketCap")
    liquidity = (pair.get("liquidity") or {}).get("usd")
    created_at = pair.get("pairCreatedAt")

    if market_cap is None or liquidity is None or created_at is None:
        return "wait", None

    market_cap = float(market_cap)
    liquidity = float(liquidity)

    pool_age = (
        time.time() * 1000 - float(created_at)
    ) / 60000

    if not MIN_MARKET_CAP <= market_cap <= MAX_MARKET_CAP:
        print("V3 REJECT: Market cap", mint)
        return "reject", None

    if liquidity < MIN_LIQUIDITY:
        print("V3 REJECT: Liquidity", mint)
        return "reject", None

    if pool_age < 0 or pool_age > MAX_POOL_AGE_MINUTES:
        print("V3 REJECT: Pool age", mint)
        return "reject", None

    # 2. RUGCHECK
    try:
        report = get_json(RUG_REPORT_URL.format(mint))
        summary = get_json(RUG_SUMMARY_URL.format(mint))
    except Exception as error:
        print("V3 WAIT: RugCheck API error", mint, str(error))
        return "wait", None

    if not isinstance(report, dict) or not isinstance(summary, dict):
        return "wait", None

    score = summary.get("score_normalised")

    if score is None:
        return "wait", None

    if float(score) > MAX_RUGCHECK_SCORE:
        print("V3 REJECT: RugCheck score", mint)
        return "reject", None

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
        return "reject", None

    if (
        "freezeAuthority" not in report
        and "freezeAuthority" not in token_info
    ):
        return "reject", None

    if mint_authority is not None or freeze_authority is not None:
        print("V3 REJECT: Active authority", mint)
        return "reject", None

    # 4. HOLDER CHECK
    holders = report.get("topHolders")

    if not isinstance(holders, list) or len(holders) < 10:
        return "wait", None

    percentages = []

    try:
        for holder in holders[:10]:
            pct = holder.get("pct")
            if pct is None:
                return "wait", None
            percentages.append(float(pct))
    except (ValueError, TypeError):
        return "wait", None

    biggest_holder = max(percentages)
    top_10_total = sum(percentages)

    if biggest_holder >= MAX_TOP_HOLDER_PCT:
        return "reject", None

    if top_10_total >= MAX_TOP_10_PCT:
        return "reject", None

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
        "🟢 PUMP.FUN SCANNER V3 - PASSED\n\n"
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
        f"https://pump.fun/coin/{mint}\n"
        f"https://dexscreener.com/solana/{mint}"
    )

    return "passed", message


async def watch_token(token, run_end):
    mint = token.get("mint")

    if not mint:
        return

    try:
        deadline = min(
            time.time() + POOL_WAIT_SECONDS,
            run_end - 3
        )

        print("V3 WATCHING:", mint)

        while time.time() < deadline:

            async with semaphore:
                status, message = await asyncio.to_thread(
                    check_coin, token
                )

            if status == "passed":
                try:
                    await asyncio.to_thread(send_telegram, message)
                    checked.add(mint)
                except Exception as error:
                    print("V3 TELEGRAM ERROR:", str(error))
                return

            if status == "reject":
                checked.add(mint)
                return

            await asyncio.sleep(POLL_INTERVAL)

        print("V3 WAIT EXPIRED:", mint)

    finally:
        active.discard(mint)


async def main():
    print("PUMP.FUN FILTERED SCANNER V3 STARTED")

    run_end = time.time() + RUN_SECONDS
    tasks = set()

    async with websockets.connect(
        PUMPPORTAL_WS,
        ping_interval=20,
        ping_timeout=20,
        close_timeout=10
    ) as ws:

        await ws.send(json.dumps({
            "method": "subscribeNewToken"
        }))

        await ws.send(json.dumps({
            "method": "subscribeMigration"
        }))

        print("V3 SUBSCRIBED TO NEW TOKENS")
        print("V3 SUBSCRIBED TO MIGRATIONS")

        while time.time() < run_end - 5:

            try:
                raw = await asyncio.wait_for(
                    ws.recv(),
                    timeout=max(1, run_end - time.time() - 5)
                )

                data = json.loads(raw)

                if not isinstance(data, dict):
                    continue

                mint = data.get("mint")

                if not mint:
                    continue

                if mint in checked or mint in active:
                    continue

                if len(active) >= MAX_PENDING:
                    print("V3 PENDING LIMIT REACHED")
                    continue

                active.add(mint)

                task = asyncio.create_task(
                    watch_token(data, run_end)
                )

                tasks.add(task)
                task.add_done_callback(tasks.discard)

            except asyncio.TimeoutError:
                break

            except Exception as error:
                print("V3 WEBSOCKET ERROR:", str(error))
                break

    if tasks:
        remaining = max(0, run_end - time.time() - 2)

        if remaining:
            _, pending = await asyncio.wait(
                tasks,
                timeout=remaining
            )

            for task in pending:
                task.cancel()

    print("FILTERED SCANNER V3 FINISHED")


if __name__ == "__main__":
    if not TELEGRAM_TOKEN or not CHAT_ID:
        raise RuntimeError("Telegram secrets missing")

    asyncio.run(main())
