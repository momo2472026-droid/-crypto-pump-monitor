import os
import requests
from datetime import datetime, timezone

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

DEXSCREENER_URL = "https://api.dexscreener.com/token-profiles/latest/v1"


def send_telegram(message):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram credentials are not configured.")
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"

    try:
        requests.post(
            url,
            json={"chat_id": TELEGRAM_CHAT_ID, "text": message},
            timeout=15
        ).raise_for_status()
    except requests.RequestException as error:
        print(f"Telegram send failed: {error}")


def scan_tokens():
    try:
        response = requests.get(DEXSCREENER_URL, timeout=20)
        response.raise_for_status()
        profiles = response.json()
    except (requests.RequestException, ValueError) as error:
        print(f"Scanner request failed: {error}")
        return

    if not isinstance(profiles, list):
        print("Unexpected API response.")
        return

    seen = set()

    for profile in profiles:
        chain = profile.get("chainId", "")
        address = profile.get("tokenAddress", "")

        if chain not in ("ethereum", "solana") or not address:
            continue

        key = (chain, address.lower())
        if key in seen:
            continue
        seen.add(key)

        try:
            url = f"https://api.dexscreener.com/latest/dex/tokens/{address}"
            result = requests.get(url, timeout=20)
            result.raise_for_status()
            pairs = result.json().get("pairs") or []
        except (requests.RequestException, ValueError):
            continue

        for pair in pairs:
            if pair.get("chainId") != chain:
                continue

            liquidity = (pair.get("liquidity") or {}).get("usd") or 0
            volume = (pair.get("volume") or {}).get("h24") or 0
            change = (pair.get("priceChange") or {}).get("h1") or 0
            symbol = (pair.get("baseToken") or {}).get("symbol", "Unknown")
            market_cap = pair.get("marketCap") or pair.get("fdv") or 0

            # Initial filters: tune these later to suit your strategy.
            if liquidity < 10000 or volume < 25000 or change < 10:
                continue

            message = (
                f"🚨 CRYPTO MOMENTUM ALERT\n\n"
                f"Token: {symbol}\n"
                f"Network: {chain}\n"
                f"1-hour change: {change:.1f}%\n"
                f"24-hour volume: ${volume:,.0f}\n"
                f"Liquidity: ${liquidity:,.0f}\n"
                f"Market cap/FDV: ${market_cap:,.0f}\n"
                f"Pair age: {pair.get('pairCreatedAt', 'Unknown')}\n"
                f"Risk check: holder concentration NOT checked\n"
                f"Pair: {pair.get('url', 'Unavailable')}\n"
                f"Checked: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}\n\n"
                f"⚠️ Momentum is not proof of a safe or profitable trade."
            )

            send_telegram(message)


if __name__ == "__main__":
    scan_tokens()
