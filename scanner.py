import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

import requests

BASE = "https://api.dexscreener.com"
CHAINS = {"ethereum", "solana", "robinhood"}
MIN_LIQUIDITY = float(os.getenv("MIN_LIQUIDITY", "10000"))
MIN_VOLUME_24H = float(os.getenv("MIN_VOLUME_24H", "25000"))
MIN_CHANGE_1H = float(os.getenv("MIN_CHANGE_1H", "10"))
MIN_SCORE = int(os.getenv("MIN_SCORE", "45"))
MAX_TOKENS = int(os.getenv("MAX_TOKENS_PER_SCAN", "100"))
ALERT_COOLDOWN_HOURS = float(os.getenv("ALERT_COOLDOWN_HOURS", "6"))

SIGNALS_FILE = Path("signals.json")
STATE_FILE = Path("scanner_state.json")
HISTORY_FILE = Path("history.json")

session = requests.Session()
session.headers.update({"User-Agent": "CryptoPulse/2.0"})


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def read_json(path, default):
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return default


def write_json(path, data):
    temp = path.with_suffix(path.suffix + ".tmp")
    with temp.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    temp.replace(path)


def number(value, default=0.0):
    try:
        result = float(value)
        return result if result == result and abs(result) != float("inf") else default
    except (TypeError, ValueError):
        return default


def api(path, params=None):
    try:
        response = session.get(BASE + path, params=params, timeout=20)
        response.raise_for_status()
        return response.json()
    except (requests.RequestException, ValueError) as exc:
        print(f"API warning: {path}: {exc}")
        return None


def discover_tokens():
    """Combine discovery feeds and pair searches."""
    found = {}

    feeds = [
        "/token-profiles/latest/v1",
        "/token-boosts/latest/v1",
        "/token-boosts/top/v1",
        "/community-takeovers/latest/v1",
    ]

    for endpoint in feeds:
        data = api(endpoint)
        if isinstance(data, list):
            for item in data:
                chain = str(item.get("chainId", "")).lower()
                address = item.get("tokenAddress")
                if chain in CHAINS and address:
                    found[(chain, address.lower())] = {
                        "chain": chain,
                        "address": address,
                        "source": endpoint.rsplit("/", 1)[-1],
                    }

    # Search terms are discovery hints, not recommendations.
    queries = [
        "WETH", "USDC", "USDT", "ETH", "SOL", "PEPE",
        "SHIB", "BONK", "WIF", "PUMP", "MEME", "MOG",
        "TURBO", "DOGE", "AI", "INU",
    ]

    for query in queries:
        data = api("/latest/dex/search", {"q": query})
        if not isinstance(data, dict):
            continue

        for pair in data.get("pairs") or []:
            chain = str(pair.get("chainId", "")).lower()
            token = pair.get("baseToken") or {}
            address = token.get("address")

            if chain in CHAINS and address:
                key = (chain, address.lower())
                found.setdefault(
                    key,
                    {
                        "chain": chain,
                        "address": address,
                        "source": "pair_search",
                    },
                )

        # Be polite to the public API.
        time.sleep(0.15)

    print(f"Discovery: {len(found)} unique token/chain combinations")
    return list(found.values())[:MAX_TOKENS]


def get_pairs(token):
    chain = token["chain"]
    address = token["address"]

    data = api(
        f"/token-pairs/v1/{quote(chain, safe='')}/{quote(address, safe='')}"
    )

    if isinstance(data, list):
        return data
    return []


