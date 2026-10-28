import json
from datetime import UTC

from bot import ledger, perms
from bot.db import now


def text(value, label, maximum=200):
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > maximum:
        raise ValueError(f"{label} must contain 1–{maximum} characters.")
    return value.strip()


class Entries:
    """Mixin for TabService. Public operations perform authorization inside the transaction."""

    async def _entry(self, group_id, entity, entry_id):
        table = {"expense": "expenses", "transfer": "transfers"}.get(entity)
        if table is None:
            raise ValueError("Unknown entry type.")
        entry = await self.db.one(f"SELECT * FROM {table} WHERE id=? AND group_id=?", (entry_id, group_id))
        if not entry:
            raise ValueError("Entry not found in this group.")
        if entity == "expense":
            entry["shares"] = await self.db.all(
                "SELECT * FROM expense_shares WHERE expense_id=?", (entry_id,)
            )
        return entry

    async def _split_spec(self, group_id, payer_id, data):
        group = await self._group(group_id)
        members = await self.db.all(
            "SELECT * FROM members WHERE group_id=? AND left_at IS NULL ORDER BY joined_at,id", (group_id,)
        )
        ids = data.get("member_ids")
        wallets = data.get("wallet_ids")
        if ids is not None and wallets is not None:
            raise ValueError("Choose people or wallets, not both.")
        if ids is not None:
            if not isinstance(ids, list) or not ids or len(set(ids)) != len(ids):
                raise ValueError("Choose distinct people.")
            if not set(ids) <= {m["id"] for m in members}:
                raise ValueError("Choose active people in this group.")
            members = [m for m in members if m["id"] in ids]
        if wallets is not None:
            if not isinstance(wallets, list) or not wallets or len(set(wallets)) != len(wallets):
                raise ValueError("Choose distinct wallets.")
            if not set(wallets) <= {m["wallet_id"] for m in members}:
                raise ValueError("Choose active wallets in this group.")
            members = [m for m in members if m["wallet_id"] in wallets]
        weights = data.get("weights")
        if weights is not None:
            if not isinstance(weights, dict):
                raise ValueError("Weights must be an object keyed by member ID.")
            weights = {int(k): str(v) for k, v in weights.items()}
        mode = data.get("split_mode", "wallet" if wallets is not None else group["split_mode"])
        return {"members": members, "mode": mode, "weights": weights}

    @staticmethod
    def _shares(amount, payer_id, spec):
        weights = spec["weights"]
        return ledger.split(
            amount,
            spec["members"],
            payer_id,
            spec["mode"],
            {int(k): v for k, v in weights.items()} if weights is not None else None,
        )

    async def _save_shares(self, entry_id, shares):
        await self.db.execute("DELETE FROM expense_shares WHERE expense_id=?", (entry_id,))
        for share in shares:
            await self.db.execute(
                "INSERT INTO expense_shares(expense_id,member_id,wallet_id,amount_minor) VALUES(?,?,?,?)",
                (entry_id, share["member_id"], share["wallet_id"], share["amount_minor"]),
            )

    async def add_expense(self, group_id, user_id, data, message_id=None):
        async with self.db.transaction():
            actor = await self._actor(group_id, user_id)
            await self._dedup(group_id, message_id)
            amount = ledger.positive_amount(data["amount_minor"])
            payer = await self._member(group_id, data.get("payer_member_id", actor["id"]), active=True)
            spec = await self._split_spec(group_id, payer["id"], data)
            shares = self._shares(amount, payer["id"], spec)
            entry_id = await self.db.execute(
                "INSERT INTO expenses(group_id,amount_minor,description,category,payer_member_id,payer_wallet_id,"
                "logger_member_id,created_at,split_spec) VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    group_id,
                    amount,
                    text(data.get("description", "Expense"), "Description"),
                    text(data.get("category", "Other"), "Category", 60),
                    payer["id"],
                    payer["wallet_id"],
                    actor["id"],
                    now(),
                    json.dumps(spec),
                ),
            )
            await self._save_shares(entry_id, shares)
            entry = await self._entry(group_id, "expense", entry_id)
            await self.db.audit(group_id, actor["id"], "create", "expense", entry_id, after=entry)
            return entry

    async def edit_expense(self, group_id, user_id, entry_id, data):
        async with self.db.transaction():
            actor = await self._actor(group_id, user_id)
            before = await self._entry(group_id, "expense", entry_id)
            perms.require(
                perms.expense_edit(actor, before), "Only the payer's wallet or the logger can edit this."
            )
            if before["deleted_at"]:
                raise ValueError("This expense was deleted.")
            self._revision(before, data)
            amount = ledger.positive_amount(data.get("amount_minor", before["amount_minor"]))
            payer_id = data.get("payer_member_id", before["payer_member_id"])
            payer_wallet = before["payer_wallet_id"]
            if payer_id != before["payer_member_id"]:
                payer_wallet = (await self._member(group_id, payer_id, active=True))["wallet_id"]
            if any(k in data for k in ("member_ids", "wallet_ids", "weights", "split_mode")):
                spec = await self._split_spec(group_id, payer_id, data)
            else:
                spec = json.loads(before["split_spec"])
            shares = self._shares(amount, payer_id, spec)
            await self.db.execute(
                "UPDATE expenses SET amount_minor=?,description=?,category=?,payer_member_id=?,payer_wallet_id=?,"
                "split_spec=?,revision=revision+1 WHERE id=?",
                (
                    amount,
                    text(data.get("description", before["description"]), "Description"),
                    text(data.get("category", before["category"]), "Category", 60),
                    payer_id,
                    payer_wallet,
                    json.dumps(spec),
                    entry_id,
                ),
            )
            await self._save_shares(entry_id, shares)
            after = await self._entry(group_id, "expense", entry_id)
            await self.db.audit(group_id, actor["id"], "edit", "expense", entry_id, before, after)
            return after

    @staticmethod
    def _revision(entry, data):
        if "revision" in data and data["revision"] != entry["revision"]:
            raise ValueError("This entry changed. Reload it before editing.")

    async def delete_expense(self, group_id, user_id, entry_id):
        async with self.db.transaction():
            actor = await self._actor(group_id, user_id)
            entry = await self._entry(group_id, "expense", entry_id)
            perms.require(
                perms.expense_edit(actor, entry), "Only the payer's wallet or the logger can delete this."
            )
            if entry["deleted_at"]:
                raise ValueError("This expense is already deleted.")
            await self.db.execute(
                "UPDATE expenses SET deleted_at=?,deleted_by=?,revision=revision+1 WHERE id=?",
                (now(), actor["id"], entry_id),
            )
            after = await self._entry(group_id, "expense", entry_id)
            await self.db.audit(group_id, actor["id"], "delete", "expense", entry_id, entry, after)
            return after

    async def review_expense(self, group_id, user_id, entry_id):
        async with self.db.transaction():
            actor = await self._actor(group_id, user_id)
            entry = await self._entry(group_id, "expense", entry_id)
            perms.require(
                perms.expense_review(actor, entry), "Only someone in this split can request a review."
            )
            if entry["deleted_at"]:
                raise ValueError("This expense was deleted.")
            recent = await self.db.one(
                "SELECT created_at FROM audit_log WHERE group_id=? AND member_id=? AND entity='expense' "
                "AND entity_id=? AND action='review' ORDER BY id DESC LIMIT 1",
                (group_id, actor["id"], entry_id),
            )
            if recent:
                from datetime import datetime

                if (datetime.now(UTC) - datetime.fromisoformat(recent["created_at"])).total_seconds() < 60:
                    raise ValueError("A review was just requested. Please wait a minute.")
            await self.db.audit(group_id, actor["id"], "review", "expense", entry_id)
            return entry

    async def add_transfer(self, group_id, user_id, data, message_id=None):
        async with self.db.transaction():
            actor = await self._actor(group_id, user_id)
            await self._dedup(group_id, message_id)
            kind = data.get("kind", "payment")
            if kind not in {"payment", "opening", "writeoff"}:
                raise ValueError("Unknown transfer kind.")
            source, target = data["from_wallet_id"], data["to_wallet_id"]
            await self._wallet(group_id, source)
            await self._wallet(group_id, target)
            if source == target:
                raise ValueError("Choose two different wallets.")
            amount = ledger.positive_amount(data["amount_minor"])
            perms.require(
                perms.owns_wallet(actor, target)
                if kind == "writeoff"
                else any(perms.owns_wallet(actor, w) for w in (source, target)),
                "Only the wallet that's owed can forgive a debt."
                if kind == "writeoff"
                else "Only a wallet involved can record this entry.",
            )
            if kind == "writeoff":
                state = await self._state(group_id)
                lines = state["lines"]
                available = sum(
                    t["amount_minor"]
                    for t in lines
                    if t["from_wallet_id"] == source and t["to_wallet_id"] == target
                )
                if amount > available:
                    raise ValueError("You can only forgive the amount currently owed to your wallet.")
            entry_id = await self.db.execute(
                "INSERT INTO transfers(group_id,kind,from_wallet_id,to_wallet_id,amount_minor,recorded_by,created_at) "
                "VALUES(?,?,?,?,?,?,?)",
                (group_id, kind, source, target, amount, actor["id"], now()),
            )
            entry = await self._entry(group_id, "transfer", entry_id)
            await self.db.audit(group_id, actor["id"], "create", "transfer", entry_id, after=entry)
            return entry

    async def undo_transfer(self, group_id, user_id, entry_id, *, chat=False):
        async with self.db.transaction():
            actor = await self._actor(group_id, user_id)
            entry = await self._entry(group_id, "transfer", entry_id)
            perms.require(
                perms.transfer_edit(actor, entry, chat=chat),
                "Only the two wallets involved can undo this. Payments older than 24h need the Web App.",
            )
            if entry["undone_at"]:
                raise ValueError("This entry is already undone.")
            await self.db.execute(
                "UPDATE transfers SET undone_at=?,undone_by=?,revision=revision+1 WHERE id=?",
                (now(), actor["id"], entry_id),
            )
            after = await self._entry(group_id, "transfer", entry_id)
            await self.db.audit(group_id, actor["id"], "undo", "transfer", entry_id, entry, after)
            return after

    async def edit_opening(self, group_id, user_id, entry_id, data):
        async with self.db.transaction():
            actor = await self._actor(group_id, user_id)
            before = await self._entry(group_id, "transfer", entry_id)
            perms.require(perms.transfer_edit(actor, before), "Only the wallets involved can edit this.")
            if before["kind"] != "opening" or before["undone_at"]:
                raise ValueError("Only an active opening balance can be edited.")
            self._revision(before, data)
            await self.db.execute(
                "UPDATE transfers SET amount_minor=?,revision=revision+1 WHERE id=?",
                (ledger.positive_amount(data["amount_minor"]), entry_id),
            )
            after = await self._entry(group_id, "transfer", entry_id)
            await self.db.audit(group_id, actor["id"], "edit", "transfer", entry_id, before, after)
            return after

    async def _dedup(self, group_id, message_id):
        if message_id is not None:
            existing = await self.db.one(
                "SELECT 1 FROM processed_messages WHERE group_id=? AND message_id=?", (group_id, message_id)
            )
            if existing:
                raise ValueError("This command has already been recorded.")
            await self.db.execute("INSERT INTO processed_messages VALUES(?,?)", (group_id, message_id))
