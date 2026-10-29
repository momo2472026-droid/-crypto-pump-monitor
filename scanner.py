import os
import json
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

import requests

# CRYPTO PULSE SCANNER — PAPER/SIGNAL MODE ONLY
# This scanner never places live trades.

DEX = "https://api.dexscreener.com"
GECKO = "https://api.geckoterminal.com/api/v2"
RUGCHECK = "https://api.rugcheck.xyz/v1"
SOLANA_RPC = os.getenv(
    "SOLANA_RPC_URL",
    "https://api.mainnet-beta.solana.com"
)

CHAINS = {
    x.strip().lower()
    for x in os.getenv(
        "CHAINS", "solana,ethereum,base,bsc"
    ).split(",")
    if x.strip()
}

MIN_LIQUIDITY = float(os.getenv("MIN_LIQUIDITY", "10000"))
MIN_VOLUME_24H = float(os.getenv("MIN_VOLUME_24H", "25000"))
MIN_CHANGE_1H = float(os.getenv("MIN_CHANGE_1H", "10"))
MIN_SCORE = int(os.getenv("MIN_SCORE", "45"))
MAX_TOKENS = int(os.getenv("MAX_TOKENS_PER_SCAN", "100"))
COOLDOWN = float(os.getenv("ALERT_COOLDOWN_HOURS", "6"))
MAX_DEV_PCT = float(os.getenv("MAX_DEV_HOLDING_PCT", "10"))
MAX_BUNDLE_PCT = float(os.getenv("MAX_BUNDLE_PCT", "25"))
TIMEOUT = 15

SIGNALS_FILE = Path("signals.json")
STATE_FILE = Path("scanner_state.json")
HISTORY_FILE = Path("history.json")
BLACKLIST_FILE = Path("rug_blacklist.json")

S = requests.Session()
S.headers.update({"User-Agent": "CryptoPulseScanner/4.0"})

GECKO_NETWORKS = {
    "solana": "solana",
    "ethereum": "eth",
    "base": "base",
    "bsc": "bsc",
    "polygon": "polygon_pos",
    "arbitrum": "arbitrum",
    "optimism": "optimism",
    "avalanche": "avax",
}


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def num(value, default=0.0):
    try:
        return float(value) if value is not None else default
    except (TypeError, ValueError):
        return default


def read_json(path, default):
    try:
        with path.open("r", encoding="utf-8") as file:
            return json.load(file)
    except (OSError, ValueError):
        return default


def write_json(path, data):
    temp = path.with_suffix(path.suffix + ".tmp")
    with temp.open("w", encoding="utf-8") as file:
        json.dump(data, file, indent=2)
    temp.replace(path)


def get_json(url, params=None, headers=None):
    try:
        response = S.get(
            url,
            params=params,
            headers=headers,
            timeout=TIMEOUT,
        )
        if response.status_code == 429:
            print("API rate limit reached:", url)
            return None
        response.raise_for_status()
        return response.json()
    except (requests.RequestException, ValueError) as error:
        print("API error:", url, str(error))
        return None


def chain_name(chain):
    chain = (chain or "").lower()
    aliases = {
        "sol": "solana",
        "eth": "ethereum",
        "matic": "polygon",
        "polygon-pos": "polygon",
        "binance-smart-chain": "bsc",
        "avax": "avalanche",
        "arbitrum-one": "arbitrum",
    }
    return aliases.get(chain, chain)


def key_for(chain, address):
    return f"{chain_name(chain)}:{str(address or '').lower()}"


def load_blacklist():
    data = read_json(BLACKLIST_FILE, {"tokens": [], "wallets": []})
    if not isinstance(data, dict):
        data = {}

    tokens = {
        str(item).lower().strip()
        for item in data.get("tokens", [])
        if isinstance(item, str)
    }
    wallets = {
        str(item).lower().strip()
        for item in data.get("wallets", [])
        if isinstance(item, str)
    }
    return tokens, wallets


def token_is_blacklisted(chain, address, tokens):
    return (
        key_for(chain, address) in tokens
        or str(address).lower() in tokens
    )


