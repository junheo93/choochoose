# choochoose 🚆

> *choo-choo*, meet **choose your train**.

A small, **privacy-respecting** command-line helper for reserving Korean
high-speed rail tickets. It searches trains, keeps politely retrying until a
seat (or wait-list slot) opens, optionally pays with a saved card, and can ping
you on **Telegram** when something happens.

Since SRT was merged into **Korail**, there is a single booking path: everything
goes through the Korail client, so KTX and former-SRT trains show up in one list
and you no longer pick an operator anywhere in the UI. See
[Merger note](#srtkorail-merger-note) for what was measured against the live API.

It's a clean reimplementation derived from
[`srtgo`](https://github.com/lapis42/srtgo) (MIT) and
[`korail2`](https://github.com/carpedm20/korail2) (BSD).

> [!WARNING]
> **Personal, non-commercial use only.** This automates *your own* bookings on
> *your own* account. Don't use it for resale/scalping or any commercial
> purpose. You are solely responsible for how you use it.

---

## Privacy by design

This was rebuilt with one rule: **nothing about you should ever leave your
machine except the calls that are strictly necessary** — the official railway
website, and (only if you opt in) *your own* Telegram bot.

| Concern | How choochoose handles it |
|---|---|
| **Where secrets live** | Login id/password, card data, Telegram token/chat-id, and saved search preferences are stored **only in your OS keyring** (Keychain on macOS, Secret Service on Linux, Credential Locker on Windows) via the [`keyring`](https://pypi.org/project/keyring/) library. No secrets are ever written to a file in this repo. |
| **Git safety** | Because nothing sensitive touches the filesystem, nothing sensitive can be committed. A strict [`.gitignore`](.gitignore) additionally blocks `.env`, `*secret*`, `*password*`, key files, and logs as a backstop. |
| **Namespaced storage** | All keyring entries are prefixed with `choochoose:` so they're isolated, auditable, and removable in one command (`choochoose --wipe`). |
| **Network egress** | The *only* outbound calls are to the Korail endpoint (`smart.letskorail.com`) and, if configured, `api.telegram.org`. There is **no telemetry, analytics, crash reporting, or any third-party call**. |
| **Telegram** | A standard, on-by-default notification channel — but it talks only to *your* bot: messages go to the chat id *you* provide, through a bot token *you* create. Never any third party. |
| **Logging** | `--debug` prints raw request/response bodies to **your local terminal only** — useful for debugging, never sent anywhere. Don't redirect that output into a file you then commit (the `.gitignore` covers `*.log` if you do). |

To erase every trace of your data from the keyring:

```bash
choochoose --wipe          # or menu item "저장된 개인정보 삭제 (keyring)"
```

---

## Install

Requires Python ≥ 3.10.

```bash
cd choochoose
conda create -y -n choochoose python=3.11
conda activate choochoose
pip install -e .
```

This installs the `choochoose` command into the `choochoose` env. Run it any
time with `conda activate choochoose && choochoose`. On Linux you may also need
a keyring backend (e.g. `pip install keyrings.alt` or a running Secret Service /
GNOME Keyring).

## Usage

```bash
choochoose
```

You'll get an interactive menu:

| Menu | What it does |
|---|---|
| 예매 시작 | Search trains and start the auto-retry reservation loop |
| 예매 확인/결제/취소 | View / pay / cancel / refund existing reservations |
| 로그인 설정 | Save your Korail login (verified on save, stored in keyring) |
| 텔레그램 설정 | Save a Telegram bot token + chat id for notifications |
| 카드 설정 | Save a card for optional auto-payment |
| 역 설정 / 역 직접 수정 | Pick the stations shown in the booking menu (64 high-speed stations across 경부·호남·전라·경전·동해·중앙·강릉선; anything else can be typed by hand) |
| 예매 옵션 설정 | Enable child/senior/disability fares, KTX-only, etc. |
| 텔레그램 봇 시작 (원격 예매) | Run the Telegram bot so you can book from your phone (see below) |
| 저장된 개인정보 삭제 | Wipe all stored secrets from the keyring |

### Telegram notifications

Telegram is choochoose's default notification channel — set it up once and
you'll be kept in the loop without watching the terminal. It only ever talks to
the bot *you* create, so it stays private.

1. Talk to [@BotFather](https://t.me/BotFather) → `/newbot` → copy the **token**.
2. Send your new bot any message, then visit
   `https://api.telegram.org/bot<TOKEN>/getUpdates` to find your **chat id**.
3. Run `choochoose` → **텔레그램 설정**, paste both. A test message confirms it.

You'll then get a Telegram ping on a successful reservation, on payment, and on
recoverable errors during the retry loop.

### Remote booking from your phone (Telegram bot)

When you can't reach the machine running choochoose directly (it's behind
NAT / a firewall / has no public IP), drive a booking entirely from the
**Telegram app on your phone**. The bot uses long-polling — it makes only
*outbound* calls to `api.telegram.org` — so no inbound port, public IP, or SSH
is needed.

> **Start it on the host while you still have access, and keep it running.**
> Thereafter you control it remotely from Telegram. Requires **로그인 설정** and
> **텔레그램 설정** to be done first.

```bash
choochoose --bot          # or menu → "텔레그램 봇 시작 (원격 예매)"
```

Then, in your Telegram chat with the bot:

| Command | What it does |
|---|---|
| `/book` | Walk through 역→날짜→시각→인원→열차선택→좌석→결제 with inline buttons, then start the auto-retry loop |
| `/status` | Show the current waiting status (try count + elapsed) |
| `/stop` | Stop the running waiting loop |
| `/cancel` | Cancel the booking form you're filling in |

Only messages from your saved **chat id** are accepted — anyone else who finds
the bot is silently ignored and can never book on your account. (MVP supports
adult passengers only; child/senior/disability fares stay CLI-only for now.)

**Keep the bot alive** so it survives logout / reboot. With `tmux`:

```bash
tmux new -s choochoose 'conda activate choochoose && choochoose --bot'
# detach: Ctrl-b d   ·   reattach: tmux attach -t choochoose
```

Or as a `systemd` user service (`~/.config/systemd/user/choochoose-bot.service`):

```ini
[Unit]
Description=choochoose Telegram booking bot
After=network-online.target

[Service]
ExecStart=%h/miniconda3/envs/choochoose/bin/choochoose --bot
Restart=on-failure

[Install]
WantedBy=default.target
```

```bash
systemctl --user enable --now choochoose-bot
loginctl enable-linger "$USER"   # keep it running after you log out
```

---

## How the reservation loop works

For each train you select, choochoose polls availability at a randomized
interval (gamma-distributed, ~1.25 s ± 0.25 s) to avoid hammering the server in
lockstep. When a seat — or a wait-list slot, if you allow it — becomes
available, it reserves immediately, optionally pays, and notifies you. Session
expiry and transient network errors are handled by re-logging-in automatically
(from the top of the next iteration, never from inside an exception handler).
Repeated login *rejections* — i.e. bad credentials — stop the loop after
`MAX_LOGIN_RETRIES` instead of hammering the login endpoint forever.

## Project layout

```
choochoose/
├── choochoose/
│   ├── cli.py    # interactive menu, reservation loop, keyring + telegram
│   ├── bot.py    # Telegram remote-booking bot (long-polling, reuses cli loop)
│   └── ktx.py    # Korail mobile API client — KTX + former SRT (BSD, korail2)
├── pyproject.toml
├── .gitignore
├── LICENSE
└── README.md
```

## SRT/Korail merger note

SRT and Korail are integrated, so choochoose books everything through
`ktx.py` (Korail) and the operator-selection step was removed from both the menu
and the Telegram bot. Measured against the live API on 2026-09-11:

* **Former-SRT trains are native Korail inventory.** 수서/동탄 departures come
  back from Korail's `ScheduleView` as `KTX-산천 3xx`, with normal 특실/일반실/
  예약대기 flags. Schedule search works even *unauthenticated*.
* **Reserving one through Korail works.** A 수서→부산 `KTX 303` reservation went
  through on the Korail endpoint (`52,400원`, 1석, 구입기한 returned) and
  cancelled cleanly.
* **`srtCheckYn` ("SRT 함께 보기") is now a no-op.** `N` and `Y` return byte-identical
  train lists on 수서→부산 and on mixed routes like 동대구→부산, because those
  trains are in Korail's own schedule either way. The flag is left at `N`.
* **The old SRT endpoint is dead, so `srt.py` was deleted.** `app.srail.or.kr`
  still accepts a login, but `search_train` returned **0 trains** on every
  date/route tried (수서→부산 / 수서→동대구, 09/12 · 09/15 · 09/20) — it has no
  inventory left to sell. The SRT client, its 32-station list, and the
  `SRTError`/NetFunnel handling are gone; `git log` has them if ever needed.

**Station list.** Korail's schedule API takes station *names*, not codes, so
`STATIONS` in [`cli.py`](choochoose/cli.py) is one merged list of 64
high-speed stations — including the former SRT-only ones
(수서 · 동탄 · 평택지제 · 서대구). All 64 were verified against the live API
(a bad name returns `WRG200004 입력값오류(출발역,도착역...)`, which is distinguishable
from `WRD000061 No Results`); anything not on the list can still be entered via
**역 직접 수정**.

> If you already had stations saved under Korail, your old shortlist is kept
> as-is — re-open **역 설정** to pick from the expanded list. Shortlists saved
> by the pre-merger *SRT* path are **not** migrated (only 예매 옵션 are), so
> those users start from the default shortlist.

## KTX/Korail anti-bot note

Korail guards its mobile API with an anti-bot system ("Dynapath"). Plain
clients get rejected with `MACRO ERROR (앱을 최신 버전으로 업데이트...)`. choochoose
includes the `DynaPathMasterEngine` token generator (ported from
[`k-skill`](https://github.com/NomaDamas/k-skill), MIT) plus the matching app
version / User-Agent in [`ktx.py`](choochoose/ktx.py) to satisfy that check.

This is an **arms race**: if Korail changes its rules, KTX will start failing
again and the engine, `_version` (`250601002`), and `USER_AGENT` will need
updating. This is the only anti-bot system left in the codebase.

## Credits

`srtgo` by DKim, `korail2` by carpedm20, Dynapath engine from `k-skill` by
NomaDamas. The `SRT` client by ryanking13 shaped the pre-merger `srt.py`, now
removed. See [LICENSE](LICENSE).
