from datetime import UTC, datetime


class PermissionDenied(ValueError):
    pass


def require(condition: bool, message: str) -> None:
    if not condition:
        raise PermissionDenied(message)


def owns_wallet(actor: dict, wallet_id: int) -> bool:
    return wallet_id in actor.get("wallet_ids", [actor["wallet_id"]])


def expense_edit(actor: dict, expense: dict) -> bool:
    return owns_wallet(actor, expense["payer_wallet_id"]) or actor["id"] == expense["logger_member_id"]


def expense_review(actor: dict, expense: dict) -> bool:
    return any(s["member_id"] == actor["id"] for s in expense["shares"])


def transfer_edit(actor: dict, transfer: dict, *, chat: bool = False, now: datetime | None = None) -> bool:
    if not any(owns_wallet(actor, transfer[k]) for k in ("from_wallet_id", "to_wallet_id")):
        return False
    if chat and transfer["kind"] == "payment":
        created = datetime.fromisoformat(transfer["created_at"])
        return ((now or datetime.now(UTC)) - created).total_seconds() <= 86400
    return True