def discover_dexscreener():
    found = {}

    endpoints = [
        f"{DEX}/token-profiles/latest/v1",
        f"{DEX}/token-boosts/latest/v1",
        f"{DEX}/token-boosts/top/v1",
        f"{DEX}/community-takeovers/latest/v1",
    ]

    for endpoint in endpoints:
        data = get_json(endpoint)
        if not isinstance(data, list):
            continue

        for item in data:
            if not isinstance(item, dict):
                continue

            chain = chain_name(item.get("chainId"))
            address = item.get("tokenAddress")

            if chain not in CHAINS or not address:
                continue

            found.setdefault(
                key_for(chain, address),
                {
                    "chain": chain,
                    "address": address,
                    "name": item.get("name") or "Unknown",
                    "symbol": item.get("symbol") or "UNKNOWN",
                    "sources": ["DexScreener"],
                },
            )

    for term in ("pump", "new", "meme", "ai"):
        data = get_json(
            f"{DEX}/latest/dex/search",
            params={"q": term},
        )

        if not isinstance(data, dict):
            continue

        for pair in data.get("pairs") or []:
            if not isinstance(pair, dict):
                continue

            base = pair.get("baseToken") or {}
            chain = chain_name(pair.get("chainId"))
            address = base.get("address")

            if chain not in CHAINS or not address:
                continue

            found.setdefault(
                key_for(chain, address),
                {
                    "chain": chain,
                    "address": address,
                    "name": base.get("name") or "Unknown",
                    "symbol": base.get("symbol") or "UNKNOWN",
                    "sources": ["DexScreener"],
                },
            )

    return found


def discover_gecko():
    found = {}

    for chain in CHAINS:
        network = GECKO_NETWORKS.get(chain)
        if not network:
            continue

        data = get_json(
            f"{GECKO}/networks/{network}/trending_pools",
            params={"page": 1},
        )

        if not isinstance(data, dict):
            continue

        for pool in data.get("data") or []:
            attrs = pool.get("attributes") or {}
            relationships = pool.get("relationships") or {}
            base_token = relationships.get("base_token") or {}
            token_data = base_token.get("data") or {}
            token_id = token_data.get("id", "")

            address = (
                token_id.split("_", 1)[-1]
                if "_" in token_id
                else attrs.get("base_token_address", "")
            )

            if not address:
                continue

            name = attrs.get("name") or "Unknown"

            found.setdefault(
                key_for(chain, address),
                {
                    "chain": chain,
                    "address": address,
                    "name": name,
                    "symbol": name.split(" / ")[0],
                    "sources": ["GeckoTerminal"],
                },
            )

    return found


def discover_dexview():
    api_key = os.getenv("DEXVIEW_API_KEY", "").strip()
    endpoint = os.getenv("DEXVIEW_DISCOVERY_URL", "").strip()

    if not api_key or not endpoint:
        print("DexView not configured; skipping.")
        return {}

    data = get_json(
        endpoint,
        headers={"X-API-KEY": api_key, "apikey": api_key},
    )

    if not isinstance(data, dict):
        return {}

    items = (
        data.get("data")
        or data.get("tokens")
        or data.get("result")
        or []
    )

    if isinstance(items, dict):
        items = items.get("items") or items.get("tokens") or []

    found = {}

    if not isinstance(items, list):
        return found

    for item in items:
        if not isinstance(item, dict):
            continue

        chain = chain_name(
            item.get("chainId")
            or item.get("chain")
            or item.get("network")
        )
        address = (
            item.get("tokenAddress")
            or item.get("address")
            or item.get("contractAddress")
        )

        if chain not in CHAINS or not address:
            continue

        found[key_for(chain, address)] = {
            "chain": chain,
            "address": address,
            "name": item.get("name") or "Unknown",
            "symbol": item.get("symbol") or "UNKNOWN",
            "sources": ["DexView"],
        }

    return found


def discover_tokens():
    combined = {}

    providers = [
        ("DexScreener", discover_dexscreener),
        ("GeckoTerminal", discover_gecko),
        ("DexView", discover_dexview),
    ]

    for label, function in providers:
        try:
            items = function()
            print(f"{label}: {len(items)} tokens discovered.")

            for key, token in items.items():
                if key not in combined:
                    combined[key] = token
                else:
                    combined[key]["sources"] = sorted(
                        set(combined[key]["sources"])
                        | set(token["sources"])
                    )

        except Exception:
            print(label, "discovery error:")
            traceback.print_exc()

    return list(combined.values())[:MAX_TOKENS]


