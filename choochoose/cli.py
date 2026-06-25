"""choochoose CLI — interactive KTX/SRT reservation helper.

Privacy notes:
  * Every secret (login id/password, card data, Telegram token/chat id, saved
    search preferences) lives ONLY in your operating-system keyring via the
    `keyring` library. Nothing is written to a file in this repo, so nothing
    sensitive can be committed to git.
  * All keyring entries are namespaced under "choochoose:" so they never clash
    with other tools and are easy to find / wipe (see `choochoose --wipe`).
  * The only network destinations are the official railway endpoints (in
    srt.py / ktx.py) and — if you opt in — your own Telegram bot. There is no
    telemetry, analytics, or any third-party call.

This project reuses the SRT client (MIT, ryanking13) and korail2 (BSD,
carpedm20). Personal, non-commercial use only.
"""

try:
    from curl_cffi.requests.exceptions import ConnectionError
except ImportError:
    from requests.exceptions import ConnectionError

from datetime import datetime, timedelta
from json.decoder import JSONDecodeError
from random import gammavariate
from termcolor import colored
from typing import Awaitable, Callable, List, Optional, Tuple, Union

import asyncio
import click
import inquirer
import keyring
import telegram
import time
import re

from .ktx import (
    Korail,
    KorailError,
    ReserveOption,
    TrainType,
    AdultPassenger,
    ChildPassenger,
    SeniorPassenger,
    Disability1To3Passenger,
    Disability4To6Passenger,
)

from .srt import (
    SRT,
    SRTError,
    SRTNetFunnelError,
    SeatType,
    Adult,
    Child,
    Senior,
    Disability1To3,
    Disability4To6,
)

# All keyring services are stored under this namespace so choochoose's secrets
# are isolated, easy to audit, and easy to wipe.
KEYRING_NAMESPACE = "choochoose"


def _svc(service: str) -> str:
    return f"{KEYRING_NAMESPACE}:{service}"


def kr_get(service: str, key: str) -> Optional[str]:
    return keyring.get_password(_svc(service), key)


def kr_set(service: str, key: str, value: str) -> None:
    keyring.set_password(_svc(service), key, value)


def kr_del(service: str, key: str) -> None:
    try:
        keyring.delete_password(_svc(service), key)
    except keyring.errors.PasswordDeleteError:
        pass


# Every (service, key) pair choochoose may persist. Used by --wipe so a user
# can remove every trace of their data from the OS keyring in one command.
KEYRING_KEYS = {
    "SRT": ["id", "pass", "ok", "station", "options",
            "departure", "arrival", "date", "time",
            "adult", "child", "senior", "disability1to3", "disability4to6"],
    "KTX": ["id", "pass", "ok", "station",
            "departure", "arrival", "date", "time",
            "adult", "child", "senior", "disability1to3", "disability4to6"],
    "telegram": ["ok", "token", "chat_id"],
    "card": ["ok", "number", "password", "birthday", "expire"],
}


STATIONS = {
    "SRT": [
        "수서", "동탄", "평택지제", "경주", "곡성", "공주", "광주송정", "구례구",
        "김천(구미)", "나주", "남원", "대전", "동대구", "마산", "목포", "밀양",
        "부산", "서대구", "순천", "여수EXPO", "여천", "오송", "울산(통도사)",
        "익산", "전주", "정읍", "진영", "진주", "창원", "창원중앙", "천안아산",
        "포항",
    ],
    "KTX": [
        "서울", "용산", "영등포", "광명", "수원", "천안아산", "오송", "대전",
        "서대전", "김천구미", "동대구", "경주", "포항", "밀양", "구포", "부산",
        "울산(통도사)", "마산", "창원중앙", "경산", "논산", "익산", "정읍",
        "광주송정", "목포", "전주", "순천", "여수EXPO", "청량리", "강릉",
        "행신", "정동진",
    ],
}
DEFAULT_STATIONS = {
    "SRT": ["수서", "대전", "동대구", "부산"],
    "KTX": ["서울", "대전", "동대구", "부산"],
}

# 예약 간격 (평균 간격 (초) = SHAPE * SCALE): gamma distribution (1.25 +/- 0.25 s)
RESERVE_INTERVAL_SHAPE = 4
RESERVE_INTERVAL_SCALE = 0.25
RESERVE_INTERVAL_MIN = 0.25

WAITING_BAR = ["|", "/", "-", "\\"]

