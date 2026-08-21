"""biorhythm-server のLAN内Bot Memory APIクライアント。"""

import json
import urllib.error
import urllib.request
from typing import Callable, Optional

from config import (
    BIORHYTHM_INTERNAL_SECRET,
    BIORHYTHM_MEMORY_API_TIMEOUT_SEC,
    BIORHYTHM_MEMORY_API_URL,
)


def _default_transport(method: str, url: str, headers: dict,
                       payload: dict, timeout: float) -> dict:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers=headers,
        method=method,
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        raw = response.read(1_000_000)
        return json.loads(raw.decode("utf-8"))


class BotMemoryClient:
    def __init__(self, base_url: str = BIORHYTHM_MEMORY_API_URL,
                 secret: str = BIORHYTHM_INTERNAL_SECRET,
                 timeout: float = BIORHYTHM_MEMORY_API_TIMEOUT_SEC,
                 transport: Optional[Callable] = None):
        self.base_url = (base_url or "").rstrip("/")
        self.secret = secret or ""
        self.timeout = timeout
        self._transport = transport or _default_transport

    @property
    def enabled(self) -> bool:
        return bool(self.base_url and self.secret)

    def _post(self, path: str, payload: dict) -> dict:
        if not self.enabled:
            raise RuntimeError("Bot Memory API is not configured")
        return self._transport(
            "POST",
            f"{self.base_url}{path}",
            {
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.secret}",
            },
            payload,
            self.timeout,
        )

    def search(self, query: str, exclude_document_ids=None, limit: int = 10) -> list:
        if not self.enabled or not query.strip():
            return []
        payload = {
            "query": query[:1000],
            "purpose": "live_filler",
            "limit": max(1, min(20, int(limit))),
        }
        if exclude_document_ids:
            payload["excludeDocumentIds"] = [
                value for value in exclude_document_ids
                if isinstance(value, int)
            ][:100]
        try:
            body = self._post("/memory/search", payload)
            memories = body.get("memories") if isinstance(body, dict) else None
            if not isinstance(memories, list):
                return []
            return [item for item in memories if (
                isinstance(item, dict)
                and isinstance(item.get("id"), int)
                and isinstance(item.get("content"), str)
                and item.get("content", "").strip()
            )]
        except Exception as error:
            print(f"[bot-memory] 検索APIを利用できません（従来話題へ戻します）: {error}")
            return []

    def record_usage(self, document_ids: list, output_ref: str = "") -> bool:
        ids = list(dict.fromkeys(
            value for value in document_ids if isinstance(value, int)
        ))[:20]
        if not ids or not self.enabled:
            return False
        try:
            self._post("/memory/usages", {
                "documentIds": ids,
                "purpose": "live_filler",
                "outputRef": output_ref,
            })
            return True
        except Exception as error:
            print(f"[bot-memory] usageを記録できません（配信は継続します）: {error}")
            return False