def get_pairs(token):
    chain = token["chain"]
    address = token["address"]

    data = get_json(f"{DEX}/token-pairs/v1/{chain}/{address}")

    if isinstance(data, list) and data:
        return [item for item in data if isinstance(item, dict)]

    network = GECKO_NETWORKS.get(chain)
    if not network:
        return []

    data = get_json(
        f"{GECKO}/networks/{network}/tokens/{address}/pools",
        params={"page": 1},
    )

    if not isinstance(data, dict):
        return []

    pairs = []

    for pool in data.get("data") or []:
        attrs = pool.get("attributes") or {}
        volume = attrs.get("volume_usd") or {}
        changes = attrs.get("price_change_percentage") or {}
        name = attrs.get("name") or "Unknown"

        pairs.append(
            {
                "chainId": chain,
                "pairAddress": attrs.get("address"),
                "url": "",
                "baseToken": {
                    "address": address,
                    "name": name,
                    "symbol": name.split(" / ")[0],
                },
                "priceUsd": attrs.get("base_token_price_usd"),
                "liquidity": {"usd": attrs.get("reserve_in_usd")},
                "volume": {
                    "h24": volume.get("h24", 0),
                    "h1": volume.get("h1", 0),
                },
                "priceChange": {
                    "h1": changes.get("h1", 0),
                    "h24": changes.get("h24", 0),
                },
                "txns": {},
                "pairCreatedAt": None,
            }
        )

    return pairs


def get_metrics(pair):
    liquidity = num((pair.get("liquidity") or {}).get("usd"))
    volume = pair.get("volume") or {}
    transactions = pair.get("txns") or {}
    hour_tx = transactions.get("h1") or {}
    day_tx = transactions.get("h24") or {}
    changes = pair.get("priceChange") or {}

    created = pair.get("pairCreatedAt")
    age = (
        max(0, (time.time() * 1000 - num(created)) / 60000)
        if created
        else None
    )

    return {
        "price_usd": num(pair.get("priceUsd")),
        "liquidity_usd": liquidity,
        "volume_24h_usd": num(volume.get("h24")),
        "volume_1h_usd": num(volume.get("h1")),
        "buys_1h": num(hour_tx.get("buys")),
        "sells_1h": num(hour_tx.get("sells")),
        "buys_24h": num(day_tx.get("buys")),
        "sells_24h": num(day_tx.get("sells")),
        "change_1h_pct": num(changes.get("h1")),
        "change_24h_pct": num(changes.get("h24")),
        "age_minutes": age,
    }


def rugcheck_report(address):
    return get_json(f"{RUGCHECK}/tokens/{address}/report")


