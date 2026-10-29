import json
import os
from datetime import datetime, timezone
from pathlib import Path

import requests


# CRYPTO PULSE PAPER TRADER
# Simulation only. Never executes real cryptocurrency trades.

STARTING_BALANCE = float(os.getenv("PAPER_STARTING_BALANCE", "1000"))
TRADE_SIZE = float(os.getenv("PAPER_TRADE_SIZE", "50"))
STOP_LOSS_PCT = float(os.getenv("PAPER_STOP_LOSS_PCT", "10"))
TAKE_PROFIT_PCT = float(os.getenv("PAPER_TAKE_PROFIT_PCT", "25"))
MIN_SCORE = int(os.getenv("MIN_SCORE", "45"))
MAX_OPEN_POSITIONS = int(os.getenv("PAPER_MAX_OPEN_POSITIONS", "10"))
TOTAL_PROFIT_TARGET = float(os.getenv("PAPER_TOTAL_PROFIT_TARGET", "100"))
MIN_LIQUIDITY = float(os.getenv("MIN_LIQUIDITY", "10000"))
MIN_VOLUME = float(os.getenv("MIN_VOLUME_24H", "25000"))

# This only permits experimental PAPER entries when risk data is unknown.
# It must never override a confirmed risk flag.
ALLOW_UNVERIFIED = (
    os.getenv("PAPER_ALLOW_UNVERIFIED", "false").strip().lower()
    in ("1", "true", "yes")
)

SIGNALS_FILE = Path("signals.json")
TRADES_FILE = Path("paper_trades.json")

DEX = "https://api.dexscreener.com"

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "CryptoPulsePaperTrader/2.0"})


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


def number(value, default=0.0):
    try:
        result = float(value)
        return result if result == result else default
    except (TypeError, ValueError):
        return default


def token_key(signal):
    return (
        f"{str(signal.get('chain', '')).lower()}:"
        f"{str(signal.get('address', '')).lower()}"
    )


def price_from_dex(chain, address):
    """Fetch a fresh price for an existing position."""

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

        best_pair = max(
            valid_pairs,
            key=lambda pair: number(
                (pair.get("liquidity") or {}).get("usd")
            ),
        )

        price = number(best_pair.get("priceUsd"))

        return price if price > 0 else None

    except (
        requests.RequestException,
        ValueError,
        TypeError,
    ) as error:
        print(
            f"Could not refresh price for {chain}:{address}: {error}"
        )
        return None


