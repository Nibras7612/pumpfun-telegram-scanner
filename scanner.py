
import os
import json
import time
import requests

# ==========================================
# DEX SCREENER V7 - ADVANCED SAFETY
# SOLANA ONLY
# ==========================================

MIN_MC = 5000
MAX_MC = 50000
MIN_LIQ = 5000
MAX_AGE_HOURS = 10
MIN_VOLUME_5M = 500
MIN_TXNS_5M = 5

MAX_RUG_SCORE = 30
MAX_TOP10_HOLDERS = 25
MAX_DEV_HOLDING = 5
MIN_LP_LOCKED = 90

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

DEX = "https://api.dexscreener.com"
RUG = "https://api.rugcheck.xyz/v1"
RPC = os.getenv(
    "SOLANA_RPC_URL",
    "https://api.mainnet-beta.solana.com"
)

SEEN_FILE = "seen_tokens.json"

session = requests.Session()
session.headers.update({"User-Agent": "SolanaScannerV7"})


def get(url):
    r = session.get(url, timeout=20)
    r.raise_for_status()
    return r.json()


def rpc(method, params):
    r = session.post(
        RPC,
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": method,
            "params": params
        },
        timeout=20
    )
    r.raise_for_status()
    data = r.json()
    if data.get("error"):
        raise RuntimeError(data["error"])
    return data.get("result")


def load_seen():
    try:
        with open(SEEN_FILE) as f:
            return set(json.load(f))
    except (OSError, ValueError):
        return set()


def save_seen(seen):
    with open(SEEN_FILE, "w") as f:
        json.dump(sorted(seen), f)


def telegram(message):
    if not BOT_TOKEN or not CHAT_ID:
        print("Telegram secrets missing")
        return False

    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"

    try:
        r = session.post(
            url,
            data={
                "chat_id": CHAT_ID,
                "text": message,
                "disable_web_page_preview": True
            },
            timeout=20
        )
        r.raise_for_status()
        return r.json().get("ok", False)
    except requests.RequestException as e:
        print("Telegram error:", e)
        return False


def get_profiles():
    data = get(f"{DEX}/token-profiles/latest/v1")
    if not isinstance(data, list):
        return []

    return [
        x for x in data
        if x.get("chainId") == "solana"
        and x.get("tokenAddress")
    ]


def get_pairs(mint):
    data = get(f"{DEX}/token-pairs/v1/solana/{mint}")
    if not isinstance(data, list):
        return []

    return [
        p for p in data
        if p.get("chainId") == "solana"
    ]


def get_rug_report(mint):
    report = get(f"{RUG}/tokens/{mint}/report")
    summary = get(f"{RUG}/tokens/{mint}/report/summary")
    return report, summary


def holder_percentage(holder):
    for key in ("pct", "percentage", "percent"):
        value = holder.get(key)
        if value is not None:
            try:
                return float(value)
            except (TypeError, ValueError):
                pass
    return None


