import json
import os
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

import requests

# Crypto Pulse Scanner
# One scan per execution: suitable for scheduled GitHub Actions.
# Signal/paper mode only. No live trading.

BASE = "https://api.dexscreener.com"
GECKO_BASE = "https://api.geckoterminal.com/api/v2"
DEXVIEW_BASE = "https://openapi.dexview.com"

CHAINS = {
    item.strip().lower()
    for item in os.getenv("CHAINS", "solana,ethereum,base,bsc").split(",")
    if item.strip()
}

MIN_LIQUIDITY = float(os.getenv("MIN_LIQUIDITY", "10000"))
MIN_VOLUME_24H = float(os.getenv("MIN_VOLUME_24H", "25000"))
MIN_CHANGE_1H = float(os.getenv("MIN_CHANGE_1H", "10"))
MIN_SCORE = int(os.getenv("MIN_SCORE", "45"))
MAX_TOKENS_PER_SCAN = int(os.getenv("MAX_TOKENS_PER_SCAN", "100"))
ALERT_COOLDOWN_HOURS = float(os.getenv("ALERT_COOLDOWN_HOURS", "6"))
REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "15"))

SIGNALS_FILE = Path("signals.json")
STATE_FILE = Path("scanner_state.json")
HISTORY_FILE = Path("history.json")
BLACKLIST_FILE = Path("rug_blacklist.json")

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "CryptoPulseScanner/2.0",
    "Accept": "application/json",
})

RUGCHECK_BASE = "https://api.rugcheck.xyz/v1"
SOLANA_RPC_URL = os.getenv(
    "SOLANA_RPC_URL",
    "https://api.mainnet-beta.solana.com",
).strip()

MAX_DEV_HOLDING_PCT = float(
    os.getenv("MAX_DEV_HOLDING_PCT", "10")
)
MAX_BUNDLE_PCT = float(
    os.getenv("MAX_BUNDLE_PCT", "25")
)


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def read_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return default


