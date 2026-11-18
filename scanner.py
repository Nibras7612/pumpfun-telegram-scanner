
import os
import json
import asyncio
import time
import requests
import websockets

# ==========================================
# PUMP.FUN FILTERED SCANNER V5
# ==========================================

MIN_MARKET_CAP = 5000
MAX_MARKET_CAP = 100000

MIN_LIQUIDITY = 3000
MAX_POOL_AGE_MINUTES = 300

MAX_RUGCHECK_SCORE = 40
MAX_TOP_HOLDER_PCT = 15
MAX_TOP_10_PCT = 45

RUN_SECONDS = 220
POOL_WAIT_SECONDS = 210
POLL_INTERVAL = 12

MAX_PENDING = 80
MAX_TOTAL_WAIT_SECONDS = 5 * 60 * 60

PENDING_FILE = "pending_tokens.json"

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

PUMPPORTAL_WS = "wss://pumpportal.fun/api/data"
DEX_URL = "https://api.dexscreener.com/token-pairs/v1/solana/{}"
RUG_REPORT_URL = "https://api.rugcheck.xyz/v1/tokens/{}/report"
RUG_SUMMARY_URL = "https://api.rugcheck.xyz/v1/tokens/{}/report/summary"

session = requests.Session()
session.headers.update({"User-Agent": "PumpFun-Filtered-Scanner-V5"})

pending = {}
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
    print("V5 TELEGRAM ALERT SENT")


def load_pending():
    if not os.path.exists(PENDING_FILE):
        print("V5: No saved pending tokens")
        return

    try:
        with open(PENDING_FILE, "r", encoding="utf-8") as file:
            saved = json.load(file)

        if not isinstance(saved, dict):
            return

        now = time.time()

        for mint, item in saved.items():
            if not isinstance(item, dict):
                continue

            token = item.get("token")
            first_seen = item.get("first_seen", now)

            if not isinstance(token, dict):
                continue

            try:
                first_seen = float(first_seen)
            except (TypeError, ValueError):
                first_seen = now

            if now - first_seen >= MAX_TOTAL_WAIT_SECONDS:
                continue

            pending[mint] = {
                "token": token,
                "first_seen": first_seen
            }

        print("V5 RESTORED:", len(pending))

    except Exception as error:
        print("V5 LOAD ERROR:", str(error))


def save_pending():
    temporary_file = PENDING_FILE + ".tmp"

    with open(temporary_file, "w", encoding="utf-8") as file:
        json.dump(pending, file, ensure_ascii=False, indent=2)

    os.replace(temporary_file, PENDING_FILE)
    print("V5 SAVED PENDING:", len(pending))


def check_coin(token):
    mint = token.get("mint")

    if not mint:
        return "reject", None

    # 1. DEXSCREENER CHECK

    try:
        pairs = get_json(DEX_URL.format(mint))
    except Exception as error:
        print("V5 DEX WAIT:", mint, str(error))
        return "wait", None

    if not isinstance(pairs, list) or not pairs:
        return "wait", None

    pairs = [
        pair for pair in pairs
        if pair.get("chainId") == "solana"
        and pair.get("baseToken", {}).get("address") == mint
    ]

    if not pairs:
        return "wait", None

    pair = max(
        pairs,
        key=lambda item: (item.get("liquidity") or {}).get("usd") or 0
    )

    market_cap = pair.get("marketCap")
    liquidity = (pair.get("liquidity") or {}).get("usd")
    created_at = pair.get("pairCreatedAt")

    if market_cap is None or liquidity is None or created_at is None:
        return "wait", None

    try:
        market_cap = float(market_cap)
        liquidity = float(liquidity)
        pool_age = (time.time() * 1000 - float(created_at)) / 60000
    except (TypeError, ValueError):
        return "wait", None

    if not MIN_MARKET_CAP <= market_cap <= MAX_MARKET_CAP:
        return "reject", None

    if liquidity < MIN_LIQUIDITY:
        return "reject", None

    if pool_age < 0 or pool_age > MAX_POOL_AGE_MINUTES:
        return "reject", None

    # 2. RUGCHECK

    try:
        report = get_json(RUG_REPORT_URL.format(mint))
        summary = get_json(RUG_SUMMARY_URL.format(mint))
    except Exception as error:
        print("V5 RUGCHECK WAIT:", mint, str(error))
        return "wait", None

    if not isinstance(report, dict) or not isinstance(summary, dict):
        return "wait", None

    score = summary.get("score_normalised")

    if score is None:
        return "wait", None

    try:
        score = float(score)
    except (TypeError, ValueError):
        return "wait", None

    if score > MAX_RUGCHECK_SCORE:
        return "reject", None

    # 3. MINT AND FREEZE AUTHORITY

    token_info = report.get("token") or {}

    mint_authority = report.get("mintAuthority")
    if mint_authority is None:
        mint_authority = token_info.get("mintAuthority")

    freeze_authority = report.get("freezeAuthority")
    if freeze_authority is None:
        freeze_authority = token_info.get("freezeAuthority")

    if "mintAuthority" not in report and "mintAuthority" not in token_info:
        return "wait", None

    if "freezeAuthority" not in report and "freezeAuthority" not in token_info:
        return "wait", None

    if mint_authority is not None or freeze_authority is not None:
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

    except (ValueError, TypeError, AttributeError):
        return "wait", None

    biggest_holder = max(percentages)
    top_10_total = sum(percentages)

    if biggest_holder >= MAX_TOP_HOLDER_PCT:
        return "reject", None

    if top_10_total >= MAX_TOP_10_PCT:
        return "reject", None

    # 5. ALL FILTERS PASSED

    name = token.get("name") or pair.get("baseToken", {}).get("name") or "Unknown"
    symbol = token.get("symbol") or pair.get("baseToken", {}).get("symbol") or "Unknown"

    message = (
        "🟢 PUMP.FUN SCANNER V5 - PASSED\n\n"
        f"Name: {name}\n"
        f"Symbol: {symbol}\n"
        f"Market Cap: ${market_cap:,.0f}\n"
        f"Liquidity: ${liquidity:,.0f}\n"
        f"Pool Age: {pool_age:.1f} minutes\n"
        f"RugCheck Score: {score:.0f}\n"
        f"Biggest Holder: {biggest_holder:.2f}%\n"
        f"Top 10 Holders: {top_10_total:.2f}%\n"
        "Mint Authority: Revoked\n"
        "Freeze Authority: Revoked\n\n"
        f"Contract:\n{mint}\n\n"
        f"https://pump.fun/coin/{mint}\n"
        f"https://dexscreener.com/solana/{mint}"
    )

    return "passed", message


