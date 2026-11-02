import json
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

CONFIG_PATH = Path("config.json")

MAX_MESSAGE_LEN = 1900
DISCORD_RETRY_LIMIT = 5
CATALOG_URL = "https://www.roblox.com/catalog/{asset_id}"
ASSET_DETAILS_URL = "https://economy.roblox.com/v2/assets/{asset_id}/details"

# Что сканируем. Face (18) исключён, анимации кроме Emote — тоже.
ASSET_TYPE_IDS = {
    # Accessories
    "Hat": 8,
    "EarAccessory": 57,
    "FaceAccessory": 42,
    "EyeAccessory": 58,
    "HairAccessory": 41,
    "NeckAccessory": 43,
    "ShoulderAccessory": 44,
    "FrontAccessory": 45,
    "BackAccessory": 46,
    "WaistAccessory": 47,
    "Gear": 19,
    # Backgrounds
    "AvatarBackground": 92,
    # Layered Clothes
    "TShirtAccessory": 64,
    "ShirtAccessory": 65,
    "PantsAccessory": 66,
    "JacketAccessory": 67,
    "SweaterAccessory": 68,
    "ShortsAccessory": 69,
    "LeftShoeAccessory": 70,
    "RightShoeAccessory": 71,
    "DressSkirtAccessory": 72,
    # Dynamic Heads
    "DynamicHead": 79,
    # Makeup
    "FaceMakeup": 88,
    "LipMakeup": 89,
    "EyeMakeup": 90,
    "EyebrowAccessory": 76,
    "EyelashAccessory": 77,
    # Emotes
    "EmoteAnimation": 61,
    # Avatar Bundles
    "Package": 32,
    # Classic Clothes
    "Shirt": 11,
    "Pants": 12,
    "TShirt": 2,
    # Badges
    "Badge": 21,
}

# Итоговые подкатегории в нужном порядке.
# Формат: (main_category, subgroup_name, [asset_types...])
SUBGROUP_DEFINITIONS = [
    ("Accessories", "Hat",    ["Hat", "EarAccessory"]),
    ("Accessories", "Face",   ["FaceAccessory", "EyeAccessory"]),
    ("Accessories", "Hair",   ["HairAccessory"]),
    ("Accessories", "Neck",   ["NeckAccessory"]),
    ("Accessories", "Shoulder", ["ShoulderAccessory"]),
    ("Accessories", "Front",  ["FrontAccessory"]),
    ("Accessories", "Back",   ["BackAccessory"]),
    ("Accessories", "Waist",  ["WaistAccessory"]),
    ("Accessories", "Gear",   ["Gear"]),
    ("Backgrounds", "Background", ["AvatarBackground"]),
    ("Layered Clothes", "Tops", [
        "TShirtAccessory", "ShirtAccessory", "JacketAccessory", "SweaterAccessory",
    ]),
    ("Layered Clothes", "Bottoms", [
        "PantsAccessory", "ShortsAccessory", "DressSkirtAccessory",
    ]),
    ("Layered Clothes", "Shoes", ["LeftShoeAccessory", "RightShoeAccessory"]),
    ("Dynamic Heads", "Dynamic Head", ["DynamicHead"]),
    ("Makeup", "Makeup", [
        "FaceMakeup", "LipMakeup", "EyeMakeup",
        "EyebrowAccessory", "EyelashAccessory",
    ]),
    ("Emotes", "Emote", ["EmoteAnimation"]),
    ("Avatar Bundles", "Bundle", ["Package"]),
    ("Classic Clothes", "Shirt", ["Shirt"]),
    ("Classic Clothes", "Pants", ["Pants"]),
    ("Classic Clothes", "T-Shirt", ["TShirt"]),
    ("Badges", "Badge", ["Badge"]),
]

# assetType -> (main_category, subgroup_name)
ASSET_TO_SUBGROUP: dict[str, tuple[str, str]] = {}
for _main, _sub, _types in SUBGROUP_DEFINITIONS:
    for _t in _types:
        ASSET_TO_SUBGROUP[_t] = (_main, _sub)

