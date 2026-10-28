# KinTab — Plan

A Telegram bot + Web App for tracking shared household spending in a group chat.
It replaces the manual "Running total: X" message with a live, pinned balance that
updates itself.

Built on the same stack as `~/python/sticker-bot`, and open to any Telegram group.

---

## 1. Core ideas

### Members
People in the group who have joined the tab by tapping **I'm in**. Guest members
(people without Telegram, e.g. a parent who pays some bills) can be added by others.

### Wallets
**A wallet is a group of people who pay as one.** Everyone starts in their own wallet.
A couple (or parents, etc.) can share one.

- KinTab only tracks money **between wallets**. How a couple settles inside their
  wallet is their business and never shows up.
- Each wallet has a **settler**: the person who usually sends the money. Reminders
  and "you owe" pings go to them. It's a label only, and it never changes the numbers.
- Every button and permission applies **per wallet**. For example, either partner can
  undo a payment from their shared wallet.

### How an expense is split
1. The amount is split between **people** (default: equally).
2. Each person's share is added to **their wallet**.
3. The payer's wallet is owed everything except its own members' shares.

Example (the original household: You | Sister + Husband):

| | Groceries 900 paid by You |
|---|---|
| Per person | 300 each |
| You | 300, already covered because you paid |
| Sister & Husband wallet | 300 + 300 = **600 owed to You** |

If Husband pays a 1,200 bill instead, it's 400 per person, so You owe their wallet
400. That's the existing 2/3 and 1/3 rules, worked out automatically.

### Split modes (per group, set by group admins)
- **By person** (default): every member has weight 1. Weights can be changed later
  (e.g. a teenager counts as 0.5).
- **By wallet**: every wallet pays an equal share, whatever its size (e.g. two
  couples splitting a holiday house 50/50).
- **Per-expense override** (Web App): choose which people or wallets an expense is
  split between, e.g. "everyone except Dan" for Dan's birthday cake.

### Money
- One currency per group (default **MVR**).
- Amounts are stored as **integer minor units** (laari), so there's no float drift.
- Uneven splits: the leftover laari go to the payer's share when the payer is in the
  split. Otherwise they go to members in join order.

### Showing balances
- Each wallet has a **net balance** (positive = is owed, negative = owes). The
  balances always add up to 0.
- **Simplified** (default): the pinned message shows the fewest transfers that settle
  everyone. The algorithm greedily matches the biggest debtor with the biggest
  creditor.
- **Direct** (group setting): shows debts pair by pair instead.
- The Web App can always explain a balance ("why do I owe this?").
- With two wallets, both modes show the same single line.

---

## 2. Setup flow (first time in a group)

