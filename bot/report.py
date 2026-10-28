"""Render a shareable balance card from a ledger snapshot, without network access."""

import io
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from bot.render import money

FONTS = Path(__file__).with_name("fonts")
ARTWORK = Path(__file__).resolve().parent.parent / "web/static/images/balance-sculpture.png"
WIDTH = 1080
MAX_ROUTES = 6
MAX_WALLETS = 6
BG = "#f7f7f2"
SURFACE = "#fffefa"
INK = "#183c30"
MUTED = "#777e76"
GREEN = "#214f3b"
PALE = "#edf0e5"
RED = "#b15243"
LINE = "#e5e7de"
HERO_MUTED = "#d5dfd1"
AVATARS = [("#f4e2d5", "#a66349"), ("#e4eadb", "#596c4d"), ("#e7eef1", "#527583")]
FONT_FILES = {
    "sans": "dm-sans/DMSans.ttf",
    "serif": "lora/Lora.ttf",
    "number": "manrope/Manrope.ttf",
}


@lru_cache(maxsize=128)
def _font(size, bold=False, family="sans"):
    font = ImageFont.truetype(str(FONTS / FONT_FILES[family]), size)
    weight = 500 if family == "serif" else 600 if bold or family == "number" else 400
    font.set_variation_by_axes(
        [
            weight if axis["name"] == b"Weight" else min(axis["maximum"], max(axis["minimum"], size))
            for axis in font.get_variation_axes()
        ]
    )
    return font


def _shorten(draw, value, font, width):
    value = " ".join(str(value).split())
    if draw.textlength(value, font=font) > width:
        # Keep work bounded even for unusually long names from imported snapshots.
        value = value[:200]
        while value and draw.textlength(value + "…", font=font) > width:
            value = value[:-1]
        value += "…"
    return value


def _text(draw, xy, value, size, *, fill=INK, bold=False, width=900, family="sans"):
    font = _font(size, bold, family)
    draw.text(xy, _shorten(draw, value, font, width), font=font, fill=fill, anchor="lt")


def _amount(draw, xy, value, size=32, *, fill=INK, width=290, anchor="rt"):
    # Large household totals must fit without losing significant digits.
    while size > 12 and draw.textlength(value, font=_font(size, family="number")) > width:
        size -= 1
    draw.text(xy, value, font=_font(size, family="number"), fill=fill, anchor=anchor)


def _pill(draw, right, top, value, *, width=360, size=18):
    font = _font(size, True)
    value = _shorten(draw, value, font, width - 36)
    left = right - draw.textlength(value, font=font) - 36
    draw.rounded_rectangle((left, top, right, top + 40), radius=20, fill=PALE)
    draw.text((left + 18, top + 10), value, font=font, fill=GREEN, anchor="lt")


