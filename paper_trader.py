import json
import os
from datetime import datetime, timezone
from pathlib import Path

import requests

# CRYPTO PULSE PAPER TRADER
# Simulation only. Never places live trades.

STARTING_BALANCE = float(
    os.getenv("PAPER_STARTING_BALANCE", "1000")
)
TRADE_SIZE = float(
    os.getenv("PAPER_TRADE_SIZE", "50")
)
STOP_LOSS_PCT = float(
    os.getenv("PAPER_STOP_LOSS_PCT", "10")
)
TAKE_PROFIT_PCT = float(
    os.getenv("PAPER_TAKE_PROFIT_PCT", "25")
)
MIN_SCORE = int(os.getenv("MIN_SCORE", "45"))
MAX_OPEN_POSITIONS = int(
    os.getenv("PAPER_MAX_OPEN_POSITIONS", "10")
)
TOTAL_PROFIT_TARGET = float(
    os.getenv("PAPER_TOTAL_PROFIT_TARGET", "100")
)
MIN_LIQUIDITY = float(
    os.getenv("MIN_LIQUIDITY", "10000")
)
MIN_VOLUME = float(
    os.getenv("MIN_VOLUME_24H", "25000")
)

# False means unverified tokens cannot be bought, even in simulation.
# Set PAPER_ALLOW_UNVERIFIED=true only if you intentionally want
# to experiment with unverified tokens in paper trading.
ALLOW_UNVERIFIED = (
    os.getenv("PAPER_ALLOW_UNVERIFIED", "false")
    .strip()
    .lower()
    in ("1", "true", "yes")
)

SIGNALS_FILE = Path("signals.json")
TRADES_FILE = Path("paper_trades.json")

DEX = "https://api.dexscreener.com"

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "CryptoPulsePaperTrader/2.0"
})


def now():
    return datetime.now(timezone.utc).isoformat()


def load_json(path, default):
    try:
        with path.open("r", encoding="utf-8") as file:
            return json.load(file)
    except (OSError, ValueError):
        return default


def save_json(path, data):
    temp = path.with_suffix(path.suffix + ".tmp")

    with temp.open("w", encoding="utf-8") as file:
        json.dump(data, file, indent=2)

    temp.replace(path)


def token_key(signal):
    chain = str(signal.get("chain", "")).lower()
    address = str(signal.get("address", "")).lower()

    if not chain or not address:
        return ""

    return f"{chain}:{address}"


def price_from_dex(chain, address):
    """Fetch the latest available DexScreener price."""

    try:
        response = SESSION.get(
            f"{DEX}/token-pairs/v1/{chain}/{address}",
            timeout=12,
        )
        response.raise_for_status()
        pairs = response.json()

        if not isinstance(pairs, list) or not pairs:
            return None

        valid_pairs = [
            pair for pair in pairs
            if isinstance(pair, dict)
        ]

        if not valid_pairs:
            return None

        best = max(
            valid_pairs,
            key=lambda pair: float(
                (pair.get("liquidity") or {}).get("usd") or 0
            ),
        )

        price = float(best.get("priceUsd") or 0)

        return price if price > 0 else None

    except (
        requests.RequestException,
        ValueError,
        TypeError,
    ):
        return None


