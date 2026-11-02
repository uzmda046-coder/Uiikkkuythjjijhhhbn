import json
import time
import requests
from datetime import datetime
from collections import defaultdict
from pathlib import Path

CONFIG_PATH = Path("config.json")

# Лимит Discord — 2000 символов. Берём с запасом.
MAX_MESSAGE_LEN = 1900
# Пауза между сообщениями (Discord: ~5 сообщений / 5 секунд на канал).
MESSAGES_DELAY_SEC = 1.2

ASSET_TYPES = [
    "Hat", "Shirt", "Pants", "TShirt", "Face", "Gear", "Package",
    "HairAccessory", "FaceAccessory", "NeckAccessory", "ShoulderAccessory",
    "FrontAccessory", "BackAccessory", "WaistAccessory", "EarAccessory", "EyeAccessory",
    "ClimbAnimation", "DeathAnimation", "FallAnimation", "IdleAnimation",
    "JumpAnimation", "RunAnimation", "SwimAnimation", "WalkAnimation", "EmoteAnimation",
]

ROBLOX_INVENTORY_URL = "https://inventory.roblox.com/v2/users/{user_id}/inventory"


def get_user_id(username: str) -> int | None:
    url = "https://users.roblox.com/v1/usernames/users"
    payload = {"usernames": [username], "excludeBannedUsers": True}
    resp = requests.post(url, json=payload, timeout=15)
    resp.raise_for_status()
    data = resp.json().get("data", [])
    return data[0]["id"] if data else None


def parse_add_time(value: str) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def fetch_all_items(user_id: int) -> list[dict]:
    items = []
    cursor = None
    while True:
        params = {"assetTypes": ",".join(ASSET_TYPES), "limit": 100}
        if cursor:
            params["cursor"] = cursor

        resp = requests.get(
            ROBLOX_INVENTORY_URL.format(user_id=user_id),
            params=params,
            timeout=30,
        )
        if resp.status_code == 403:
            raise RuntimeError(
                "Инвентарь закрыт. Откройте его в настройках приватности Roblox."
            )
        resp.raise_for_status()
        data = resp.json()

        page_items = data.get("data", [])
        items.extend(page_items)

        cursor = data.get("nextPageCursor")
        if not cursor or not page_items:
            break
    return items


def filter_by_period(
    items: list[dict], start_date: str | None, end_date: str | None
) -> list[dict]:
    start_dt = datetime.fromisoformat(start_date).date() if start_date else None
    end_dt = datetime.fromisoformat(end_date).date() if end_date else None

    filtered = []
    for item in items:
        add_time = parse_add_time(item.get("addTime"))
        if not add_time:
            continue
        item_date = add_time.date()
        if start_dt and item_date < start_dt:
            continue
        if end_dt and item_date > end_dt:
            continue
        filtered.append(item)
    return filtered


def build_category_block(category: str, cat_items: list[dict]) -> str:
    """Один блок статистики для категории (3 строки)."""
    cat_items.sort(key=lambda x: parse_add_time(x.get("addTime")) or datetime.max)
    first, last = cat_items[0], cat_items[-1]

    first_id = first.get("assetId") or first.get("id")
    last_id = last.get("assetId") or last.get("id")
    first_time = parse_add_time(first.get("addTime"))
    last_time = parse_add_time(last.get("addTime"))

    first_str = first_time.strftime("%Y-%m-%d") if first_time else "—"
    last_str = last_time.strftime("%Y-%m-%d") if last_time else "—"

    return (
        f"**{category}** — `{len(cat_items)}` шт.\n"
        f"  • Первый: assetId `{first_id}` ({first_str})\n"
        f"  • Последний: assetId `{last_id}` ({last_str})"
    )


def split_into_messages(header: str, blocks: list[str], limit: int = MAX_MESSAGE_LEN) -> list[str]:
    """Жадно пакует блоки в сообщения, не разрезая блоки."""
    messages: list[str] = []
    current = header
    for block in blocks:
        candidate = f"{current}\n\n{block}" if current else block
        if len(candidate) > limit and current:
            messages.append(current)
            current = block
        else:
            current = candidate
    if current.strip():
        messages.append(current)
    return messages


def build_report(items: list[dict], user_id: int) -> list[str]:
    """Возвращает список сообщений (готовых к отправке в Discord)."""
    by_category: dict[str, list[dict]] = defaultdict(list)
    for item in items:
        asset_type = item.get("assetType")
        if asset_type in ASSET_TYPES:
            by_category[asset_type].append(item)

    total_items = sum(len(v) for v in by_category.values())

    header = (
        f"📊 **Статистика инвентаря Roblox** (userId: `{user_id}`)\n"
        f"Всего предметов за период: **{total_items}**"
    )

    if not by_category:
        return [header + "\n\n_За указанный период предметов не найдено._"]

    blocks = [build_category_block(cat, by_category[cat]) for cat in sorted(by_category)]

    # Разбиваем по лимиту
    chunks = split_into_messages(header, blocks)

    # Если сообщений больше одного — добавим нумерацию вида (1/3)
    if len(chunks) > 1:
        total = len(chunks)
        chunks = [f"{chunk}\n\n_({i + 1}/{total})_" for i, chunk in enumerate(chunks)]

    return chunks


def send_messages_to_discord(token: str, channel_id: int, messages: list[str]) -> None:
    url = f"https://discord.com/api/v10/channels/{channel_id}/messages"
    headers = {
        "Authorization": f"Bot {token}",
        "Content-Type": "application/json",
    }
    for i, content in enumerate(messages):
        if len(content) > 2000:
            raise RuntimeError(
                f"Сообщение #{i + 1} получилось длиной {len(content)} символов — "
                "это превышает лимит Discord. Уменьшите MAX_MESSAGE_LEN."
            )
        resp = requests.post(
            url, headers=headers, json={"content": content}, timeout=30
        )
        if resp.status_code not in (200, 201):
            raise RuntimeError(
                f"Discord API вернул {resp.status_code} на сообщении #{i + 1}: {resp.text}"
            )
        if i < len(messages) - 1:
            time.sleep(MESSAGES_DELAY_SEC)


def main() -> None:
    if not CONFIG_PATH.exists():
        raise SystemExit("Файл config.json не найден.")

    config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))

    user_id = config.get("user_id")
    if not user_id:
        username = config.get("username")
        if not username:
            raise SystemExit("Укажите user_id или username в config.json.")
        user_id = get_user_id(username)
        if not user_id:
            raise SystemExit(f"Пользователь '{username}' не найден.")

    print("Загрузка инвентаря...")
    all_items = fetch_all_items(user_id)
    print(f"Предметов до фильтрации: {len(all_items)}")

    filtered = filter_by_period(
        all_items, config.get("start_date"), config.get("end_date")
    )
    print(f"Предметов после фильтрации по датам: {len(filtered)}")

    messages = build_report(filtered, user_id)
    print(f"Сформировано сообщений: {len(messages)}")

    send_messages_to_discord(
        config["discord_bot_token"],
        config["discord_channel_id"],
        messages,
    )
    print("Отчёт отправлен в Discord.")


if __name__ == "__main__":
    main()