def analyse_rug(report, summary):
    token = report.get("token") or {}
    holders = report.get("topHolders") or []
    meta = report.get("tokenMeta") or {}

    score = summary.get("score_normalised")
    if score is None:
        score = report.get("score_normalised")

    # Require all ten holder records for a Top 10 calculation.
    pcts = [holder_percentage(h) for h in holders[:10]]
    top10 = None
    if len(pcts) == 10 and all(x is not None for x in pcts):
        top10 = sum(pcts)

    dev_pct = None
    supply = token.get("supply")
    decimals = token.get("decimals")
    creator_balance = report.get("creatorBalance")

    try:
        if supply is not None and creator_balance is not None:
            supply_ui = float(supply) / (10 ** int(decimals or 0))
            if supply_ui > 0:
                dev_pct = float(creator_balance) / supply_ui * 100
    except (TypeError, ValueError, OverflowError):
        pass

    lp_locked = summary.get("lpLockedPct")
    if lp_locked is None:
        lp_locked = report.get("lpLockedPct")

    extensions = report.get("token_extensions")
    program = str(
        report.get("tokenProgram")
        or summary.get("tokenProgram")
        or ""
    )

    extension_text = json.dumps(
        extensions or {}
    ).lower()

    risky_extensions = [
        x for x in (
            "permanentdelegate",
            "transferfee",
            "transferhook",
            "defaultaccountstate"
        )
        if x in extension_text
    ]

    return {
        "score": score,
        "mint": token.get(
            "mintAuthority",
            report.get("mintAuthority")
        ),
        "freeze": token.get(
            "freezeAuthority",
            report.get("freezeAuthority")
        ),
        "top10": top10,
        "dev": dev_pct,
        "lp": lp_locked,
        "risks": report.get("risks") or summary.get("risks") or [],
        "insiders": report.get("insiderNetworks"),
        "extensions": extensions,
        "token2022": (
            "tokenz" in program.lower()
            or "token-2022" in program.lower()
        ),
        "risky_extensions": risky_extensions,
        "metadata_mutable": meta.get("mutable")
    }


def check_pair(pair):
    created = pair.get("pairCreatedAt")
    if not created:
        return None

    age = (time.time() * 1000 - int(created)) / 3600000
    mc = pair.get("marketCap") or pair.get("fdv")
    liq = (pair.get("liquidity") or {}).get("usd")
    volume = pair.get("volume") or {}
    txns = pair.get("txns") or {}
    m5 = txns.get("m5") or {}

    buys = int(m5.get("buys", 0))
    sells = int(m5.get("sells", 0))
    vol5 = float(volume.get("m5") or 0)

    checks = {
        "Pair age <= 10 hours":
            0 <= age <= MAX_AGE_HOURS,
        "Market cap range":
            mc is not None and MIN_MC <= float(mc) <= MAX_MC,
        "Liquidity >= $5,000":
            liq is not None and float(liq) >= MIN_LIQ,
        "5m volume >= $500":
            vol5 >= MIN_VOLUME_5M,
        "5m transactions >= 5":
            buys + sells >= MIN_TXNS_5M,
        "Buys >= sells":
            buys >= sells
    }

    return {
        "checks": checks,
        "age": age,
        "mc": mc,
        "liq": liq,
        "volume": vol5,
        "buys": buys,
        "sells": sells
    }


def safety_checks(rug):
    score = rug["score"]
    top10 = rug["top10"]
    dev = rug["dev"]
    lp = rug["lp"]
    insiders = rug["insiders"]

    checks = {
        "RugCheck score <= 30":
            score is not None and float(score) <= MAX_RUG_SCORE,

        "Mint authority revoked":
            rug["mint"] is None,

        "Freeze authority revoked":
            rug["freeze"] is None,

        "Top 10 holders <= 25%":
            top10 is not None and top10 <= MAX_TOP10_HOLDERS,

        "Dev holding <= 5%":
            dev is not None and dev <= MAX_DEV_HOLDING,

        "LP locked >= 90%":
            lp is not None and float(lp) >= MIN_LP_LOCKED,

        "Sniper / insider networks":
            None if insiders is None else len(insiders) == 0,

        # Aggregate DEX data cannot prove organic volume.
        "Fake volume analysis": None,

        # A real sell simulation needs a transaction simulation provider.
        "Sellability simulation": None,

        "Token-2022 extensions":
            None if rug["token2022"] and rug["extensions"] is None
            else len(rug["risky_extensions"]) == 0
    }

    return checks


def fmt(value, suffix=""):
    if value is None:
        return "N/A"
    try:
        return f"{float(value):,.2f}{suffix}"
    except (ValueError, TypeError):
        return str(value)