def main():
    signals = load_json(SIGNALS_FILE, [])

    if not isinstance(signals, list):
        signals = []

    portfolio = load_json(TRADES_FILE, None)

    if not isinstance(portfolio, dict):
        portfolio = {
            "mode": "PAPER_ONLY",
            "starting_balance": STARTING_BALANCE,
            "cash": STARTING_BALANCE,
            "open_positions": {},
            "closed_trades": [],
            "created_at": now(),
        }

    positions = portfolio.setdefault("open_positions", {})
    closed = portfolio.setdefault("closed_trades", [])

    if not isinstance(positions, dict):
        positions = {}
        portfolio["open_positions"] = positions

    if not isinstance(closed, list):
        closed = []
        portfolio["closed_trades"] = closed

    # Keep an existing portfolio's original balance.
    starting = float(
        portfolio.get("starting_balance", STARTING_BALANCE)
    )
    cash = float(portfolio.get("cash", starting))

    markets = {}

    for signal in signals:
        if not isinstance(signal, dict):
            continue

        key = token_key(signal)
        metrics = signal.get("metrics") or {}
        price = float(metrics.get("price_usd") or 0)

        if key and price > 0:
            markets[key] = {
                "price": price,
                "signal": signal,
            }

    # Refresh prices for existing positions, even when they no
    # longer appear in the scanner's current candidate list.
    for key, position in list(positions.items()):
        if key in markets:
            continue

        price = price_from_dex(
            str(position.get("chain") or ""),
            str(position.get("address") or ""),
        )

        if price:
            markets[key] = {
                "price": price,
                "signal": None,
            }

    # --------------------------------------------------------
    # MANAGE OPEN POSITIONS FIRST
    # --------------------------------------------------------

    for key in list(positions.keys()):
        position = positions[key]
        market = markets.get(key)

        # Do not invent prices if the market data is unavailable.
        if not market:
            continue

        price = market["price"]
        entry = float(position.get("entry_price") or 0)

        if entry <= 0:
            continue

        pnl_pct = (price / entry - 1) * 100
        exit_reason = None

        if pnl_pct <= -STOP_LOSS_PCT:
            exit_reason = "STOP_LOSS"
        elif pnl_pct >= TAKE_PROFIT_PCT:
            exit_reason = "TAKE_PROFIT"

        if not exit_reason:
            continue

        quantity = float(position.get("quantity") or 0)
        proceeds = quantity * price
        cost = float(position.get("cost_usd") or 0)
        pnl = proceeds - cost

        cash += proceeds

        closed.append({
            **position,
            "exit_time": now(),
            "exit_price": price,
            "exit_value_usd": round(proceeds, 8),
            "realised_pnl_usd": round(pnl, 8),
            "realised_pnl_pct": round(pnl_pct, 4),
            "exit_reason": exit_reason,
            "status": "CLOSED",
        })

        print(
            f"PAPER SELL {position.get('symbol', 'UNKNOWN')} | "
            f"{exit_reason} | "
            f"P/L ${pnl:.2f} ({pnl_pct:.2f}%)"
        )

        del positions[key]

    # Calculate realised profit AFTER exits have been processed.
    realised = sum(
        float(trade.get("realised_pnl_usd", 0))
        for trade in closed
    )

    # Once reached, pause new purchases.
    # Existing positions continue to be managed.
    target_hit = realised >= TOTAL_PROFIT_TARGET

    if target_hit:
        print(
            f"Realised profit target ${TOTAL_PROFIT_TARGET:.2f} "
            "reached; new paper buys paused."
        )

    # --------------------------------------------------------
    # OPEN NEW PAPER POSITIONS
    # --------------------------------------------------------

    elif TRADE_SIZE > 0 and cash >= TRADE_SIZE:

        for signal in signals:
            if not isinstance(signal, dict):
                continue

            key = token_key(signal)

            if not key or key in positions:
                continue

            if len(positions) >= MAX_OPEN_POSITIONS:
                break

            metrics = signal.get("metrics") or {}
            price = float(metrics.get("price_usd") or 0)
            score = int(signal.get("score") or 0)

            liquidity = float(
                metrics.get("liquidity_usd") or 0
            )
            volume = float(
                metrics.get("volume_24h_usd") or 0
            )

            if price <= 0 or score < MIN_SCORE:
                continue

            if liquidity < MIN_LIQUIDITY:
                continue

            if volume < MIN_VOLUME:
                continue

            checks = signal.get("onchain_checks") or {}

            verified = bool(
                signal.get("paper_eligible")
                and checks.get("verified")
                and not checks.get("skip")
            )

            if not verified:
                if not ALLOW_UNVERIFIED:
                    print(
                        f"PAPER SKIP "
                        f"{signal.get('symbol', 'UNKNOWN')}: "
                        f"risk checks not verified "
                        f"({signal.get('risk_status', 'UNKNOWN')})"
                    )
                    continue

                print(
                    f"PAPER EXPERIMENTAL ENTRY "
                    f"{signal.get('symbol', 'UNKNOWN')}: "
                    "unverified risk data; simulation only."
                )

            if cash < TRADE_SIZE:
                break

            quantity = TRADE_SIZE / price

            position = {
                "token_key": key,
                "name": signal.get("name", "Unknown"),
                "symbol": signal.get("symbol", "UNKNOWN"),
                "chain": signal.get("chain"),
                "address": signal.get("address"),
                "entry_time": now(),
                "entry_price": price,
                "quantity": quantity,
                "cost_usd": TRADE_SIZE,
                "score_at_entry": score,
                "risk_status": signal.get(
                    "risk_status", "UNKNOWN"
                ),
                "risk_verified": verified,
                "stop_loss_price": (
                    price * (1 - STOP_LOSS_PCT / 100)
                ),
                "take_profit_price": (
                    price * (1 + TAKE_PROFIT_PCT / 100)
                ),
                "status": "OPEN",
            }

            positions[key] = position
            cash -= TRADE_SIZE

            print(
                f"PAPER BUY {position['symbol']} | "
                f"${TRADE_SIZE:.2f} at ${price:.10g} | "
                f"Score {score} | "
                f"Risk {position['risk_status']}"
            )

            if cash < TRADE_SIZE:
                break

    # --------------------------------------------------------
    # UPDATE PORTFOLIO VALUES
    # --------------------------------------------------------

    unrealised = 0.0
    open_value = 0.0

    for key, position in positions.items():
        market = markets.get(key)

        if market:
            price = market["price"]
        else:
            # Keep the last known price when the API fails.
            price = float(
                position.get("current_price")
                or position.get("entry_price")
                or 0
            )

        quantity = float(position.get("quantity") or 0)
        cost = float(position.get("cost_usd") or 0)
        entry = float(position.get("entry_price") or 0)

        value = quantity * price
        position_pnl = value - cost

        position.update({
            "current_price": price,
            "current_value_usd": round(value, 8),
            "unrealised_pnl_usd": round(position_pnl, 8),
            "unrealised_pnl_pct": (
                round((price / entry - 1) * 100, 4)
                if entry > 0 else 0
            ),
        })

        unrealised += position_pnl
        open_value += value

    realised = sum(
        float(trade.get("realised_pnl_usd", 0))
        for trade in closed
    )

    equity = cash + open_value

    portfolio.update({
        "mode": "PAPER_ONLY",
        "starting_balance": starting,
        "cash": round(cash, 8),
        "open_positions": positions,
        "closed_trades": closed[-2000:],
        "open_position_count": len(positions),
        "closed_trade_count": len(closed),
        "open_positions_value_usd": round(open_value, 8),
        "unrealised_pnl_usd": round(unrealised, 8),
        "realised_pnl_usd": round(realised, 8),
        "total_equity_usd": round(equity, 8),
        "total_pnl_usd": round(equity - starting, 8),
        "profit_target_usd": TOTAL_PROFIT_TARGET,
        "profit_target_hit": (
            realised >= TOTAL_PROFIT_TARGET
        ),
        "last_updated": now(),
        "settings": {
            "trade_size_usd": TRADE_SIZE,
            "stop_loss_pct": STOP_LOSS_PCT,
            "take_profit_pct": TAKE_PROFIT_PCT,
            "minimum_score": MIN_SCORE,
            "maximum_open_positions": MAX_OPEN_POSITIONS,
            "allow_unverified_paper_entries": ALLOW_UNVERIFIED,
        },
    })

    save_json(TRADES_FILE, portfolio)

    print(
        f"Paper trading finished | "
        f"Cash ${cash:.2f} | "
        f"Open positions {len(positions)} | "
        f"Realised P/L ${realised:.2f} | "
        f"Unrealised P/L ${unrealised:.2f} | "
        f"Equity ${equity:.2f}"
    )


if __name__ == "__main__":
    main()