def _avatar(draw, x, y, name, index, size=56):
    background, foreground = AVATARS[index % len(AVATARS)]
    draw.ellipse((x, y, x + size, y + size), fill=background)
    initial = str(name).strip()[:1].upper() or "?"
    draw.text(
        (x + size / 2, y + size / 2),
        initial,
        font=_font(size // 2, family="serif"),
        fill=foreground,
        anchor="mm",
    )


def _surface(draw, box):
    draw.rounded_rectangle(box, radius=26, fill=SURFACE, outline=LINE, width=1)


@lru_cache(maxsize=1)
def _artwork():
    with Image.open(ARTWORK) as source:
        artwork = source.convert("RGBA")
    artwork.thumbnail((320, 320), Image.Resampling.LANCZOS)
    return artwork


def _balance_card(image, draw, summary, group):
    card = Image.new("RGBA", (952, 256), GREEN)
    card.alpha_composite(_artwork(), (657, -26))
    mask = Image.new("L", card.size)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, 951, 255), radius=32, fill=255)
    image.paste(card, (64, 372), mask)
    label = "ALL SETTLED" if not summary["routes"] else "STILL TO SETTLE"
    _text(draw, (96, 404), f"{label} · {group['currency']}", 19, bold=True, fill=HERO_MUTED, width=610)
    _amount(draw, (96, 450), money(summary["owed_minor"], group), 82, fill=SURFACE, width=610, anchor="lt")
    note = (
        "A little less to keep track of."
        if not summary["routes"]
        else "Send money, then record it with /paid."
    )
    _text(draw, (96, 568), note, 22, fill=HERO_MUTED, width=610)


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
    route_height = 116 * len(routes) if routes else 160
    route_height += 42 if omitted_routes else 0
    wallet_height = 90 * len(people) + 40 if people else 120
    wallet_height += 42 if omitted_wallets else 0
    wallet_top = 854 + route_height + 128
    height = wallet_top + wallet_height + 152
    image = Image.new("RGB", (WIDTH, height), BG)
    draw = ImageDraw.Draw(image)

    draw.rounded_rectangle((64, 50, 120, 110), radius=17, fill=GREEN)
    _text(draw, (73, 56), "k.", 46, family="serif", fill=SURFACE, width=50)
    _text(draw, (138, 63), "KinTab", 36, bold=True, family="number", width=230)
    _pill(draw, 1016, 63, group["title"], width=520)
    draw.line((64, 145, 1016, 145), fill=LINE, width=1)
    _text(draw, (64, 184), "THE HOUSEHOLD TAB", 17, bold=True, fill=MUTED)
    _text(draw, (64, 222), "The damage.", 76, family="serif")
    _text(draw, (64, 318), "Your shared spending, all in one place.", 26, fill=MUTED)

    _balance_card(image, draw, summary, group)
    _surface(draw, (64, 650, 1016, 752))
    _text(draw, (94, 674), "Shared spending", 25, bold=True, width=410)
    _text(draw, (94, 713), "Expenses only · all time", 19, fill=MUTED, width=410)
    _amount(draw, (986, 682), f"{group['currency']} {money(summary['spent_minor'], group)}", 35, width=440)

    _text(draw, (64, 792), "Who owes whom", 36, family="serif", width=620)
    _pill(draw, 1016, 789, group["debt_mode"].capitalize(), width=280)
    y = 854
    if not routes:
        _surface(draw, (64, y, 1016, y + 144))
        draw.ellipse((92, y + 36, 156, y + 100), fill=PALE)
        draw.line([(110, y + 68), (122, y + 80), (141, y + 56)], fill=GREEN, width=4)
        _text(draw, (184, y + 35), "All settled. Household peace restored.", 29, family="serif", width=790)
        _text(draw, (184, y + 85), "No awkward reminders required.", 23, fill=MUTED, width=790)
        y += 160
    for index, route in enumerate(routes):
        source = wallets[route["from_wallet_id"]]
        target = wallets[route["to_wallet_id"]]
        _surface(draw, (64, y, 1016, y + 104))
        _avatar(draw, 88, y + 24, source["name"], index)
        _text(draw, (170, y + 21), source["name"], 26, bold=True, width=480)
        _text(draw, (170, y + 62), f"owes {target['name']}", 22, fill=MUTED, width=480)
        _amount(draw, (986, y + 38), f"{group['currency']} {money(route['amount_minor'], group)}", width=300)
        y += 116
    if omitted_routes:
        _text(draw, (70, y + 7), f"+ {omitted_routes} more payments in Open tab", 21, fill=MUTED)
        y += 42

    _text(draw, (64, y + 36), "Wallet balances", 36, family="serif")
    _text(draw, (64, y + 87), "Net balances · shared wallets count as one", 21, fill=MUTED)
    _surface(draw, (64, wallet_top, 1016, wallet_top + wallet_height))
    y = wallet_top + 20
    if not people:
        _text(draw, (94, y + 27), "A fresh tab. The possibilities are expensive.", 25, fill=MUTED, width=890)
    for index, wallet in enumerate(people):
        balance = wallet["balance_minor"]
        color = RED if balance < 0 else GREEN if balance > 0 else MUTED
        label = "OWES" if balance < 0 else "IS OWED" if balance > 0 else "ALL SETTLED"
        _avatar(draw, 90, y + 15, wallet["name"], index, size=52)
        _text(draw, (166, y + 12), wallet["name"], 25, bold=True, width=430)
        _text(draw, (166, y + 49), label, 16, bold=True, fill=color, width=430)
        _amount(
            draw,
            (986, y + 26),
            f"{group['currency']} {money(abs(balance), group)}",
            30,
            fill=color,
            width=350,
        )
        if index < len(people) - 1:
            draw.line((166, y + 87, 986, y + 87), fill=LINE, width=1)
        y += 90
    if omitted_wallets:
        _text(draw, (94, y + 9), f"+ {omitted_wallets} more wallets in Open tab", 21, fill=MUTED)

    footer_top = wallet_top + wallet_height + 40
    draw.line((64, footer_top, 1016, footer_top), fill=LINE, width=1)
    _text(draw, (64, footer_top + 26), "Shared, settled.", 23, bold=True, fill=GREEN)
    _text(draw, (64, footer_top + 65), "Full details in Open tab · /damage", 19, fill=MUTED, width=620)
    _amount(
        draw, (1016, footer_top + 65), datetime.now(UTC).strftime("%d %b %Y · UTC"), 18, fill=MUTED, width=300
    )
    output = io.BytesIO()
    output.name = "kintab-damage.png"
    image.save(output, format="PNG", optimize=True)
    output.seek(0)
    return output