1. **The bot is added and posts a welcome** with **[✋ I'm in]**. Bots can't list
   group members, so joining by tap is required. It also keeps out people in the chat
   who aren't part of the household.
2. **People tap I'm in.** The same message updates in place:
   `Joined: Athphane, Sister, Husband` · **[I'm in] [💼 Set up wallets] [✅ Done]**.
   Everyone starts in their own wallet.
3. **Linking wallets needs consent.** Sister taps *Set up wallets → Share a wallet
   with… → Husband*. The bot posts **[Accept] [Decline]**, and only Husband's tap
   works. Whoever proposed it becomes the settler (this can be changed later).
   Linking or unlinking wallets is only allowed while the wallets involved have a
   balance of 0.
4. **Done.** The bot confirms the defaults (currency MVR, split equally per person).
   Only Telegram group admins can change them.
5. **Optional opening balance.** **[Add opening balance] [Start fresh]**, e.g.
   "Sister & Husband owe me 6,000". It's recorded like any other entry, with an Undo.
6. **The running total is pinned.** If the bot isn't an admin with pin rights, it
   asks once. Without pin rights, it shows the running total under every expense or
   payment message instead.

---

## 3. Day-to-day in the chat

### Commands
Privacy mode stays **on**, so the bot only sees commands and replies to its own
messages, never normal chat.

| Command | What it does |
|---|---|
| `/spent 450 groceries` | You paid 450, split by the group default |
| `/spent 1200 electricity @sister` | Sister paid (you logged it). Sister gets pinged |
| reply to someone's message with `/spent 1200 electricity` | That person paid. Works for people without a @username |
| `/paid 3000` | Record a payment your wallet sent. If there's more than one possible recipient, the bot asks who with buttons |
| `/paid 3000 @sister` | Record a payment to a specific person's wallet |
| `/tab` | Show the running total and re-pin it |
| `/leave` | Leave the tab |
| `/export` | CSV of all entries (sent to you in DM) |
| `/forget` | Group admins only: delete this group's whole tab, after a confirmation step |
| `/help` | How it works |

Amount parsing accepts `450`, `1,200`, `1200.50`. The rest of the message is the
description. If `/spent` comes from someone who hasn't joined, the bot asks them to
tap **I'm in** first and logs nothing.

### Every expense records two people
- **Payer**: whose money it was. This decides who's owed.
- **Logger**: who typed it. This only matters for permissions.

When the payer isn't obvious (e.g. a guest), the bot shows a "Who paid?" row of
member buttons.

### What the bot posts
```
🛒 Athphane paid MVR 450.00 · Groceries
Sister & Husband owe 300.00
Running total: Sister & Husband owe Athphane 6,540.00
[Edit] [Delete] [Not mine?]
```
```
💸 Sister & Husband paid Athphane MVR 3,000.00
Running total: Sister & Husband owe Athphane 3,540.00
[Undo]
```

### The pinned running total
Edited in place after every change:
```
📌 KinTab — Household
Sister & Husband → Athphane   6,240.00  (Sister settles)
Dan (left) → Athphane           350.00
Last: Electricity · 1,200 (today)
[Open tab]  [Settle up]
```
Non-members also see **[I'm in]** here, so people can join late.

---

## 4. Permissions

Telegram shows the same buttons to everyone in a group, so **the bot checks who
tapped**. Anyone not allowed gets a popup alert (e.g. *"Only Sister & Husband or
Athphane can undo this"*) and nothing changes. The Web App enforces the same rules.

| Thing | Action | Who can do it |
|---|---|---|
| Expense | Edit / Delete | The payer's wallet and the logger |
| Expense | Not mine? | Anyone in the split. It doesn't change anything, just pings the payer to review it |
| Payment | Undo (24h in chat, then in the Web App) | Both wallets involved |
| Opening balance | Undo / Edit | Both wallets involved |
| Leftover debt | Forgive (write-off) | **Only the wallet that's owed** |
| Pinned total | Settle up | Wallets that owe money |
| Wallet link | Accept / Decline | Only the invited person |
| Group settings, removing members, `/forget` | — | Telegram group admins |

Everything is **instant** (no confirmation step), but every change leaves a trail.
An undone entry stays in the chat, edited to show *"~~Paid 3,000~~ · undone by
Sister"*. Edits post a short note in the chat: *"✏️ Groceries 450 → 540 by Athphane"*.
All of this goes into an audit log.

---

## 5. Leaving

- **Anyone can leave or be removed at any time**, through `/leave`, a group admin, or
  automatically when they leave or are kicked from the Telegram group (the bot
  receives the service message).
- A member who leaves is **left out of new expenses** from then on. Their past
  entries stay as they are.
- **Their unsettled balance stays on the tab**, shown as `Dan (left)`, until one of
  these happens:
  1. **It's settled**: a payment is recorded as usual.
  2. **It's written off**: only the wallet that's owed can **Forgive** it. This is
     logged as *"Athphane forgave Dan 350"*.
- **If a member of a shared wallet leaves**, the wallet and its balance stay with the
  remaining member(s). It isn't split back into personal debts.
- **No automatic redistribution** of a leaver's debt to other members. If a group
  wants that, the people owed forgive it and then log a shared "Dan's unpaid share"
  expense, which is visible and can be undone.
- **Late joiners** only share in expenses logged after they join.

---

## 6. Web App

Opened from **[Open tab]** on the pinned message.

> Telegram only allows `web_app` inline buttons in private chats. In groups, use a
> **Direct Link Mini App** URL button instead (`https://t.me/<bot>/app?startapp=<group_token>`,
> set up with BotFather `/newapp`). `group_token` is an opaque random ID, not the chat ID.
> The server verifies initData and then checks the user is a member of that group.

Screens:
- **Home**: balance card (simplified lines, plus your wallet's net), **Add expense**
  and **Settle up** buttons.
- **History**: expenses and payments, filterable by month, category and payer.
  Entries you can't act on are read-only.
- **Add / edit expense**: amount, description, category, who paid, the split (all /
  pick people or wallets / custom weights), and later a receipt photo.
- **Balance breakdown**: which entries add up to each balance.
- **Monthly summary**: total household spend and each wallet's share.
- **Group**: members, wallets (link and unlink requests, change the settler),
  guests, and settings (admins only).

Same frontend approach as sticker-bot: plain HTML/CSS/JS, Telegram theme
variables, haptics, and `?v=<hash>` cache-busting.

---

## 7. Architecture

Same layout as `sticker-bot`:

```
kintab/
├── main.py              # starts the Kurigram client + aiohttp web server
├── bot/
│   ├── config.py        # config.ini → Settings
│   ├── db.py            # SQLite connection, schema, migrations
│   ├── ledger.py        # pure functions: splits, balances, simplification
│   ├── groups.py        # group / member / wallet operations
│   ├── entries.py       # expenses, payments, opening balances, write-offs, undo
│   ├── perms.py         # who may do what (shared by bot + Web App)
│   ├── render.py        # message text for posts and the pinned total
│   ├── handlers.py      # commands, callback buttons, service messages
│   ├── parse.py         # "/spent 1,200.50 electricity @x" parsing
│   └── webapp.py        # aiohttp app + JSON API (initData auth)
├── web/                 # index.html, static/app.js, static/app.css
├── tests/               # pytest: ledger math, parsing, permissions
├── config.ini.example
├── Dockerfile
└── docker-compose.yml   # external `cloudflare` network, like sticker-bot
```

- **Kurigram** (MTProto) bot client; **aiohttp** web server in the same event loop.
- **SQLite** through `aiosqlite`, WAL mode, a single file in `data/`.
- Web API auth uses the same `verify_init_data` HMAC as sticker-bot, plus a
  membership check for each group.
- `ledger.py` has no I/O, so it's simple to test thoroughly.
- Callback data stays under Telegram's 64-byte limit: `u:<entry_id>`, `j:<group_id>` etc.
- Handle **group → supergroup migration** (the chat ID changes) by updating
  `groups.chat_id`.
- Rate-limit edits to the pinned message (debounce about 2s) to avoid flood waits.

### Deployment
- Public URL: **https://kintab.athfan.dev** (`public_url` in config.ini)
- The Cloudflare tunnel routes it to **`http://kintab-bot:8080`**. In
  docker-compose this means `container_name: kintab-bot`, joining the external
  `cloudflare` network, listening on `0.0.0.0:8080` inside the container, with no
  host port published (same as sticker-bot).
- Volumes: `./config.ini` (read-only), `./data` (SQLite DB + logs), `./workdir`
  (Kurigram session). Runs as `PUID:PGID` 1000.

### Data model

```sql
groups(
  id, chat_id UNIQUE, token UNIQUE, title,
  currency DEFAULT 'MVR', minor_units DEFAULT 2,
  split_mode  -- 'person' | 'wallet'
  debt_mode   -- 'simplified' | 'direct'
  pinned_message_id, setup_message_id, created_at
)

members(
  id, group_id, tg_user_id NULL,          -- NULL = guest
  name, username, weight DEFAULT 1,
  wallet_id, joined_at, left_at NULL
)

wallets(
  id, group_id, settler_member_id, created_at, closed_at NULL
)

wallet_invites(
  id, group_id, from_member_id, to_member_id, status, created_at
)

expenses(
  id, group_id, amount_minor, description, category,
  payer_member_id, payer_wallet_id,       -- wallet captured when logged
  logger_member_id, created_at,
  deleted_at NULL, deleted_by NULL,
  chat_message_id
)

expense_shares(
  expense_id, member_id, wallet_id,       -- wallet captured when logged
  amount_minor
)

transfers(
  id, group_id, kind,                     -- 'payment' | 'opening' | 'writeoff'
  from_wallet_id, to_wallet_id, amount_minor,
  recorded_by, created_at,
  undone_at NULL, undone_by NULL,
  chat_message_id
)

audit_log(
  id, group_id, member_id, action, entity, entity_id, before_json, after_json, created_at
)
```

**Balance rule** (net per wallet, ignoring deleted and undone rows):
- Expense: payer wallet `+amount`, each share's wallet `−share`.
- Transfer: `from` wallet `+amount`, `to` wallet `−amount`.
  - payment A→B: from=A, to=B
  - opening "A owes B X": from=B, to=A
  - write-off "B forgives A X": from=A, to=B

Shares and payers store the wallet **as it was when the entry was logged**, so
changing wallets later never rewrites history.

---

## 8. Build phases

1. **Foundations**: project skeleton, config, DB schema, `ledger.py` (splits,
   rounding, balances, simplification) with pytest coverage.
2. **Chat core**: setup message and I'm in, `/spent`, `/paid`, `/tab`, post
   formatting, the pinned running total, and permission-checked
   Edit/Delete/Undo/Not mine? buttons.
3. **Wallets and lifecycle**: wallet invites with consent, settler, opening
   balance, `/leave`, auto-leave on service messages, Forgive, guests, group
   migration.
4. **Web App**: Direct Link Mini App, auth + membership checks, home, history,
   add/edit expense with custom splits, breakdown, group screen.
5. **Ship**: `/export`, `/forget`, Docker + Cloudflare tunnel, README, and moving
   the real household over (opening balance of the current amount owed).

---

## 9. Later

- Recurring bills with reminders ("Electricity due on the 10th")
- Receipt photos, then OCR for the amount
- Optional tracking *inside* a wallet (e.g. Husband owes Sister)
- Monthly summary posted to the group automatically
- Multiple currencies per group
