"""Commands, short callback payloads, and replies to bot prompts. Privacy mode stays on."""

import asyncio
import io
import logging
import secrets
import time

from pyrogram import enums, filters
from pyrogram.errors import RPCError
from pyrogram.handlers import CallbackQueryHandler, MessageHandler
from pyrogram.types import ForceReply
from pyrogram.types import InlineKeyboardButton as B
from pyrogram.types import InlineKeyboardMarkup as K

from bot import parse, perms, render, report
from bot.webapp import telegram_membership

log = logging.getLogger(__name__)
HELP = (
    "KinTab tracks shared spending between wallets. Tap I'm in on the group welcome to join.\n\n"
    "/spent 450 groceries — you paid, split by the group default\n"
    "/spent 1200 electricity @sister — someone else paid\n"
    "Or reply to their message with /spent 1200 electricity.\n"
    "/paid 3000 [@recipient] — your wallet sent money\n"
    "/damage (or /owed) — a picture of who owes whom\n"
    "/tab — running total and re-pin\n/leave — stop sharing new expenses; old debts remain\n"
    "/export — all entries and audit trail in a CSV, sent in DM\n"
    "/forget — delete the tab after confirmation\n\n"
    "Any joined member can add people, assign wallets and manage group settings in Open tab. "
    "Positive balance = you are owed; negative = you owe. "
    "Either partner in a wallet can act for it. Payments can be undone in chat for 24h, then in the Web App. "
    "Start a private chat with me once so I can DM your exports."
)


