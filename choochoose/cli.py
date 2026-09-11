"""choochoose CLI — interactive Korail (KTX / 구 SRT) reservation helper.

Privacy notes:
  * Every secret (login id/password, card data, Telegram token/chat id, saved
    search preferences) lives ONLY in your operating-system keyring via the
    `keyring` library. Nothing is written to a file in this repo, so nothing
    sensitive can be committed to git.
  * All keyring entries are namespaced under "choochoose:" so they never clash
    with other tools and are easy to find / wipe (see `choochoose --wipe`).
  * The only network destinations are the official Korail endpoint (ktx.py)
    and — if you opt in — your own Telegram bot. There is no telemetry,
    analytics, or any third-party call.

SRT was merged into Korail, so there is exactly one backend: ktx.py. This
project's structure derives from srtgo (MIT) and its Korail client from korail2
(BSD, carpedm20). Personal, non-commercial use only.
"""

try:
    from curl_cffi.requests.exceptions import ConnectionError
except ImportError:
    from requests.exceptions import ConnectionError

from datetime import datetime, timedelta
from json.decoder import JSONDecodeError
from random import gammavariate
from termcolor import colored
from typing import Awaitable, Callable, List, Optional, Tuple

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
    NoResultsError,
    ReserveOption,
    TrainType,
    AdultPassenger,
    ChildPassenger,
    SeniorPassenger,
    Disability1To3Passenger,
    Disability4To6Passenger,
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
    "KTX": ["id", "pass", "ok", "station", "options",
            "departure", "arrival", "date", "time",
            "adult", "child", "senior", "disability1to3", "disability4to6"],
    "telegram": ["ok", "token", "chat_id"],
    "card": ["ok", "number", "password", "birthday", "expire"],
    # 통합 전 SRT 경로가 남긴 항목들. --wipe가 옛 사용자의 잔여 데이터까지
    # 지우도록 목록만 유지한다 (더 이상 쓰이지 않음).
    "SRT": ["id", "pass", "ok", "station", "options",
            "departure", "arrival", "date", "time",
            "adult", "child", "senior", "disability1to3", "disability4to6"],
}


# 역 목록. Korail 조회 API는 역 코드가 아니라 역명 문자열을 그대로 받으므로
# (ktx.py의 txtGoStart/txtGoEnd) 노선 단위로 넓게 둘 수 있다. 아래 64개는
# 라이브 API로 전수 확인했다 (잘못된 역명은 WRG200004로 거부된다).
# 여기 없는 역은 메뉴의 "역 직접 수정"으로 입력하면 된다.
STATIONS = [
    # 경부선 · 경부고속선
    "서울", "용산", "영등포", "광명", "수원", "천안아산", "오송", "대전",
    "김천구미", "동대구", "경산", "밀양", "구포", "부산", "경주",
    "울산(통도사)", "행신",
    # 수서평택고속선 (구 SRT 전용역 — 통합 후 Korail에서 KTX-산천으로 잡힌다)
    "수서", "동탄", "평택지제", "서대구",
    # 호남선 · 호남고속선
    "서대전", "계룡", "논산", "공주", "익산", "정읍", "광주송정", "나주",
    "목포",
    # 전라선
    "전주", "남원", "곡성", "구례구", "순천", "여천", "여수EXPO",
    # 경전선
    "진영", "창원", "창원중앙", "마산", "진주",
    # 동해선
    "포항", "태화강", "부전",
    # 중앙선 (KTX-이음)
    "청량리", "상봉", "양평", "서원주", "원주", "제천", "단양", "풍기",
    "영주", "안동",
    # 강릉선 · 동해북부
    "만종", "횡성", "둔내", "평창", "진부(오대산)", "강릉", "정동진",
    "묵호", "동해",
]
DEFAULT_STATIONS = ["서울", "용산", "광명", "천안아산", "대전", "동대구",
                    "부산", "광주송정", "강릉"]

# 키링 서비스명. 기존 사용자의 저장된 로그인/역/옵션이 그대로 살아 있도록
# 통합 전과 같은 "KTX"를 계속 쓴다.
RAIL = "KTX"

# 예약 간격 (평균 간격 (초) = SHAPE * SCALE): gamma distribution (1.25 +/- 0.25 s)
RESERVE_INTERVAL_SHAPE = 4
RESERVE_INTERVAL_SCALE = 0.25
RESERVE_INTERVAL_MIN = 0.25

# 재로그인이 이만큼 연속으로 실패하면 루프를 접는다. 자격증명이 틀린 경우는
# 기다린다고 낫지 않는데, 봇의 handle_error는 항상 True라 그대로 두면
# 1.25초마다 코레일 로그인 엔드포인트를 영원히 두드린다.
MAX_LOGIN_RETRIES = 3