RailType = Union[str, None]
ChoiceType = Union[int, None]


@click.command()
@click.option("--debug", is_flag=True, help="Verbose request/response logging (local terminal only)")
@click.option("--wipe", is_flag=True, help="Delete every choochoose secret from the OS keyring and exit")
@click.option("--bot", is_flag=True, help="Start the Telegram bot for remote booking (long-polling, outbound only)")
def choochoose(debug=False, wipe=False, bot=False):
    if wipe:
        wipe_secrets()
        return

    if bot:
        start_bot(debug)
        return

    MENU_CHOICES = [
        ("예매 시작", 1),
        ("예매 확인/결제/취소", 2),
        ("로그인 설정", 3),
        ("텔레그램 설정", 4),
        ("카드 설정", 5),
        ("역 설정", 6),
        ("역 직접 수정", 7),
        ("예매 옵션 설정", 8),
        ("텔레그램 봇 시작 (원격 예매)", 10),
        ("저장된 개인정보 삭제 (keyring)", 9),
        ("나가기", -1),
    ]

    RAIL_CHOICES = [
        (colored("SRT", "red"), "SRT"),
        (colored("KTX", "cyan"), "KTX"),
        ("취소", -1),
    ]

    ACTIONS = {
        1: lambda rt: reserve(rt, debug),
        2: lambda rt: check_reservation(rt, debug),
        3: lambda rt: set_login(rt, debug),
        4: lambda _: set_telegram(),
        5: lambda _: set_card(),
        6: lambda rt: set_station(rt),
        7: lambda rt: edit_station(rt),
        8: lambda _: set_options(),
        9: lambda _: wipe_secrets(),
        10: lambda _: start_bot(debug),
    }

    while True:
        choice = inquirer.list_input(
            message="메뉴 선택 (↕:이동, Enter: 선택)", choices=MENU_CHOICES
        )

        if choice == -1:
            break

        if choice in {1, 2, 3, 6, 7}:
            rail_type = inquirer.list_input(
                message="열차 선택 (↕:이동, Enter: 선택, Ctrl-C: 취소)",
                choices=RAIL_CHOICES,
            )
            if rail_type in {-1, None}:
                continue
        else:
            rail_type = None

        action = ACTIONS.get(choice)
        if action:
            action(rail_type)


def start_bot(debug=False) -> None:
    """Launch the Telegram remote-booking bot (imported lazily to avoid a
    circular import and to keep the bot dependency out of the normal CLI path)."""
    from .bot import run_bot

    run_bot(debug=debug)


def wipe_secrets() -> None:
    """Remove every choochoose-stored secret from the OS keyring."""
    if not inquirer.confirm(
        message=colored(
            "keyring에 저장된 모든 로그인/카드/텔레그램/예매 정보를 삭제할까요?",
            "green", "on_red",
        ),
        default=False,
    ):
        print("취소되었습니다.")
        return

    for service, keys in KEYRING_KEYS.items():
        for key in keys:
            kr_del(service, key)
    print("저장된 모든 개인정보를 keyring에서 삭제했습니다.")


def set_station(rail_type: RailType) -> bool:
    stations, default_station_key = get_station(rail_type)

    if not (
        station_info := inquirer.prompt(
            [
                inquirer.Checkbox(
                    "stations",
                    message="역 선택 (↕:이동, Space: 선택, Enter: 완료, Ctrl-A: 전체선택, Ctrl-R: 선택해제, Ctrl-C: 취소)",
                    choices=stations,
                    default=default_station_key,
                )
            ]
        )
    ):
        return False

    if not (selected := station_info["stations"]):
        print("선택된 역이 없습니다.")
        return False

    kr_set(rail_type, "station", (selected_stations := ",".join(selected)))
    print(f"선택된 역: {selected_stations}")
    return True


def edit_station(rail_type: RailType) -> bool:
    stations, default_station_key = get_station(rail_type)
    station_info = inquirer.prompt(
        [
            inquirer.Text(
                "stations",
                message="역 수정 (예: 수서,대전,동대구)",
                default=kr_get(rail_type, "station") or "",
            )
        ]
    )
    if not station_info:
        return False

    if not (selected := station_info["stations"]):
        print("선택된 역이 없습니다.")
        return False

    selected = [s.strip() for s in selected.split(",")]

    # Verify all stations contain Korean characters
    hangul = re.compile("[가-힣]+")
    for station in selected:
        if not hangul.search(station):
            print(f"'{station}'는 잘못된 입력입니다. 기본 역으로 설정합니다.")
            selected = DEFAULT_STATIONS[rail_type]
            break

    kr_set(rail_type, "station", (selected_stations := ",".join(selected)))
    print(f"선택된 역: {selected_stations}")
    return True


