"""Валидация initData из Telegram Mini App.

Чистая функция без зависимостей от БД и веб-фреймворка — можно
импортировать и тестировать где угодно.

Алгоритм Telegram:
  secret_key = HMAC_SHA256(key="WebAppData", msg=bot_token)
  вычисленная подпись = HMAC_SHA256(key=secret_key,
                                    msg=данные_отсортированные)
  и сравнивается с переданным полем hash.
"""
import hashlib
import hmac
import time
import urllib.parse
from typing import Optional

# Срок жизни подписанного initData (сек). 24 часов хватает с запасом.
INIT_DATA_MAX_AGE = 24 * 3600


def validate_init_data(init_data: str, bot_token: str) -> Optional[dict]:
    """Проверяет подпись initData и возвращает данные (или None).

    Возвращает dict с полями из initData (user, auth_date, query_id...),
    где user — JSON-строка с id пользователя.
    """
    if not init_data or not bot_token:
        return None
    try:
        parsed = urllib.parse.parse_qs(init_data, keep_blank_values=True)
    except Exception:
        return None

    received_hash = (parsed.get("hash") or [""])[0]
    if not received_hash:
        return None

    # Поля кроме hash, отсортированные по ключу, склеенные как k=v\n...
    data = {k: v[0] for k, v in parsed.items() if k != "hash"}
    check_string = "\n".join(f"{k}={v}" for k, v in sorted(data.items()))

    secret_key = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    calc_hash = hmac.new(secret_key, check_string.encode(), hashlib.sha256).hexdigest()

    if not hmac.compare_digest(calc_hash, received_hash):
        return None

    # Устаревшая подпись (страница давно открыта).
    auth_date = data.get("auth_date")
    if auth_date:
        try:
            if abs(int(auth_date) - int(time.time())) > INIT_DATA_MAX_AGE:
                return None
        except (TypeError, ValueError):
            return None
    return data