async def watch_token(mint, run_end):
    item = pending.get(mint)

    if not item:
        return

    token = item["token"]
    first_seen = item["first_seen"]

    deadline = min(
        time.time() + POOL_WAIT_SECONDS,
        run_end - 5,
        first_seen + MAX_TOTAL_WAIT_SECONDS
    )

    print("V5 WATCHING:", mint)

    try:
        while time.time() < deadline:

            if mint not in pending:
                return

            async with semaphore:
                status, message = await asyncio.to_thread(
                    check_coin, token
                )

            if status == "passed":
                try:
                    await asyncio.to_thread(send_telegram, message)
                    pending.pop(mint, None)
                    print("V5 PASSED:", mint)
                except Exception as error:
                    print("V5 TELEGRAM RETRY:", str(error))
                return

            if status == "reject":
                pending.pop(mint, None)
                print("V5 REJECTED:", mint)
                return

            await asyncio.sleep(POLL_INTERVAL)

        if mint in pending:
            if time.time() >= first_seen + MAX_TOTAL_WAIT_SECONDS:
                pending.pop(mint, None)
                print("V5 EXPIRED:", mint)
            else:
                print("V5 SAVED FOR NEXT RUN:", mint)

    except asyncio.CancelledError:
        print("V5 WATCH PAUSED:", mint)
        raise


async def main():
    print("PUMP.FUN FILTERED SCANNER V5 STARTED")

    load_pending()

    run_end = time.time() + RUN_SECONDS
    tasks = {}

    def start_watch(mint):
        if mint in tasks or mint not in pending:
            return

        if len(tasks) >= MAX_PENDING:
            return

        task = asyncio.create_task(
            watch_token(mint, run_end)
        )

        tasks[mint] = task

        def remove_finished(done_task, token_mint=mint):
            if tasks.get(token_mint) is done_task:
                tasks.pop(token_mint, None)

        task.add_done_callback(remove_finished)

    for mint in list(pending.keys()):
        start_watch(mint)

    try:
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

            print("V5 SUBSCRIBED TO NEW TOKENS")
            print("V5 SUBSCRIBED TO MIGRATIONS")

            while time.time() < run_end - 8:

                try:
                    raw = await asyncio.wait_for(
                        ws.recv(),
                        timeout=max(
                            1,
                            run_end - time.time() - 8
                        )
                    )

                    data = json.loads(raw)

                    if not isinstance(data, dict):
                        continue

                    mint = data.get("mint")

                    if not mint or mint in pending:
                        continue

                    if len(pending) >= MAX_PENDING:
                        print("V5 PENDING LIMIT REACHED")
                        continue

                    pending[mint] = {
                        "token": data,
                        "first_seen": time.time()
                    }

                    print("V5 NEW TOKEN:", mint)

                    start_watch(mint)

                except asyncio.TimeoutError:
                    break

    except Exception as error:
        print("V5 WEBSOCKET ERROR:", str(error))

    remaining = max(
        0,
        run_end - time.time() - 3
    )

    if tasks and remaining:
        await asyncio.wait(
            list(tasks.values()),
            timeout=remaining
        )

    for task in list(tasks.values()):
        if not task.done():
            task.cancel()

    if tasks:
        await asyncio.gather(
            *list(tasks.values()),
            return_exceptions=True
        )

    save_pending()

    print("FILTERED SCANNER V5 FINISHED")


if __name__ == "__main__":

    if not TELEGRAM_TOKEN or not CHAT_ID:
        raise RuntimeError("Telegram secrets missing")

    asyncio.run(main())
