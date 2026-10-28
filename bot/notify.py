"""Telegram presentation and debounced pinned totals, shared by chat and the API."""

import asyncio
import logging

from pyrogram import enums
from pyrogram.errors import ChatAdminRequired, FloodWait, MessageIdInvalid, MessageNotModified, RPCError
from pyrogram.types import InlineKeyboardButton as B
from pyrogram.types import InlineKeyboardMarkup as K

from bot import render

log = logging.getLogger(__name__)


class Notifier:
    def __init__(self, client, service, settings):
        self.client, self.service, self.settings = client, service, settings
        self.tasks = {}
        self.pin_locks = {}
        self.generations = {}
        self.entry_locks = {}

    def tab_keyboard(self, group):
        return K(
            [
                [
                    B("Open tab", url=self.settings.mini_app_url(group["token"])),
                    B("Settle up", callback_data=f"s:{group['id']}"),
                ],
                [B("✋ I'm in", callback_data=f"j:{group['id']}")],
            ]
        )

    def entry_keyboard(self, entity, entry, group):
        open_tab = [B("Open tab", url=self.settings.mini_app_url(group["token"]))]
        if entity == "expense":
            if entry["deleted_at"]:
                return K([open_tab])
            return K(
                [
                    [
                        B("Edit", callback_data=f"e:{entry['id']}"),
                        B("Delete", callback_data=f"d:{entry['id']}"),
                        B("Not mine?", callback_data=f"n:{entry['id']}"),
                    ],
                    open_tab,
                ]
            )
        if entry["undone_at"]:
            return K([open_tab])
        buttons = [B("Undo", callback_data=f"u:{entry['id']}")]
        if entry["kind"] == "opening":
            buttons.append(B("Edit", callback_data=f"o:{entry['id']}"))
        return K([buttons, open_tab])

    async def setup(self, group_id):
        state = await self.service.snapshot(group_id)
        group = state["group"]
        keys = K(
            [
                [
                    B("✋ I'm in", callback_data=f"j:{group_id}"),
                    B("💼 Set up wallets", callback_data=f"w:{group_id}"),
                    B("✅ Done", callback_data=f"f:{group_id}"),
                ],
                [B("Open tab", url=self.settings.mini_app_url(group["token"]))],
            ]
        )
        if group["setup_message_id"]:
            try:
                await self.client.edit_message_text(
                    group["chat_id"],
                    group["setup_message_id"],
                    render.welcome(state),
                    reply_markup=keys,
                    parse_mode=enums.ParseMode.HTML,
                )
                return
            except MessageNotModified:
                return
            except (MessageIdInvalid, RPCError):
                log.info("Replacing unavailable setup message for group %s", group_id)
        msg = await self.client.send_message(
            group["chat_id"], render.welcome(state), reply_markup=keys, parse_mode=enums.ParseMode.HTML
        )
        await self.group_fields(group_id, setup_message_id=msg.id)

    async def group_fields(self, group_id, **fields):
        allowed = {"setup_message_id", "pinned_message_id", "pin_ok", "pin_warned"}
        if not set(fields) <= allowed:
            raise ValueError("Invalid presentation field.")
        async with self.service.db.transaction():
            await self.service.db.execute(
                "UPDATE groups SET " + ",".join(f"{key}=?" for key in fields) + " WHERE id=?",
                (*fields.values(), group_id),
            )

    def schedule(self, group_id):
        # Coalesce bursts into one edit every two seconds, without starving under continuous traffic.
        self.generations[group_id] = self.generations.get(group_id, 0) + 1
        if group_id not in self.tasks or self.tasks[group_id].done():
            self.tasks[group_id] = asyncio.create_task(self._later(group_id))

    async def _later(self, group_id):
        while True:
            await asyncio.sleep(2)
            generation = self.generations[group_id]
            try:
                await self.pin(group_id)
            except FloodWait as error:
                await asyncio.sleep(error.value)
                continue
            except (RPCError, ValueError):
                log.exception("Could not refresh running total")
            if self.generations[group_id] == generation:
                return

    async def pin(self, group_id):
        async with self.pin_locks.setdefault(group_id, asyncio.Lock()):
            state = await self.service.snapshot(group_id)
            group = state["group"]
            message_id = group["pinned_message_id"]
            if message_id:
                try:
                    await self.client.edit_message_text(
                        group["chat_id"],
                        message_id,
                        render.running(state),
                        reply_markup=self.tab_keyboard(group),
                        parse_mode=enums.ParseMode.HTML,
                    )
                except MessageNotModified:
                    pass
                except MessageIdInvalid:
                    message_id = None
            if not message_id:
                msg = await self.client.send_message(
                    group["chat_id"],
                    render.running(state),
                    reply_markup=self.tab_keyboard(group),
                    parse_mode=enums.ParseMode.HTML,
                )
                message_id = msg.id
                await self.group_fields(group_id, pinned_message_id=message_id)
            if group["pin_ok"]:
                return
            try:
                await self.client.pin_chat_message(group["chat_id"], message_id, disable_notification=True)
                await self.group_fields(group_id, pin_ok=1)
            except ChatAdminRequired:
                await self.group_fields(group_id, pin_ok=0)
                if not group["pin_warned"]:
                    await self.client.send_message(
                        group["chat_id"],
                        "Give me admin permission to pin messages. "
                        "Until then, I’ll include the running total under each entry.",
                    )
                    await self.group_fields(group_id, pin_warned=1)

    async def changed(self, group_id, entity, entry, *, action="create", before=None, actor_id=None):
        async with self.entry_locks.setdefault((entity, entry["id"]), asyncio.Lock()):
            await self._changed(group_id, entity, entry, action=action, before=before, actor_id=actor_id)

    async def _changed(self, group_id, entity, entry, *, action="create", before=None, actor_id=None):
        """Ledger commit already succeeded. Telegram delivery errors must never imply rollback."""
        self.schedule(group_id)
        try:
            state = await self.service.snapshot(group_id)
            group = state["group"]
            event_entry = entry
            entry = next(
                e for e in state["expenses" if entity == "expense" else "transfers"] if e["id"] == entry["id"]
            )
            content = render.entry_text(state, entity, entry)
            keys = self.entry_keyboard(entity, entry, group)
            if entry["chat_message_id"]:
                try:
                    await self.client.edit_message_text(
                        group["chat_id"],
                        entry["chat_message_id"],
                        content,
                        reply_markup=keys,
                        parse_mode=enums.ParseMode.HTML,
                    )
                except MessageNotModified:
                    pass
            else:
                message = await self.client.send_message(
                    group["chat_id"], content, reply_markup=keys, parse_mode=enums.ParseMode.HTML
                )
                table = "expenses" if entity == "expense" else "transfers"
                async with self.service.db.transaction():
                    await self.service.db.execute(
                        f"UPDATE {table} SET chat_message_id=? WHERE id=? AND group_id=?",
                        (message.id, entry["id"], group_id),
                    )
            if action == "edit":
                actor = next(m for m in state["members"] if m["tg_user_id"] == actor_id)
                label = event_entry["description"] if entity == "expense" else "Opening balance"
                from html import escape

                await self.client.send_message(
                    group["chat_id"],
                    f"✏️ {escape(label)} {render.money(before['amount_minor'], group)} → "
                    f"{render.money(event_entry['amount_minor'], group)} by {render.mention(actor)}",
                    parse_mode=enums.ParseMode.HTML,
                )
        except (RPCError, ValueError):
            log.exception("Entry committed but Telegram notification failed; use /tab to refresh")

    async def review(self, group_id, entry, user_id):
        state = await self.service.snapshot(group_id, user_id)
        payer = next(m for m in state["members"] if m["id"] == entry["payer_member_id"])
        from html import escape

        await self.client.send_message(
            state["group"]["chat_id"],
            f"{render.mention(payer)}, {render.mention(state['me'])} asks you to review "
            f"{escape(entry['description'])}.",
            parse_mode=enums.ParseMode.HTML,
        )

    async def invite(self, group_id, invite):
        state = await self.service.snapshot(group_id)
        source = next(m for m in state["members"] if m["id"] == invite["from_member_id"])
        target = next(m for m in state["members"] if m["id"] == invite["to_member_id"])
        await self.client.send_message(
            state["group"]["chat_id"],
            f"{render.mention(target)}, {render.mention(source)} wants to share a wallet. "
            "Accepting joins both active wallets; the proposer becomes the settler.",
            parse_mode=enums.ParseMode.HTML,
            reply_markup=K(
                [
                    [
                        B("Accept", callback_data=f"a:{invite['id']}"),
                        B("Decline", callback_data=f"x:{invite['id']}"),
                    ]
                ]
            ),
        )

    async def close(self):
        for task in self.tasks.values():
            task.cancel()
        await asyncio.gather(*self.tasks.values(), return_exceptions=True)