def get_station(rail_type: RailType) -> Tuple[List[str], List[str]]:
    stations = STATIONS[rail_type]
    station_key = kr_get(rail_type, "station")

    if not station_key:
        return stations, DEFAULT_STATIONS[rail_type]

    valid_keys = [x for x in station_key.split(",")]
    return stations, valid_keys


def set_options():
    default_options = get_options()
    choices = inquirer.prompt(
        [
            inquirer.Checkbox(
                "options",
                message="예매 옵션 선택 (Space: 선택, Enter: 완료, Ctrl-A: 전체선택, Ctrl-R: 선택해제, Ctrl-C: 취소)",
                choices=[
                    ("어린이", "child"),
                    ("경로우대", "senior"),
                    ("중증장애인", "disability1to3"),
                    ("경증장애인", "disability4to6"),
                    ("KTX만", "ktx"),
                ],
                default=default_options,
            )
        ]
    )

    if choices is None:
        return

    options = choices.get("options", [])
    kr_set("SRT", "options", ",".join(options))


def get_options():
    options = kr_get("SRT", "options") or ""
    return options.split(",") if options else []


def set_telegram() -> bool:
    token = kr_get("telegram", "token") or ""
    chat_id = kr_get("telegram", "chat_id") or ""

    telegram_info = inquirer.prompt(
        [
            inquirer.Text(
                "token",
                message="텔레그램 token (Enter: 완료, Ctrl-C: 취소)",
                default=token,
            ),
            inquirer.Text(
                "chat_id",
                message="텔레그램 chat_id (Enter: 완료, Ctrl-C: 취소)",
                default=chat_id,
            ),
        ]
    )
    if not telegram_info:
        return False

    token, chat_id = telegram_info["token"], telegram_info["chat_id"]

    try:
        kr_set("telegram", "ok", "1")
        kr_set("telegram", "token", token)
        kr_set("telegram", "chat_id", chat_id)
        tgprintf = get_telegram()
        asyncio.run(tgprintf("[choochoose] 텔레그램 설정 완료 🚆"))
        return True
    except Exception as err:
        print(err)
        kr_del("telegram", "ok")
        return False


def get_telegram() -> Optional[Callable[[str], Awaitable[None]]]:
    token = kr_get("telegram", "token")
    chat_id = kr_get("telegram", "chat_id")

    async def tgprintf(text):
        if token and chat_id:
            bot = telegram.Bot(token=token)
            async with bot:
                await bot.send_message(chat_id=chat_id, text=text)

    return tgprintf


def set_card() -> None:
    card_info = {
        "number": kr_get("card", "number") or "",
        "password": kr_get("card", "password") or "",
        "birthday": kr_get("card", "birthday") or "",
        "expire": kr_get("card", "expire") or "",
    }

    card_info = inquirer.prompt(
        [
            inquirer.Password(
                "number",
                message="신용카드 번호 (하이픈 제외(-), Enter: 완료, Ctrl-C: 취소)",
                default=card_info["number"],
            ),
            inquirer.Password(
                "password",
                message="카드 비밀번호 앞 2자리 (Enter: 완료, Ctrl-C: 취소)",
                default=card_info["password"],
            ),
            inquirer.Password(
                "birthday",
                message="생년월일 (YYMMDD) / 사업자등록번호 (Enter: 완료, Ctrl-C: 취소)",
                default=card_info["birthday"],
            ),
            inquirer.Password(
                "expire",
                message="카드 유효기간 (YYMM, Enter: 완료, Ctrl-C: 취소)",
                default=card_info["expire"],
            ),
        ]
    )
    if card_info:
        for key, value in card_info.items():
            kr_set("card", key, value)
        kr_set("card", "ok", "1")


def pay_card(rail, reservation) -> bool:
    if kr_get("card", "ok"):
        birthday = kr_get("card", "birthday")
        return rail.pay_with_card(
            reservation,
            kr_get("card", "number"),
            kr_get("card", "password"),
            birthday,
            kr_get("card", "expire"),
            0,
            "J" if len(birthday) == 6 else "S",
        )
    return False


