from contextlib import asynccontextmanager

import aioboto3
from botocore.config import Config
from botocore.exceptions import ClientError

from agentbox.config import Settings


class ObjectStorage:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.bucket = settings.s3_bucket
        self._session = aioboto3.Session(
            aws_access_key_id=settings.s3_access_key,
            aws_secret_access_key=settings.s3_secret_key,
            region_name=settings.s3_region,
        )
        self._config = Config(signature_version="s3v4", s3={"addressing_style": "path"})

    @asynccontextmanager
    async def _client(self, public: bool = False):
        endpoint = self.settings.s3_endpoint
        if public and self.settings.s3_public_endpoint:
            endpoint = self.settings.s3_public_endpoint
        async with self._session.client("s3", endpoint_url=endpoint, config=self._config) as client:
            yield client

    async def ensure_bucket(self) -> None:
        async with self._client() as c:
            try:
                await c.head_bucket(Bucket=self.bucket)
            except ClientError:
                await c.create_bucket(Bucket=self.bucket)

    async def put_bytes(self, key: str, data: bytes, content_type: str = "application/octet-stream") -> None:
        async with self._client() as c:
            await c.put_object(Bucket=self.bucket, Key=key, Body=data, ContentType=content_type)

    async def get_bytes(self, key: str) -> bytes:
        async with self._client() as c:
            obj = await c.get_object(Bucket=self.bucket, Key=key)
            async with obj["Body"] as stream:
                return await stream.read()

    async def delete(self, key: str) -> None:
        async with self._client() as c:
            await c.delete_object(Bucket=self.bucket, Key=key)

    async def head(self, key: str) -> dict | None:
        async with self._client() as c:
            try:
                r = await c.head_object(Bucket=self.bucket, Key=key)
            except ClientError as e:
                if e.response.get("Error", {}).get("Code") in ("404", "NoSuchKey", "NotFound"):
                    return None
                raise
            return {"size": r["ContentLength"], "content_type": r.get("ContentType", "")}

    async def presign_put(self, key: str, content_type: str, expires_seconds: int = 900) -> str:
        async with self._client(public=True) as c:
            return await c.generate_presigned_url(
                "put_object", Params={"Bucket": self.bucket, "Key": key, "ContentType": content_type},
                ExpiresIn=expires_seconds,
            )

    async def presign_get(self, key: str, filename: str, expires_seconds: int = 600) -> str:
        safe = filename.replace('"', "").replace("\n", "")
        async with self._client(public=True) as c:
            return await c.generate_presigned_url(
                "get_object",
                Params={"Bucket": self.bucket, "Key": key,
                        "ResponseContentDisposition": f'attachment; filename="{safe}"'},
                ExpiresIn=expires_seconds,
            )