def inspect_solana(token, state):
    if token["chain"] != "solana":
        return {
            "skip": True,
            "verified": False,
            "hard_block": False,
            "status": "UNKNOWN",
            "reasons": [
                "Developer/bundle checks are not configured for this chain"
            ],
            "developer_wallet": None,
            "developer_holding_pct": None,
            "bundle_pct": None,
        }

    report = rugcheck_report(token["address"])

    if not isinstance(report, dict):
        return {
            "skip": True,
            "verified": False,
            "hard_block": False,
            "status": "UNKNOWN",
            "reasons": ["RugCheck unavailable; risk checks UNKNOWN"],
            "developer_wallet": None,
            "developer_holding_pct": None,
            "bundle_pct": None,
        }

    creator = (
        report.get("creator")
        or report.get("creatorAddress")
        or report.get("creator_address")
    )

    if isinstance(creator, dict):
        creator = creator.get("address") or creator.get("wallet")

    if not creator:
        creator = (report.get("tokenMeta") or {}).get("updateAuthority")

    risks = report.get("risks") or []
    risk_text = " ".join(str(item) for item in risks).lower()

    bundle = None

    for obj in (report, report.get("graphInsidersDetected")):
        if not isinstance(obj, dict):
            continue

        for field in (
            "bundlePct",
            "bundle_pct",
            "bundledPct",
            "insiderPct",
            "insider_pct",
        ):
            if obj.get(field) is not None:
                value = num(obj.get(field), -1)

                if 0 <= value <= 100:
                    bundle = value
                    break

        if bundle is not None:
            break

    dev_pct = None
    holders = report.get("topHolders") or report.get("top_holders") or []

    if creator and isinstance(holders, list):
        for holder in holders:
            if not isinstance(holder, dict):
                continue

            wallet = (
                holder.get("owner")
                or holder.get("address")
                or holder.get("wallet")
                or holder.get("ownerAddress")
            )

            if str(wallet).lower() != str(creator).lower():
                continue

            for field in ("pct", "percentage", "share"):
                if holder.get(field) is not None:
                    value = num(holder[field], -1)
                    if 0 <= value <= 100:
                        dev_pct = value
                    break

            break

    reasons = []
    skip = False
    hard_block = False

    if not creator:
        reasons.append("Developer wallet UNKNOWN")
        skip = True
    elif dev_pct is None:
        reasons.append("Developer ownership UNKNOWN; no matching holder data")
        skip = True
    elif dev_pct > MAX_DEV_PCT:
        reasons.append(
            f"Developer owns {dev_pct:.2f}% "
            f"(limit {MAX_DEV_PCT:.1f}%)"
        )
        skip = True
        hard_block = True
    else:
        reasons.append(f"Reported developer holding: {dev_pct:.2f}%")

    if bundle is None:
        reasons.append("Bundle concentration UNKNOWN in provider report")
        skip = True
    elif bundle > MAX_BUNDLE_PCT:
        reasons.append(
            f"Bundle/insider percentage {bundle:.2f}% "
            f"exceeds {MAX_BUNDLE_PCT:.1f}%"
        )
        skip = True
        hard_block = True
    else:
        reasons.append(
            f"Reported bundle/insider concentration: {bundle:.2f}%"
        )

    severe_terms = (
        "rugpull",
        "rug pull",
        "honeypot",
        "freeze authority",
        "mint authority",
    )

    if any(term in risk_text for term in severe_terms):
        reasons.append("RugCheck reports a potentially severe risk")
        skip = True
        hard_block = True

    if creator:
        try:
            response = S.post(
                SOLANA_RPC,
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "getBalance",
                    "params": [
                        creator,
                        {"commitment": "confirmed"},
                    ],
                },
                timeout=TIMEOUT,
            )
            response.raise_for_status()
            payload = response.json()
            lamports = (payload.get("result") or {}).get("value")

            if lamports is not None:
                balance = float(lamports) / 1e9
                dev_state = state.setdefault("developer_wallets", {})
                previous = dev_state.get(creator)

                if previous is not None and abs(balance - float(previous)) >= 0.01:
                    delta = balance - float(previous)
                    telegram(
                        "CRYPTO PULSE — DEVELOPER WALLET BALANCE CHANGE\n"
                        f"Token: {token['address']}\n"
                        f"Wallet: {creator}\n"
                        f"SOL balance change: {delta:+.4f} SOL\n"
                        "This is not proof of a token sale."
                    )

                dev_state[creator] = balance

        except (
            requests.RequestException,
            ValueError,
            TypeError,
        ) as error:
            print("Developer balance check failed:", str(error))

    verified = bool(
        creator
        and dev_pct is not None
        and bundle is not None
        and dev_pct <= MAX_DEV_PCT
        and bundle <= MAX_BUNDLE_PCT
        and not any(term in risk_text for term in severe_terms)
    )

    status = (
        "FLAGGED"
        if hard_block
        else ("VERIFIED" if verified else "UNKNOWN")
    )

    return {
        "skip": skip,
        "verified": verified,
        "hard_block": hard_block,
        "status": status,
        "developer_wallet": creator,
        "developer_holding_pct": dev_pct,
        "bundle_pct": bundle,
        "reasons": reasons,
        "rugcheck_score": report.get("score"),
        "rugcheck_risks": risks,
    }


def wallet_is_blacklisted(wallet, wallets):
    return bool(wallet and str(wallet).lower() in wallets)