def set_login(rail_type="SRT", debug=False):
    credentials = {
        "id": kr_get(rail_type, "id") or "",
        "pass": kr_get(rail_type, "pass") or "",
    }

    login_info = inquirer.prompt(
        [
            inquirer.Text(
                "id",
                message=f"{rail_type} 계정 아이디 (멤버십 번호, 이메일, 전화번호)",
                default=credentials["id"],
            ),
            inquirer.Password(
                "pass",
                message=f"{rail_type} 계정 패스워드",
                default=credentials["pass"],
            ),
        ]
    )
    if not login_info:
        return False

    try:
        SRT(
            login_info["id"], login_info["pass"], verbose=debug
        ) if rail_type == "SRT" else Korail(
            login_info["id"], login_info["pass"], verbose=debug
        )

        kr_set(rail_type, "id", login_info["id"])
        kr_set(rail_type, "pass", login_info["pass"])
        kr_set(rail_type, "ok", "1")
        return True
    except SRTError as err:
        print(err)
        kr_del(rail_type, "ok")
        return False


def login(rail_type="SRT", debug=False):
    if kr_get(rail_type, "id") is None or kr_get(rail_type, "pass") is None:
        set_login(rail_type)

    user_id = kr_get(rail_type, "id")
    password = kr_get(rail_type, "pass")

    rail = SRT if rail_type == "SRT" else Korail
    return rail(user_id, password, verbose=debug)


