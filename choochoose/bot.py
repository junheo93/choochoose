"""choochoose Telegram bot — drive a booking remotely from your phone.

Why a long-polling Telegram bot: it makes only *outbound* calls to
api.telegram.org, so it works even when the host running choochoose sits behind
NAT / a firewall and you cannot reach it directly. Start it once on the host
(see README / systemd note) and control it from the Telegram app afterwards.

Privacy / safety:
  * The bot reuses the token + chat id already stored in your OS keyring; no new
    secret is introduced and nothing is written to disk.
  * Only messages from the saved chat id (you) are handled — every handler is
    gated by an owner filter, so anyone else who finds the bot is silently
    ignored and can never book on your account.

This module reuses the reservation logic in cli.py (run_reserve_loop) so the
auto-retry / re-login / error handling behaves identically to the CLI. The bot
adds only the conversation UI (inline keyboards) and a background runner.

MVP scope: adult passengers only. Child/senior/disability fares stay CLI-only
for now.
"""

from datetime import datetime, timedelta

import asyncio

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    filters,
)

from .cli import (
    get_options,
    get_station,
    kr_get,
    login,
    run_reserve_loop,
)

from .ktx import (
    AdultPassenger,
    ReserveOption,
    TrainType,
)
from .srt import (
    Adult,
    SeatType,
)

# Conversation states
(RAIL, DEP, ARR, DATE, TIME, ADULT, TRAINS, SEAT, PAY) = range(9)

DATE_PAGE = 9  # date buttons per page (3 columns x 3 rows)

SEAT_CHOICES = [
    ("일반실 우선", "GENERAL_FIRST"),
    ("일반실만", "GENERAL_ONLY"),
    ("특실 우선", "SPECIAL_FIRST"),
    ("특실만", "SPECIAL_ONLY"),
]

HELP_TEXT = (
    "🚆 choochoose 원격 예매 봇\n\n"
    "/book — 예매 시작 (열차→역→날짜→시각→인원→열차선택→좌석→결제)\n"
    "/status — 진행 중인 예매 대기 상태\n"
    "/stop — 진행 중인 예매 대기 중지\n"
    "/cancel — 입력 중인 예매 취소\n\n"
    "MVP: 성인 인원만 지원합니다."
)


# ---------------------------------------------------------------------------
# Keyboard helpers
# ---------------------------------------------------------------------------
def _btn(text, data):
    return InlineKeyboardButton(text, callback_data=data)


def _now():
    return datetime.now() + timedelta(minutes=10)


def _date_choices(rail_type):
    now = _now()
    if rail_type == "SRT":
        max_days = 30 if now.hour >= 7 else 29
    else:
        max_days = 31 if now.hour >= 7 else 30
    return [
        (
            (now + timedelta(days=i)).strftime("%m/%d %a"),
            (now + timedelta(days=i)).strftime("%Y%m%d"),
        )
        for i in range(max_days + 1)
    ]


def _station_keyboard(rail_type, prefix):
    _, keys = get_station(rail_type)
    rows = [
        [_btn(name, f"{prefix}|{name}") for name in keys[i : i + 2]]
        for i in range(0, len(keys), 2)
    ]
    rows.append([_btn("취소", "cancel")])
    return InlineKeyboardMarkup(rows)


