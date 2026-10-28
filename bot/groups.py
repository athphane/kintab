import csv
import io
import re
import secrets
from fractions import Fraction

from bot import ledger, perms
from bot.db import now
from bot.entries import Entries, text


class TabService(Entries):
    def __init__(self, db):
        self.db = db

    async def ensure_group(self, chat_id, title):
        async with self.db.transaction():
            group = await self.db.one("SELECT * FROM groups WHERE chat_id=?", (chat_id,))
            if not group:
                group_id = await self.db.execute(
                    "INSERT INTO groups(chat_id,token,title,created_at) VALUES(?,?,?,?)",
                    (chat_id, secrets.token_urlsafe(24), title[:200], now()),
                )
                await self.db.audit(group_id, None, "create", "group", group_id)
                group = await self._group(group_id)
            elif title != group["title"]:
                await self.db.execute("UPDATE groups SET title=? WHERE id=?", (title[:200], group["id"]))
                group["title"] = title[:200]
            return group

    async def _group(self, group_id):
        group = await self.db.one("SELECT * FROM groups WHERE id=?", (group_id,))
        if not group:
            raise ValueError("This tab no longer exists.")
        return group

    async def group_for_token(self, token):
        async with self.db.transaction():
            return await self.db.one("SELECT * FROM groups WHERE token=?", (token,))

    async def _member(self, group_id, member_id, *, active=False):
        member = await self.db.one("SELECT * FROM members WHERE group_id=? AND id=?", (group_id, member_id))
        if not member or (active and member["left_at"]):
            raise ValueError("Choose an active member in this group." if active else "Member not found.")
        return member

    async def _wallet(self, group_id, wallet_id):
        wallet = await self.db.one("SELECT * FROM wallets WHERE group_id=? AND id=?", (group_id, wallet_id))
        if not wallet:
            raise ValueError("Wallet not found in this group.")
        return wallet

    async def _actor(self, group_id, user_id):
        member = await self.db.one(
            "SELECT * FROM members WHERE group_id=? AND tg_user_id=? AND left_at IS NULL", (group_id, user_id)
        )
        perms.require(bool(member), "Tap I'm in to join this tab first.")
        # A closed historical wallet remains controlled by its settler's current wallet.
        historical = await self.db.all(
            "SELECT w.id FROM wallets w JOIN members m ON m.id=w.settler_member_id "
            "WHERE w.group_id=? AND w.closed_at IS NOT NULL AND m.wallet_id=?",
            (group_id, member["wallet_id"]),
        )
        member["wallet_ids"] = [member["wallet_id"], *(w["id"] for w in historical)]
        return member

    async def join(self, group_id, user_id, name, username=None):
        async with self.db.transaction():
            await self._group(group_id)
            member = await self.db.one(
                "SELECT * FROM members WHERE group_id=? AND tg_user_id=?", (group_id, user_id)
            )
            if member:
                if member["left_at"]:
                    # Rejoin the same wallet: historical debt never disappears.
                    await self.db.execute(
                        "UPDATE members SET left_at=NULL,joined_at=?,name=?,username=? WHERE id=?",
                        (now(), text(name, "Name", 100), username, member["id"]),
                    )
                    await self.db.audit(
                        group_id, member["id"], "rejoin", "member", member["id"], before=member
                    )
                else:
                    await self.db.execute(
                        "UPDATE members SET name=?,username=? WHERE id=?",
                        (text(name, "Name", 100), username, member["id"]),
                    )
                return await self._member(group_id, member["id"])
            return await self._new_member(group_id, user_id, name, username)

    async def _new_member(self, group_id, user_id, name, username=None, actor_id=None, wallet_id=None):
        new_wallet = wallet_id is None
        if new_wallet:
            wallet_id = await self.db.execute(
                "INSERT INTO wallets(group_id,created_at) VALUES(?,?)", (group_id, now())
            )
        member_id = await self.db.execute(
            "INSERT INTO members(group_id,tg_user_id,name,username,wallet_id,joined_at) VALUES(?,?,?,?,?,?)",
            (group_id, user_id, text(name, "Name", 100), username, wallet_id, now()),
        )
        if new_wallet:
            await self.db.execute("UPDATE wallets SET settler_member_id=? WHERE id=?", (member_id, wallet_id))
        member = await self._member(group_id, member_id)
        await self.db.audit(
            group_id, actor_id or member_id, "join" if user_id else "guest", "member", member_id, after=member
        )
        return member

    async def _active_wallet(self, group_id, wallet_id):
        if type(wallet_id) is not int or wallet_id <= 0:
            raise ValueError("Choose a valid wallet ID.")
        wallet = await self._wallet(group_id, wallet_id)
        active = await self.db.one(
            "SELECT id FROM members WHERE wallet_id=? AND left_at IS NULL LIMIT 1", (wallet_id,)
        )
        if wallet["closed_at"] or not active:
            raise ValueError("Choose a wallet with active members.")
        return wallet

    async def add_guest(self, group_id, user_id, name, wallet_id=None):
        async with self.db.transaction():
            actor = await self._actor(group_id, user_id)
            if wallet_id is not None:
                await self._active_wallet(group_id, wallet_id)
            member = await self._new_member(group_id, None, name, actor_id=actor["id"], wallet_id=wallet_id)
            if wallet_id is not None:
                await self._cancel_wallet_invites(group_id, wallet_id)
            return member

    async def _state(self, group_id):
        group = await self._group(group_id)
        members = await self.db.all(
            "SELECT * FROM members WHERE group_id=? ORDER BY joined_at,id", (group_id,)
        )
        wallets = await self.db.all("SELECT * FROM wallets WHERE group_id=? ORDER BY id", (group_id,))
        expenses = await self.db.all(
            "SELECT * FROM expenses WHERE group_id=? ORDER BY created_at,id", (group_id,)
        )
        shares = await self.db.all(
            "SELECT s.* FROM expense_shares s JOIN expenses e ON e.id=s.expense_id WHERE e.group_id=?",
            (group_id,),
        )
        share_map = {}
        for share in shares:
            share_map.setdefault(share["expense_id"], []).append(share)
        for expense in expenses:
            expense["shares"] = share_map.get(expense["id"], [])
        transfers = await self.db.all(
            "SELECT * FROM transfers WHERE group_id=? ORDER BY created_at,id", (group_id,)
        )
        net = {w["id"]: 0 for w in wallets} | ledger.balances(expenses, transfers)
        simple = ledger.simplify(net)
        pairs = ledger.direct(expenses, transfers)
        for wallet in wallets:
            people = [m for m in members if m["wallet_id"] == wallet["id"]]
            active = [m for m in people if not m["left_at"]]
            settler = next((m for m in members if m["id"] == wallet["settler_member_id"]), None)
            wallet["name"] = " & ".join(m["name"] for m in (active or people)) or (
                f"{settler['name']} (previous wallet)" if settler else "Previous wallet"
            )
            if people and not active:
                wallet["name"] += " (left)"
            wallet["settler"] = settler
            wallet["balance_minor"] = net[wallet["id"]]
        return {
            "group": group,
            "members": members,
            "wallets": wallets,
            "expenses": expenses,
            "transfers": transfers,
            "balances": net,
            "simplified": simple,
            "direct": pairs,
            "lines": pairs if group["debt_mode"] == "direct" else simple,
        }

    async def snapshot(self, group_id, user_id=None):
        async with self.db.transaction():
            actor = await self._actor(group_id, user_id) if user_id is not None else None
            state = await self._state(group_id)
            if actor:
                state["me"] = actor
                for expense in state["expenses"]:
                    expense["can_edit"] = not expense["deleted_at"] and perms.expense_edit(actor, expense)
                    expense["can_review"] = not expense["deleted_at"] and perms.expense_review(actor, expense)
                for transfer in state["transfers"]:
                    transfer["can_undo"] = not transfer["undone_at"] and perms.transfer_edit(actor, transfer)
            return state

    async def leave(self, group_id, user_id=None, member_id=None, *, service=False):
        async with self.db.transaction():
            if service:
                member = await self.db.one(
                    "SELECT * FROM members WHERE group_id=? AND tg_user_id=?", (group_id, user_id)
                )
                if not member or member["left_at"]:
                    return None
                actor_id = None
            else:
                actor = await self._actor(group_id, user_id)
                member = await self._member(
                    group_id, member_id if member_id is not None else actor["id"], active=True
                )
                actor_id = actor["id"]
            await self.db.execute("UPDATE members SET left_at=? WHERE id=?", (now(), member["id"]))
            await self.db.execute(
                "UPDATE wallet_invites SET status='cancelled' WHERE status='pending' "
                "AND (from_member_id=? OR to_member_id=?)",
                (member["id"], member["id"]),
            )
            wallet = await self._wallet(group_id, member["wallet_id"])
            remaining = await self.db.one(
                "SELECT id FROM members WHERE wallet_id=? AND left_at IS NULL ORDER BY joined_at,id LIMIT 1",
                (wallet["id"],),
            )
            if remaining and wallet["settler_member_id"] == member["id"]:
                await self.db.execute(
                    "UPDATE wallets SET settler_member_id=? WHERE id=?", (remaining["id"], wallet["id"])
                )
                await self.db.audit(
                    group_id,
                    actor_id,
                    "settler",
                    "wallet",
                    wallet["id"],
                    before=wallet,
                    after=await self._wallet(group_id, wallet["id"]),
                )
            await self.db.audit(
                group_id,
                actor_id,
                "leave",
                "member",
                member["id"],
                before=member,
                after=await self._member(group_id, member["id"]),
            )
            return member

    async def invite(self, group_id, user_id, to_member_id):
        async with self.db.transaction():
            actor = await self._actor(group_id, user_id)
            target = await self._member(group_id, to_member_id, active=True)
            if actor["wallet_id"] == target["wallet_id"]:
                raise ValueError("You already share a wallet.")
            if target["tg_user_id"] is None:
                raise ValueError(
                    "Guests cannot accept invitations. Use Manage shared wallets to combine their wallet directly."
                )
            state = await self._state(group_id)
            if any(state["balances"].get(w, 0) for w in (actor["wallet_id"], target["wallet_id"])):
                raise ValueError("Both wallets must have zero balance before linking.")
            existing = await self.db.one(
                "SELECT id FROM wallet_invites WHERE group_id=? AND from_member_id=? "
                "AND to_member_id=? AND status='pending'",
                (group_id, actor["id"], target["id"]),
            )
            if existing:
                raise ValueError("There is already a pending invitation.")
            invite_id = await self.db.execute(
                "INSERT INTO wallet_invites(group_id,from_member_id,to_member_id,from_wallet_id,to_wallet_id,created_at) "
                "VALUES(?,?,?,?,?,?)",
                (group_id, actor["id"], target["id"], actor["wallet_id"], target["wallet_id"], now()),
            )
            await self.db.audit(group_id, actor["id"], "invite", "wallet_invite", invite_id, after=target)
            return await self.db.one("SELECT * FROM wallet_invites WHERE id=?", (invite_id,))

    async def respond_invite(self, group_id, user_id, invite_id, accept):
        async with self.db.transaction():
            actor = await self._actor(group_id, user_id)
            invite = await self.db.one(
                "SELECT * FROM wallet_invites WHERE id=? AND group_id=?", (invite_id, group_id)
            )
            if not invite or invite["status"] != "pending":
                raise ValueError("This invitation is no longer pending.")
            perms.require(actor["id"] == invite["to_member_id"], "Only the invited person can respond.")
            proposer = await self._member(group_id, invite["from_member_id"], active=True)
            if accept:
                if (proposer["wallet_id"], actor["wallet_id"]) != (
                    invite["from_wallet_id"],
                    invite["to_wallet_id"],
                ):
                    raise ValueError("The wallets changed. Send a new invitation before linking.")
                state = await self._state(group_id)
                source, target = actor["wallet_id"], proposer["wallet_id"]
                if source == target or any(state["balances"].get(w, 0) for w in (source, target)):
                    raise ValueError(
                        "Wallets changed or have a balance. Settle first and send a new invitation."
                    )
                await self._merge_wallets(group_id, source, target)
                await self.db.execute(
                    "UPDATE wallets SET settler_member_id=? WHERE id=?", (proposer["id"], target)
                )
            await self.db.execute(
                "UPDATE wallet_invites SET status=? WHERE id=?",
                ("accepted" if accept else "declined", invite_id),
            )
            await self.db.audit(
                group_id,
                actor["id"],
                "accept" if accept else "decline",
                "wallet_invite",
                invite_id,
                before={"invite": invite, "state": state} if accept else invite,
                after=await self._state(group_id),
            )

    async def _merge_wallets(self, group_id, source, target):
        await self.db.execute(
            "UPDATE members SET wallet_id=? WHERE group_id=? AND wallet_id=? AND left_at IS NULL",
            (target, group_id, source),
        )
        # Departed members retain their original wallet and its history.
        remaining = await self.db.one("SELECT id FROM members WHERE wallet_id=? LIMIT 1", (source,))
        if not remaining:
            await self.db.execute("UPDATE wallets SET closed_at=? WHERE id=?", (now(), source))

    async def _cancel_wallet_invites(self, group_id, wallet_id):
        await self.db.execute(
            "UPDATE wallet_invites SET status='cancelled' WHERE group_id=? AND status='pending' "
            "AND (from_wallet_id=? OR to_wallet_id=?)",
            (group_id, wallet_id, wallet_id),
        )

    async def link_wallets(self, group_id, user_id, from_wallet_id, to_wallet_id):
        if any(type(wallet_id) is not int or wallet_id <= 0 for wallet_id in (from_wallet_id, to_wallet_id)):
            raise ValueError("Choose valid wallet IDs.")
        async with self.db.transaction():
            actor = await self._actor(group_id, user_id)
            for wallet_id in (from_wallet_id, to_wallet_id):
                wallet = await self._wallet(group_id, wallet_id)
                active = await self.db.one(
                    "SELECT id FROM members WHERE wallet_id=? AND left_at IS NULL LIMIT 1", (wallet_id,)
                )
                if wallet["closed_at"] or not active:
                    raise ValueError("Choose wallets with active members.")
            if from_wallet_id == to_wallet_id:
                raise ValueError("Choose two different wallets.")
            before = await self._state(group_id)
            if any(before["balances"].get(w, 0) for w in (from_wallet_id, to_wallet_id)):
                raise ValueError("Both wallets must have zero balance before linking.")
            await self._merge_wallets(group_id, from_wallet_id, to_wallet_id)
            for wallet_id in (from_wallet_id, to_wallet_id):
                await self._cancel_wallet_invites(group_id, wallet_id)
            await self.db.audit(
                group_id, actor["id"], "link", "wallet", to_wallet_id, before, await self._state(group_id)
            )

    async def assign_wallet(self, group_id, user_id, member_id, wallet_id=None):
        async with self.db.transaction():
            actor = await self._actor(group_id, user_id)
            member = await self._member(group_id, member_id, active=True)
            target = await self._active_wallet(group_id, wallet_id) if wallet_id is not None else None
            if target and target["id"] == member["wallet_id"]:
                return member
            people = await self.db.all(
                "SELECT * FROM members WHERE wallet_id=? AND left_at IS NULL ORDER BY joined_at,id",
                (member["wallet_id"],),
            )
            if target is None and len(people) < 2:
                raise ValueError("This member already has their own wallet.")
            state = await self._state(group_id)
            if any(state["balances"].get(w, 0) for w in (member["wallet_id"], wallet_id)):
                raise ValueError("Wallets must have zero balance before moving an existing member.")
            if target is None:
                wallet_id = await self.db.execute(
                    "INSERT INTO wallets(group_id,settler_member_id,created_at) VALUES(?,?,?)",
                    (group_id, member["id"], now()),
                )
            old = await self._wallet(group_id, member["wallet_id"])
            await self.db.execute("UPDATE members SET wallet_id=? WHERE id=?", (wallet_id, member["id"]))
            remaining = [person for person in people if person["id"] != member["id"]]
            if remaining and old["settler_member_id"] == member["id"]:
                await self.db.execute(
                    "UPDATE wallets SET settler_member_id=? WHERE id=?", (remaining[0]["id"], old["id"])
                )
                await self.db.audit(
                    group_id,
                    actor["id"],
                    "settler",
                    "wallet",
                    old["id"],
                    before=old,
                    after=await self._wallet(group_id, old["id"]),
                )
            elif not remaining:
                historical = await self.db.one(
                    "SELECT id FROM members WHERE wallet_id=? LIMIT 1", (old["id"],)
                )
                if not historical:
                    await self.db.execute("UPDATE wallets SET closed_at=? WHERE id=?", (now(), old["id"]))
                    await self.db.audit(
                        group_id,
                        actor["id"],
                        "close",
                        "wallet",
                        old["id"],
                        before=old,
                        after=await self._wallet(group_id, old["id"]),
                    )
            for changed_wallet_id in (old["id"], wallet_id):
                await self._cancel_wallet_invites(group_id, changed_wallet_id)
            after = await self._member(group_id, member["id"])
            await self.db.audit(
                group_id,
                actor["id"],
                "assign" if target else "unlink",
                "member",
                member["id"],
                before=member,
                after=after,
            )
            return after

    async def unlink(self, group_id, user_id, member_id=None):
        if member_id is None:
            state = await self.snapshot(group_id, user_id)
            member_id = state["me"]["id"]
        return await self.assign_wallet(group_id, user_id, member_id)

    async def set_settler(self, group_id, user_id, member_id):
        async with self.db.transaction():
            actor = await self._actor(group_id, user_id)
            member = await self._member(group_id, member_id, active=True)
            wallet_id = member["wallet_id"]
            before = await self._wallet(group_id, wallet_id)
            await self.db.execute("UPDATE wallets SET settler_member_id=? WHERE id=?", (member_id, wallet_id))
            await self.db.audit(
                group_id,
                actor["id"],
                "settler",
                "wallet",
                wallet_id,
                before,
                await self._wallet(group_id, wallet_id),
            )

    async def update_settings(self, group_id, user_id, data):
        async with self.db.transaction():
            actor = await self._actor(group_id, user_id)
            before = await self._group(group_id)
            currency = data.get("currency", before["currency"])
            digits = data.get("minor_units", before["minor_units"])
            mode, debt = (
                data.get("split_mode", before["split_mode"]),
                data.get("debt_mode", before["debt_mode"]),
            )
            if not isinstance(currency, str) or not re.fullmatch(r"[A-Z]{3}", currency):
                raise ValueError("Use a three-letter currency code.")
            if (
                type(digits) is not int
                or not 0 <= digits <= 3
                or mode not in {"person", "wallet"}
                or debt not in {"direct", "simplified"}
            ):
                raise ValueError("Invalid group settings.")
            exists = await self.db.one(
                "SELECT id FROM expenses WHERE group_id=? UNION ALL SELECT id FROM transfers "
                "WHERE group_id=? LIMIT 1",
                (group_id, group_id),
            )
            if exists and (currency != before["currency"] or digits != before["minor_units"]):
                raise ValueError("Currency and decimal places cannot change once entries exist.")
            await self.db.execute(
                "UPDATE groups SET currency=?,minor_units=?,split_mode=?,debt_mode=? WHERE id=?",
                (currency, digits, mode, debt, group_id),
            )
            for member_id, weight in data.get("weights", {}).items():
                member = await self._member(group_id, int(member_id), active=True)
                if not 0 < Fraction(str(weight)) <= 1000:
                    raise ValueError("Weights must be greater than zero and at most 1000.")
                await self.db.execute("UPDATE members SET weight=? WHERE id=?", (str(weight), member["id"]))
                await self.db.audit(
                    group_id, actor["id"], "weight", "member", member["id"], member, {"weight": str(weight)}
                )
            await self.db.audit(
                group_id, actor["id"], "settings", "group", group_id, before, await self._group(group_id)
            )

    async def finish_setup(self, group_id, user_id):
        async with self.db.transaction():
            actor = await self._actor(group_id, user_id)
            await self.db.execute("UPDATE groups SET setup_done=1 WHERE id=?", (group_id,))
            await self.db.audit(group_id, actor["id"], "setup", "group", group_id)

    async def migrate(self, old_chat_id, new_chat_id):
        async with self.db.transaction():
            group = await self.db.one("SELECT * FROM groups WHERE chat_id=?", (old_chat_id,))
            if group:
                # Destination can have an empty welcome created before this service event arrived.
                destination = await self.db.one("SELECT * FROM groups WHERE chat_id=?", (new_chat_id,))
                if destination:
                    occupied = await self.db.one(
                        "SELECT id FROM members WHERE group_id=? LIMIT 1", (destination["id"],)
                    )
                    if occupied:
                        raise ValueError("Migration destination already has a tab; refusing to overwrite it.")
                    await self.db.execute("DELETE FROM groups WHERE id=?", (destination["id"],))
                await self.db.execute(
                    "UPDATE groups SET chat_id=?,pinned_message_id=NULL,setup_message_id=NULL,pin_ok=0 WHERE id=?",
                    (new_chat_id, group["id"]),
                )
                await self.db.audit(
                    group["id"], None, "migrate", "group", group["id"], group, {"chat_id": new_chat_id}
                )

    async def forget(self, group_id, user_id, *, confirmation):
        async with self.db.transaction():
            await self._actor(group_id, user_id)
            group = await self._group(group_id)
            perms.require(
                secrets.compare_digest(str(confirmation), group["token"]), "Confirm deletion first."
            )
            # Break the circular settler reference before cascading the whole group.
            await self.db.execute("UPDATE wallets SET settler_member_id=NULL WHERE group_id=?", (group_id,))
            await self.db.execute("DELETE FROM groups WHERE id=?", (group_id,))

    async def export_csv(self, group_id, user_id):
        state = await self.snapshot(group_id, user_id)
        stream = io.StringIO(newline="")
        writer = csv.writer(stream)
        writer.writerow(
            [
                "entity",
                "id",
                "created_at",
                "kind",
                "amount_minor",
                "currency",
                "description",
                "category",
                "payer_member_id",
                "payer_wallet_id",
                "logger_member_id",
                "from_wallet_id",
                "to_wallet_id",
                "status",
                "shares_json",
                "revision",
                "actor",
                "before_json",
                "after_json",
            ]
        )

        def safe(value):
            value = str(value)
            return "'" + value if value.startswith(("=", "+", "-", "@", "\t", "\r")) else value

        import json

        for entry in state["expenses"]:
            writer.writerow(
                [
                    "expense",
                    entry["id"],
                    entry["created_at"],
                    "expense",
                    entry["amount_minor"],
                    state["group"]["currency"],
                    safe(entry["description"]),
                    safe(entry["category"]),
                    entry["payer_member_id"],
                    entry["payer_wallet_id"],
                    entry["logger_member_id"],
                    "",
                    "",
                    "deleted" if entry["deleted_at"] else "active",
                    json.dumps(entry["shares"]),
                    entry["revision"],
                    "",
                    "",
                    "",
                ]
            )
        for entry in state["transfers"]:
            writer.writerow(
                [
                    "transfer",
                    entry["id"],
                    entry["created_at"],
                    entry["kind"],
                    entry["amount_minor"],
                    state["group"]["currency"],
                    "",
                    "",
                    "",
                    "",
                    entry["recorded_by"],
                    entry["from_wallet_id"],
                    entry["to_wallet_id"],
                    "undone" if entry["undone_at"] else "active",
                    "",
                    entry["revision"],
                    "",
                    "",
                    "",
                ]
            )
        async with self.db.transaction():
            # Recheck membership after the snapshot and before returning the export.
            await self._actor(group_id, user_id)
            audit = await self.db.all("SELECT * FROM audit_log WHERE group_id=? ORDER BY id", (group_id,))
        for row in audit:
            writer.writerow(
                [
                    "audit",
                    row["entity_id"],
                    row["created_at"],
                    row["action"],
                    "",
                    "",
                    row["entity"],
                    "",
                    "",
                    "",
                    "",
                    "",
                    "",
                    "",
                    "",
                    "",
                    row["member_id"],
                    row["before_json"],
                    row["after_json"],
                ]
            )
        return stream.getvalue()
