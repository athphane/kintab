"""Render a shareable balance card from a ledger snapshot, without network access."""

import io
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from bot.render import money

FONTS = Path(__file__).with_name("fonts")
WIDTH = 1080
MAX_ROUTES = 6
MAX_WALLETS = 6
BG = "#f5f2e9"
INK = "#252d24"
MUTED = "#73776d"
GREEN = "#45603b"
RED = "#ad5144"
LINE = "#e0e2d7"


@lru_cache(maxsize=64)
def _font(size, bold=False):
    return ImageFont.truetype(str(FONTS / ("DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf")), size)


def _text(draw, xy, value, size, *, fill=INK, bold=False, width=900):
    value = " ".join(str(value).split())
    font = _font(size, bold)
    if draw.textlength(value, font=font) > width:
        # Keep work bounded even for unusually long names from imported snapshots.
        value = value[:200]
        while value and draw.textlength(value + "…", font=font) > width:
            value = value[:-1]
        value += "…"
    draw.text(xy, value, font=font, fill=fill, anchor="lt")


def _amount(draw, xy, value, size=32, *, fill=INK, width=290):
    # Large household totals must fit without losing significant digits.
    while size > 12 and draw.textlength(value, font=_font(size, True)) > width:
        size -= 1
    draw.text(xy, value, font=_font(size, True), fill=fill, anchor="rt")


def damage_summary(state):
    active_wallets = {m["wallet_id"] for m in state["members"] if not m["left_at"]}
    wallets = [w for w in state["wallets"] if w["id"] in active_wallets or w["balance_minor"]]
    wallets.sort(key=lambda w: (-abs(w["balance_minor"]), w["id"]))
    return {
        "spent_minor": sum(e["amount_minor"] for e in state["expenses"] if not e["deleted_at"]),
        # Match the group's chosen settlement plan, including direct-mode cycles.
        "owed_minor": sum(line["amount_minor"] for line in state["lines"]),
        "routes": sorted(state["lines"], key=lambda line: -line["amount_minor"]),
        "wallets": wallets,
    }


def damage_image(state):
    summary = damage_summary(state)
    group = state["group"]
    wallets = {w["id"]: w for w in state["wallets"]}
    routes = summary["routes"][:MAX_ROUTES]
    people = summary["wallets"][:MAX_WALLETS]
    omitted_routes = len(summary["routes"]) - len(routes)
    omitted_wallets = len(summary["wallets"]) - len(people)
    route_height = 90 * len(routes) if routes else 125
    route_height += 38 if omitted_routes else 0
    wallet_height = 74 * len(people) if people else 58
    wallet_height += 38 if omitted_wallets else 0
    height = 740 + route_height + wallet_height
    image = Image.new("RGB", (WIDTH, height), BG)
    draw = ImageDraw.Draw(image)

    _text(draw, (56, 40), "kintab / THE HOUSEHOLD TAB", 20, bold=True, fill=GREEN)
    _text(draw, (56, 93), "THE DAMAGE.", 69, bold=True, width=820)
    _text(draw, (59, 180), group["title"], 27, fill=MUTED, width=840)
    # A tiny coin with a very relaxed attitude to household accounting.
    draw.ellipse((903, 91, 1019, 207), fill="#e9c972", outline=INK, width=3)
    draw.ellipse((934, 128, 941, 137), fill=INK)
    draw.ellipse((980, 128, 987, 137), fill=INK)
    draw.arc((941, 138, 984, 171), start=10, end=170, fill=INK, width=4)

    draw.rounded_rectangle((56, 246, 1024, 431), radius=24, fill=GREEN)
    _text(draw, (83, 272), "STILL TO SETTLE", 20, bold=True, fill="#dce7ce")
    _text(draw, (83, 318), group["currency"], 28, fill="#dce7ce", width=90)
    _amount(draw, (660, 313), money(summary["owed_minor"], group), 65, fill="white", width=466)
    draw.line((700, 275, 700, 400), fill="#7c916e", width=2)
    _text(draw, (731, 281), "SHARED SPENDING", 18, fill="#dce7ce", bold=True, width=255)
    _amount(draw, (993, 329), money(summary["spent_minor"], group), 31, fill="white", width=260)
    _text(draw, (731, 380), "All recorded expenses", 17, fill="#dce7ce", width=260)

    y = 474
    _text(draw, (60, y), "THE PAYBACK PLAN", 25, bold=True, width=600)
    _amount(draw, (1020, y + 3), group["debt_mode"].upper(), 17, fill=MUTED, width=270)
    y += 53
    if not routes:
        draw.rounded_rectangle((56, y, 1024, y + 108), radius=18, fill="#e4ecd9")
        _text(draw, (82, y + 23), "All settled. Household peace restored.", 30, bold=True, fill=GREEN)
        _text(draw, (82, y + 68), "No awkward reminders required.", 23, fill=GREEN)
        y += 125
    for index, route in enumerate(routes, 1):
        source = wallets[route["from_wallet_id"]]
        target = wallets[route["to_wallet_id"]]
        draw.rounded_rectangle((56, y, 1024, y + 78), radius=15, fill="white")
        draw.ellipse((75, y + 18, 115, y + 58), fill="#edf0e4")
        _text(draw, (88, y + 26), index, 18, bold=True, fill=GREEN, width=25)
        _text(draw, (134, y + 12), source["name"], 25, bold=True, width=530)
        _text(draw, (134, y + 46), f"owes {target['name']}", 22, fill=MUTED, width=530)
        _amount(draw, (998, y + 27), f"{group['currency']} {money(route['amount_minor'], group)}", width=310)
        y += 90
    if omitted_routes:
        _text(draw, (60, y + 3), f"+ {omitted_routes} more payments in Open tab", 21, fill=MUTED)
        y += 38

    y += 25
    _text(draw, (60, y), "WALLET VIBES", 25, bold=True)
    _text(draw, (60, y + 37), "Net balances / shared wallets count as one", 19, fill=MUTED)
    y += 79
    if not people:
        _text(draw, (60, y), "A fresh tab. The possibilities are expensive.", 25, fill=MUTED)
        y += 58
    for wallet in people:
        balance = wallet["balance_minor"]
        color = RED if balance < 0 else GREEN if balance > 0 else MUTED
        label = "OWES" if balance < 0 else "IS OWED" if balance > 0 else "ALL SQUARE"
        _text(draw, (60, y + 8), wallet["name"], 25, bold=True, width=500)
        _text(draw, (60, y + 42), label, 16, bold=True, fill=color, width=400)
        _amount(
            draw,
            (1020, y + 16),
            f"{group['currency']} {money(abs(balance), group)}",
            30,
            fill=color,
            width=420,
        )
        draw.line((60, y + 69, 1020, y + 69), fill=LINE, width=1)
        y += 74
    if omitted_wallets:
        _text(draw, (60, y + 3), f"+ {omitted_wallets} more wallets in Open tab", 21, fill=MUTED)
        y += 38

    draw.line((56, height - 82, 1024, height - 82), fill=LINE, width=2)
    _text(draw, (60, height - 54), "The tab remembers. /damage", 19, fill=MUTED, width=660)
    _amount(
        draw, (1020, height - 54), datetime.now(UTC).strftime("%d %b %Y / UTC"), 17, fill=MUTED, width=300
    )
    output = io.BytesIO()
    output.name = "kintab-damage.png"
    image.save(output, format="PNG", optimize=True)
    output.seek(0)
    return output