WAITING_BAR = ["|", "/", "-", "\\"]


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

    ACTIONS = {
        1: lambda: reserve(debug),
        2: lambda: check_reservation(debug),
        3: lambda: set_login(debug),
        4: set_telegram,
        5: set_card,
        6: set_station,
        7: edit_station,
        8: set_options,
        9: wipe_secrets,
        10: lambda: start_bot(debug),
    }

    while True:
        choice = inquirer.list_input(
            message="메뉴 선택 (↕:이동, Enter: 선택)", choices=MENU_CHOICES
        )

        if choice == -1:
            break

        action = ACTIONS.get(choice)
        if action:
            try:
                action()
            except KorailError as err:
                # 로그인 미설정이나 API 오류로 메뉴 동작 하나가 실패해도
                # CLI 전체가 traceback으로 죽지 않고 메뉴로 돌아간다.
                print(colored(err.msg, "green", "on_red") + "\n")


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


def set_station() -> bool:
    stations, default_station_key = get_station()

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

    kr_set(RAIL, "station", (selected_stations := ",".join(selected)))
    print(f"선택된 역: {selected_stations}")
    return True


def edit_station() -> bool:
    stations, default_station_key = get_station()
    station_info = inquirer.prompt(
        [
            inquirer.Text(
                "stations",
                message="역 수정 (예: 수서,대전,동대구)",
                default=kr_get(RAIL, "station") or "",
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
            selected = DEFAULT_STATIONS
            break

    kr_set(RAIL, "station", (selected_stations := ",".join(selected)))
    print(f"선택된 역: {selected_stations}")
    return True


def get_station() -> Tuple[List[str], List[str]]:
    station_key = kr_get(RAIL, "station")

    if not station_key:
        return STATIONS, DEFAULT_STATIONS

    valid_keys = [x for x in station_key.split(",")]
    return STATIONS, valid_keys


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
    kr_set(RAIL, "options", ",".join(options))


def get_options():
    # 통합 전에는 옵션이 "SRT" 서비스에 저장됐다. 기존 사용자의 저장값을 잃지
    # 않도록 한 번만 옮겨온다.
    options = kr_get(RAIL, "options")
    if options is None and (legacy := kr_get("SRT", "options")) is not None:
        kr_set(RAIL, "options", legacy)
        kr_del("SRT", "options")
        options = legacy
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


def set_login(debug=False):
    credentials = {
        "id": kr_get(RAIL, "id") or "",
        "pass": kr_get(RAIL, "pass") or "",
    }

    login_info = inquirer.prompt(
        [
            inquirer.Text(
                "id",
                message="코레일 계정 아이디 (멤버십 번호, 이메일, 전화번호)",
                default=credentials["id"],
            ),
            inquirer.Password(
                "pass",
                message="코레일 계정 패스워드",
                default=credentials["pass"],
            ),
        ]
    )
    if not login_info:
        return False

    try:
        rail = Korail(login_info["id"], login_info["pass"], verbose=debug)

        # Korail.login()은 자격증명이 틀려도 예외를 던지지 않고 False를
        # 반환한다. .logined 를 확인하지 않으면 오타 난 비밀번호가 그대로
        # "검증 완료(ok=1)"로 키링에 저장된다.
        if not rail.logined:
            print("로그인에 실패했습니다. 아이디/비밀번호를 확인하세요.")
            kr_del(RAIL, "ok")
            return False

        kr_set(RAIL, "id", login_info["id"])
        kr_set(RAIL, "pass", login_info["pass"])
        kr_set(RAIL, "ok", "1")
        return True
    except KorailError as err:
        print(err)
        kr_del(RAIL, "ok")
        return False


def login(debug=False):
    if kr_get(RAIL, "id") is None or kr_get(RAIL, "pass") is None:
        # 입력을 취소하거나 실패하면 자격증명이 없다. 그대로 진행하면
        # Korail(None, None)이 되어 이메일 정규식에서 TypeError가 난다.
        if not set_login(debug=debug):
            raise KorailError("로그인 정보가 설정되지 않았습니다")

    return Korail(kr_get(RAIL, "id"), kr_get(RAIL, "pass"), verbose=debug)


def reserve(debug=False):
    rail = login(debug=debug)

    # Get date, time, stations, and passenger info
    now = datetime.now() + timedelta(minutes=10)
    today = now.strftime("%Y%m%d")
    this_time = now.strftime("%H%M%S")

    defaults = {
        "departure": kr_get(RAIL, "departure") or "서울",
        "arrival": kr_get(RAIL, "arrival") or "동대구",
        "date": kr_get(RAIL, "date") or today,
        "time": kr_get(RAIL, "time") or "120000",
        "adult": int(kr_get(RAIL, "adult") or 1),
        "child": int(kr_get(RAIL, "child") or 0),
        "senior": int(kr_get(RAIL, "senior") or 0),
        "disability1to3": int(kr_get(RAIL, "disability1to3") or 0),
        "disability4to6": int(kr_get(RAIL, "disability4to6") or 0),
    }

    # Set default stations if departure equals arrival
    if defaults["departure"] == defaults["arrival"]:
        defaults["arrival"] = "동대구" if defaults["departure"] == "서울" else None
        defaults["departure"] = (
            defaults["departure"] if defaults["arrival"] else "서울"
        )

    stations, station_key = get_station()
    options = get_options()

    # Calculate dynamic booking window (D-31, opens at 07:00)
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
        "adult": AdultPassenger,
        "child": ChildPassenger,
        "senior": SeniorPassenger,
        "disability1to3": Disability1To3Passenger,
        "disability4to6": Disability4To6Passenger,
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
        kr_set(RAIL, key, str(value))

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
        "include_no_seats": True,
        **({"train_type": TrainType.KTX} if "ktx" in options else {}),
    }

    try:
        trains = rail.search_train(**params)
    except NoResultsError:
        # 조회 결과 없음. 해당 구간에 직통 열차가 없거나(코레일 조회 API는
        # 환승 경로를 돌려주지 않는다) 그 날짜·시간대에 운행이 없다.
        print(
            colored("조회된 열차가 없습니다", "green", "on_red")
            + " — 직통 열차가 없는 구간이거나 해당 날짜·시간대에 운행이 없습니다.\n"
        )
        return

    def train_decorator(train):
        msg = train.__repr__()
        return (
            msg.replace("예약가능", colored("가능", "green"))
            .replace("가능", colored("가능", "green"))
            .replace("신청하기", colored("가능", "green"))
        )

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
    q_options = [
        inquirer.List(
            "type",
            message="선택 유형",
            choices=[
                ("일반실 우선", ReserveOption.GENERAL_FIRST),
                ("일반실만", ReserveOption.GENERAL_ONLY),
                ("특실 우선", ReserveOption.SPECIAL_FIRST),
                ("특실만", ReserveOption.SPECIAL_ONLY),
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

        # 예매는 이 시점에 이미 끝났다. 결제 단계에서 난 예외를 밖으로
        # 흘리면 재시도 루프가 같은 열차를 다시 예매하려 든다.
        try:
            if (
                pay
                and not getattr(reserve, "is_waiting", False)
                and pay_card(rail, reserve)
            ):
                notify("💳 ✨ 결제 성공!!! ✨ 💳")
        except Exception as ex:  # noqa: BLE001 — 예매는 지킨다
            notify(
                f"⚠️ 예매는 완료됐지만 결제에 실패했습니다: {getattr(ex, 'msg', ex)}\n"
                "구입기한 내에 코레일 앱에서 직접 결제하세요."
            )

    i_try = 0
    relogin = False
    login_fails = 0
    start_time = time.time()
    while True:
        if should_stop is not None and should_stop():
            return False
        try:
            i_try += 1
            if on_tick is not None:
                on_tick(i_try, time.time() - start_time)

            # 재로그인은 except 블록이 아니라 여기서 한다. except 안에서
            # login()을 부르면 재로그인이 던진 예외(예: ConnectionError)를
            # 아무도 잡지 못해 루프가 통째로 죽는다.
            if relogin:
                try:
                    rail = login(debug=debug)
                except KorailError as ex:
                    # 키링에 자격증명이 없다. 재시도해도 달라지지 않는다.
                    handle_error(ex)
                    return False
                if not rail.logined:
                    # 응답은 왔는데 로그인이 안 됐다 = 자격증명 문제.
                    # 기다린다고 나아지지 않으므로 몇 번만 시도하고 접는다.
                    login_fails += 1
                    if login_fails >= MAX_LOGIN_RETRIES:
                        handle_error(
                            KorailError(
                                "재로그인에 반복 실패했습니다. 로그인 정보를 확인하세요."
                            )
                        )
                        return False
                    _sleep()
                    continue
                login_fails = 0
                relogin = False

            trains = rail.search_train(**params)
            for i in indices:
                if _is_seat_available(trains[i], option):
                    _reserve(trains[i])
                    return True
            _sleep()

        except KorailError as ex:
            msg = ex.msg
            if "Need to Login" in msg:
                if debug:
                    print(
                        f"\nException: {ex}\nType: {type(ex)}\nArgs: {ex.args}\nMessage: {msg}"
                    )
                relogin = True
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
            relogin = True
            _sleep()

        except ConnectionError as ex:
            # 네트워크 단절은 일시적이다. 로그인 실패 카운터는 올리지 않고
            # 계속 재시도한다.
            if not handle_error(ex, "연결이 끊겼습니다"):
                return False
            relogin = True
            _sleep()

        except Exception as ex:
            if debug:
                print("\nUndefined exception")
            if not handle_error(ex):
                return False
            relogin = True
            _sleep()


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


def _is_seat_available(train, seat_type):
    if not train.has_seat():
        return train.has_waiting_list()
    if seat_type in [ReserveOption.GENERAL_FIRST, ReserveOption.SPECIAL_FIRST]:
        return train.has_seat()
    if seat_type == ReserveOption.GENERAL_ONLY:
        return train.has_general_seat()
    return train.has_special_seat()


def check_reservation(debug=False):
    rail = login(debug=debug)

    while True:
        reservations = rail.reservations()
        tickets = rail.tickets()

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