ROBLOX_INVENTORY_BASE = "https://inventory.roblox.com/v2/users/{user_id}/inventory/{asset_type_id}"
ROBLOX_USERNAME_URL = "https://users.roblox.com/v1/usernames/users"
DISCORD_MESSAGES_URL = "https://discord.com/api/v10/channels/{channel_id}/messages"

_NAME_CACHE: dict[int, str] = {}


# ---------------------------------------------------------------------------
# Сеть
# ---------------------------------------------------------------------------

def build_session() -> requests.Session:
    session = requests.Session()
    retry = Retry(
        total=3,
        backoff_factor=1.5,
        status_forcelist=(500, 502, 503, 504),
        allowed_methods=frozenset(["GET", "POST"]),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


# ---------------------------------------------------------------------------
# Конфиг
# ---------------------------------------------------------------------------

def load_config(path: Path) -> dict:
    if not path.exists():
        raise SystemExit(f"Файл {path} не найден.")

    try:
        config = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Ошибка в JSON-конфиге: {exc}") from exc

    for field in ("discord_bot_token", "discord_channel_id"):
        if not config.get(field):
            raise SystemExit(f"В конфиге не заполнено обязательное поле: {field}")

    if not isinstance(config["discord_channel_id"], int):
        raise SystemExit("discord_channel_id должен быть числом (без кавычек).")

    for date_field in ("start_date", "end_date"):
        value = config.get(date_field)
        if value is not None:
            try:
                datetime.fromisoformat(value)
            except (TypeError, ValueError) as exc:
                raise SystemExit(
                    f"Поле {date_field} должно быть в формате YYYY-MM-DD или null."
                ) from exc

    if not config.get("user_id") and not config.get("username"):
        raise SystemExit("Укажите user_id или username в config.json.")

    return config


def get_user_id(session: requests.Session, username: str) -> int:
    try:
        resp = session.post(
            ROBLOX_USERNAME_URL,
            json={"usernames": [username], "excludeBannedUsers": True},
            timeout=15,
        )
        resp.raise_for_status()
    except requests.RequestException as exc:
        raise SystemExit(f"Не удалось найти пользователя '{username}': {exc}") from exc

    data = resp.json().get("data", [])
    if not data:
        raise SystemExit(f"Пользователь '{username}' не найден.")
    return data[0]["id"]


# ---------------------------------------------------------------------------
# Дата и имя предмета
# ---------------------------------------------------------------------------

def get_item_date(item: dict) -> str | None:
    return item.get("created") or item.get("addTime")


def parse_add_time(value) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def sanitize_name(name: str) -> str:
    name = name.replace("\n", " ").replace("\r", " ").strip()
    for ch in ("\\", "[", "]"):
        name = name.replace(ch, "\\" + ch)
    return name or "Unknown"


def get_item_name(
    session: requests.Session, asset_id: int, item: dict | None = None
) -> str:
    if item and item.get("name"):
        return sanitize_name(str(item["name"]))

    if asset_id in _NAME_CACHE:
        return _NAME_CACHE[asset_id]

    name = f"Asset {asset_id}"
    try:
        resp = session.get(ASSET_DETAILS_URL.format(asset_id=asset_id), timeout=15)
        if resp.status_code == 200:
            payload = resp.json() or {}
            name = str(payload.get("Name") or name)
    except (requests.RequestException, ValueError):
        pass

    name = sanitize_name(name)
    _NAME_CACHE[asset_id] = name
    return name


def get_item_asset_id(item: dict) -> int | None:
    raw = item.get("assetId") or item.get("id")
    try:
        return int(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None


def format_item_line(item: dict, session: requests.Session, date_str: str) -> str:
    asset_id = get_item_asset_id(item)
    if not asset_id:
        return f"— ({date_str})"
    name = get_item_name(session, asset_id, item)
    url = CATALOG_URL.format(asset_id=asset_id)
    return f"{asset_id} - [{name}]({url}) ({date_str})"


# ---------------------------------------------------------------------------
# Загрузка инвентаря
# ---------------------------------------------------------------------------

def fetch_items_for_type(
    session: requests.Session, user_id: int, asset_type: str, asset_type_id: int
) -> list[dict]:
    items: list[dict] = []
    cursor = None
    url = ROBLOX_INVENTORY_BASE.format(user_id=user_id, asset_type_id=asset_type_id)

    while True:
        params = {"limit": 100}
        if cursor:
            params["cursor"] = cursor

        try:
            resp = session.get(url, params=params, timeout=30)
        except requests.RequestException as exc:
            print(f"    [network] {asset_type}: {exc} — skipping type.")
            break

        if resp.status_code == 403:
            raise SystemExit(
                "Inventory is private. Open it: Settings → Privacy → "
                "\"Who can see my inventory\" → Everyone."
            )
        if resp.status_code == 400:
            print(f"    [skip] {asset_type}: 400 (type not supported).")
            break
        if resp.status_code == 429:
            retry_after = float(resp.headers.get("Retry-After", 5))
            print(f"    [429] {asset_type}: waiting {retry_after:.0f}s...")
            time.sleep(retry_after)
            continue
        if resp.status_code >= 500:
            print(f"    [5xx] {asset_type}: {resp.status_code} — skipping type.")
            break

        try:
            resp.raise_for_status()
        except requests.HTTPError as exc:
            print(f"    [HTTP] {asset_type}: {exc} — skipping type.")
            break

        try:
            data = resp.json()
        except ValueError:
            print(f"    [JSON] {asset_type}: invalid response — skipping type.")
            break

        page_items = data.get("data", [])
        for item in page_items:
            item.setdefault("assetType", asset_type)
        items.extend(page_items)

        cursor = data.get("nextPageCursor")
        if not cursor or not page_items:
            break

    return items


def fetch_all_items(session: requests.Session, user_id: int) -> list[dict]:
    all_items: list[dict] = []
    total_types = len(ASSET_TYPE_IDS)

    for index, (asset_type, asset_type_id) in enumerate(ASSET_TYPE_IDS.items(), 1):
        print(f"[{index}/{total_types}] {asset_type}...")
        type_items = fetch_items_for_type(session, user_id, asset_type, asset_type_id)
        print(f"    found: {len(type_items)}")
        all_items.extend(type_items)
        time.sleep(0.3)

    return all_items


# ---------------------------------------------------------------------------
# Фильтрация
# ---------------------------------------------------------------------------

def filter_by_period(
    items: list[dict], start_date: str | None, end_date: str | None
) -> list[dict]:
    start = datetime.fromisoformat(start_date).date() if start_date else None
    end = datetime.fromisoformat(end_date).date() if end_date else None

    filtered: list[dict] = []
    for item in items:
        add_time = parse_add_time(get_item_date(item))
        if add_time is None:
            continue
        item_date = add_time.date()
        if start and item_date < start:
            continue
        if end and item_date > end:
            continue
        filtered.append(item)
    return filtered


# ---------------------------------------------------------------------------
# Отчёт
# ---------------------------------------------------------------------------

def build_subgroup_block(
    subgroup_name: str, cat_items: list[dict], session: requests.Session
) -> str:
    fallback = datetime.max.replace(tzinfo=timezone.utc)
    cat_items.sort(key=lambda x: parse_add_time(get_item_date(x)) or fallback)

    first, last = cat_items[0], cat_items[-1]
    first_time = parse_add_time(get_item_date(first))
    last_time = parse_add_time(get_item_date(last))

    first_str = first_time.strftime("%Y-%m-%d") if first_time else "—"
    last_str = last_time.strftime("%Y-%m-%d") if last_time else "—"

    first_line = format_item_line(first, session, first_str)
    last_line = format_item_line(last, session, last_str)

    count = len(cat_items)
    return (
        f"**{subgroup_name}** — `{count}` item{'s' if count != 1 else ''}\n"
        f"  • First: {first_line}\n"
        f"  • Last: {last_line}"
    )


def build_report_units(
    items: list[dict], session: requests.Session
) -> list[str]:
    """Возвращает список «атомарных» блоков. Заголовок категории склеен с первым блоком,
    чтобы при разбиении по длине заголовок не оторвался от содержимого."""
    by_subgroup: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for item in items:
        asset_type = item.get("assetType")
        mapping = ASSET_TO_SUBGROUP.get(asset_type)
        if not mapping:
            continue
        by_subgroup[mapping].append(item)

    units: list[str] = []
    current_main: str | None = None
    for main_cat, subgroup_name, _asset_types in SUBGROUP_DEFINITIONS:
        key = (main_cat, subgroup_name)
        if key not in by_subgroup:
            continue
        block = build_subgroup_block(subgroup_name, by_subgroup[key], session)

        if main_cat != current_main:
            units.append(f"### {main_cat}\n\n{block}")
            current_main = main_cat
        else:
            units.append(block)

    return units


def split_into_messages(
    header: str, units: list[str], limit: int = MAX_MESSAGE_LEN
) -> list[str]:
    messages: list[str] = []
    current = header
    for unit in units:
        candidate = f"{current}\n\n{unit}" if current else unit
        if len(candidate) > limit and current:
            messages.append(current)
            current = unit
        else:
            current = candidate
    if current.strip():
        messages.append(current)
    return messages


def build_report(
    items: list[dict], user_id: int, session: requests.Session
) -> list[str]:
    total_items = len(items)
    header = (
        f"📊 **Roblox Inventory Statistics** (userId: `{user_id}`)\n"
        f"Total items in period: **{total_items}**"
    )

    units = build_report_units(items, session)
    if not units:
        return [header + "\n\n_No items found in the specified period._"]

    chunks = split_into_messages(header, units)

    if len(chunks) > 1:
        total = len(chunks)
        chunks = [f"{chunk}\n\n_({i + 1}/{total})_" for i, chunk in enumerate(chunks)]

    return chunks


# ---------------------------------------------------------------------------
# Discord
# ---------------------------------------------------------------------------

def send_discord_messages(
    session: requests.Session, token: str, channel_id: int, messages: list[str]
) -> None:
    url = DISCORD_MESSAGES_URL.format(channel_id=channel_id)
    headers = {
        "Authorization": f"Bot {token}",
        "Content-Type": "application/json",
    }

    for index, content in enumerate(messages, 1):
        if len(content) > 2000:
            raise SystemExit(
                f"Message #{index} is {len(content)} chars — exceeds Discord limit."
            )

        attempt = 0
        while True:
            attempt += 1
            try:
                resp = session.post(
                    url, headers=headers, json={"content": content}, timeout=30
                )
            except requests.RequestException as exc:
                raise SystemExit(f"Network error while sending to Discord: {exc}") from exc

            if resp.status_code in (200, 201):
                break

            if resp.status_code == 429:
                if attempt > DISCORD_RETRY_LIMIT:
                    raise SystemExit("Discord: too many 429 retries.")
                try:
                    retry_after = float(resp.json().get("retry_after", 2))
                except (ValueError, TypeError):
                    retry_after = 2.0
                print(f"    [Discord 429] waiting {retry_after:.1f}s...")
                time.sleep(retry_after)
                continue

            raise SystemExit(
                f"Discord API returned {resp.status_code} on message #{index}: {resp.text}"
            )

        print(f"    sent message {index}/{len(messages)}")
        if index < len(messages):
            time.sleep(1.2)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    try:
        config = load_config(CONFIG_PATH)
    except SystemExit as exc:
        print(f"Config error: {exc}", file=sys.stderr)
        return

    session = build_session()

    user_id = config.get("user_id")
    if not user_id:
        username = config["username"]
        print(f"Resolving userId for '{username}'...")
        user_id = get_user_id(session, username)
        print(f"  userId: {user_id}")

    print("Fetching inventory...")
    all_items = fetch_all_items(session, user_id)
    print(f"Items before filtering: {len(all_items)}")

    filtered = filter_by_period(
        all_items, config.get("start_date"), config.get("end_date")
    )
    print(f"Items after date filter: {len(filtered)}")

    messages = build_report(filtered, user_id, session)
    print(f"Prepared messages: {len(messages)}")

    send_discord_messages(
        session,
        config["discord_bot_token"],
        config["discord_channel_id"],
        messages,
    )
    print("Done: report sent to Discord.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nInterrupted by user.", file=sys.stderr)
        sys.exit(1)
    except SystemExit as exc:
        if exc.code not in (0, None):
            print(f"Exit: {exc}", file=sys.stderr)
        sys.exit(exc.code or 1)
    except Exception as exc:
        print(f"Unexpected error: {exc}", file=sys.stderr)
        sys.exit(1)