def score_pair(pair):
    metrics = get_metrics(pair)
    score = 0
    reasons = []
    risks = []

    liquidity = metrics["liquidity_usd"]
    volume = metrics["volume_24h_usd"]

    if liquidity >= 100000:
        score += 20
        reasons.append("Strong liquidity")
    elif liquidity >= 25000:
        score += 14
        reasons.append("Good liquidity")
    elif liquidity >= MIN_LIQUIDITY:
        score += 8
        reasons.append("Minimum liquidity reached")
    else:
        risks.append("Low liquidity")

    if volume >= 250000:
        score += 20
        reasons.append("High 24h volume")
    elif volume >= 50000:
        score += 14
        reasons.append("Good 24h volume")
    elif volume >= MIN_VOLUME_24H:
        score += 8
        reasons.append("Minimum volume reached")
    else:
        risks.append("Low 24h volume")

    buys = metrics["buys_1h"]
    sells = metrics["sells_1h"]
    total = buys + sells

    if total >= 10:
        ratio = buys / total

        if ratio >= 0.65:
            score += 15
            reasons.append("Buy activity stronger")
        elif ratio <= 0.35:
            risks.append("Sell activity stronger")
        else:
            score += 5
    else:
        risks.append("Limited transaction data")

    change = metrics["change_1h_pct"]

    if change >= 25:
        score += 15
        reasons.append("Strong 1h momentum")
    elif change >= MIN_CHANGE_1H:
        score += 10
        reasons.append("Positive 1h momentum")
    elif change < -10:
        risks.append("Negative 1h momentum")

    age = metrics["age_minutes"]

    if age is not None:
        if 5 <= age <= 360:
            score += 10
            reasons.append("Recently created pool")
        elif age < 5:
            risks.append("Extremely new pool")

    if liquidity > 0 and volume > 0:
        ratio = volume / liquidity

        if ratio >= 1:
            score += 10
        if ratio >= 20:
            risks.append("Unusually high volume/liquidity ratio")

    return min(score, 100), reasons, risks, metrics


def telegram(message):
    bot = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat = os.getenv("TELEGRAM_CHAT_ID", "").strip()

    if not bot or not chat:
        print("Telegram not configured; alert:\n" + message)
        return False

    try:
        response = S.post(
            f"https://api.telegram.org/bot{bot}/sendMessage",
            data={"chat_id": chat, "text": message},
            timeout=TIMEOUT,
        )
        response.raise_for_status()
        return True
    except requests.RequestException as error:
        print("Telegram error:", str(error))
        return False


def format_alert(signal):
    metrics = signal["metrics"]
    checks = signal.get("onchain_checks") or {}

    return (
        "CRYPTO PULSE SIGNAL — PAPER MODE\n"
        f"{signal['name']} ({signal['symbol']})\n"
        f"Chain: {signal['chain']}\n"
        f"Score: {signal['score']}/100\n"
        f"Risk status: {checks.get('status', 'UNKNOWN')}\n"
        f"Price: ${metrics['price_usd']:.10g}\n"
        f"Liquidity: ${metrics['liquidity_usd']:,.0f}\n"
        f"24h volume: ${metrics['volume_24h_usd']:,.0f}\n"
        f"1h change: {metrics['change_1h_pct']:.2f}%\n"
        f"Sources: {', '.join(signal['sources'])}\n"
        f"Risks: {', '.join((signal.get('risks') or [])[:5])}\n"
        f"Risk checks: {', '.join((checks.get('reasons') or [])[:3])}\n"
        f"Token address: {signal['address']}"
    )


