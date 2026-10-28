import hashlib
import hmac
import json
import logging
import time
from datetime import UTC
from pathlib import Path
from urllib.parse import parse_qsl, quote

from aiohttp import web
from pyrogram import enums
from pyrogram.errors import RPCError

from bot import ledger
from bot.perms import PermissionDenied, require

log = logging.getLogger(__name__)
WEB_DIR = Path(__file__).resolve().parent.parent / "web"
SERVICE = web.AppKey("service", object)
SETTINGS = web.AppKey("settings", object)
CLIENT = web.AppKey("client", object)
NOTIFIER = web.AppKey("notifier", object)


def verify_init_data(init_data: str, bot_token: str, *, now: float | None = None) -> dict | None:
    """Validate signed Telegram data, including freshness and duplicate field rejection."""
    try:
        pairs = parse_qsl(init_data, keep_blank_values=True, strict_parsing=True)
        fields = dict(pairs)
        if len(pairs) != len(fields):
            return None
        received = fields.pop("hash", "")
        check_string = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
        secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
        expected = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, received):
            return None
        age = (time.time() if now is None else now) - int(fields["auth_date"])
        if not -30 <= age <= 3600:
            return None
        user = json.loads(fields["user"])
        if not isinstance(user, dict) or type(user.get("id")) is not int or user["id"] <= 0:
            return None
        return {"user": user, "start_param": fields.get("start_param")}
    except (ValueError, KeyError, TypeError):
        return None


async def telegram_membership(client, chat_id, user_id):
    try:
        member = await client.get_chat_member(chat_id, user_id)
    except RPCError as error:
        raise PermissionDenied(
            "Could not verify your Telegram group membership. Try again from the group."
        ) from error
    require(
        member.status
        in {enums.ChatMemberStatus.OWNER, enums.ChatMemberStatus.ADMINISTRATOR, enums.ChatMemberStatus.MEMBER}
        or (member.status == enums.ChatMemberStatus.RESTRICTED and member.is_member),
        "You must be a member of this Telegram group.",
    )


@web.middleware
async def errors(request, handler):
    try:
        response = await handler(request)
    except PermissionDenied as error:
        response = web.json_response({"error": str(error)}, status=403)
    except (ValueError, KeyError, TypeError, ZeroDivisionError, OverflowError) as error:
        response = web.json_response(
            {"error": str(error) if isinstance(error, ValueError) else "Invalid request."}, status=400
        )
    except web.HTTPException as error:
        response = web.json_response({"error": error.reason}, status=error.status)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    if request.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    return response


async def context(request):
    auth = verify_init_data(request.headers.get("X-Telegram-Init-Data", ""), request.app[SETTINGS].bot_token)
    if not auth:
        raise web.HTTPUnauthorized(reason="Reopen the tab from Telegram to sign in.")
    token = request.match_info["token"]
    if auth["start_param"] and not hmac.compare_digest(auth["start_param"], token):
        raise PermissionDenied("This Telegram session belongs to a different tab.")
    service = request.app[SERVICE]
    group = await service.group_for_token(token)
    if not group:
        raise web.HTTPNotFound(reason="Tab not found.")
    user_id = auth["user"]["id"]
    await telegram_membership(request.app[CLIENT], group["chat_id"], user_id)
    # DB membership is independent of Telegram membership; both are required on every route.
    state = await service.snapshot(group["id"], user_id)
    return service, group, user_id, state


async def state(request):
    *_, snapshot = await context(request)
    service = request.app[SERVICE]
    async with service.db.transaction():
        snapshot["invites"] = await service.db.all(
            "SELECT * FROM wallet_invites WHERE group_id=? AND status='pending'", (snapshot["group"]["id"],)
        )
        snapshot["audit"] = await service.db.all(
            "SELECT * FROM audit_log WHERE group_id=? ORDER BY id DESC LIMIT 200", (snapshot["group"]["id"],)
        )
    return web.json_response(snapshot)


async def history(request):
    *_, snapshot = await context(request)
    month, category, payer = (request.query.get(k) for k in ("month", "category", "payer"))
    expenses = [
        e
        for e in snapshot["expenses"]
        if (not month or e["created_at"].startswith(month))
        and (not category or e["category"] == category)
        and (not payer or str(e["payer_member_id"]) == payer)
    ]
    transfers = [
        t
        for t in snapshot["transfers"]
        if (not month or t["created_at"].startswith(month)) and not category and not payer
    ]
    return web.json_response({"expenses": expenses, "transfers": transfers})


async def summary(request):
    *_, snapshot = await context(request)
    from datetime import datetime

    month = request.query.get("month", datetime.now(UTC).strftime("%Y-%m"))
    import re

    if not re.fullmatch(r"[0-9]{4}-(?:0[1-9]|1[0-2])", month):
        raise ValueError("Use a month like 2026-10.")
    return web.json_response(ledger.summary(snapshot["expenses"], snapshot["members"], month))


async def breakdown(request):
    *_, snapshot = await context(request)
    wallet_id = int(request.query.get("wallet", snapshot["me"]["wallet_id"]))
    if wallet_id not in snapshot["balances"]:
        raise ValueError("Wallet not found.")
    rows = []
    for e in snapshot["expenses"]:
        if e["deleted_at"]:
            continue
        paid = e["amount_minor"] if e["payer_wallet_id"] == wallet_id else 0
        share = sum(s["amount_minor"] for s in e["shares"] if s["wallet_id"] == wallet_id)
        if paid or share:
            rows.append(
                {
                    "entity": "expense",
                    "id": e["id"],
                    "description": e["description"],
                    "created_at": e["created_at"],
                    "paid_minor": paid,
                    "share_minor": share,
                    "delta_minor": paid - share,
                }
            )
    for t in snapshot["transfers"]:
        if t["undone_at"] or wallet_id not in (t["from_wallet_id"], t["to_wallet_id"]):
            continue
        rows.append(
            {
                "entity": "transfer",
                "id": t["id"],
                "description": t["kind"].title(),
                "created_at": t["created_at"],
                "delta_minor": t["amount_minor"] * (1 if t["from_wallet_id"] == wallet_id else -1),
            }
        )
    return web.json_response(
        {
            "wallet_id": wallet_id,
            "balance_minor": snapshot["balances"][wallet_id],
            "rows": sorted(rows, key=lambda r: r["created_at"]),
        }
    )


