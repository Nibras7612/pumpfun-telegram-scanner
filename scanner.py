import os
import json
import asyncio
import time
import requests
import websockets

# ==========================================
# PUMP.FUN FILTERED SCANNER V4
# Pending tokens are saved between GitHub Actions runs.
# ==========================================

MIN_MARKET_CAP = 5000
MAX_MARKET_CAP = 50000
MIN_LIQUIDITY = 5000
MAX_POOL_AGE_MINUTES = 10
MAX_RUGCHECK_SCORE = 20
MAX_TOP_HOLDER_PCT = 10
MAX_TOP_10_PCT = 35

RUN_SECONDS = 220
POOL_WAIT_SECONDS = 210
POLL_INTERVAL = 12
MAX_PENDING = 80
MAX_TOTAL_WAIT_SECONDS = 30 * 60

PENDING_FILE = "pending_tokens.json"

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

PUMPPORTAL_WS = "wss://pumpportal.fun/api/data"
DEX_URL = "https://api.dexscreener.com/token-pairs/v1/solana/{}"
RUG_REPORT_URL = "https://api.rugcheck.xyz/v1/tokens/{}/report"
RUG_SUMMARY_URL = "https://api.rugcheck.xyz/v1/tokens/{}/report/summary"

session = requests.Session()
session.headers.update({"User-Agent": "PumpFun-Filtered-Scanner-V4"})

pending = {}
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
    print("V4 TELEGRAM ALERT SENT")


def load_pending():
    if not os.path.exists(PENDING_FILE):
        print("V4: No saved pending-token file yet")
        return

    try:
        with open(PENDING_FILE, "r", encoding="utf-8") as file:
            saved = json.load(file)

        if not isinstance(saved, dict):
            print("V4 WARNING: pending_tokens.json format invalid")
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
                print("V4 EXPIRED: saved token reached 30-minute limit", mint)
                continue

            pending[mint] = {
                "token": token,
                "first_seen": first_seen
            }

        print("V4 RESTORED PENDING TOKENS:", len(pending))

    except Exception as error:
        print("V4 WARNING: Could not load pending tokens:", str(error))


def save_pending():
    try:
        temporary_file = PENDING_FILE + ".tmp"

        with open(temporary_file, "w", encoding="utf-8") as file:
            json.dump(pending, file, ensure_ascii=False, indent=2)

        os.replace(temporary_file, PENDING_FILE)
        print("V4 SAVED PENDING TOKENS:", len(pending))

    except Exception as error:
        print("V4 ERROR SAVING PENDING TOKENS:", str(error))
        raise