def main():
    print("Crypto Pulse scan starting:", now_iso())

    state = read_json(
        STATE_FILE,
        {"last_alerts": {}, "developer_wallets": {}},
    )
    history = read_json(HISTORY_FILE, {})
    old_signals = read_json(SIGNALS_FILE, [])

    if not isinstance(state, dict):
        state = {}
    if not isinstance(history, dict):
        history = {}
    if not isinstance(old_signals, list):
        old_signals = []

    last_alerts = state.setdefault("last_alerts", {})
    token_blacklist, wallet_blacklist = load_blacklist()
    tokens = discover_tokens()

    print("Unique tokens discovered:", len(tokens))

    results = []
    current_time = time.time()

    for token in tokens:
        chain = token["chain"]
        address = token["address"]
        token_key = key_for(chain, address)

        if token_is_blacklisted(chain, address, token_blacklist):
            print("Blacklisted token skipped:", token_key)
            continue

        checks = inspect_solana(token, state)

        if wallet_is_blacklisted(
            checks.get("developer_wallet"),
            wallet_blacklist,
        ):
            print("Blacklisted developer wallet:", token_key)
            continue

        # Unknown and flagged candidates remain visible in the scanner output.
        # The paper trader separately enforces hard risk exclusions.
        pairs = get_pairs(token)

        if not pairs:
            continue

        best_pair = max(
            pairs,
            key=lambda pair: num(
                (pair.get("liquidity") or {}).get("usd")
            ),
        )

        score, reasons, risks, metrics = score_pair(best_pair)

        if metrics["liquidity_usd"] < MIN_LIQUIDITY:
            continue
        if metrics["volume_24h_usd"] < MIN_VOLUME_24H:
            continue

        history.setdefault(token_key, [])
        history[token_key].append(
            {
                "timestamp": current_time,
                "price_usd": metrics["price_usd"],
            }
        )

        history[token_key] = [
            item
            for item in history[token_key]
            if current_time - num(item.get("timestamp")) <= 7 * 86400
        ][-2000:]

        returns = {}

        for minutes in (5, 15, 30, 60, 240, 1440):
            cutoff = current_time - minutes * 60

            candidates = [
                item
                for item in history[token_key]
                if num(item.get("timestamp")) <= cutoff
                and num(item.get("price_usd")) > 0
            ]

            if candidates and metrics["price_usd"] > 0:
                previous = max(
                    candidates,
                    key=lambda item: num(item["timestamp"]),
                )
                previous_price = num(previous["price_usd"])

                if previous_price > 0:
                    returns[f"{minutes}m_pct"] = (
                        metrics["price_usd"] / previous_price - 1
                    ) * 100

        signal = {
            "timestamp": now_iso(),
            "chain": chain,
            "address": address,
            "name": token.get("name", "Unknown"),
            "symbol": token.get("symbol", "UNKNOWN"),
            "pair_address": best_pair.get("pairAddress"),
            "url": best_pair.get("url", ""),
            "sources": token["sources"],
            "score": score,
            "reasons": reasons,
            "risks": risks,
            "metrics": metrics,
            "returns": returns,
            "mode": "PAPER_ONLY",
            "onchain_checks": checks,
            "paper_eligible": bool(
                checks.get("verified")
                and not checks.get("skip")
                and not checks.get("hard_block")
            ),
            "risk_status": checks.get("status", "UNKNOWN"),
        }

        results.append(signal)

        if score >= MIN_SCORE:
            last_alert = num(last_alerts.get(token_key))

            previous_score = max(
                [
                    num(item.get("score"))
                    for item in old_signals
                    if key_for(
                        item.get("chain"),
                        item.get("address"),
                    ) == token_key
                ],
                default=0,
            )

            if (
                current_time - last_alert >= COOLDOWN * 3600
                or score >= previous_score + 10
            ):
                telegram(format_alert(signal))
                last_alerts[token_key] = current_time

    results.sort(
        key=lambda item: (
            item["score"],
            item["metrics"]["liquidity_usd"],
        ),
        reverse=True,
    )

    write_json(SIGNALS_FILE, results[:300])
    write_json(HISTORY_FILE, history)

    verified_count = sum(
        1 for item in results if item.get("paper_eligible")
    )
    flagged_count = sum(
        1
        for item in results
        if item.get("risk_status") == "FLAGGED"
    )
    unknown_count = sum(
        1
        for item in results
        if item.get("risk_status") == "UNKNOWN"
    )

    state.update(
        {
            "last_scan": now_iso(),
            "last_discovery_count": len(tokens),
            "last_signal_count": len(results),
            "verified_signal_count": verified_count,
            "unknown_count": unknown_count,
            "flagged_count": flagged_count,
            "unknown_or_flagged_count": unknown_count + flagged_count,
        }
    )

    write_json(STATE_FILE, state)

    print(
        f"Scan finished. {len(results)} candidates; "
        f"{verified_count} verified, "
        f"{unknown_count} unknown, "
        f"{flagged_count} flagged. Paper mode only."
    )


if __name__ == "__main__":
    try:
        main()
    except Exception:
        traceback.print_exc()
        raise
