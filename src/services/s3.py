"""
S3-compatible storage (Garage) client using aioboto3 for native async I/O.
"""

from typing import BinaryIO

import aioboto3
from botocore.exceptions import ClientError

from src.core.config import settings

_session = aioboto3.Session()


def _client():
    return _session.client(
        "s3",
        endpoint_url=settings.s3_endpoint_url,
        aws_access_key_id=settings.s3_access_key_id,
        aws_secret_access_key=settings.s3_secret_access_key,
        region_name=settings.s3_region,
    )


async def upload_fileobj(fileobj: BinaryIO, s3_key: str, content_type: str) -> None:
    async with _client() as s3:
        await s3.upload_fileobj(
            fileobj,
            settings.s3_bucket,
            s3_key,
            ExtraArgs={"ContentType": content_type},
        )


async def generate_presigned_download_url(s3_key: str, expires_in: int = 3600) -> str:
    async with _client() as s3:
        return await s3.generate_presigned_url(
            "get_object",
            Params={"Bucket": settings.s3_bucket, "Key": s3_key},
            ExpiresIn=expires_in,
        )


async def delete_object(s3_key: str) -> None:
    async with _client() as s3:
        await s3.delete_object(Bucket=settings.s3_bucket, Key=s3_key)


async def upload_bytes(data: bytes, s3_key: str, content_type: str) -> None:
    import io
    async with _client() as s3:
        await s3.upload_fileobj(
            io.BytesIO(data),
            settings.s3_bucket,
            s3_key,
            ExtraArgs={"ContentType": content_type},
        )


async def download_bytes(s3_key: str) -> bytes:
    async with _client() as s3:
        response = await s3.get_object(Bucket=settings.s3_bucket, Key=s3_key)
        return await response["Body"].read()


async def object_exists(s3_key: str) -> bool:
    async with _client() as s3:
        try:
            await s3.head_object(Bucket=settings.s3_bucket, Key=s3_key)
            return True
        except ClientError:
            return False