def make_message(pair, mint, data, rug, checks):
    base = pair.get("baseToken") or {}

    check_lines = []
    for name, passed in checks.items():
        if passed is True:
            status = "PASS"
        elif passed is False:
            status = "FAIL"
        else:
            status = "UNKNOWN"
        check_lines.append(f"{status} - {name}")

    risk_lines = []
    for risk in rug["risks"][:5]:
        risk_lines.append(
            f"- {risk.get('level', 'info')}: "
            f"{risk.get('name', 'Risk')}"
        )

    return (
        "🛡️ DEX SCREENER V7\n"
        "SOLANA ADVANCED SAFETY\n\n"
        f"Name: {base.get('name', 'Unknown')}\n"
        f"Symbol: {base.get('symbol', 'Unknown')}\n"
        f"Market Cap: ${fmt(data['mc'])}\n"
        f"Liquidity: ${fmt(data['liq'])}\n"
        f"5m Volume: ${fmt(data['volume'])}\n"
        f"Buys/Sells: {data['buys']}/{data['sells']}\n"
        f"Token Age: {fmt(data['age'], ' hours')}\n\n"
        f"RugCheck Score: {fmt(rug['score'])}\n"
        f"Top 10 Holders: {fmt(rug['top10'], '%')}\n"
        f"Dev Holding: {fmt(rug['dev'], '%')}\n"
        f"LP Locked: {fmt(rug['lp'], '%')}\n"
        f"Mint Authority: "
        f"{'REVOKED' if rug['mint'] is None else 'ACTIVE/UNKNOWN'}\n"
        f"Freeze Authority: "
        f"{'REVOKED' if rug['freeze'] is None else 'ACTIVE/UNKNOWN'}\n"
        f"Token-2022: {rug['token2022']}\n"
        f"Risky Extensions: "
        f"{', '.join(rug['risky_extensions']) or 'None detected'}\n\n"
        "SAFETY CHECKS\n"
        + "\n".join(check_lines)
        + "\n\nRUGCHECK FLAGS\n"
        + ("\n".join(risk_lines) if risk_lines else "No flags returned")
        + f"\n\nToken: {mint}"
        + f"\nDEX: {pair.get('url', '')}"
        + f"\nRugCheck: https://rugcheck.xyz/tokens/{mint}"
        + "\n\n⚠️ Checks do not guarantee safety."
    )


def main():
    seen = load_seen()
    updated = set(seen)
    sent = 0

    print("DEX SCREENER V7 STARTED")

    try:
        profiles = get_profiles()
    except Exception as e:
        print("Profile API error:", e)
        return

    processed = set()

    for profile in profiles:
        mint = profile["tokenAddress"]

        if mint in processed:
            continue
        processed.add(mint)

        try:
            pairs = get_pairs(mint)
            pairs.sort(
                key=lambda p: float(
                    (p.get("liquidity") or {}).get("usd") or 0
                ),
                reverse=True
            )

            for pair in pairs:
                pair_id = pair.get("pairAddress")

                if not pair_id or pair_id in updated:
                    continue

                try:
                    data = check_pair(pair)
                    if not data:
                        continue

                    if not all(
                        value is True
                        for value in data["checks"].values()
                    ):
                        continue

                    report, summary = get_rug_report(mint)
                    rug = analyse_rug(report, summary)
                    checks = safety_checks(rug)

                    # Reject any failed measurable check.
                    if any(value is False for value in checks.values()):
                        print("REJECTED:", mint)
                        continue

                    # Unknown advanced checks are disclosed, not called PASS.
                    # To reject all UNKNOWN checks, add:
                    # if any(value is None for value in checks.values()):
                    #     continue

                    message = make_message(
                        pair, mint, data, rug, checks
                    )

                    if telegram(message):
                        updated.add(pair_id)
                        sent += 1
                        print("ALERT SENT:", mint)

                    break

                except Exception as e:
                    print("Safety check error:", mint, e)
                    break

            time.sleep(0.2)

        except Exception as e:
            print("Token error:", mint, e)

    save_seen(updated)
    print("ALERTS SENT:", sent)
    print("DEX SCREENER V7 FINISHED")


if __name__ == "__main__":
    main()