def reserve(rail_type="SRT", debug=False):
    rail = login(rail_type, debug=debug)
    is_srt = rail_type == "SRT"

    # Get date, time, stations, and passenger info
    now = datetime.now() + timedelta(minutes=10)
    today = now.strftime("%Y%m%d")
    this_time = now.strftime("%H%M%S")

    defaults = {
        "departure": kr_get(rail_type, "departure") or ("수서" if is_srt else "서울"),
        "arrival": kr_get(rail_type, "arrival") or "동대구",
        "date": kr_get(rail_type, "date") or today,
        "time": kr_get(rail_type, "time") or "120000",
        "adult": int(kr_get(rail_type, "adult") or 1),
        "child": int(kr_get(rail_type, "child") or 0),
        "senior": int(kr_get(rail_type, "senior") or 0),
        "disability1to3": int(kr_get(rail_type, "disability1to3") or 0),
        "disability4to6": int(kr_get(rail_type, "disability4to6") or 0),
    }

    # Set default stations if departure equals arrival
    if defaults["departure"] == defaults["arrival"]:
        defaults["arrival"] = (
            "동대구" if defaults["departure"] in ("수서", "서울") else None
        )
        defaults["departure"] = (
            defaults["departure"]
            if defaults["arrival"]
            else ("수서" if is_srt else "서울")
        )

    stations, station_key = get_station(rail_type)
    options = get_options()

    # Calculate dynamic booking window (SRT: D-30, KTX: D-31; both open at 07:00)
    if is_srt:
        max_days = 30 if now.hour >= 7 else 29
    else:
        max_days = 31 if now.hour >= 7 else 30

    # Generate date choices within the window
    date_choices = [
        (
            (now + timedelta(days=i)).strftime("%Y/%m/%d %a"),
            (now + timedelta(days=i)).strftime("%Y%m%d"),
        )
        for i in range(max_days + 1)
    ]
    time_choices = [(f"{h:02d}", f"{h:02d}0000") for h in range(24)]

    # Build inquirer questions
    q_info = [
        inquirer.List(
            "departure",
            message="출발역 선택 (↕:이동, Enter: 선택, Ctrl-C: 취소)",
            choices=station_key,
            default=defaults["departure"],
        ),
        inquirer.List(
            "arrival",
            message="도착역 선택 (↕:이동, Enter: 선택, Ctrl-C: 취소)",
            choices=station_key,
            default=defaults["arrival"],
        ),
        inquirer.List(
            "date",
            message="출발 날짜 선택 (↕:이동, Enter: 선택, Ctrl-C: 취소)",
            choices=date_choices,
            default=defaults["date"],
        ),
        inquirer.List(
            "time",
            message="출발 시각 선택 (↕:이동, Enter: 선택, Ctrl-C: 취소)",
            choices=time_choices,
            default=defaults["time"],
        ),
        inquirer.List(
            "adult",
            message="성인 승객수 (↕:이동, Enter: 선택, Ctrl-C: 취소)",
            choices=range(10),
            default=defaults["adult"],
        ),
    ]

    passenger_types = {
        "child": "어린이",
        "senior": "경로우대",
        "disability1to3": "1~3급 장애인",
        "disability4to6": "4~6급 장애인",
    }

    passenger_classes = {
        "adult": Adult if is_srt else AdultPassenger,
        "child": Child if is_srt else ChildPassenger,
        "senior": Senior if is_srt else SeniorPassenger,
        "disability1to3": Disability1To3 if is_srt else Disability1To3Passenger,
        "disability4to6": Disability4To6 if is_srt else Disability4To6Passenger,
    }

    PASSENGER_TYPE = {
        passenger_classes["adult"]: "어른/청소년",
        passenger_classes["child"]: "어린이",
        passenger_classes["senior"]: "경로우대",
        passenger_classes["disability1to3"]: "1~3급 장애인",
        passenger_classes["disability4to6"]: "4~6급 장애인",
    }

    # Add passenger type questions if enabled in options
    for key, label in passenger_types.items():
        if key in options:
            q_info.append(
                inquirer.List(
                    key,
                    message=f"{label} 승객수 (↕:이동, Enter: 선택, Ctrl-C: 취소)",
                    choices=range(10),
                    default=defaults[key],
                )
            )

    info = inquirer.prompt(q_info)

    # Validate input info
    if not info:
        print(colored("예매 정보 입력 중 취소되었습니다", "green", "on_red") + "\n")
        return

    if info["departure"] == info["arrival"]:
        print(colored("출발역과 도착역이 같습니다", "green", "on_red") + "\n")
        return

    # Save preferences
    for key, value in info.items():
        kr_set(rail_type, key, str(value))

    # Adjust time if needed
    if info["date"] == today and int(info["time"]) < int(this_time):
        info["time"] = this_time

    # Build passenger list
    passengers = []
    total_count = 0
    for key, cls in passenger_classes.items():
        if key in info and info[key] > 0:
            passengers.append(cls(info[key]))
            total_count += info[key]

    # Validate passenger count
    if not passengers:
        print(colored("승객수는 0이 될 수 없습니다", "green", "on_red") + "\n")
        return

    if total_count >= 10:
        print(colored("승객수는 10명을 초과할 수 없습니다", "green", "on_red") + "\n")
        return

    msg_passengers = [
        f"{PASSENGER_TYPE[type(passenger)]} {passenger.count}명"
        for passenger in passengers
    ]
    print(*msg_passengers)

    # Search for trains
    params = {
        "dep": info["departure"],
        "arr": info["arrival"],
        "date": info["date"],
        "time": info["time"],
        "passengers": [passenger_classes["adult"](total_count)],
        **(
            {"available_only": False}
            if is_srt
            else {
                "include_no_seats": True,
                **({"train_type": TrainType.KTX} if "ktx" in options else {}),
            }
        ),
    }

    trains = rail.search_train(**params)

    def train_decorator(train):
        msg = train.__repr__()
        return (
            msg.replace("예약가능", colored("가능", "green"))
            .replace("가능", colored("가능", "green"))
            .replace("신청하기", colored("가능", "green"))
        )

    if not trains:
        print(colored("예약 가능한 열차가 없습니다", "green", "on_red") + "\n")
        return

    # Get train selection
    q_choice = [
        inquirer.Checkbox(
            "trains",
            message="예약할 열차 선택 (↕:이동, Space: 선택, Enter: 완료, Ctrl-A: 전체선택, Ctrl-R: 선택해제, Ctrl-C: 취소)",
            choices=[(train_decorator(train), i) for i, train in enumerate(trains)],
            default=None,
        ),
    ]

    choice = inquirer.prompt(q_choice)
    if choice is None or not choice["trains"]:
        print(colored("선택한 열차가 없습니다!", "green", "on_red") + "\n")
        return

    n_trains = len(choice["trains"])

    # Get seat type preference
    seat_type = SeatType if is_srt else ReserveOption
    q_options = [
        inquirer.List(
            "type",
            message="선택 유형",
            choices=[
                ("일반실 우선", seat_type.GENERAL_FIRST),
                ("일반실만", seat_type.GENERAL_ONLY),
                ("특실 우선", seat_type.SPECIAL_FIRST),
                ("특실만", seat_type.SPECIAL_ONLY),
            ],
        ),
        inquirer.Confirm("pay", message="예매 시 카드 결제", default=False),
    ]

    options = inquirer.prompt(q_options)
    if options is None:
        print(colored("예매 정보 입력 중 취소되었습니다", "green", "on_red") + "\n")
        return

    # CLI front-end: print celebratory status to the terminal AND mirror it to
    # Telegram, render the spinning waiting bar, and use the interactive error
    # handler. The actual retry/reserve logic lives in run_reserve_loop so the
    # Telegram bot can reuse it with its own callbacks.
    tgprintf = get_telegram()

    def _notify(msg):
        print(colored(f"\n\n{msg}\n", "red", "on_green"))
        asyncio.run(tgprintf(msg))

    def _tick(i_try, elapsed):
        hours, remainder = divmod(int(elapsed), 3600)
        minutes, seconds = divmod(remainder, 60)
        print(
            f"\r예매 대기 중... {WAITING_BAR[i_try & 3]} {i_try:4d} ({hours:02d}:{minutes:02d}:{seconds:02d}) ",
            end="",
            flush=True,
        )

    run_reserve_loop(
        rail,
        rail_type=rail_type,
        params=params,
        indices=choice["trains"],
        passengers=passengers,
        option=options["type"],
        pay=options["pay"],
        notify=_notify,
        handle_error=_handle_error,
        on_tick=_tick,
        debug=debug,
    )


