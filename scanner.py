import os
import json
import requests
from datetime import datetime, timezone
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
DEXSCREENER_URL = "https://api.dexscreener.com"
SIGNALS_FILE = "signals.json"
# Networks to monitor
SUPPORTED_CHAINS = {"ethereum", "solana", "robinhood"}
# Minimum requirements for an alert
MIN_LIQUIDITY = 10000
MIN_VOLUME_24H = 25000
MIN_CHANGE_1H = 10
# Searches broaden discovery beyond latest token profiles.
SEARCH_TERMS = [
    "WETH",
    "USDC",
    "USDT",
    "PEPE",
    "MEME",
    "PUMP",
    "BONK",
    "WIF",
    "ETH",
    "SOL",
]
def send_telegram(message):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram credentials are not configured.")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        response = requests.post(
            url,
            json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": message,
                "disable_web_page_preview": True,
            },
            timeout=15,
        )
        response.raise_for_status()
    except requests.RequestException as error:
        print(f"Telegram send failed: {error}")
def get_json(url, params=None):
    response = requests.get(url, params=params, timeout=20)
    response.raise_for_status()
    return response.json()
def discover_pairs():
    pairs_by_id = {}
    diagnostics = {"profiles": 0, "searches": 0, "api_errors": 0}
    # Discover recently profiled tokens.
    try:
        profiles = get_json(
            f"{DEXSCREENER_URL}/token-profiles/latest/v1"
        )
        if isinstance(profiles, list):
            diagnostics["profiles"] = len(profiles)
            for profile in profiles:
                chain = profile.get("chainId", "")
                address = profile.get("tokenAddress", "")
                if chain not in SUPPORTED_CHAINS or not address:
                    continue
                try:
                    data = get_json(
                        f"{DEXSCREENER_URL}/latest/dex/tokens/{address}"
                    )
                    for pair in data.get("pairs") or []:
                        if pair.get("chainId") in SUPPORTED_CHAINS:
                            pair_id = pair.get("pairAddress")
                            if pair_id:
                                key = (
                                    pair.get("chainId"),
                                    pair_id.lower(),
                                )
                                pairs_by_id[key] = pair
                except (requests.RequestException, ValueError):
                    diagnostics["api_errors"] += 1
    except (requests.RequestException, ValueError) as error:
        print(f"Profile discovery failed: {error}")
        diagnostics["api_errors"] += 1
    # Search for more pairs, including tokens without recent profiles.
    for term in SEARCH_TERMS:
        try:
            data = get_json(
                f"{DEXSCREENER_URL}/latest/dex/search",
                params={"q": term},
            )
            diagnostics["searches"] += 1
            for pair in data.get("pairs") or []:
                if pair.get("chainId") not in SUPPORTED_CHAINS:
                    continue
                pair_id = pair.get("pairAddress")
                if not pair_id:
                    continue
                key = (pair.get("chainId"), pair_id.lower())
                pairs_by_id[key] = pair
        except (requests.RequestException, ValueError) as error:
            diagnostics["api_errors"] += 1
            print(f"Search for {term} failed: {error}")
    print(
        f"Discovery: {len(pairs_by_id)} unique pairs; "
        f"{diagnostics['profiles']} profiles; "
        f"{diagnostics['searches']} searches; "
        f"{diagnostics['api_errors']} API errors."
    )
    return list(pairs_by_id.values())
def scan_tokens():
    now = datetime.now(timezone.utc)
    updated = now.strftime("%Y-%m-%d %H:%M UTC")
    dashboard_signals = []
    seen_signals = set()
    diagnostics = {
        chain: {"discovered": 0, "passed": 0}
        for chain in SUPPORTED_CHAINS
    }
    rejected = {"liquidity": 0, "volume": 0, "change": 0}
    pairs = discover_pairs()
    for pair in pairs:
        chain = pair.get("chainId", "")
        if chain not in SUPPORTED_CHAINS:
            continue
        diagnostics[chain]["discovered"] += 1
        token = pair.get("baseToken") or {}
        symbol = token.get("symbol", "Unknown")
        address = token.get("address", "")
        pair_address = pair.get("pairAddress", "")
        liquidity = (pair.get("liquidity") or {}).get("usd") or 0
        volume = (pair.get("volume") or {}).get("h24") or 0
        change = (pair.get("priceChange") or {}).get("h1") or 0
        try:
            liquidity = float(liquidity)
            volume = float(volume)
            change = float(change)
        except (TypeError, ValueError):
            continue
        if liquidity < MIN_LIQUIDITY:
            rejected["liquidity"] += 1
            continue
        if volume < MIN_VOLUME_24H:
            rejected["volume"] += 1
            continue
        if change < MIN_CHANGE_1H:
            rejected["change"] += 1
            continue
        signal_key = (chain, pair_address.lower())
        if signal_key in seen_signals:
            continue
        seen_signals.add(signal_key)
        market_cap = pair.get("marketCap") or pair.get("fdv") or 0
        price = pair.get("priceUsd") or "—"
        pair_url = pair.get("url", "")
        pair_created = pair.get("pairCreatedAt")
        signal = {
            "token": symbol,
            "chain": chain,
            "address": address,
            "price": price,
            "change": round(change, 2),
            "volume": round(volume, 2),
            "liquidity": round(liquidity, 2),
            "marketCap": round(float(market_cap or 0), 2),
            "pairUrl": pair_url,
            "pairAddress": pair_address,
            "pairCreatedAt": pair_created,
            "updated": updated,
        }
        dashboard_signals.append(signal)
        diagnostics[chain]["passed"] += 1
        message = (
            f"🚨 CRYPTO MOMENTUM ALERT\n\n"
            f"Token: {symbol}\n"
            f"Network: {chain}\n"
            f"1-hour change: {change:.1f}%\n"
            f"24-hour volume: ${volume:,.0f}\n"
            f"Liquidity: ${liquidity:,.0f}\n"
            f"Market cap/FDV: ${float(market_cap or 0):,.0f}\n"
            f"Pair created timestamp: {pair_created or 'Unknown'}\n"
            f"Holder concentration: NOT checked\n"
            f"Pair: {pair_url or 'Unavailable'}\n"
            f"Checked: {updated}\n\n"
            f"⚠️ Momentum does not guarantee safety or profit."
        )
        send_telegram(message)
    try:
        with open(SIGNALS_FILE, "w", encoding="utf-8") as file:
            json.dump(
                {
                    "updated": updated,
                    "signals": dashboard_signals,
                },
                file,
                indent=2,
            )
        print("Network results:")
        for chain, counts in diagnostics.items():
            print(
                f"{chain}: discovered={counts['discovered']}, "
                f"passed_filters={counts['passed']}"
            )
        print(f"Filter rejections: {rejected}")
        print(f"Saved {len(dashboard_signals)} dashboard signals.")
    except OSError as error:
        print(f"Could not save dashboard signals: {error}")
if __name__ == "__main__":
    scan_tokens()