def _date_keyboard(rail_type, page):
    choices = _date_choices(rail_type)
    pages = max(1, (len(choices) + DATE_PAGE - 1) // DATE_PAGE)
    page = max(0, min(page, pages - 1))
    chunk = choices[page * DATE_PAGE : page * DATE_PAGE + DATE_PAGE]
    # Pad each row to exactly 3 cells with blank (noop) buttons so the column
    # widths stay identical across pages — Telegram spreads a row's buttons over
    # the full width, so a short last row would otherwise stretch wider.
    blank = _btn("⠀", "noop")
    rows = []
    for i in range(0, len(chunk), 3):
        row = [_btn(lbl, f"date|{val}") for lbl, val in chunk[i : i + 3]]
        row += [blank] * (3 - len(row))
        rows.append(row)
    # Keep the nav row a fixed 3 slots so the layout never shifts between pages:
    # an unavailable arrow becomes a hollow, do-nothing (noop) placeholder.
    left = _btn("◀", f"datepg|{page - 1}") if page > 0 else _btn("◁", "noop")
    right = (
        _btn("▶", f"datepg|{page + 1}") if page < pages - 1 else _btn("▷", "noop")
    )
    rows.append([left, _btn(f"{page + 1}/{pages}", "noop"), right])
    rows.append([_btn("취소", "cancel")])
    return InlineKeyboardMarkup(rows)


def _time_keyboard():
    rows = [
        [_btn(f"{h:02d}", f"time|{h:02d}") for h in range(r, r + 4)]
        for r in range(0, 24, 4)
    ]
    rows.append([_btn("취소", "cancel")])
    return InlineKeyboardMarkup(rows)


def _adult_keyboard():
    rows = [
        [_btn(str(n), f"adult|{n}") for n in range(r, r + 3)]
        for r in range(1, 10, 3)
    ]
    rows.append([_btn("취소", "cancel")])
    return InlineKeyboardMarkup(rows)


def _trains_keyboard(trains, selected):
    rows = []
    for i, train in enumerate(trains):
        mark = "✅" if i in selected else "⬜"
        rows.append([_btn(f"{mark} {str(train)}"[:60], f"train|{i}")])
    rows.append([_btn("▶ 시작", "trains|done"), _btn("취소", "cancel")])
    return InlineKeyboardMarkup(rows)


def _seat_keyboard():
    rows = [[_btn(label, f"seat|{name}")] for label, name in SEAT_CHOICES]
    rows.append([_btn("취소", "cancel")])
    return InlineKeyboardMarkup(rows)


def _pay_keyboard(card_ok):
    rows = []
    if card_ok:
        rows.append([_btn("예 (카드 자동결제)", "pay|yes")])
    rows.append([_btn("아니오 (결제 안 함)", "pay|no")])
    rows.append([_btn("취소", "cancel")])
    return InlineKeyboardMarkup(rows)


# ---------------------------------------------------------------------------
# Conversation handlers
# ---------------------------------------------------------------------------
async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(HELP_TEXT)


async def cmd_book(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.application.bot_data.get("session"):
        await update.message.reply_text(
            "이미 예매 대기가 진행 중입니다. /status 로 확인하거나 /stop 으로 중지하세요."
        )
        return ConversationHandler.END

    context.user_data.clear()
    await update.message.reply_text(
        "열차를 선택하세요.",
        reply_markup=InlineKeyboardMarkup(
            [[_btn("SRT", "rail|SRT"), _btn("KTX", "rail|KTX")], [_btn("취소", "cancel")]]
        ),
    )
    return RAIL


async def on_rail(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    rail_type = q.data.split("|", 1)[1]

    if not kr_get(rail_type, "id") or not kr_get(rail_type, "pass"):
        await q.edit_message_text(
            f"{rail_type} 로그인이 설정돼 있지 않습니다. 먼저 서버에서 '로그인 설정'을 완료하세요."
        )
        return ConversationHandler.END

    context.user_data["rail_type"] = rail_type
    await q.edit_message_text(
        f"[{rail_type}] 출발역을 선택하세요.",
        reply_markup=_station_keyboard(rail_type, "dep"),
    )
    return DEP


async def on_dep(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    context.user_data["dep"] = q.data.split("|", 1)[1]
    rail_type = context.user_data["rail_type"]
    await q.edit_message_text(
        f"출발: {context.user_data['dep']}\n도착역을 선택하세요.",
        reply_markup=_station_keyboard(rail_type, "arr"),
    )
    return ARR


async def on_arr(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    arr = q.data.split("|", 1)[1]
    if arr == context.user_data["dep"]:
        await q.answer("출발역과 도착역이 같습니다.", show_alert=True)
        return ARR
    context.user_data["arr"] = arr
    rail_type = context.user_data["rail_type"]
    await q.edit_message_text(
        f"{context.user_data['dep']} → {arr}\n날짜를 선택하세요.",
        reply_markup=_date_keyboard(rail_type, 0),
    )
    return DATE


async def on_date_page(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    page = int(q.data.split("|", 1)[1])
    rail_type = context.user_data["rail_type"]
    await q.edit_message_reply_markup(reply_markup=_date_keyboard(rail_type, page))
    return DATE


async def on_noop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    return DATE


async def on_date(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    context.user_data["date"] = q.data.split("|", 1)[1]
    await q.edit_message_text(
        f"날짜: {context.user_data['date']}\n출발 시각을 선택하세요.",
        reply_markup=_time_keyboard(),
    )
    return TIME


async def on_time(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    context.user_data["time"] = q.data.split("|", 1)[1] + "0000"
    await q.edit_message_text(
        f"시각: {context.user_data['time'][:2]}시\n성인 인원수를 선택하세요.",
        reply_markup=_adult_keyboard(),
    )
    return ADULT


async def on_adult(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    ud = context.user_data
    ud["adult"] = int(q.data.split("|", 1)[1])
    rail_type = ud["rail_type"]
    is_srt = rail_type == "SRT"
    options = get_options()
    debug = context.application.bot_data.get("debug", False)

    # Adjust time if the chosen date is today and the time is already past.
    now = _now()
    if ud["date"] == now.strftime("%Y%m%d") and int(ud["time"]) < int(
        now.strftime("%H%M%S")
    ):
        ud["time"] = now.strftime("%H%M%S")

    adult_cls = Adult if is_srt else AdultPassenger
    params = {
        "dep": ud["dep"],
        "arr": ud["arr"],
        "date": ud["date"],
        "time": ud["time"],
        "passengers": [adult_cls(ud["adult"])],
        **(
            {"available_only": False}
            if is_srt
            else {
                "include_no_seats": True,
                **({"train_type": TrainType.KTX} if "ktx" in options else {}),
            }
        ),
    }
    ud["params"] = params
    ud["passengers"] = [adult_cls(ud["adult"])]

    await q.edit_message_text("열차를 검색 중입니다…")
    try:
        rail = await asyncio.to_thread(login, rail_type, debug)
        trains = await asyncio.to_thread(rail.search_train, **params)
    except Exception as ex:  # noqa: BLE001 — surface any search failure to the user
        await q.edit_message_text(f"열차 검색 실패: {ex}")
        return ConversationHandler.END

    if not trains:
        await q.edit_message_text("예약 가능한 열차가 없습니다. /book 으로 다시 시도하세요.")
        return ConversationHandler.END

    ud["rail"] = rail
    ud["trains"] = trains
    ud["sel"] = set()
    await q.edit_message_text(
        f"{ud['dep']} → {ud['arr']} {ud['date']} {ud['time'][:2]}시\n"
        "예약할 열차를 선택(토글)한 뒤 ▶ 시작을 누르세요.",
        reply_markup=_trains_keyboard(trains, ud["sel"]),
    )
    return TRAINS


async def on_train_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    i = int(q.data.split("|", 1)[1])
    sel = context.user_data["sel"]
    sel.discard(i) if i in sel else sel.add(i)
    await q.edit_message_reply_markup(
        reply_markup=_trains_keyboard(context.user_data["trains"], sel)
    )
    return TRAINS


async def on_trains_done(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not context.user_data["sel"]:
        await q.answer("열차를 1개 이상 선택하세요.", show_alert=True)
        return TRAINS
    await q.answer()
    await q.edit_message_text(
        "좌석 유형을 선택하세요.", reply_markup=_seat_keyboard()
    )
    return SEAT


async def on_seat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    name = q.data.split("|", 1)[1]
    is_srt = context.user_data["rail_type"] == "SRT"
    seat_enum = SeatType if is_srt else ReserveOption
    context.user_data["option"] = getattr(seat_enum, name)

    card_ok = bool(kr_get("card", "ok"))
    await q.edit_message_text(
        "예매 성공 시 카드로 자동 결제할까요?"
        + ("" if card_ok else "\n(저장된 카드가 없어 '아니오'만 가능합니다.)"),
        reply_markup=_pay_keyboard(card_ok),
    )
    return PAY


async def on_pay(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    ud = context.user_data
    ud["pay"] = q.data.split("|", 1)[1] == "yes"

    cfg = {
        "rail": ud["rail"],
        "rail_type": ud["rail_type"],
        "params": ud["params"],
        "indices": sorted(ud["sel"]),
        "passengers": ud["passengers"],
        "option": ud["option"],
        "pay": ud["pay"],
    }
    await q.edit_message_text(
        f"🚆 예매 대기를 시작합니다 ({len(cfg['indices'])}개 열차).\n"
        "자리가 나면 알려드립니다. /status 확인, /stop 중지."
    )
    context.application.create_task(_run_session(context.application, cfg))
    return ConversationHandler.END


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text("입력을 취소했습니다.")
    else:
        await update.message.reply_text("입력을 취소했습니다.")
    return ConversationHandler.END


# ---------------------------------------------------------------------------
# Background booking session + control commands
# ---------------------------------------------------------------------------
async def _run_session(application, cfg):
    loop = asyncio.get_running_loop()
    bot = application.bot
    chat_id = int(kr_get("telegram", "chat_id"))
    debug = application.bot_data.get("debug", False)

    stop_event = asyncio.Event()
    status = {"tries": 0, "elapsed": 0}
    application.bot_data["session"] = {"stop": stop_event, "status": status}

    def notify(msg):
        asyncio.run_coroutine_threadsafe(
            bot.send_message(chat_id=chat_id, text=msg), loop
        )

    def handle_error(ex, msg=None):
        notify(msg or f"⚠️ 오류: {getattr(ex, 'msg', ex)}")
        return True  # keep retrying; the bot never blocks on a prompt

    def on_tick(i_try, elapsed):
        status["tries"] = i_try
        status["elapsed"] = elapsed

    def should_stop():
        return stop_event.is_set()

    def work():
        return run_reserve_loop(
            cfg["rail"],
            rail_type=cfg["rail_type"],
            params=cfg["params"],
            indices=cfg["indices"],
            passengers=cfg["passengers"],
            option=cfg["option"],
            pay=cfg["pay"],
            notify=notify,
            handle_error=handle_error,
            on_tick=on_tick,
            should_stop=should_stop,
            debug=debug,
        )

    try:
        success = await loop.run_in_executor(None, work)
    except Exception as ex:  # noqa: BLE001 — report and end cleanly
        await bot.send_message(chat_id=chat_id, text=f"❌ 예매 루프 오류 종료: {ex}")
        return
    finally:
        application.bot_data.pop("session", None)

    if success:
        await bot.send_message(chat_id=chat_id, text="✅ 예매 루프를 종료합니다 (성공).")
    elif stop_event.is_set():
        await bot.send_message(chat_id=chat_id, text="⏹️ 예매 대기를 중지했습니다.")
    else:
        await bot.send_message(chat_id=chat_id, text="❎ 예매 루프를 종료합니다.")


async def cmd_stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    session = context.application.bot_data.get("session")
    if not session:
        await update.message.reply_text("진행 중인 예매 대기가 없습니다.")
        return
    session["stop"].set()
    await update.message.reply_text("중지를 요청했습니다. 잠시 후 종료됩니다.")


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    session = context.application.bot_data.get("session")
    if not session:
        await update.message.reply_text("진행 중인 예매 대기가 없습니다.")
        return
    st = session["status"]
    elapsed = int(st["elapsed"])
    h, rem = divmod(elapsed, 3600)
    m, s = divmod(rem, 60)
    await update.message.reply_text(
        f"⏳ 예매 대기 중… 시도 {st['tries']}회 (경과 {h:02d}:{m:02d}:{s:02d})"
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def run_bot(debug=False):
    token = kr_get("telegram", "token")
    chat_id = kr_get("telegram", "chat_id")
    if not token or not chat_id:
        print(
            "텔레그램 token/chat_id가 설정돼 있지 않습니다. "
            "먼저 메뉴의 '텔레그램 설정'을 완료하세요."
        )
        return

    owner = filters.Chat(chat_id=int(chat_id))
    app = Application.builder().token(token).concurrent_updates(True).build()
    app.bot_data["debug"] = debug

    conv = ConversationHandler(
        entry_points=[CommandHandler("book", cmd_book, filters=owner)],
        states={
            RAIL: [CallbackQueryHandler(on_rail, pattern=r"^rail\|")],
            DEP: [CallbackQueryHandler(on_dep, pattern=r"^dep\|")],
            ARR: [CallbackQueryHandler(on_arr, pattern=r"^arr\|")],
            DATE: [
                CallbackQueryHandler(on_date, pattern=r"^date\|"),
                CallbackQueryHandler(on_date_page, pattern=r"^datepg\|"),
                CallbackQueryHandler(on_noop, pattern=r"^noop$"),
            ],
            TIME: [CallbackQueryHandler(on_time, pattern=r"^time\|")],
            ADULT: [CallbackQueryHandler(on_adult, pattern=r"^adult\|")],
            TRAINS: [
                CallbackQueryHandler(on_train_toggle, pattern=r"^train\|"),
                CallbackQueryHandler(on_trains_done, pattern=r"^trains\|done$"),
            ],
            SEAT: [CallbackQueryHandler(on_seat, pattern=r"^seat\|")],
            PAY: [CallbackQueryHandler(on_pay, pattern=r"^pay\|")],
        },
        fallbacks=[
            CommandHandler("cancel", cmd_cancel, filters=owner),
            CallbackQueryHandler(cmd_cancel, pattern=r"^cancel$"),
        ],
        per_chat=True,
    )
    app.add_handler(conv)
    app.add_handler(CommandHandler("start", cmd_help, filters=owner))
    app.add_handler(CommandHandler("help", cmd_help, filters=owner))
    app.add_handler(CommandHandler("stop", cmd_stop, filters=owner))
    app.add_handler(CommandHandler("status", cmd_status, filters=owner))

    print(
        f"텔레그램 봇이 시작됐습니다 (chat_id={chat_id}). "
        "폰 텔레그램에서 /book 으로 예매를 시작하세요. 종료는 Ctrl-C."
    )
    app.run_polling()
