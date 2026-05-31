# choochoose 🚆

> *choo-choo*, meet **choose your train**.

A small, **privacy-respecting** command-line helper for reserving Korean
high-speed rail tickets (**SRT** and **KTX/Korail**). It searches trains, keeps
politely retrying until a seat (or wait-list slot) opens, optionally pays with a
saved card, and can ping you on **Telegram** when something happens.

It's a clean reimplementation derived from
[`srtgo`](https://github.com/lapis42/srtgo) (MIT), the
[`SRT`](https://github.com/ryanking13/SRT) client (MIT), and
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
| **Network egress** | The *only* outbound calls are to the railway endpoints (`app.srail.or.kr`, `smart.letskorail.com`, and their NetFunnel queue hosts) and, if configured, `api.telegram.org`. There is **no telemetry, analytics, crash reporting, or any third-party call**. |
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
| 로그인 설정 | Save your SRT or KTX login (verified on save, stored in keyring) |
| 텔레그램 설정 | Save a Telegram bot token + chat id for notifications |
| 카드 설정 | Save a card for optional auto-payment |
| 역 설정 / 역 직접 수정 | Pick the stations shown in the booking menu |
| 예매 옵션 설정 | Enable child/senior/disability fares, KTX-only, etc. |
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

---

## How the reservation loop works

For each train you select, choochoose polls availability at a randomized
interval (gamma-distributed, ~1.25 s ± 0.25 s) to avoid hammering the server in
lockstep. When a seat — or a wait-list slot, if you allow it — becomes
available, it reserves immediately, optionally pays, and notifies you. Session
expiry, the SRT NetFunnel queue, and transient network errors are handled by
re-logging-in or re-queuing automatically.

## Project layout

```
choochoose/
├── choochoose/
│   ├── cli.py    # interactive menu, reservation loop, keyring + telegram
│   ├── srt.py    # SRT mobile API client            (MIT, from SRT/srtgo)
│   └── ktx.py    # Korail/KTX mobile API client      (BSD, from korail2)
├── pyproject.toml
├── .gitignore
├── LICENSE
└── README.md
```

## KTX/Korail anti-bot note

Korail guards its mobile API with an anti-bot system ("Dynapath"). Plain
clients get rejected with `MACRO ERROR (앱을 최신 버전으로 업데이트...)`. choochoose
includes the `DynaPathMasterEngine` token generator (ported from
[`k-skill`](https://github.com/NomaDamas/k-skill), MIT) plus the matching app
version / User-Agent in [`ktx.py`](choochoose/ktx.py) to satisfy that check.

This is an **arms race**: if Korail changes its rules, KTX will start failing
again and the engine, `_version` (`250601002`), and `USER_AGENT` will need
updating. SRT uses a different system (NetFunnel) and is unaffected.

## Credits

`srtgo` by DKim, `SRT` by ryanking13, `korail2` by carpedm20, Dynapath engine
from `k-skill` by NomaDamas. See [LICENSE](LICENSE).