def run_reserve_loop(
    rail,
    *,
    rail_type,
    params,
    indices,
    passengers,
    option,
    pay,
    notify,
    handle_error,
    on_tick=None,
    should_stop=None,
    debug=False,
):
    """I/O-agnostic auto-retry reservation loop shared by the CLI and the bot.

    notify(msg): deliver a human-readable status/success line.
    handle_error(ex, msg=None) -> bool: report an error; return False to abort.
    on_tick(i_try, elapsed): optional per-iteration progress hook.
    should_stop() -> bool: optional cancel check, polled each iteration.
    Returns True once a reservation succeeds, False if aborted/given up.
    """

    def _reserve(train):
        reserve = rail.reserve(train, passengers=passengers, option=option)
        msg = f"{reserve}"
        if hasattr(reserve, "tickets") and reserve.tickets:
            msg += "\n" + "\n".join(map(str, reserve.tickets))

        notify(f"🎫 🎉 예매 성공!!! 🎉 🎫\n{msg}")

        if pay and not reserve.is_waiting and pay_card(rail, reserve):
            notify("💳 ✨ 결제 성공!!! ✨ 💳")

    i_try = 0
    start_time = time.time()
    while True:
        if should_stop is not None and should_stop():
            return False
        try:
            i_try += 1
            if on_tick is not None:
                on_tick(i_try, time.time() - start_time)

            trains = rail.search_train(**params)
            for i in indices:
                if _is_seat_available(trains[i], option, rail_type):
                    _reserve(trains[i])
                    return True
            _sleep()

        except SRTError as ex:
            msg = ex.msg
            if "정상적인 경로로 접근 부탁드립니다" in msg or isinstance(
                ex, SRTNetFunnelError
            ):
                if debug:
                    print(
                        f"\nException: {ex}\nType: {type(ex)}\nArgs: {ex.args}\nMessage: {msg}"
                    )
                rail.clear()
            elif "로그인 후 사용하십시오" in msg:
                if debug:
                    print(
                        f"\nException: {ex}\nType: {type(ex)}\nArgs: {ex.args}\nMessage: {msg}"
                    )
                rail = login(rail_type, debug=debug)
                if not rail.is_login and not handle_error(ex):
                    return False
            elif not any(
                err in msg
                for err in (
                    "잔여석없음",
                    "사용자가 많아 접속이 원활하지 않습니다",
                    "예약대기 접수가 마감되었습니다",
                    "예약대기자한도수초과",
                )
            ):
                if not handle_error(ex):
                    return False
            _sleep()

        except KorailError as ex:
            msg = ex.msg
            if "Need to Login" in msg:
                rail = login(rail_type, debug=debug)
                if not rail.is_login and not handle_error(ex):
                    return False
            elif not any(
                err in msg
                for err in ("Sold out", "잔여석없음", "예약대기자한도수초과")
            ):
                if not handle_error(ex):
                    return False
            _sleep()

        except JSONDecodeError as ex:
            if debug:
                print(
                    f"\nException: {ex}\nType: {type(ex)}\nArgs: {ex.args}\nMessage: {ex.msg}"
                )
            _sleep()
            rail = login(rail_type, debug=debug)

        except ConnectionError as ex:
            if not handle_error(ex, "연결이 끊겼습니다"):
                return False
            rail = login(rail_type, debug=debug)

        except Exception as ex:
            if debug:
                print("\nUndefined exception")
            if not handle_error(ex):
                return False
            rail = login(rail_type, debug=debug)