async def export(request):
    service, group, user_id, *_ = await context(request)
    return web.Response(
        text=await service.export_csv(group["id"], user_id),
        content_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="kintab.csv"'},
    )


async def action(request):
    service, group, user_id, snapshot = await context(request)
    data = await request.json()
    if not isinstance(data, dict):
        raise ValueError("Send a JSON object.")
    kind = data.pop("action", "")
    group_id = group["id"]
    notifier = request.app[NOTIFIER]
    entry = None
    entity = None
    before = None
    change = "create"
    entry_id = data.get("id")
    if kind in {"edit_expense", "edit_opening"}:
        entity = "expense" if kind == "edit_expense" else "transfer"
        before = next(
            (e for e in snapshot["expenses" if entity == "expense" else "transfers"] if e["id"] == entry_id),
            None,
        )
        if not before:
            raise ValueError("Entry not found.")
        change = "edit"
    if kind == "add_expense":
        entity, entry = "expense", await service.add_expense(group_id, user_id, data)
    elif kind == "edit_expense":
        entry = await service.edit_expense(group_id, user_id, entry_id, data)
    elif kind == "delete_expense":
        entity, entry = "expense", await service.delete_expense(group_id, user_id, entry_id)
    elif kind == "review_expense":
        reviewed = await service.review_expense(group_id, user_id, entry_id)
        try:
            await notifier.review(group_id, reviewed, user_id)
        except RPCError:
            log.exception("Review recorded but Telegram ping failed")
    elif kind == "add_transfer":
        entity, entry = "transfer", await service.add_transfer(group_id, user_id, data)
    elif kind == "undo_transfer":
        entity, entry = "transfer", await service.undo_transfer(group_id, user_id, entry_id)
    elif kind == "edit_opening":
        entry = await service.edit_opening(group_id, user_id, entry_id, data)
    elif kind == "invite":
        invite = await service.invite(group_id, user_id, data["member_id"])
        try:
            await notifier.invite(group_id, invite)
        except RPCError:
            log.exception("Invitation saved; delivery failed, still available in Group screen")
    elif kind == "respond_invite":
        if type(data.get("accept")) is not bool:
            raise ValueError("Choose accept or decline.")
        await service.respond_invite(group_id, user_id, data["id"], data["accept"])
    elif kind == "link_wallets":
        await service.link_wallets(group_id, user_id, data["from_wallet_id"], data["to_wallet_id"])
    elif kind == "unlink":
        await service.unlink(group_id, user_id, data.get("member_id"))
    elif kind == "settler":
        await service.set_settler(group_id, user_id, data["member_id"])
    elif kind == "assign_wallet":
        await service.assign_wallet(group_id, user_id, data["member_id"], data.get("wallet_id"))
    elif kind in {"guest", "member"}:
        await service.add_guest(group_id, user_id, data["name"], data.get("wallet_id"))
    elif kind == "leave":
        await service.leave(group_id, user_id, data.get("member_id"))
    elif kind == "settings":
        await service.update_settings(group_id, user_id, data)
    elif kind == "forget":
        await service.forget(group_id, user_id, confirmation=data["confirmation"])
        return web.json_response({"ok": True, "forgotten": True})
    else:
        raise ValueError("Unknown action.")
    if entry is not None:
        await notifier.changed(group_id, entity, entry, action=change, before=before, actor_id=user_id)
    else:
        notifier.schedule(group_id)
    return web.json_response({"ok": True, "entry": entry})


def create_app(settings, service, client, notifier):
    app = web.Application(middlewares=[errors], client_max_size=64 * 1024)
    app[SETTINGS], app[SERVICE], app[CLIENT], app[NOTIFIER] = settings, service, client, notifier
    html = (WEB_DIR / "index.html").read_text()
    html = html.replace("__KINTAB_BOT_URL__", f"https://t.me/{quote(settings.bot_username, safe='')}")
    for asset in ("static/app.css", "static/app.js"):
        digest = hashlib.sha256((WEB_DIR / asset).read_bytes()).hexdigest()[:12]
        html = html.replace(f'"{asset}"', f'"/{asset}?v={digest}"')

    async def index(request):
        return web.Response(text=html, content_type="text/html", headers={"Cache-Control": "no-cache"})

    async def health(request):
        async with service.db.transaction():
            await service.db.one("SELECT 1")
        return web.json_response({"ok": True})

    app.router.add_get("/", index)
    app.router.add_get("/healthz", health)
    for route, handler in {
        "state": state,
        "history": history,
        "summary": summary,
        "breakdown": breakdown,
        "export": export,
    }.items():
        app.router.add_get("/api/{token}/" + route, handler)
    app.router.add_post("/api/{token}/action", action)
    app.router.add_static("/static/", WEB_DIR / "static")
    return app


async def start_web(settings, service, client, notifier):
    runner = web.AppRunner(create_app(settings, service, client, notifier))
    await runner.setup()
    await web.TCPSite(runner, settings.web_host, settings.web_port).start()
    return runner