class ChatHandlers:
    def __init__(self, client, service, notifier):
        self.client, self.service, self.notifier = client, service, notifier
        self.pending = {}
        self.prompts = {}
        self.callback_lock = asyncio.Lock()

    def remember(self, mapping, key, value):
        current = time.monotonic()
        for expired in [k for k, v in mapping.items() if v[0] < current]:
            del mapping[expired]
        if len(mapping) >= 5000:
            raise ValueError("Too many pending actions. Please try again shortly.")
        mapping[key] = (current + 600, value)

    def recall(self, mapping, key):
        stored = mapping.get(key)
        if not stored or stored[0] < time.monotonic():
            raise ValueError("This prompt expired. Run the command again.")
        return stored[1]

    async def group(self, message):
        if message.chat.type not in {enums.ChatType.GROUP, enums.ChatType.SUPERGROUP}:
            raise ValueError("Use this command in your household group.")
        return await self.service.ensure_group(message.chat.id, message.chat.title or "Household")

    async def require_actor(self, group_id, user_id):
        return (await self.service.snapshot(group_id, user_id))["me"]

    async def prompt(self, group, user_id, kind, payload, message):
        sent = await self.client.send_message(
            group["chat_id"],
            message,
            reply_markup=ForceReply(selective=True),
            parse_mode=enums.ParseMode.DISABLED,
        )
        self.remember(
            self.prompts,
            (group["chat_id"], sent.id),
            {"group_id": group["id"], "user_id": user_id, "kind": kind, **payload},
        )

    async def choose(self, group, user_id, kind, payload, choices, message):
        nonce = secrets.token_urlsafe(8)
        self.remember(
            self.pending, nonce, {"group_id": group["id"], "user_id": user_id, "kind": kind, **payload}
        )
        await self.client.send_message(
            group["chat_id"],
            message,
            reply_markup=K([[B(name[:60], callback_data=f"c:{nonce}:{choice}")] for choice, name in choices]),
        )

    async def command(self, client, message):
        try:
            if not message.from_user or message.from_user.is_bot:
                raise ValueError("Send commands from your personal Telegram account.")
            user_id = message.from_user.id
            command = message.command[0].split("@", 1)[0].lower()
            if command in {"help", "start"} and message.chat.type == enums.ChatType.PRIVATE:
                await message.reply_text(HELP, parse_mode=enums.ParseMode.DISABLED)
                return
            group = await self.group(message)
            gid = group["id"]
            if command in {"start", "help"}:
                if command == "start":
                    await self.notifier.setup(gid)
                else:
                    await message.reply_text(HELP, parse_mode=enums.ParseMode.DISABLED)
                return
            if command in {"damage", "owed"}:
                state = await self.service.snapshot(gid)
                photo = await asyncio.to_thread(report.damage_image, state)
                try:
                    await client.send_photo(
                        group["chat_id"],
                        photo,
                        caption="The damage is in. Here’s who owes whom. "
                        f"({group['debt_mode'].capitalize()} debts.)\n"
                        "Send money your usual way, then record it with /paid. Open tab for the full details.",
                        reply_markup=self.notifier.tab_keyboard(group),
                        parse_mode=enums.ParseMode.DISABLED,
                    )
                finally:
                    photo.close()
                return
            if command == "tab":
                state = await self.service.snapshot(gid)
                await message.reply_text(
                    render.running(state),
                    reply_markup=self.notifier.tab_keyboard(group),
                    parse_mode=enums.ParseMode.HTML,
                )
                await self.notifier.group_fields(gid, pin_ok=0)
                await self.notifier.pin(gid)
                return
            if command == "forget":
                await telegram_membership(client, group["chat_id"], user_id)
                await self.require_actor(gid, user_id)
                await self.choose(
                    group,
                    user_id,
                    "forget",
                    {"confirmation": group["token"]},
                    [(1, "Delete this group's whole tab"), (0, "Cancel")],
                    "Delete all members, expenses, payments and audit records? This cannot be undone.",
                )
                return
            try:
                actor = await self.require_actor(gid, user_id)
            except perms.PermissionDenied:
                await self.notifier.setup(gid)
                raise
            if command == "leave":
                await self.service.leave(gid, user_id)
                await message.reply_text("You left the tab. Your past entries and unsettled balance remain.")
                self.notifier.schedule(gid)
                return
            if command == "export":
                data = await self.service.export_csv(gid, user_id)
                document = io.BytesIO(data.encode("utf-8-sig"))
                document.name = "kintab.csv"
                try:
                    await client.send_document(
                        user_id, document, caption="KinTab — all entries and audit records"
                    )
                except RPCError:
                    await message.reply_text("Start a private chat with me first, then run /export again.")
                else:
                    await message.reply_text("CSV sent to you in DM.")
                return
            parsed = parse.parse_command(message.text, group["minor_units"])
            state = await self.service.snapshot(gid, user_id)
            target = None
            if parsed.username:
                target = next(
                    (
                        m
                        for m in state["members"]
                        if not m["left_at"] and m["username"] and m["username"].lower() == parsed.username
                    ),
                    None,
                )
            elif message.reply_to_message and message.reply_to_message.from_user:
                reply_user = message.reply_to_message.from_user
                target = next(
                    (m for m in state["members"] if m["tg_user_id"] == reply_user.id and not m["left_at"]),
                    None,
                )
            explicit = parsed.username is not None or message.reply_to_message is not None
            if command == "spent":
                data = {"amount_minor": parsed.amount_minor, "description": parsed.description or "Expense"}
                if explicit and not target:
                    choices = [(m["id"], m["name"]) for m in state["members"] if not m["left_at"]]
                    await self.choose(
                        group,
                        user_id,
                        "payer",
                        {"data": data, "message_id": message.id},
                        choices,
                        "Who paid? Choose a joined member or guest.",
                    )
                else:
                    data["payer_member_id"] = (target or actor)["id"]
                    entry = await self.service.add_expense(gid, user_id, data, message.id)
                    await self.notifier.changed(gid, "expense", entry)
            elif command == "paid":
                if target:
                    data = {
                        "amount_minor": parsed.amount_minor,
                        "from_wallet_id": actor["wallet_id"],
                        "to_wallet_id": target["wallet_id"],
                    }
                    entry = await self.service.add_transfer(gid, user_id, data, message.id)
                    await self.notifier.changed(gid, "transfer", entry)
                else:
                    recipients = {
                        line["to_wallet_id"]
                        for line in state["lines"]
                        if line["from_wallet_id"] == actor["wallet_id"]
                    }
                    choices = [
                        (w["id"], w["name"])
                        for w in state["wallets"]
                        if w["id"] != actor["wallet_id"]
                        and (explicit or not recipients or w["id"] in recipients)
                        and (not w["closed_at"] or state["balances"].get(w["id"]))
                    ]
                    if not choices:
                        raise ValueError("There is no other wallet to pay yet.")
                    if len(choices) == 1 and not explicit:
                        entry = await self.service.add_transfer(
                            gid,
                            user_id,
                            {
                                "amount_minor": parsed.amount_minor,
                                "from_wallet_id": actor["wallet_id"],
                                "to_wallet_id": choices[0][0],
                            },
                            message.id,
                        )
                        await self.notifier.changed(gid, "transfer", entry)
                    else:
                        await self.choose(
                            group,
                            user_id,
                            "recipient",
                            {"amount_minor": parsed.amount_minor, "message_id": message.id},
                            choices,
                            "Which wallet received the payment?",
                        )
        except (ValueError, KeyError, ZeroDivisionError) as error:
            await message.reply_text(str(error), parse_mode=enums.ParseMode.DISABLED)
        except RPCError:
            log.exception("Telegram command delivery failed")

    async def callback(self, client, query):
        try:
            async with self.callback_lock:
                await self._callback(client, query)
        except (ValueError, KeyError, ZeroDivisionError) as error:
            await query.answer(str(error)[:190], show_alert=True)
        except RPCError:
            log.exception("Telegram callback failed")

    async def _callback(self, client, query):
        if not query.message or not query.from_user:
            raise ValueError("Open this action in the group.")
        group = await self.group(query.message)
        gid, user_id = group["id"], query.from_user.id
        parts = query.data.split(":")
        action, key = parts[0], parts[1]
        if action in {"j", "w", "f", "s", "b", "z"} and int(key) != gid:
            raise ValueError("This button belongs to another tab.")
        if action == "j":
            await telegram_membership(client, group["chat_id"], user_id)
            await self.service.join(gid, user_id, query.from_user.first_name, query.from_user.username)
            await query.answer("You're in!")
            await self.notifier.setup(gid)
            if group["setup_done"]:
                self.notifier.schedule(gid)
            return
        if action == "c":
            pending = self.recall(self.pending, key)
            perms.require(
                pending["user_id"] == user_id and pending["group_id"] == gid,
                "Only the person who ran this command can choose.",
            )
            choice = int(parts[2])
            kind = pending["kind"]
            if kind == "forget":
                if choice:
                    await telegram_membership(client, group["chat_id"], user_id)
                    await self.service.forget(gid, user_id, confirmation=pending["confirmation"])
                del self.pending[key]
                await query.message.edit_text("Tab deleted." if choice else "Deletion cancelled.")
                await query.answer()
                return
            actor = await self.require_actor(gid, user_id)
            if kind == "payer":
                entry = await self.service.add_expense(
                    gid, user_id, pending["data"] | {"payer_member_id": choice}, pending["message_id"]
                )
                await self.notifier.changed(gid, "expense", entry)
            elif kind == "recipient":
                entry = await self.service.add_transfer(
                    gid,
                    user_id,
                    {
                        "amount_minor": pending["amount_minor"],
                        "from_wallet_id": actor["wallet_id"],
                        "to_wallet_id": choice,
                    },
                    pending["message_id"],
                )
                await self.notifier.changed(gid, "transfer", entry)
            elif kind == "invite":
                invite = await self.service.invite(gid, user_id, choice)
                await self.notifier.invite(gid, invite)
            elif kind == "opening":
                # Creator is the creditor; choose debtor, then enter the starting amount.
                await self.prompt(
                    group,
                    user_id,
                    "opening",
                    {"debtor": choice, "creditor": actor["wallet_id"]},
                    "Reply with the amount this wallet owes yours (e.g. 6000).",
                )
            elif kind == "settle":
                await self.prompt(
                    group, user_id, "payment", {"target": choice}, "Reply with the amount your wallet sent."
                )
            del self.pending[key]
            await query.message.edit_reply_markup(reply_markup=None)
            await query.answer("Selected")
            return
        state = await self.service.snapshot(gid, user_id)
        actor = state["me"]
        if action == "w":
            choices = [
                (m["id"], m["name"])
                for m in state["members"]
                if not m["left_at"] and m["tg_user_id"] is not None and m["wallet_id"] != actor["wallet_id"]
            ]
            if choices:
                await self.choose(
                    group,
                    user_id,
                    "invite",
                    {},
                    choices,
                    "Share a wallet with… (both wallets must be settled)",
                )
            else:
                await client.send_message(
                    group["chat_id"],
                    "Other people need to tap I'm in first. "
                    "Open tab → Group to change the settler or unlink.",
                )
        elif action in {"a", "x"}:
            await self.service.respond_invite(gid, user_id, int(key), action == "a")
            await query.message.edit_text(
                "Wallet invitation accepted." if action == "a" else "Wallet invitation declined."
            )
            self.notifier.schedule(gid)
        elif action == "f":
            await self.service.finish_setup(gid, user_id)
            await client.send_message(
                group["chat_id"],
                f"Ready: {group['currency']}, split {'equally by person' if group['split_mode'] == 'person' else 'equally by wallet'}. "
                "Any joined member can manage this tab. Add an opening balance, or start fresh at zero.",
                reply_markup=K(
                    [
                        [
                            B("Add opening balance", callback_data=f"b:{gid}"),
                            B("Start fresh", callback_data=f"z:{gid}"),
                        ]
                    ]
                ),
            )
            await self.notifier.pin(gid)
        elif action == "z":
            await self.notifier.pin(gid)
            await query.answer("Ready. Existing entries are kept.")
            return
        elif action == "b":
            choices = [
                (w["id"], w["name"])
                for w in state["wallets"]
                if w["id"] != actor["wallet_id"] and not w["closed_at"]
            ]
            if not choices:
                raise ValueError("Another wallet must join first.")
            await self.choose(
                group, user_id, "opening", {}, choices, "Which wallet owes yours the opening balance?"
            )
        elif action == "s":
            debts = [line for line in state["lines"] if perms.owns_wallet(actor, line["from_wallet_id"])]
            perms.require(bool(debts), "Your wallet does not owe money.")
            choices = [
                (w["id"], w["name"])
                for w in state["wallets"]
                if w["id"] in {d["to_wallet_id"] for d in debts}
            ]
            if any(d["from_wallet_id"] != actor["wallet_id"] for d in debts):
                await client.send_message(
                    group["chat_id"], "Open tab → Settle up to settle a previous wallet."
                )
            else:
                await self.choose(
                    group,
                    user_id,
                    "settle",
                    {},
                    choices,
                    "Who did your wallet pay? Record after sending the money.",
                )
            # Pings go to the settler; the label never changes the amounts.
            settler = next(w for w in state["wallets"] if w["id"] == actor["wallet_id"])["settler"]
            if settler and settler["tg_user_id"] != user_id:
                await client.send_message(
                    group["chat_id"],
                    f"{render.mention(settler)}, your wallet is settling up.",
                    parse_mode=enums.ParseMode.HTML,
                )
        elif action in {"d", "e", "n"}:
            entry = next((e for e in state["expenses"] if e["id"] == int(key)), None)
            if not entry:
                raise ValueError("Expense not found.")
            if action == "d":
                changed = await self.service.delete_expense(gid, user_id, entry["id"])
                await self.notifier.changed(gid, "expense", changed)
            elif action == "n":
                reviewed = await self.service.review_expense(gid, user_id, entry["id"])
                await self.notifier.review(gid, reviewed, user_id)
            else:
                perms.require(entry["can_edit"], "Only the payer's wallet or logger can edit this expense.")
                await self.prompt(
                    group,
                    user_id,
                    "expense_edit",
                    {"id": entry["id"], "revision": entry["revision"]},
                    "Reply with the new amount and description (e.g. 540 groceries). Use Open tab for other changes.",
                )
        elif action == "u":
            entry = await self.service.undo_transfer(gid, user_id, int(key), chat=True)
            await self.notifier.changed(gid, "transfer", entry)
        elif action == "o":
            entry = next((t for t in state["transfers"] if t["id"] == int(key)), None)
            if not entry or entry["kind"] != "opening":
                raise ValueError("Opening balance not found.")
            perms.require(entry["can_undo"], "Only the wallets involved can edit this opening balance.")
            await self.prompt(
                group,
                user_id,
                "opening_edit",
                {"id": entry["id"], "revision": entry["revision"]},
                "Reply with the new opening amount.",
            )
        else:
            raise ValueError("Unknown button.")
        await query.answer("Done")

    async def reply(self, client, message):
        if (
            not message.reply_to_message
            or not message.from_user
            or not message.text
            or message.text.startswith("/")
        ):
            return
        key = (message.chat.id, message.reply_to_message.id)
        if key not in self.prompts:
            return
        try:
            pending = self.recall(self.prompts, key)
            perms.require(
                pending["user_id"] == message.from_user.id,
                "Only the person who requested this edit can reply.",
            )
            gid, user_id = pending["group_id"], message.from_user.id
            state = await self.service.snapshot(gid, user_id)
            parsed = parse.parse_command("/spent " + message.text, state["group"]["minor_units"])
            kind = pending["kind"]
            before = None
            if kind == "expense_edit":
                before = next(e for e in state["expenses"] if e["id"] == pending["id"])
                data = {"amount_minor": parsed.amount_minor, "revision": pending["revision"]}
                if parsed.description:
                    data["description"] = parsed.description
                entry = await self.service.edit_expense(gid, user_id, pending["id"], data)
                entity = "expense"
            elif kind == "opening_edit":
                before = next(t for t in state["transfers"] if t["id"] == pending["id"])
                entry = await self.service.edit_opening(
                    gid,
                    user_id,
                    pending["id"],
                    {"amount_minor": parsed.amount_minor, "revision": pending["revision"]},
                )
                entity = "transfer"
            else:
                data = {"amount_minor": parsed.amount_minor}
                if kind == "opening":
                    data |= {
                        "kind": "opening",
                        "from_wallet_id": pending["creditor"],
                        "to_wallet_id": pending["debtor"],
                    }
                else:
                    data |= {"from_wallet_id": state["me"]["wallet_id"], "to_wallet_id": pending["target"]}
                entry = await self.service.add_transfer(gid, user_id, data, message.id)
                entity = "transfer"
            del self.prompts[key]
            await self.notifier.changed(
                gid, entity, entry, action="edit" if before else "create", before=before, actor_id=user_id
            )
        except (ValueError, KeyError, ZeroDivisionError) as error:
            await message.reply_text(str(error), parse_mode=enums.ParseMode.DISABLED)
        except RPCError:
            log.exception("Reply delivery failed")

    async def service_message(self, client, message):
        try:
            if message.migrate_to_chat_id:
                await self.service.migrate(message.chat.id, message.migrate_to_chat_id)
                return
            if message.migrate_from_chat_id:
                await self.service.migrate(message.migrate_from_chat_id, message.chat.id)
                return
            if message.new_chat_members and any(u.id == client.me.id for u in message.new_chat_members):
                group = await self.group(message)
                await self.notifier.setup(group["id"])
            if message.left_chat_member:
                group = await self.group(message)
                await self.service.leave(group["id"], message.left_chat_member.id, service=True)
                self.notifier.schedule(group["id"])
        except (ValueError, RPCError):
            log.exception("Could not process group lifecycle event")


def register_handlers(client, service, notifier):
    handlers = ChatHandlers(client, service, notifier)
    client.add_handler(
        MessageHandler(
            handlers.command,
            filters.command(
                ["start", "help", "spent", "paid", "tab", "damage", "owed", "leave", "export", "forget"]
            ),
        ),
        group=0,
    )
    client.add_handler(MessageHandler(handlers.service_message, filters.service & filters.group), group=1)
    client.add_handler(MessageHandler(handlers.reply, filters.reply & filters.text & filters.group), group=2)
    client.add_handler(CallbackQueryHandler(handlers.callback))
    return handlers
