"""Клиент Google Gemini с памятью диалога и поддержкой картинок.

Использует официальный SDK google-genai (новый, не старый google-generativeai).
Ключ берётся из GEMINI_API_KEY (см. config.py).

Память: перед каждым запросом из БД достаются последние 10 сообщений
пользователя (таблица gemini_history) и подмешиваются в диалог.
Картинки: если передан image_bytes, он отправляется как Part.from_bytes
вместе с текстом (мультимодальный запрос).
"""
import logging
from typing import Optional

from config import settings

logger = logging.getLogger(__name__)

# Сколько последних сообщений истории подмешивать в запрос.
HISTORY_LIMIT = 10

# Системная инструкция: бот-помощник для студентов техникума.
SYSTEM_PROMPT = (
    "Ты — дружелюбный помощник в Telegram-боте расписания КГТТ (колледж). "
    "Отвечай кратко и по делу, на русском языке. Помогай с учёбой, "
    "объясняй темы, решай задачи. Если присылают фото — опиши или реши, "
    "что на нём."
)


class GeminiError(RuntimeError):
    """Ошибка при обращении к Gemini (нет ключа, сбой сети, блокировка)."""


class AsyncGeminiClient:
    """Обёртка над google-genai: история + текст + картинки."""

    def __init__(self, api_key: Optional[str] = None, model: Optional[str] = None):
        self.api_key = api_key or settings.GEMINI_API_KEY
        self.model_name = model or settings.GEMINI_MODEL
        self._client = None

    # ---------------------------------------------------------------- client

    def _ensure_client(self):
        """Ленивая инициализация клиента (чтобы импорт не падал без ключа)."""
        if self._client is not None:
            return self._client
        if not self.api_key:
            raise GeminiError(
                "GEMINI_API_KEY не задан — добавь ключ в .env "
                "(получить: https://aistudio.google.com/apikey)"
            )
        try:
            from google import genai
        except ImportError as exc:  # pragma: no cover
            raise GeminiError(
                "Библиотека google-genai не установлена: pip install google-genai"
            ) from exc
        self._client = genai.Client(api_key=self.api_key)
        return self._client

    # -------------------------------------------------------------- generate

    async def generate(
        self,
        prompt: str,
        image_bytes: Optional[bytes] = None,
        *,
        history: Optional[list] = None,
        mime_type: str = "image/jpeg",
    ) -> str:
        """Отправляет в Gemini историю + новый текст (+ картинку) и возвращает ответ.

        history: список объектов GeminiHistory или кортежей (role, content).
        """
        client = self._ensure_client()

        try:
            from google.genai import types
        except ImportError as exc:  # pragma: no cover
            raise GeminiError("Не удалось импортировать google.genai.types") from exc

        # 1) системная инструкция + предыдущие сообщения
        contents: list = []
        for item in history or []:
            if isinstance(item, tuple):
                role, text = item[0], item[1]
            else:
                role, text = getattr(item, "role", "user"), getattr(item, "content", "")
            if not text:
                continue
            contents.append(
                types.Content(
                    role="user" if role == "user" else "model",
                    parts=[types.Part.from_text(text=text)],
                )
            )

        # 2) новое сообщение: текст и, если есть, картинка
        parts = []
        if image_bytes:
            parts.append(types.Part.from_bytes(data=image_bytes, mime_type=mime_type))
        if prompt:
            parts.append(types.Part.from_text(text=prompt))
        if not parts:
            raise GeminiError("Пустой запрос: нет ни текста, ни картинки")
        contents.append(types.Content(role="user", parts=parts))

        config = types.GenerateContentConfig(system_instruction=SYSTEM_PROMPT)

        try:
            response = await client.aio.models.generate_content(
                model=self.model_name,
                contents=contents,
                config=config,
            )
        except Exception as exc:
            logger.exception("ошибка Gemini")
            raise GeminiError(str(exc)) from exc

        text = getattr(response, "text", None)
        if not text:
            # Иногда ответ приходит частями — собираем вручную.
            try:
                text = "".join(
                    p.text for c in response.candidates for p in c.content.parts if p.text
                )
            except Exception:
                text = ""
        return (text or "").strip() or "🤔 Не удалось получить ответ, попробуй ещё раз."


# Готовый клиент для хендлеров.
gemini_client = AsyncGeminiClient()