def write_json(path, data):
    temp = path.with_suffix(path.suffix + ".tmp")
    with open(temp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    temp.replace(path)


def number(value, default=0.0):
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def api(url, params=None, headers=None):
    try:
        response = SESSION.get(
            url,
            params=params,
            headers=headers,
            timeout=REQUEST_TIMEOUT,
        )
        if response.status_code == 429:
            print(f"Rate limited by {url}; skipping this request.")
            return None
        response.raise_for_status()
        return response.json()
    except (requests.RequestException, ValueError) as exc:
        print(f"API request failed ({url}): {exc}")
        return None


def norm_chain(chain):
    chain = (chain or "").lower()
    aliases = {
        "sol": "solana",
        "eth": "ethereum",
        "bsc": "bsc",
        "binance-smart-chain": "bsc",
        "polygon-pos": "polygon",
        "matic": "polygon",
        "avax": "avalanche",
        "arbitrum-one": "arbitrum",
    }
    return aliases.get(chain, chain)


def token_key(chain, address):
    return f"{norm_chain(chain)}:{(address or '').lower()}"


def load_blacklist():
    data = read_json(
        BLACKLIST_FILE,
        {"wallets": [], "tokens": [], "updated": None},
    )
    if not isinstance(data, dict):
        data = {"wallets": [], "tokens": [], "updated": None}

    wallets = {
        str(x).strip().lower()
        for x in data.get("wallets", [])
        if isinstance(x, str) and x.strip()
    }
    tokens = {
        str(x).strip().lower()
        for x in data.get("tokens", [])
        if isinstance(x, str) and x.strip()
    }
    return wallets, tokens


def is_blacklisted(chain, address, wallets, tokens):
    key = token_key(chain, address)
    return key in tokens or (address or "").lower() in tokens


def dex_pair_to_record(pair, source="DexScreener"):
    if not isinstance(pair, dict):
        return None

    base_token = pair.get("baseToken") or {}
    address = base_token.get("address")
    chain = norm_chain(pair.get("chainId"))

    if not address or not chain:
        return None

    return {
        "chain": chain,
        "address": address,
        "name": base_token.get("name") or "Unknown",
        "symbol": base_token.get("symbol") or "UNKNOWN",
        "source": source,
        "pair": pair,
    }


def discover_dexscreener():
    found = {}

    urls = [
        f"{BASE}/token-profiles/latest/v1",
        f"{BASE}/token-boosts/latest/v1",
        f"{BASE}/token-boosts/top/v1",
        f"{BASE}/community-takeovers/latest/v1",
    ]

    for url in urls:
        data = api(url)
        if isinstance(data, list):
            for item in data:
                if not isinstance(item, dict):
                    continue

                chain = norm_chain(item.get("chainId"))
                address = item.get("tokenAddress")

                if chain in CHAINS and address:
                    found.setdefault(
                        token_key(chain, address),
                        {
                            "chain": chain,
                            "address": address,
                            "name": (
                                item.get("description")
                                or item.get("name")
                                or "Unknown"
                            ),
                            "symbol": item.get("symbol") or "UNKNOWN",
                            "source": "DexScreener",
                        },
                    )

    for term in ("pump", "new", "meme", "ai", "sol"):
        data = api(
            f"{BASE}/latest/dex/search",
            params={"q": term},
        )
        if not isinstance(data, dict):
            continue

        for pair in data.get("pairs") or []:
            record = dex_pair_to_record(pair)
            if record and record["chain"] in CHAINS:
                key = token_key(
                    record["chain"],
                    record["address"],
                )
                if key not in found:
                    found[key] = {
                        k: record[k]
                        for k in (
                            "chain",
                            "address",
                            "name",
                            "symbol",
                            "source",
                        )
                    }

    return found


def gecko_network(chain):
    mapping = {
        "solana": "solana",
        "ethereum": "eth",
        "base": "base",
        "bsc": "bsc",
        "polygon": "polygon_pos",
        "arbitrum": "arbitrum",
        "optimism": "optimism",
        "avalanche": "avax",
    }
    return mapping.get(chain)


def discover_geckoterminal():
    found = {}

    # GeckoTerminal public API network-specific pool endpoints.
    for chain in CHAINS:
        network = gecko_network(chain)
        if not network:
            continue

        data = api(
            f"{GECKO_BASE}/networks/{network}/trending_pools",
            params={"page": 1},
        )
        if not isinstance(data, dict):
            continue

        for item in data.get("data") or []:
            attrs = item.get("attributes") or {}
            rel = item.get("relationships") or {}
            token_rel = rel.get("base_token") or {}
            token_id = (
                (token_rel.get("data") or {}).get("id", "")
            )

            address = (
                token_id.split("_", 1)[-1]
                if "_" in token_id
                else ""
            )
            if not address:
                address = attrs.get("base_token_address") or ""
            if not address:
                continue

            key = token_key(chain, address)
            found.setdefault(
                key,
                {
                    "chain": chain,
                    "address": address,
                    "name": attrs.get("name") or "Unknown",
                    "symbol": (
                        attrs.get("name", "UNKNOWN").split(" / ")[0]
                    ),
                    "source": "GeckoTerminal",
                },
            )

    return found


def discover_dexview():
    """Best-effort discovery; skips cleanly without configured access."""
    found = {}

    # Do not assume undocumented DexView endpoints.
    key = os.getenv("DEXVIEW_API_KEY", "").strip()
    if not key:
        print(
            "DexView: no DEXVIEW_API_KEY configured; "
            "skipping DexView API requests."
        )
        return found

    headers = {"X-API-KEY": key, "apikey": key}
    endpoint = os.getenv("DEXVIEW_DISCOVERY_URL", "").strip()

    if not endpoint:
        print(
            "DexView: set DEXVIEW_DISCOVERY_URL to a "
            "documented endpoint to enable discovery."
        )
        return found

    data = api(endpoint, headers=headers)
    if not isinstance(data, dict):
        return found

    items = (
        data.get("data")
        or data.get("tokens")
        or data.get("result")
        or []
    )

    if isinstance(items, dict):
        items = items.get("items") or items.get("tokens") or []

    if not isinstance(items, list):
        return found

    for item in items:
        if not isinstance(item, dict):
            continue

        chain = norm_chain(
            item.get("chainId")
            or item.get("chain")
            or item.get("network")
        )
        address = (
            item.get("tokenAddress")
            or item.get("address")
            or item.get("contractAddress")
        )

        if chain in CHAINS and address:
            found.setdefault(
                token_key(chain, address),
                {
                    "chain": chain,
                    "address": address,
                    "name": item.get("name") or "Unknown",
                    "symbol": item.get("symbol") or "UNKNOWN",
                    "source": "DexView",
                },
            )

    return found
