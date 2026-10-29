import json
import os
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

import requests

# ============================================================
# CRYPTO PULSE SCANNER
# Paper-trading / signal mode only. No live trades are placed.
# ============================================================

DEX = "https://api.dexscreener.com"
GECKO = "https://api.geckoterminal.com/api/v2"
RUGCHECK = "https://api.rugcheck.xyz/v1"
SOLANA_RPC = os.getenv(
    "SOLANA_RPC_URL",
    "https://api.mainnet-beta.solana.com",
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
S.headers.update({"User-Agent": "CryptoPulseScanner/3.0"})


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def num(value, default=0.0):
    try:
        return float(value) if value is not None else default
    except (TypeError, ValueError):
        return default


def read_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def write_json(path, data):
    temp = path.with_suffix(path.suffix + ".tmp")
    with open(temp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    temp.replace(path)


def get_json(url, params=None, headers=None):
    try:
        r = S.get(
            url,
            params=params,
            headers=headers,
            timeout=TIMEOUT,
        )
        if r.status_code == 429:
            print("API rate limit reached:", url)
            return None
        r.raise_for_status()
        return r.json()
    except (requests.RequestException, ValueError) as e:
        print("API error:", url, str(e))
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
    return f"{chain_name(chain)}:{str(address).lower()}"


def load_blacklist():
    data = read_json(
        BLACKLIST_FILE,
        {"tokens": [], "wallets": []},
    )
    if not isinstance(data, dict):
        data = {}

    tokens = {
        str(x).lower().strip()
        for x in data.get("tokens", [])
        if isinstance(x, str)
    }
    wallets = {
        str(x).lower().strip()
        for x in data.get("wallets", [])
        if isinstance(x, str)
    }
    return tokens, wallets


def token_is_blacklisted(chain, address, token_blacklist):
    return (
        key_for(chain, address) in token_blacklist
        or str(address).lower() in token_blacklist
    )


# ------------------------------------------------------------
# TOKEN DISCOVERY: DexScreener
# ------------------------------------------------------------

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


# ------------------------------------------------------------
# TOKEN DISCOVERY: GeckoTerminal
# ------------------------------------------------------------

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
            base_rel = relationships.get("base_token") or {}
            token_data = base_rel.get("data") or {}
            token_id = token_data.get("id", "")

            address = (
                token_id.split("_", 1)[-1]
                if "_" in token_id
                else attrs.get("base_token_address", "")
            )
            if not address:
                continue

            found.setdefault(
                key_for(chain, address),
                {
                    "chain": chain,
                    "address": address,
                    "name": attrs.get("name") or "Unknown",
                    "symbol": (
                        attrs.get("name") or "UNKNOWN"
                    ).split(" / ")[0],
                    "sources": ["GeckoTerminal"],
                },
            )

    return found


# ------------------------------------------------------------
# TOKEN DISCOVERY: optional DexView integration
# Configure the documented endpoint and API key in secrets.
# ------------------------------------------------------------

def discover_dexview():
    api_key = os.getenv("DEXVIEW_API_KEY", "").strip()
    endpoint = os.getenv("DEXVIEW_DISCOVERY_URL", "").strip()

    if not api_key or not endpoint:
        print("DexView not configured; skipping.")
        return {}

    data = get_json(
        endpoint,
        headers={
            "X-API-KEY": api_key,
            "apikey": api_key,
        },
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

    for label, function in (
        ("DexScreener", discover_dexscreener),
        ("GeckoTerminal", discover_gecko),
        ("DexView", discover_dexview),
    ):
        try:
            results = function()
            print(f"{label}: {len(results)} tokens discovered.")

            for key, token in results.items():
                if key not in combined:
                    combined[key] = token
                else:
                    sources = set(combined[key]["sources"])
                    sources.update(token["sources"])
                    combined[key]["sources"] = sorted(sources)

        except Exception:
            print(f"{label} discovery error:")
            traceback.print_exc()

    return list(combined.values())[:MAX_TOKENS]


# ------------------------------------------------------------
# MARKET DATA
# ------------------------------------------------------------

def get_pairs(token):
    chain = token["chain"]
    address = token["address"]

    data = get_json(
        f"{DEX}/token-pairs/v1/{chain}/{address}"
    )
    if isinstance(data, list) and data:
        return [x for x in data if isinstance(x, dict)]

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

        pairs.append({
            "chainId": chain,
            "pairAddress": attrs.get("address"),
            "url": "",
            "baseToken": {
                "address": address,
                "name": attrs.get("name") or "Unknown",
                "symbol": (
                    attrs.get("name") or "UNKNOWN"
                ).split(" / ")[0],
            },
            "priceUsd": attrs.get("base_token_price_usd"),
            "liquidity": {
                "usd": attrs.get("reserve_in_usd"),
            },
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
        })

    return pairs


def get_metrics(pair):
    liquidity = num((pair.get("liquidity") or {}).get("usd"))
    volume = pair.get("volume") or {}
    txns = pair.get("txns") or {}
    h1 = txns.get("h1") or {}
    h24 = txns.get("h24") or {}
    changes = pair.get("priceChange") or {}

    age = None
    created = pair.get("pairCreatedAt")
    if created:
        age = max(0, (time.time() * 1000 - num(created)) / 60000)

    return {
        "price_usd": num(pair.get("priceUsd")),
        "liquidity_usd": liquidity,
        "volume_24h_usd": num(volume.get("h24")),
        "volume_1h_usd": num(volume.get("h1")),
        "buys_1h": num(h1.get("buys")),
        "sells_1h": num(h1.get("sells")),
        "buys_24h": num(h24.get("buys")),
        "sells_24h": num(h24.get("sells")),
        "change_1h_pct": num(changes.get("h1")),
        "change_24h_pct": num(changes.get("h24")),
        "age_minutes": age,
    }


# ------------------------------------------------------------
# DEVELOPER + BUNDLE RISK CHECKS
# RugCheck data varies. Missing evidence is UNKNOWN, not SAFE.
# ------------------------------------------------------------

def rugcheck_report(address):
    return get_json(
        f"{RUGCHECK}/tokens/{address}/report"
    )


def inspect_solana(token, state):
    if token["chain"] != "solana":
        return {
            "skip": True,
            "verified": False,
            "reasons": [
                "Developer/bundle checks only configured for Solana"
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
            "reasons": [
                "RugCheck unavailable; risk checks UNKNOWN"
            ],
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
        creator = (
            creator.get("address")
            or creator.get("wallet")
        )

    if not creator:
        meta = report.get("tokenMeta") or {}
        creator = meta.get("updateAuthority")

    risks = report.get("risks") or []
    risk_text = " ".join(
        str(x) for x in risks
    ).lower()

    # Only accept a bundle percentage if the API explicitly supplies it.
    bundle_pct = None
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
            value = obj.get(field)
            if value is not None:
                candidate = num(value, -1)
                if 0 <= candidate <= 100:
                    bundle_pct = candidate
                    break
        if bundle_pct is not None:
            break

    # Try to match creator against the reported top-holder list.
    dev_pct = None
    holders = (
        report.get("topHolders")
        or report.get("top_holders")
        or []
    )

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
                    dev_pct = num(holder[field], -1)
                    if 0 <= dev_pct <= 100:
                        break
                    dev_pct = None
            break

    reasons = []
    skip = False

    if not creator:
        reasons.append("Developer wallet UNKNOWN")
        skip = True
    elif dev_pct is None:
        reasons.append(
            "Developer ownership UNKNOWN; no matching holder data"
        )
        skip = True
    elif dev_pct > MAX_DEV_PCT:
        reasons.append(
            f"Developer owns {dev_pct:.2f}% "
            f"(limit {MAX_DEV_PCT:.1f}%)"
        )
        skip = True
    else:
        reasons.append(f"Reported developer holding: {dev_pct:.2f}%")

    if bundle_pct is None:
        reasons.append(
            "Bundle concentration UNKNOWN in provider report"
        )
        skip = True
    elif bundle_pct > MAX_BUNDLE_PCT:
        reasons.append(
            f"Reported bundle/insider percentage "
            f"{bundle_pct:.2f}% exceeds {MAX_BUNDLE_PCT:.1f}%"
        )
        skip = True
    else:
        reasons.append(
            f"Reported bundle/insider concentration: {bundle_pct:.2f}%"
        )

    # Balance-change alerts are not proof of a token sale.
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
                balance = float(lamports) / 1_000_000_000
                dev_state = state.setdefault(
                    "developer_wallets", {}
                )
                previous = dev_state.get(creator)

                if (
                    previous is not None
                    and abs(balance - float(previous)) >= 0.01
                ):
                    delta = balance - float(previous)
                    telegram(
                        "CRYPTO PULSE — DEVELOPER WALLET CHANGE\n"
                        f"Token: {token['address']}\n"
                        f"Wallet: {creator}\n"
                        f"SOL balance change: {delta:+.4f} SOL\n"
                        "This is not proof of a token sale."
                    )

                dev_state[creator] = balance

        except (requests.RequestException, ValueError, TypeError) as e:
            print("Developer balance check failed:", str(e))

    return {
        "skip": skip,
        "verified": bool(
            creator and dev_pct is not None and bundle_pct is not None
        ),
        "developer_wallet": creator,
        "developer_holding_pct": dev_pct,
        "bundle_pct": bundle_pct,
        "reasons": reasons,
        "rugcheck_score": report.get("score"),
        "rugcheck_risks": risks,
    }


# ------------------------------------------------------------
# BLACKLIST
# Tokens in rug_blacklist.json are excluded.
# Wallet blacklist matching applies when the wallet is identified.
# ------------------------------------------------------------

def wallet_is_blacklisted(wallet, wallet_blacklist):
    return bool(
        wallet
        and str(wallet).lower() in wallet_blacklist
    )


# ------------------------------------------------------------
# SCORING
# ------------------------------------------------------------

def score_pair(pair):
    m = get_metrics(pair)
    score = 0
    reasons = []
    risks = []

    if m["liquidity_usd"] >= 100000:
        score += 20
        reasons.append("Strong liquidity")
    elif m["liquidity_usd"] >= 25000:
        score += 14
        reasons.append("Good liquidity")
    elif m["liquidity_usd"] >= MIN_LIQUIDITY:
        score += 8
        reasons.append("Minimum liquidity reached")
    else:
        risks.append("Low liquidity")

    if m["volume_24h_usd"] >= 250000:
        score += 20
        reasons.append("High 24h volume")
    elif m["volume_24h_usd"] >= 50000:
        score += 14
        reasons.append("Good 24h volume")
    elif m["volume_24h_usd"] >= MIN_VOLUME_24H:
        score += 8
        reasons.append("Minimum volume reached")
    else:
        risks.append("Low 24h volume")

    buys = m["buys_1h"]
    sells = m["sells_1h"]
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

    change = m["change_1h_pct"]
    if change >= 25:
        score += 15
        reasons.append("Strong 1h momentum")
    elif change >= MIN_CHANGE_1H:
        score += 10
        reasons.append("Positive 1h momentum")
    elif change < -10:
        risks.append("Negative 1h momentum")

    age = m["age_minutes"]
    if age is not None:
        if 5 <= age <= 360:
            score += 10
            reasons.append("Recently created pool")
        elif age < 5:
            risks.append("Extremely new pool")

    if m["liquidity_usd"] > 0 and m["volume_24h_usd"] > 0:
        ratio = m["volume_24h_usd"] / m["liquidity_usd"]
        if ratio >= 1:
            score += 10
        if ratio >= 20:
            risks.append("Unusually high volume/liquidity ratio")

    return min(score, 100), reasons, risks, m


# ------------------------------------------------------------
# TELEGRAM
# ------------------------------------------------------------

def telegram(message):
    bot = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat = os.getenv("TELEGRAM_CHAT_ID", "").strip()

    if not bot or not chat:
        print("Telegram not configured; alert:")
        print(message)
        return False

    try:
        response = S.post(
            f"https://api.telegram.org/bot{bot}/sendMessage",
            data={"chat_id": chat, "text": message},
            timeout=TIMEOUT,
        )
        response.raise_for_status()
        return True
    except requests.RequestException as e:
        print("Telegram error:", str(e))
        return False


def format_alert(signal):
    m = signal["metrics"]
    return (
        "CRYPTO PULSE SIGNAL — PAPER MODE\n"
        f"{signal['name']} ({signal['symbol']})\n"
        f"Chain: {signal['chain']}\n"
        f"Score: {signal['score']}/100\n"
        f"Price: ${m['price_usd']:.10g}\n"
        f"Liquidity: ${m['liquidity_usd']:,.0f}\n"
        f"24h volume: ${m['volume_24h_usd']:,.0f}\n"
        f"1h change: {m['change_1h_pct']:.2f}%\n"
        f"Sources: {', '.join(signal['sources'])}\n"
        f"Risks: {', '.join(signal['risks'][:5])}\n"
        f"Token address: {signal['address']}"
    )


# ------------------------------------------------------------
# MAIN SCAN
# ------------------------------------------------------------

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
        token_key_value = key_for(chain, address)

        if token_is_blacklisted(
            chain, address, token_blacklist
        ):
            print("Blacklisted token skipped:", token_key_value)
            continue

        # Strict risk filtering currently applies to Solana only.
        onchain = inspect_solana(token, state)

        if wallet_is_blacklisted(
            onchain.get("developer_wallet"), wallet_blacklist
        ):
            print("Blacklisted developer wallet:", token_key_value)
            continue

        if onchain.get("skip"):
            print(
                "Risk checks unknown/high; skipped:",
                token_key_value,
                "; ".join(onchain.get("reasons", [])),
            )
            continue

        pairs = get_pairs(token)
        if not pairs:
            continue

        best_pair = max(
            pairs,
            key=lambda p: num(
                (p.get("liquidity") or {}).get("usd")
            ),
        )

        score, reasons, risks, metrics = score_pair(best_pair)

        if metrics["liquidity_usd"] < MIN_LIQUIDITY:
            continue
        if metrics["volume_24h_usd"] < MIN_VOLUME_24H:
            continue

        # Save price snapshots for historical comparisons.
        history.setdefault(token_key_value, [])
        history[token_key_value].append({
            "timestamp": current_time,
            "price_usd": metrics["price_usd"],
        })
        history[token_key_value] = [
            item for item in history[token_key_value]
            if current_time - num(item.get("timestamp")) <= 7 * 86400
        ][-2000:]

        returns = {}
        for minutes in (5, 15, 30, 60, 240, 1440):
            cutoff = current_time - minutes * 60
            candidates = [
                item for item in history[token_key_value]
                if num(item.get("timestamp")) <= cutoff
                and num(item.get("price_usd")) > 0
            ]
            if candidates and metrics["price_usd"] > 0:
                previous = max(
                    candidates,
                    key=lambda item: num(item["timestamp"]),
                )
                returns[f"{minutes}m_pct"] = (
                    metrics["price_usd"]
                    / num(previous["price_usd"]) - 1
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
            "onchain_checks": onchain,
        }

        results.append(signal)

        if score >= MIN_SCORE:
            last = num(last_alerts.get(token_key_value))
            cooldown_seconds = COOLDOWN * 3600

            previous_score = max(
                [
                    num(item.get("score"))
                    for item in old_signals
                    if key_for(
                        item.get("chain"),
                        item.get("address"),
                    ) == token_key_value
                ],
                default=0,
            )

            if (
                current_time - last >= cooldown_seconds
                or score >= previous_score + 10
            ):
                telegram(format_alert(signal))
                last_alerts[token_key_value] = current_time

    results.sort(
        key=lambda item: (
            item["score"],
            item["metrics"]["liquidity_usd"],
        ),
        reverse=True,
    )

    write_json(SIGNALS_FILE, results[:300])
    write_json(HISTORY_FILE, history)

    state["last_scan"] = now_iso()
    state["last_discovery_count"] = len(tokens)
    state["last_signal_count"] = len(results)

    write_json(STATE_FILE, state)

    print(
        f"Scan finished. {len(results)} qualifying signals. "
        "Paper mode only."
    )


if __name__ == "__main__":
    try:
        main()
    except Exception:
        traceback.print_exc()
        raise