def _sleep():
    time.sleep(
        gammavariate(RESERVE_INTERVAL_SHAPE, RESERVE_INTERVAL_SCALE)
        + RESERVE_INTERVAL_MIN
    )


def _handle_error(ex, msg=None):
    msg = (
        msg
        or f"\nException: {ex}, Type: {type(ex)}, Message: {ex.msg if hasattr(ex, 'msg') else 'No message attribute'}"
    )
    print(msg)
    tgprintf = get_telegram()
    asyncio.run(tgprintf(msg))
    return inquirer.confirm(message="계속할까요", default=True)


def _is_seat_available(train, seat_type, rail_type):
    if rail_type == "SRT":
        if not train.seat_available():
            return train.reserve_standby_available()
        if seat_type in [SeatType.GENERAL_FIRST, SeatType.SPECIAL_FIRST]:
            return train.seat_available()
        if seat_type == SeatType.GENERAL_ONLY:
            return train.general_seat_available()
        return train.special_seat_available()
    else:
        if not train.has_seat():
            return train.has_waiting_list()
        if seat_type in [ReserveOption.GENERAL_FIRST, ReserveOption.SPECIAL_FIRST]:
            return train.has_seat()
        if seat_type == ReserveOption.GENERAL_ONLY:
            return train.has_general_seat()
        return train.has_special_seat()


def check_reservation(rail_type="SRT", debug=False):
    rail = login(rail_type, debug=debug)

    while True:
        reservations = (
            rail.get_reservations() if rail_type == "SRT" else rail.reservations()
        )
        tickets = [] if rail_type == "SRT" else rail.tickets()

        all_reservations = []
        for t in tickets:
            t.is_ticket = True
            all_reservations.append(t)
        for r in reservations:
            if hasattr(r, "paid") and r.paid:
                r.is_ticket = True
            else:
                r.is_ticket = False
            all_reservations.append(r)

        if not reservations and not tickets:
            print(colored("예약 내역이 없습니다", "green", "on_red") + "\n")
            return

        choices = [
            (str(reservation), i) for i, reservation in enumerate(all_reservations)
        ] + [("텔레그램으로 예매 정보 전송", -2), ("돌아가기", -1)]

        choice = inquirer.list_input(message="예약 취소 (Enter: 결정)", choices=choices)

        # No choice or go back
        if choice in (None, -1):
            return

        # Send reservation info to telegram
        if choice == -2:
            out = []
            if all_reservations:
                out.append("[ 예매 내역 ]")
                for reservation in all_reservations:
                    out.append(f"🚅{reservation}")
                    if rail_type == "SRT":
                        out.extend(map(str, reservation.tickets))

            if out:
                tgprintf = get_telegram()
                asyncio.run(tgprintf("\n".join(out)))
            return

        # If choice is an unpaid reservation, ask to pay or cancel
        if (
            not all_reservations[choice].is_ticket
            and not all_reservations[choice].is_waiting
        ):
            answer = inquirer.list_input(
                message=f"결재 대기 승차권: {all_reservations[choice]}",
                choices=[("결제하기", 1), ("취소하기", 2)],
            )

            if answer == 1:
                if pay_card(rail, all_reservations[choice]):
                    print(
                        colored("\n\n💳 ✨ 결제 성공!!! ✨ 💳\n\n", "green", "on_red"),
                        end="",
                    )
            elif answer == 2:
                rail.cancel(all_reservations[choice])
            return

        # Else
        if inquirer.confirm(
            message=colored("정말 취소하시겠습니까", "green", "on_red")
        ):
            try:
                if all_reservations[choice].is_ticket:
                    rail.refund(all_reservations[choice])
                else:
                    rail.cancel(all_reservations[choice])
            except Exception as err:
                raise err
            return


if __name__ == "__main__":
    choochoose()
