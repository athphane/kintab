import re
from dataclasses import dataclass

from bot.ledger import positive_amount

MONEY = re.compile(r"(?:[0-9]+|[1-9][0-9]{0,2}(?:,[0-9]{3})+)(?:\.[0-9]+)?\Z")
MENTION = re.compile(r"(?<!\S)@([A-Za-z0-9_]{1,32})(?!\S)")


def amount_minor(text: str, digits: int = 2) -> int:
    if not isinstance(text, str) or not MONEY.fullmatch(text.strip()):
        raise ValueError("Use an amount like 450, 1,200 or 1200.50.")
    whole, _, fraction = text.strip().replace(",", "").partition(".")
    if len(fraction) > digits:
        raise ValueError(f"This currency uses at most {digits} decimal places.")
    return positive_amount(int(whole) * 10**digits + int(fraction.ljust(digits, "0") or "0"))


@dataclass(frozen=True)
class Parsed:
    amount_minor: int
    description: str
    username: str | None


def parse_command(text: str, digits: int = 2) -> Parsed:
    parts = text.split(maxsplit=2)
    if len(parts) < 2:
        raise ValueError("Add an amount, e.g. /spent 450 groceries or /paid 3000 @sister.")
    rest = parts[2] if len(parts) > 2 else ""
    mentions = list(MENTION.finditer(rest))
    if len(mentions) > 1:
        raise ValueError("Mention only one person.")
    username = mentions[0][1].lower() if mentions else None
    description = MENTION.sub("", rest).strip()
    return Parsed(amount_minor(parts[1], digits), description, username)
