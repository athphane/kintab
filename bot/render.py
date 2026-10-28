from html import escape


def _label(value, limit=100):
    value = " ".join(str(value).split())
    return escape(value if len(value) <= limit else value[: limit - 1] + "…")


def _units(value):
    # Budget the serialized HTML as well as astral Unicode characters conservatively.
    return len(value.encode("utf-16-le")) // 2


def _block(header, rows, footer=(), *, limit=3700):
    selected = []
    for row in rows:
        candidate = "\n".join([*header, *selected, row, *footer])
        if _units(candidate) + 80 > limit:
            break
        selected.append(row)
    if len(selected) < len(rows):
        selected.append(f"… {len(rows) - len(selected)} more in Open tab.")
    return "\n".join([*header, *selected, *footer])


def money(amount, group):
    digits = group["minor_units"]
    whole, part = divmod(abs(amount), 10**digits)
    return ("−" if amount < 0 else "") + f"{whole:,}" + (f".{part:0{digits}d}" if digits else "")


def mention(member):
    name = _label(member["name"], 70)
    return f'<a href="tg://user?id={member["tg_user_id"]}">{name}</a>' if member.get("tg_user_id") else name


def running(state, *, limit=3700):
    group = state["group"]
    wallets = {w["id"]: w for w in state["wallets"]}
    header = [f"📌 KinTab — {_label(group['title'], 80)}", f"{group['currency']} · {group['debt_mode']}"]
    lines = []
    for line in state["lines"]:
        source, target = wallets[line["from_wallet_id"]], wallets[line["to_wallet_id"]]
        settler = source.get("settler")
        label = f" ({_label(settler['name'], 50)} settles)" if settler else ""
        lines.append(
            f"{_label(source['name'], 70)} → {_label(target['name'], 70)}  "
            f"<b>{money(line['amount_minor'], group)}</b>{label}"
        )
    if not state["lines"]:
        lines.append("All settled ✨")
    entries = [e | {"label": e["description"]} for e in state["expenses"] if not e["deleted_at"]]
    entries += [t | {"label": t["kind"].title()} for t in state["transfers"] if not t["undone_at"]]
    footer = []
    if entries:
        last = max(entries, key=lambda e: e["created_at"])
        footer.append(
            f"Last: {_label(last['label'], 80)} · {money(last['amount_minor'], group)} ({last['created_at'][:10]})"
        )
    return _block(header, lines, footer, limit=limit)


def entry_text(state, entity, entry):
    group = state["group"]
    members = {m["id"]: m for m in state["members"]}
    wallets = {w["id"]: w for w in state["wallets"]}
    amount = f"{group['currency']} {money(entry['amount_minor'], group)}"
    details, footer = [], []
    if entity == "expense":
        line = f"🛒 {mention(members[entry['payer_member_id']])} paid {amount} · {_label(entry['description'], 200)}"
        if entry["deleted_at"]:
            line = f"<s>{line}</s>"
            footer.append(f"Deleted by {mention(members[entry['deleted_by']])}")
        else:
            debts = {}
            for share in entry["shares"]:
                if share["wallet_id"] != entry["payer_wallet_id"]:
                    debts[share["wallet_id"]] = debts.get(share["wallet_id"], 0) + share["amount_minor"]
            for wallet, share in debts.items():
                details.append(f"{_label(wallets[wallet]['name'], 100)} owe {money(share, group)}")
            if entry["logger_member_id"] != entry["payer_member_id"]:
                footer.append(f"Logged by {mention(members[entry['logger_member_id']])}")
    else:
        source = _label(wallets[entry["from_wallet_id"]]["name"], 100)
        target = _label(wallets[entry["to_wallet_id"]]["name"], 100)
        line = {
            "payment": f"💸 {source} paid {target} {amount}",
            "opening": f"🏁 Opening balance: {target} owes {source} {amount}",
            "writeoff": f"🤝 {target} forgave {source} {amount}",
        }[entry["kind"]]
        if entry["undone_at"]:
            line = f"<s>{line}</s>"
            footer.append(f"Undone by {mention(members[entry['undone_by']])}")
    line = _block([line], details, footer, limit=2300 if not group["pin_ok"] else 3700)
    if not group["pin_ok"]:
        total = running(state, limit=3700 - _units(line) - 30).split("\n", 2)[-1]
        line += "\n\nRunning total:\n" + total
    return line


def welcome(state):
    members = [m for m in state["members"] if not m["left_at"]]
    names = []
    for member in members:
        label = _label(member["name"], 70)
        if _units(", ".join([*names, label])) > 2700:
            break
        names.append(label)
    joined = ", ".join(names) or "Nobody yet"
    if len(names) < len(members):
        joined += f" · and {len(members) - len(names)} more (see Open tab)"
    return (
        "👋 KinTab tracks shared household spending between wallets.\n"
        "Tap <b>I'm in</b> to join. Everyone starts with their own wallet.\n"
        f"Joined: {joined}\n\nCouples can share a wallet. Shares are equal per person by default.\n"
        "Any joined member can add people, assign wallets and manage this tab in Open tab → Group."
    )