def check_coin(token):
    mint = token.get("mint")

    if not mint:
        return "reject", None

    # 1. DEX POOL CHECK
    try:
        pairs = get_json(DEX_URL.format(mint))
    except Exception as error:
        print("V4 WAIT: DEX API error", mint, str(error))
        return "wait", None

    if not isinstance(pairs, list) or not pairs:
        print("V4 WAIT: Pool not created yet", mint)
        return "wait", None

    pairs = [
        pair for pair in pairs
        if pair.get("chainId") == "solana"
        and pair.get("baseToken", {}).get("address") == mint
    ]

    if not pairs:
        print("V4 WAIT: Matching pool not found", mint)
        return "wait", None

    pair = max(
        pairs,
        key=lambda item: (item.get("liquidity") or {}).get("usd") or 0
    )

    market_cap = pair.get("marketCap")
    liquidity = (pair.get("liquidity") or {}).get("usd")
    created_at = pair.get("pairCreatedAt")

    if market_cap is None or liquidity is None or created_at is None:
        print("V4 WAIT: Pool data incomplete", mint)
        return "wait", None

    try:
        market_cap = float(market_cap)
        liquidity = float(liquidity)
        pool_age = (time.time() * 1000 - float(created_at)) / 60000
    except (TypeError, ValueError):
        print("V4 WAIT: Invalid pool data", mint)
        return "wait", None

    if not MIN_MARKET_CAP <= market_cap <= MAX_MARKET_CAP:
        print("V4 REJECT: Market cap", mint)
        return "reject", None

    if liquidity < MIN_LIQUIDITY:
        print("V4 REJECT: Liquidity", mint)
        return "reject", None

    if pool_age < 0 or pool_age > MAX_POOL_AGE_MINUTES:
        print("V4 REJECT: Pool age", mint)
        return "reject", None

    # 2. RUGCHECK
    try:
        report = get_json(RUG_REPORT_URL.format(mint))
        summary = get_json(RUG_SUMMARY_URL.format(mint))
    except Exception as error:
        print("V4 WAIT: RugCheck API error", mint, str(error))
        return "wait", None

    if not isinstance(report, dict) or not isinstance(summary, dict):
        return "wait", None

    score = summary.get("score_normalised")
    if score is None:
        print("V4 WAIT: RugCheck score unavailable", mint)
        return "wait", None

    try:
        score = float(score)
    except (TypeError, ValueError):
        return "wait", None

    if score > MAX_RUGCHECK_SCORE:
        print("V4 REJECT: RugCheck score", mint)
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
        print("V4 WAIT: Mint authority data unavailable", mint)
        return "wait", None

    if "freezeAuthority" not in report and "freezeAuthority" not in token_info:
        print("V4 WAIT: Freeze authority data unavailable", mint)
        return "wait", None

    if mint_authority is not None or freeze_authority is not None:
        print("V4 REJECT: Active authority", mint)
        return "reject", None

    # 4. HOLDER CHECK
    holders = report.get("topHolders")

    if not isinstance(holders, list) or len(holders) < 10:
        print("V4 WAIT: Holder data incomplete", mint)
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
        print("V4 REJECT: Top holder percentage", mint)
        return "reject", None

    if top_10_total >= MAX_TOP_10_PCT:
        print("V4 REJECT: Top 10 holder percentage", mint)
        return "reject", None

    # 5. ALL FILTERS PASSED
    name = token.get("name") or pair.get("baseToken", {}).get("name") or "Unknown"
    symbol = token.get("symbol") or pair.get("baseToken", {}).get("symbol") or "Unknown"

    message = (
        "🟢 PUMP.FUN SCANNER V4 - PASSED\n\n"
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

    print("V4 WATCHING:", mint)

    try:
        while time.time() < deadline:
            if mint not in pending:
                return

            async with semaphore:
                status, message = await asyncio.to_thread(check_coin, token)

            if status == "passed":
                try:
                    await asyncio.to_thread(send_telegram, message)
                    pending.pop(mint, None)
                    print("V4 PASSED AND REMOVED:", mint)
                except Exception as error:
                    print("V4 TELEGRAM ERROR; WILL RETRY:", mint, str(error))
                return

            if status == "reject":
                pending.pop(mint, None)
                print("V4 REMOVED REJECTED TOKEN:", mint)
                return

            await asyncio.sleep(POLL_INTERVAL)

        if mint in pending:
            if time.time() >= first_seen + MAX_TOTAL_WAIT_SECONDS:
                pending.pop(mint, None)
                print("V4 EXPIRED AFTER 30 MINUTES:", mint)
            else:
                print("V4 SAVED FOR NEXT RUN:", mint)

    except asyncio.CancelledError:
        print("V4 WATCH PAUSED FOR NEXT RUN:", mint)
        raise


async def main():
    print("PUMP.FUN FILTERED SCANNER V4 STARTED")
    load_pending()

    run_end = time.time() + RUN_SECONDS
    tasks = {}

    def start_watch(mint):
        if mint in tasks or mint not in pending:
            return
        if len(tasks) >= MAX_PENDING:
            print("V4 PENDING LIMIT REACHED")
            return

        task = asyncio.create_task(watch_token(mint, run_end))
        tasks[mint] = task

        def remove_finished(done_task, token_mint=mint):
            if tasks.get(token_mint) is done_task:
                tasks.pop(token_mint, None)

        task.add_done_callback(remove_finished)

    # Resume tokens saved by the previous GitHub Actions run.
    for mint in list(pending.keys()):
        start_watch(mint)

    try:
        async with websockets.connect(
            PUMPPORTAL_WS,
            ping_interval=20,
            ping_timeout=20,
            close_timeout=10
        ) as ws:

            await ws.send(json.dumps({"method": "subscribeNewToken"}))
            await ws.send(json.dumps({"method": "subscribeMigration"}))

            print("V4 SUBSCRIBED TO NEW TOKENS")
            print("V4 SUBSCRIBED TO MIGRATIONS")

            while time.time() < run_end - 8:
                try:
                    raw = await asyncio.wait_for(
                        ws.recv(),
                        timeout=max(1, run_end - time.time() - 8)
                    )

                    data = json.loads(raw)
                    if not isinstance(data, dict):
                        continue

                    mint = data.get("mint")
                    if not mint or mint in pending:
                        continue

                    if len(pending) >= MAX_PENDING:
                        print("V4 PENDING LIMIT REACHED; SKIPPING:", mint)
                        continue

                    pending[mint] = {
                        "token": data,
                        "first_seen": time.time()
                    }
                    print("V4 NEW TOKEN SAVED:", mint)
                    start_watch(mint)

                except asyncio.TimeoutError:
                    break

    except Exception as error:
        print("V4 WEBSOCKET ERROR:", str(error))

    # Give active checks time to finish; unfinished tokens stay in pending.
    remaining = max(0, run_end - time.time() - 3)
    if tasks and remaining:
        await asyncio.wait(list(tasks.values()), timeout=remaining)

    for task in tasks.values():
        if not task.done():
            task.cancel()

    if tasks:
        await asyncio.gather(*tasks.values(), return_exceptions=True)

    save_pending()
    print("FILTERED SCANNER V4 FINISHED")


if __name__ == "__main__":
    if not TELEGRAM_TOKEN or not CHAT_ID:
        raise RuntimeError("Telegram secrets missing")

    asyncio.run(main())
