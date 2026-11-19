import os
import json
import time
import requests

# ==========================================
# DEX SCREENER V8 - ADVANCED SAFETY
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

SEEN_FILE = "seen_tokens.json"

session = requests.Session()
session.headers.update({"User-Agent": "SolanaScannerV8"})


def get(url):
    for attempt in range(3):
        try:
            r = session.get(url, timeout=25)

            if r.status_code == 429:
                time.sleep(3 * (attempt + 1))
                continue

            r.raise_for_status()
            return r.json()

        except requests.RequestException:
            if attempt == 2:
                raise
            time.sleep(2)

    raise RuntimeError("API request failed")


def load_seen():
    try:
        with open(SEEN_FILE, "r") as f:
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
        item for item in data
        if item.get("chainId") == "solana"
        and item.get("tokenAddress")
    ]


def get_pairs(mint):
    data = get(f"{DEX}/token-pairs/v1/solana/{mint}")

    if not isinstance(data, list):
        return []

    return [
        pair for pair in data
        if pair.get("chainId") == "solana"
    ]


def get_rug_report(mint):
    report = get(f"{RUG}/tokens/{mint}/report")
    summary = get(f"{RUG}/tokens/{mint}/report/summary")
    return report, summary


def number(value):
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def holder_percentage(holder):
    for key in ("pct", "percentage", "percent"):
        value = number(holder.get(key))
        if value is not None:
            return value
    return None


def get_authority(report, token, key):
    if key in token:
        return token[key], True

    if key in report:
        return report[key], True

    return None, False


def analyse_rug(report, summary):
    token = report.get("token") or {}
    holders = report.get("topHolders") or []
    meta = report.get("tokenMeta") or {}

    score = summary.get("score_normalised")
    if score is None:
        score = report.get("score_normalised")

    mint_authority, mint_known = get_authority(
        report, token, "mintAuthority"
    )

    freeze_authority, freeze_known = get_authority(
        report, token, "freezeAuthority"
    )

    top10 = None
    if len(holders) >= 10:
        percentages = [
            holder_percentage(h) for h in holders[:10]
        ]

        if all(x is not None for x in percentages):
            top10 = sum(percentages)

    dev_pct = None
    supply = number(token.get("supply"))
    creator_balance = number(report.get("creatorBalance"))

    if supply is not None and supply > 0 and creator_balance is not None:
        dev_pct = creator_balance / supply * 100

    lp_locked = number(summary.get("lpLockedPct"))
    if lp_locked is None:
        lp_locked = number(report.get("lpLockedPct"))

    program = str(
        summary.get("tokenProgram")
        or report.get("tokenProgram")
        or ""
    )

    token2022 = "tokenz" in program.lower()

    extensions = report.get("token_extensions")
    extension_text = json.dumps(extensions or {}).lower()

    risky_extensions = [
        name for name in (
            "permanentdelegate",
            "transferfee",
            "transferhook",
            "defaultaccountstate"
        )
        if name in extension_text
    ]

    if not token2022:
        extension_check = True
    elif extensions is None:
        extension_check = None
    else:
        extension_check = len(risky_extensions) == 0

    insider_networks = report.get("insiderNetworks")

    if insider_networks is not None:
        insider_check = len(insider_networks) == 0
    elif holders and all("insider" in h for h in holders):
        insider_check = not any(h.get("insider") for h in holders)
    else:
        insider_check = None

    return {
        "score": number(score),
        "mint": mint_authority,
        "mint_known": mint_known,
        "freeze": freeze_authority,
        "freeze_known": freeze_known,
        "top10": top10,
        "dev": dev_pct,
        "lp": lp_locked,
        "risks": report.get("risks") or summary.get("risks") or [],
        "insider_check": insider_check,
        "token2022": token2022,
        "extensions": extensions,
        "risky_extensions": risky_extensions,
        "extension_check": extension_check,
        "metadata_mutable": meta.get("mutable")
    }


def check_pair(pair):
    created = pair.get("pairCreatedAt")

    if not created:
        return None

    age = (time.time() * 1000 - int(created)) / 3600000

    mc = number(pair.get("marketCap") or pair.get("fdv"))
    liquidity = number((pair.get("liquidity") or {}).get("usd"))

    volume = pair.get("volume") or {}
    txns = pair.get("txns") or {}
    m5 = txns.get("m5") or {}

    buys = int(m5.get("buys") or 0)
    sells = int(m5.get("sells") or 0)
    volume5 = number(volume.get("m5")) or 0

    checks = {
        "Age within 10 hours": 0 <= age <= MAX_AGE_HOURS,
        "Market cap $5K-$50K":
            mc is not None and MIN_MC <= mc <= MAX_MC,
        "Liquidity >= $5K":
            liquidity is not None and liquidity >= MIN_LIQ,
        "5m volume >= $500": volume5 >= MIN_VOLUME_5M,
        "5m transactions >= 5": buys + sells >= MIN_TXNS_5M,
        "Buys >= sells": buys >= sells
    }

    return {
        "checks": checks,
        "age": age,
        "mc": mc,
        "liq": liquidity,
        "volume": volume5,
        "buys": buys,
        "sells": sells
    }


