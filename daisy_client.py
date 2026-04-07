import asyncio
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional

import aiohttp

from config import (
    DAISY_API_KEY,
    DAISY_BASE_URL,
    DAISY_INCLUDE_IS_FINAL,
    DAISY_MAX_RETRIES,
    DAISY_RETRY_BASE_DELAY_SECS,
    DAISY_TIMEOUT_SECS,
    DAISY_UPDATE_PATH,
)


@dataclass
class DaisyClientConfig:
    base_url: Optional[str] = DAISY_BASE_URL
    update_path: str = DAISY_UPDATE_PATH
    api_key: Optional[str] = DAISY_API_KEY
    timeout_secs: float = DAISY_TIMEOUT_SECS
    max_retries: int = DAISY_MAX_RETRIES
    retry_base_delay_secs: float = DAISY_RETRY_BASE_DELAY_SECS
    include_is_final: bool = DAISY_INCLUDE_IS_FINAL


class DaisyClient:
    def __init__(self, logger, config: Optional[DaisyClientConfig] = None):
        self._logger = logger
        self._cfg = config or DaisyClientConfig()
        self._session: Optional[aiohttp.ClientSession] = None

    def enabled(self) -> bool:
        return bool(self._cfg.base_url)

    async def start(self) -> None:
        if not self.enabled() or self._session:
            return
        timeout = aiohttp.ClientTimeout(total=self._cfg.timeout_secs)
        self._session = aiohttp.ClientSession(timeout=timeout)

    async def close(self) -> None:
        if self._session:
            await self._session.close()
            self._session = None

    async def post_update(self, payload: Dict[str, Any]) -> None:
        if not self.enabled():
            return
        await self.start()
        assert self._session is not None

        url = self._cfg.base_url.rstrip("/") + self._cfg.update_path
        headers = {"Content-Type": "application/json"}
        if self._cfg.api_key:
            headers["Authorization"] = f"Bearer {self._cfg.api_key}"

        last_exc: Optional[BaseException] = None
        for attempt in range(self._cfg.max_retries + 1):
            try:
                async with self._session.post(url, json=payload, headers=headers) as resp:
                    if 200 <= resp.status < 300:
                        return
                    body = await resp.text()
                    raise RuntimeError(f"DAISY POST failed: status={resp.status} body={body[:500]}")
            except Exception as exc:
                last_exc = exc
                if attempt >= self._cfg.max_retries:
                    break
                delay = self._cfg.retry_base_delay_secs * (2**attempt)
                await asyncio.sleep(delay)

        self._logger.warning(f"DAISY update dropped after retries: {last_exc}")

    async def send_transcript(
        self,
        *,
        conversation_id: str,
        speaker: str,
        text: str,
        timestamp: str,
        is_final: Optional[bool] = None,
    ) -> None:
        data: Dict[str, Any] = {"speaker": speaker, "text": text, "timestamp": timestamp}
        if self._cfg.include_is_final and is_final is not None:
            data["is_final"] = is_final
        payload = {"conversation_id": conversation_id, "type": "TRANSCRIPT", "data": data}
        await self.post_update(payload)

    async def send_suggestion(
        self,
        *,
        conversation_id: str,
        intent: str,
        suggested_reply: str,
    ) -> None:
        payload = {
            "conversation_id": conversation_id,
            "type": "SUGGESTION",
            "data": {"intent": intent, "suggested_reply": suggested_reply},
        }
        await self.post_update(payload)