def risk_decision(signal):
    """
    Returns:
        allowed: whether an entry may be considered
        verified: whether required risk checks passed
        reason: explanation for logs
    """

    checks = signal.get("onchain_checks") or {}

    status = str(
        signal.get("risk_status")
        or checks.get("status")
        or "UNKNOWN"
    ).upper()

    hard_block = (
        bool(checks.get("hard_block"))
        or status == "FLAGGED"
    )

    # Confirmed risk flags always block entries.
    if hard_block:
        return False, False, "Confirmed risk flag"

    verified = bool(
        checks.get("verified")
        and not checks.get("skip")
        and not checks.get("hard_block")
        and signal.get("paper_eligible", False)
    )

    if verified:
        return True, True, "Risk checks verified"

    # An unknown status is not proof of safety.
    # The optional override applies only to paper simulation.
    if ALLOW_UNVERIFIED:
        return True, False, "Experimental paper entry; risk UNKNOWN"

    return False, False, "Risk checks incomplete; verified data required"


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

    # Preserve existing portfolio balances across workflow runs.
    starting_balance = number(
        portfolio.get("starting_balance"),
        STARTING_BALANCE,
    )
    cash = number(portfolio.get("cash"), starting_balance)

    markets = {}

    # Prices from the current scanner output.
    for signal in signals:
        key = token_key(signal)
        metrics = signal.get("metrics") or {}
        price = number(metrics.get("price_usd"))

        if key and price > 0:
            markets[key] = {
                "price": price,
                "signal": signal,
            }

    # Refresh prices for open positions even if they have disappeared
    # from the latest scanner results.
    for key, position in list(positions.items()):
        if key in markets:
            continue

        chain = str(position.get("chain") or "")
        address = str(position.get("address") or "")

        if not chain or not address:
            continue

        price = price_from_dex(chain, address)

        if price is not None:
            markets[key] = {
                "price": price,
                "signal": None,
            }

    # Process exits before considering new entries.
    for key in list(positions.keys()):
        position = positions[key]
        market = markets.get(key)

        # Do not invent a price if the market data is unavailable.
        # The position stays open until a usable price is available.
        if not market:
            print(
                f"PRICE UNAVAILABLE: {position.get('symbol', 'UNKNOWN')}; "
                "position remains open."
            )
            continue

        price = number(market.get("price"))
        entry_price = number(position.get("entry_price"))

        if price <= 0 or entry_price <= 0:
            continue

        pnl_pct = (price / entry_price - 1) * 100

        exit_reason = None

        if pnl_pct <= -STOP_LOSS_PCT:
            exit_reason = "STOP_LOSS"
        elif pnl_pct >= TAKE_PROFIT_PCT:
            exit_reason = "TAKE_PROFIT"

        if not exit_reason:
            continue

        quantity = number(position.get("quantity"))
        proceeds = quantity * price
        cost = number(position.get("cost_usd"))
        pnl = proceeds - cost

        cash += proceeds

        closed.append(
            {
                **position,
                "exit_time": now(),
                "exit_price": price,
                "exit_value_usd": round(proceeds, 8),
                "realised_pnl_usd": round(pnl, 8),
                "realised_pnl_pct": round(pnl_pct, 4),
                "exit_reason": exit_reason,
                "status": "CLOSED",
            }
        )

        print(
            f"PAPER SELL {position.get('symbol', 'UNKNOWN')} | "
            f"{exit_reason} | P/L ${pnl:.2f} ({pnl_pct:.2f}%)"
        )

        del positions[key]

    # Recalculate realised profit after processing exits.
    realised = sum(
        number(trade.get("realised_pnl_usd"))
        for trade in closed
    )

    target_hit = realised >= TOTAL_PROFIT_TARGET

    if target_hit:
        print(
            f"Realised profit target ${TOTAL_PROFIT_TARGET:.2f} reached. "
            "New paper buys paused."
        )

    elif TRADE_SIZE > 0 and cash >= TRADE_SIZE:
        # Consider higher-scoring signals first.
        ranked_signals = sorted(
            signals,
            key=lambda signal: number(signal.get("score")),
            reverse=True,
        )

        for signal in ranked_signals:
            key = token_key(signal)

            if not key or key in positions:
                continue

            if len(positions) >= MAX_OPEN_POSITIONS:
                break

            metrics = signal.get("metrics") or {}

            price = number(metrics.get("price_usd"))
            score = int(number(signal.get("score")))

            if price <= 0 or score < MIN_SCORE:
                continue

            liquidity = number(metrics.get("liquidity_usd"))
            volume = number(metrics.get("volume_24h_usd"))

            if liquidity < MIN_LIQUIDITY:
                continue

            if volume < MIN_VOLUME:
                continue

            allowed, verified, reason = risk_decision(signal)

            if not allowed:
                print(
                    f"PAPER SKIP {signal.get('symbol', 'UNKNOWN')}: "
                    f"{reason}"
                )
                continue

            if not verified:
                print(
                    f"PAPER EXPERIMENTAL ENTRY "
                    f"{signal.get('symbol', 'UNKNOWN')}: "
                    f"{reason}. Simulation only."
                )

            if cash < TRADE_SIZE:
                break

            quantity = TRADE_SIZE / price

            checks = signal.get("onchain_checks") or {}

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
                "risk_status": (
                    signal.get("risk_status")
                    or checks.get("status")
                    or "UNKNOWN"
                ),
                "risk_verified": verified,
                "stop_loss_price": price * (
                    1 - STOP_LOSS_PCT / 100
                ),
                "take_profit_price": price * (
                    1 + TAKE_PROFIT_PCT / 100
                ),
                "status": "OPEN",
            }

            positions[key] = position
            cash -= TRADE_SIZE

            print(
                f"PAPER BUY {position['symbol']} | "
                f"${TRADE_SIZE:.2f} at ${price:.10g} | "
                f"Score {score} | Risk {position['risk_status']}"
            )

            if cash < TRADE_SIZE:
                break

    # Recalculate current value and unrealised P/L.
    unrealised = 0.0
    open_value = 0.0

    for key, position in positions.items():
        market = markets.get(key)

        if market:
            price = number(market.get("price"))
        else:
            # Preserve the last known price when no refresh is available.
            price = number(
                position.get("current_price"),
                number(position.get("entry_price")),
            )

        quantity = number(position.get("quantity"))
        cost = number(position.get("cost_usd"))
        entry_price = number(position.get("entry_price"))

        value = quantity * price
        position_pnl = value - cost

        pnl_pct = (
            (price / entry_price - 1) * 100
            if entry_price > 0
            else 0
        )

        position.update(
            {
                "current_price": price,
                "current_value_usd": round(value, 8),
                "unrealised_pnl_usd": round(position_pnl, 8),
                "unrealised_pnl_pct": round(pnl_pct, 4),
            }
        )

        unrealised += position_pnl
        open_value += value

    # Calculate the portfolio summary.
    realised = sum(
        number(trade.get("realised_pnl_usd"))
        for trade in closed
    )

    equity = cash + open_value
    target_hit = realised >= TOTAL_PROFIT_TARGET

    portfolio.update(
        {
            "mode": "PAPER_ONLY",
            "starting_balance": starting_balance,
            "cash": round(cash, 8),
            "open_positions": positions,
            "closed_trades": closed[-2000:],
            "open_position_count": len(positions),
            "closed_trade_count": len(closed),
            "open_positions_value_usd": round(open_value, 8),
            "unrealised_pnl_usd": round(unrealised, 8),
            "realised_pnl_usd": round(realised, 8),
            "total_equity_usd": round(equity, 8),
            "total_pnl_usd": round(equity - starting_balance, 8),
            "profit_target_usd": TOTAL_PROFIT_TARGET,
            "profit_target_hit": target_hit,
            "last_updated": now(),
            "settings": {
                "trade_size_usd": TRADE_SIZE,
                "stop_loss_pct": STOP_LOSS_PCT,
                "take_profit_pct": TAKE_PROFIT_PCT,
                "minimum_score": MIN_SCORE,
                "maximum_open_positions": MAX_OPEN_POSITIONS,
                "allow_unverified_paper_entries": ALLOW_UNVERIFIED,
            },
        }
    )

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