def score_pair(pair):
    """Transparent heuristic score; not a probability of profit."""
    liquidity = number((pair.get("liquidity") or {}).get("usd"))
    volume = pair.get("volume") or {}
    volume24 = number(volume.get("h24"))
    volume1 = number(volume.get("h1"))
    change = pair.get("priceChange") or {}
    change1 = number(change.get("h1"))
    change24 = number(change.get("h24"))
    txns = pair.get("txns") or {}
    tx1 = txns.get("h1") or {}
    buys = number(tx1.get("buys"))
    sells = number(tx1.get("sells"))
    age_hours = None

    created = number(pair.get("pairCreatedAt"))
    if created > 0:
        age_hours = max(0, (time.time() * 1000 - created) / 3_600_000)

    score = 0
    reasons = []

    # Volume
    if volume24 >= 250_000:
        score += 25
        reasons.append("Very strong 24h volume")
    elif volume24 >= 100_000:
        score += 20
        reasons.append("Strong 24h volume")
    elif volume24 >= MIN_VOLUME_24H:
        score += 12
        reasons.append("Meets 24h volume threshold")

    # Buy/sell balance
    total_tx = buys + sells
    if total_tx >= 10:
        buy_ratio = buys / total_tx
        if buy_ratio >= 0.70:
            score += 20
            reasons.append("Buy-heavy recent transactions")
        elif buy_ratio >= 0.55:
            score += 12
            reasons.append("More buys than sells")
        elif buy_ratio < 0.35:
            score -= 10
            reasons.append("Sell-heavy recent transactions")
    else:
        buy_ratio = None

    # Price momentum
    if 5 <= change1 <= 30:
        score += 15
        reasons.append("Positive 1h momentum")
    elif 0 < change1 < 5:
        score += 8
    elif change1 > 30:
        score += 8
        reasons.append("Large 1h rise; reversal risk")
    elif change1 < -10:
        score -= 10
        reasons.append("Negative 1h momentum")

    # Liquidity
    if liquidity >= 100_000:
        score += 15
        reasons.append("High liquidity")
    elif liquidity >= 50_000:
        score += 12
    elif liquidity >= MIN_LIQUIDITY:
        score += 8
    else:
        reasons.append("Low liquidity")

    # Pair age: new pairs can be interesting but riskier.
    if age_hours is not None:
        if 0 <= age_hours <= 1:
            score += 10
            reasons.append("Very new pair")
        elif age_hours <= 6:
            score += 7
            reasons.append("Recently created pair")
        elif age_hours <= 24:
            score += 4

    # Recent activity
    if volume1 >= 10_000 and total_tx >= 30:
        score += 10
        reasons.append("Strong recent activity")
    elif total_tx >= 10:
        score += 5

    score = max(0, min(100, score))

    risks = []
    if liquidity < MIN_LIQUIDITY:
        risks.append("LOW LIQUIDITY")
    if volume24 < MIN_VOLUME_24H:
        risks.append("LOW 24H VOLUME")
    if change1 > 100:
        risks.append("EXTREME 1H PRICE RISE")
    if change24 < -50:
        risks.append("LARGE 24H DECLINE")
    if age_hours is not None and age_hours < 1:
        risks.append("VERY NEW PAIR")
    if liquidity > 0 and volume24 / liquidity > 20:
        risks.append("UNUSUALLY HIGH VOLUME/LIQUIDITY RATIO")
    if not pair.get("priceUsd"):
        risks.append("PRICE DATA MISSING")

    return {
        "score": score,
        "reasons": reasons,
        "risks": risks,
        "buyRatio": round(buy_ratio * 100, 1) if buy_ratio is not None else None,
        "ageHours": round(age_hours, 2) if age_hours is not None else None,
        "buys1h": int(buys),
        "sells1h": int(sells),
        "volume1h": volume1,
        "change1h": change1,
        "change24h": change24,
    }


def telegram(message):
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")

    if not token or not chat_id:
        print("Telegram not configured; skipping alert.")
        return

    try:
        response = session.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": message,
                "disable_web_page_preview": True,
            },
            timeout=20,
        )
        response.raise_for_status()
        if not response.json().get("ok"):
            print("Telegram API did not confirm the message.")
    except requests.RequestException as exc:
        print(f"Telegram warning: {exc}")


def format_alert(signal, update=False):
    risks = signal.get("risks") or []
    reasons = signal.get("reasons") or []
    risk_text = ", ".join(risks) if risks else "No configured market-data flags"
    reason_text = ", ".join(reasons[:4]) if reasons else "Limited supporting data"
    buy_ratio = signal.get("buyRatio")
    buy_text = f"{buy_ratio}% buys" if buy_ratio is not None else "Buy/sell data unavailable"

    label = "SIGNAL UPDATE" if update else "NEW SIGNAL"
    return (
        f"🚨 CRYPTO PULSE — {label}\n"
        f"{signal.get('name', 'Unknown')} ({signal.get('symbol', '?')})\n"
        f"Network: {signal['chain']}\n"
        f"Momentum score: {signal['score']}/100\n"
        f"Price: ${signal['priceUsd']}\n"
        f"1h: {signal['change1h']:+.2f}% | 24h: {signal['change24h']:+.2f}%\n"
        f"Volume 24h: ${signal['volume24h']:,.0f}\n"
        f"Liquidity: ${signal['liquidity']:,.0f}\n"
        f"Activity: {signal['buys1h']} buys / {signal['sells1h']} sells ({buy_text})\n"
        f"Risk flags: {risk_text}\n"
        f"Why it scored: {reason_text}\n"
        f"{signal['pairUrl']}"
    )


