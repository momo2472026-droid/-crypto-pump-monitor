import json
import os
from datetime import datetime, timezone
from pathlib import Path

STARTING_BALANCE = float(os.getenv("PAPER_STARTING_BALANCE", "1000"))
TRADE_SIZE = float(os.getenv("PAPER_TRADE_SIZE", "50"))
STOP_LOSS_PCT = float(os.getenv("PAPER_STOP_LOSS_PCT", "10"))
TAKE_PROFIT_PCT = float(os.getenv("PAPER_TAKE_PROFIT_PCT", "25"))
MIN_SCORE = int(os.getenv("MIN_SCORE", "45"))
MAX_OPEN_POSITIONS = int(os.getenv("PAPER_MAX_OPEN_POSITIONS", "10"))

SIGNALS_FILE = Path("signals.json")
TRADES_FILE = Path("paper_trades.json")


def now():
    return datetime.now(timezone.utc).isoformat()


def load_json(path, default):
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def save_json(path, data):
    temp = path.with_suffix(path.suffix + ".tmp")
    with temp.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    temp.replace(path)


def token_key(signal):
    return (
        f"{signal.get('chain', '').lower()}:"
        f"{signal.get('address', '').lower()}"
    )


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
            "last_updated": now(),
        }

    positions = portfolio.setdefault("open_positions", {})
    closed = portfolio.setdefault("closed_trades", [])
    portfolio.setdefault("starting_balance", STARTING_BALANCE)
    portfolio.setdefault("cash", portfolio["starting_balance"])

    # Do not reset an existing portfolio when environment settings change.
    starting_balance = float(portfolio["starting_balance"])
    cash = float(portfolio["cash"])

    latest_prices = {}

    for signal in signals:
        key = token_key(signal)
        metrics = signal.get("metrics") or {}
        price = float(metrics.get("price_usd") or 0)

        if key and price > 0:
            latest_prices[key] = {
                "price": price,
                "signal": signal,
            }

    # Manage existing positions first.
    for key in list(positions.keys()):
        position = positions[key]
        market = latest_prices.get(key)

        # No current price means we cannot safely calculate an exit.
        if not market:
            continue

        price = market["price"]
        entry = float(position["entry_price"])

        if entry <= 0:
            continue

        pnl_pct = (price / entry - 1) * 100
        exit_reason = None

        if pnl_pct <= -STOP_LOSS_PCT:
            exit_reason = "STOP_LOSS"
        elif pnl_pct >= TAKE_PROFIT_PCT:
            exit_reason = "TAKE_PROFIT"

        if exit_reason:
            quantity = float(position["quantity"])
            proceeds = quantity * price
            cost = float(position["cost_usd"])
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
                f"PAPER SELL {position['symbol']} | "
                f"{exit_reason} | P/L ${pnl:.2f} ({pnl_pct:.2f}%)"
            )

            del positions[key]

    # Open positions only when all available scanner checks passed.
    profit_target_hit = (
        float(portfolio.get("realised_pnl_usd", 0))
        >= float(os.getenv("PAPER_TOTAL_PROFIT_TARGET", "100"))
    )

    if profit_target_hit:
        print("Paper profit target reached; new buys paused.")

    elif TRADE_SIZE > 0 and cash >= TRADE_SIZE:
        for signal in signals:
            key = token_key(signal)

            if not key or key in positions:
                continue

            if len(positions) >= MAX_OPEN_POSITIONS:
                break

            metrics = signal.get("metrics") or {}
            price = float(metrics.get("price_usd") or 0)
            score = int(signal.get("score") or 0)
            liquidity = float(metrics.get("liquidity_usd") or 0)
            volume = float(metrics.get("volume_24h_usd") or 0)
            checks = signal.get("onchain_checks") or {}

            if price <= 0 or score < MIN_SCORE:
                continue

            if liquidity < float(os.getenv("MIN_LIQUIDITY", "10000")):
                continue

            if volume < float(os.getenv("MIN_VOLUME_24H", "25000")):
                continue

            # Never buy a token that the scanner marked unsafe,
            # unknown, or not verified by its configured risk checks.
            if checks.get("skip") or not checks.get("verified"):
                print(f"PAPER SKIP {signal.get('symbol')}: risk checks not verified")
                continue

            if TRADE_SIZE > cash:
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
                "stop_loss_price": price * (1 - STOP_LOSS_PCT / 100),
                "take_profit_price": price * (1 + TAKE_PROFIT_PCT / 100),
                "status": "OPEN",
            }

            positions[key] = position
            cash -= TRADE_SIZE

            print(
                f"PAPER BUY {position['symbol']} | "
                f"${TRADE_SIZE:.2f} at ${price:.10g} | "
                f"Score {score}"
            )

            if cash < TRADE_SIZE:
                break

    # Mark-to-market portfolio values.
    unrealised = 0.0
    open_value = 0.0

    for key, position in positions.items():
        market = latest_prices.get(key)
        current_price = (
            market["price"]
            if market
            else float(position["entry_price"])
        )

        quantity = float(position["quantity"])
        cost = float(position["cost_usd"])
        value = quantity * current_price

        position["current_price"] = current_price
        position["current_value_usd"] = round(value, 8)
        position["unrealised_pnl_usd"] = round(value - cost, 8)
        position["unrealised_pnl_pct"] = round(
            (current_price / float(position["entry_price"]) - 1) * 100,
            4,
        )

        unrealised += value - cost
        open_value += value

    realised = sum(
        float(trade.get("realised_pnl_usd", 0))
        for trade in closed
    )

    portfolio.update({
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
        "total_equity_usd": round(cash + open_value, 8),
        "total_pnl_usd": round(cash + open_value - starting_balance, 8),
        "last_updated": now(),
        "settings": {
            "trade_size_usd": TRADE_SIZE,
            "stop_loss_pct": STOP_LOSS_PCT,
            "take_profit_pct": TAKE_PROFIT_PCT,
            "minimum_score": MIN_SCORE,
            "maximum_open_positions": MAX_OPEN_POSITIONS,
        },
    })

    save_json(TRADES_FILE, portfolio)

    print(
        f"Paper trading finished | Cash ${cash:.2f} | "
        f"Open positions {len(positions)} | "
        f"Realised P/L ${realised:.2f} | "
        f"Unrealised P/L ${unrealised:.2f} | "
        f"Equity ${cash + open_value:.2f}"
    )


if __name__ == "__main__":
    main()
