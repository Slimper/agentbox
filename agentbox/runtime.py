from dataclasses import dataclass
from typing import Any

import httpx

from agentbox.config import Settings, get_settings
from agentbox.db.session import Database
from agentbox.domains.dns import DnsPythonResolver
from agentbox.storage.s3 import ObjectStorage


@dataclass
class Runtime:
    settings: Settings
    db: Database
    storage: ObjectStorage
    http: httpx.AsyncClient
    dns: Any = None

    @classmethod
    def create(cls, settings: Settings | None = None) -> "Runtime":
        settings = settings or get_settings()
        return cls(
            settings=settings,
            db=Database(settings.database_url),
            storage=ObjectStorage(settings),
            http=httpx.AsyncClient(timeout=10.0, follow_redirects=False),
            dns=DnsPythonResolver(settings),
        )

    async def close(self) -> None:
        await self.http.aclose()
        await self.db.dispose()