def safety_checks(rug):
    return {
        "RugCheck score <= 30":
            rug["score"] is not None
            and rug["score"] <= MAX_RUG_SCORE,

        "Mint authority revoked":
            rug["mint_known"] and rug["mint"] is None,

        "Freeze authority revoked":
            rug["freeze_known"] and rug["freeze"] is None,

        "Top 10 holders <= 25%":
            rug["top10"] is not None
            and rug["top10"] <= MAX_TOP10_HOLDERS,

        "Dev holding <= 5%":
            rug["dev"] is not None
            and rug["dev"] <= MAX_DEV_HOLDING,

        "LP locked >= 90%":
            rug["lp"] is not None
            and rug["lp"] >= MIN_LP_LOCKED,

        "Sniper / insider check":
            rug["insider_check"],

        "Token-2022 extensions":
            rug["extension_check"],

        # These two cannot be confirmed by the APIs used here.
        "Fake volume detection": None,
        "Sellability simulation": None
    }


def fmt(value, suffix=""):
    if value is None:
        return "UNKNOWN"

    return f"{value:,.2f}{suffix}"


def make_message(pair, mint, data, rug, checks):
    base = pair.get("baseToken") or {}

    lines = []

    for name, result in checks.items():
        if result is True:
            status = "PASS"
        elif result is False:
            status = "FAIL"
        else:
            status = "UNKNOWN"

        lines.append(f"{status} - {name}")

    risks = []

    for risk in rug["risks"][:5]:
        if isinstance(risk, dict):
            risks.append(
                f"- {risk.get('level', 'info')}: "
                f"{risk.get('name', 'Risk')}"
            )

    mint_status = (
        "REVOKED" if rug["mint_known"] and rug["mint"] is None
        else "ACTIVE" if rug["mint_known"]
        else "UNKNOWN"
    )

    freeze_status = (
        "REVOKED" if rug["freeze_known"] and rug["freeze"] is None
        else "ACTIVE" if rug["freeze_known"]
        else "UNKNOWN"
    )

    extension_names = (
        ", ".join(rug["risky_extensions"])
        if rug["risky_extensions"]
        else "None detected / UNKNOWN"
    )

    return (
        "🛡️ DEX SCREENER V8\n"
        "SOLANA ADVANCED SCANNER\n\n"
        f"Name: {base.get('name', 'Unknown')}\n"
        f"Symbol: {base.get('symbol', 'Unknown')}\n"
        f"Market Cap: ${fmt(data['mc'])}\n"
        f"Liquidity: ${fmt(data['liq'])}\n"
        f"5m Volume: ${fmt(data['volume'])}\n"
        f"Buys/Sells: {data['buys']}/{data['sells']}\n"
        f"Pair Age: {fmt(data['age'], ' hours')}\n\n"
        f"RugCheck Score: {fmt(rug['score'])}\n"
        f"Top 10 Holders: {fmt(rug['top10'], '%')}\n"
        f"Dev Holding: {fmt(rug['dev'], '%')}\n"
        f"LP Locked: {fmt(rug['lp'], '%')}\n"
        f"Mint Authority: {mint_status}\n"
        f"Freeze Authority: {freeze_status}\n"
        f"Token-2022: {rug['token2022']}\n"
        f"Risky Extensions: {extension_names}\n"
        f"Metadata Mutable: {rug['metadata_mutable']}\n\n"
        "SAFETY CHECKS\n"
        + "\n".join(lines)
        + "\n\nRUGCHECK FLAGS\n"
        + ("\n".join(risks) if risks else "No flags returned")
        + f"\n\nToken: {mint}"
        + f"\nDEX: {pair.get('url', '')}"
        + f"\nRugCheck: https://rugcheck.xyz/tokens/{mint}"
        + "\n\n⚠️ UNKNOWN means not verified."
        + "\n⚠️ No scanner can guarantee a token is safe."
    )


def main():
    seen = load_seen()
    updated = set(seen)
    sent = 0

    print("DEX SCREENER V8 STARTED")

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
                key=lambda p: number(
                    (p.get("liquidity") or {}).get("usd")
                ) or 0,
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

                    if not all(data["checks"].values()):
                        continue

                    report, summary = get_rug_report(mint)
                    rug = analyse_rug(report, summary)
                    checks = safety_checks(rug)

                    # All measurable core safety checks must pass.
                    required_checks = [
                        "RugCheck score <= 30",
                        "Mint authority revoked",
                        "Freeze authority revoked",
                        "Top 10 holders <= 25%",
                        "Dev holding <= 5%",
                        "LP locked >= 90%",
                        "Token-2022 extensions"
                    ]

                    if not all(checks[name] is True for name in required_checks):
                        print("SAFETY REJECTED:", mint, checks)
                        continue

                    # Insider check: reject if known to fail.
                    if checks["Sniper / insider check"] is False:
                        print("INSIDER REJECTED:", mint)
                        continue

                    message = make_message(
                        pair, mint, data, rug, checks
                    )

                    if telegram(message):
                        updated.add(pair_id)
                        sent += 1
                        print("ALERT SENT:", mint)

                    break

                except Exception as e:
                    print("Token safety error:", mint, e)
                    break

            time.sleep(0.5)

        except Exception as e:
            print("Token error:", mint, e)

    save_seen(updated)

    print("ALERTS SENT:", sent)
    print("DEX SCREENER V8 FINISHED")


if __name__ == "__main__":
    main()