def main():
    state = read_json(STATE_FILE, {"alerts": {}, "firstSeen": {}})
    history = read_json(HISTORY_FILE, {})
    old_signals = read_json(SIGNALS_FILE, [])
    if not isinstance(old_signals, list):
        old_signals = []

    now = datetime.now(timezone.utc)
    now_ts = now.timestamp()
    candidates = discover_tokens()
    best_pairs = {}
    errors = 0

    for token in candidates:
        pairs = get_pairs(token)
        if not pairs:
            continue

        # Keep the most liquid pool for each token.
        for pair in pairs:
            if str(pair.get("chainId", "")).lower() not in CHAINS:
                continue

            base = pair.get("baseToken") or {}
            address = str(base.get("address", "")).lower()
            if not address:
                continue

            key = (str(pair.get("chainId", "")).lower(), address)
            current_liquidity = number((pair.get("liquidity") or {}).get("usd"))
            previous_liquidity = number(
                (best_pairs.get(key, {}).get("liquidity") or {}).get("usd")
            )
            if key not in best_pairs or current_liquidity > previous_liquidity:
                best_pairs[key] = pair

        time.sleep(0.12)

    signals = []
    new_alerts = 0
    tracked = 0

    for (chain, address), pair in best_pairs.items():
        base = pair.get("baseToken") or {}
        liquidity = number((pair.get("liquidity") or {}).get("usd"))
        volume24 = number((pair.get("volume") or {}).get("h24"))
        price = number(pair.get("priceUsd"), -1)

        if price <= 0:
            continue

        metrics = score_pair(pair)
        key = f"{chain}:{address}"
        first_seen = state["firstSeen"].setdefault(key, now.isoformat())

        signal = {
            "id": key,
            "name": base.get("name") or "Unknown",
            "symbol": base.get("symbol") or "?",
            "chain": chain,
            "address": base.get("address") or address,
            "pairAddress": pair.get("pairAddress") or "",
            "priceUsd": f"{price:.12g}",
            "change1h": metrics["change1h"],
            "change24h": metrics["change24h"],
            "volume1h": metrics["volume1h"],
            "volume24h": volume24,
            "liquidity": liquidity,
            "marketCap": number(pair.get("marketCap")),
            "fdv": number(pair.get("fdv")),
            "buys1h": metrics["buys1h"],
            "sells1h": metrics["sells1h"],
            "buyRatio": metrics["buyRatio"],
            "ageHours": metrics["ageHours"],
            "score": metrics["score"],
            "reasons": metrics["reasons"],
            "risks": metrics["risks"],
            "firstSeen": first_seen,
            "updated": now.isoformat(),
            "pairCreatedAt": pair.get("pairCreatedAt"),
            "pairUrl": pair.get("url") or "",
        }

        # Historical snapshots support later performance calculations.
        history.setdefault(key, [])
        history[key].append({
            "time": now.isoformat(),
            "timestamp": now_ts,
            "price": price,
            "liquidity": liquidity,
            "volume24h": volume24,
            "score": metrics["score"],
        })
        history[key] = history[key][-600:]
        tracked += 1

        # Compute price change from the closest snapshot at/after each target.
        snapshots = history[key]
        for minutes in (5, 15, 30, 60, 240, 1440):
            eligible = [
                item for item in snapshots
                if now_ts - item["timestamp"] >= minutes * 60
            ]
            field = f"return{minutes}m"
            if eligible:
                start = eligible[-1]["price"]
                signal[field] = round((price / start - 1) * 100, 2) if start else None
            else:
                signal[field] = None

        # Basic alert filters.
        qualifies = (
            liquidity >= MIN_LIQUIDITY
            and volume24 >= MIN_VOLUME_24H
            and metrics["score"] >= MIN_SCORE
        )

        if qualifies:
            previous_alert = state["alerts"].get(key, {})
            last_alert_ts = number(previous_alert.get("timestamp"))
            cooldown = ALERT_COOLDOWN_HOURS * 3600
            score_improved = metrics["score"] >= number(previous_alert.get("score")) + 15

            if not previous_alert or (now_ts - last_alert_ts >= cooldown) or score_improved:
                is_update = bool(previous_alert)
                telegram(format_alert(signal, update=is_update))
                state["alerts"][key] = {
                    "timestamp": now_ts,
                    "score": metrics["score"],
                    "price": price,
                }
                new_alerts += 1

        signals.append(signal)

    signals.sort(key=lambda item: (item["score"], item["volume24h"]), reverse=True)

    # Preserve history for the dashboard, but limit current displayed signals.
    write_json(SIGNALS_FILE, signals[:300])
    write_json(HISTORY_FILE, history)
    write_json(STATE_FILE, state)

    print(
        f"Scan complete: candidates={len(candidates)}, "
        f"pairs={len(best_pairs)}, tracked={tracked}, "
        f"displayed={min(len(signals), 300)}, alerts={new_alerts}, "
        f"history_tokens={len(history)}, errors={errors}"
    )


if __name__ == "__main__":
    main()
