"""Клиент публичного API Яндекс.Диска: листинг папок и скачивание файлов.

Токен не нужен — используется публичная ссылка (public_key).

Важно: скачивание идёт по готовому прямому `file`-URL, который Яндекс.Диск
возвращает прямо в ответе листинга. Эндпоинт /download для публичных ссылок
disk.360.yandex.ru может отдавать другой href, отвечающий 403 Forbidden,
поэтому он оставлен только как фолбэк.
"""
import asyncio
import logging
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

RESOURCES_URL = "https://cloud-api.yandex.net/v1/disk/public/resources"
DOWNLOAD_URL = "https://cloud-api.yandex.net/v1/disk/public/resources/download"

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "*/*",
}


class YaDiskError(Exception):
    pass


class YaDiskClient:
    def __init__(self, public_url: str, timeout: float = 15.0, retries: int = 3):
        self.public_url = public_url
        self.timeout = timeout
        self.retries = retries
        self._client: Optional[httpx.AsyncClient] = None

    async def __aenter__(self) -> "YaDiskClient":
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(self.timeout),
            follow_redirects=True,
            headers=BROWSER_HEADERS,
        )
        return self

    async def __aexit__(self, *exc) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            raise RuntimeError("клиент не инициализирован (используйте `async with`)")
        return self._client

    async def _get(self, url: str, params: Optional[dict] = None) -> httpx.Response:
        last_exc: Optional[Exception] = None
        for attempt in range(1, self.retries + 1):
            try:
                resp = await self.client.get(url, params=params)
                resp.raise_for_status()
                return resp
            except httpx.HTTPError as exc:
                last_exc = exc
                logger.warning("запрос не удался (попытка %d/%d): %s", attempt, self.retries, exc)
                if attempt < self.retries:
                    await asyncio.sleep(min(2 ** (attempt - 1), 8))
        raise YaDiskError(f"не удалось выполнить запрос после {self.retries} попыток: {last_exc}")

    async def list_resources(self, path: Optional[str] = None) -> list[dict]:
        """Список элементов публичной папки (или её подпапки по path)."""
        params: dict = {"public_key": self.public_url, "limit": 100, "sort": "created"}
        if path:
            params["path"] = str(path).lstrip("/")
        resp = await self._get(RESOURCES_URL, params)
        return resp.json().get("_embedded", {}).get("items", [])

    async def download_item(self, item: dict) -> bytes:
        """Скачивает файл по готовому прямому `file`-URL из элемента листинга."""
        url = item.get("file")
        if not url:
            # фолбэк, если прямого URL нет
            return await self._download_via_api(item.get("path") or item.get("name") or "")
        return (await self._get(url)).content

    async def _download_via_api(self, path: str) -> bytes:
        params = {"public_key": self.public_url, "path": "/" + str(path).lstrip("/")}
        resp = await self._get(DOWNLOAD_URL, params)
        href = resp.json().get("href")
        if not href:
            raise YaDiskError(f"нет ссылки на скачивание для {path}")
        return (await self._get(href)).content
