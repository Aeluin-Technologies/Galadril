"""S3 transit layer for batch offloading."""

from __future__ import annotations

import asyncio
import io
import uuid
from typing import TYPE_CHECKING
import pyarrow as pa
import pyarrow.parquet as pq
import structlog

if TYPE_CHECKING:
    from galadril_vision.connectors.s3.client import S3Client
    from galadril_vision.common.schemas import CanonicalRecord

logger = structlog.get_logger(__name__)


class S3TransitService:
    """Encapsulates Arrow serialization and multi-part remote storage transit tracking."""

    def __init__(self, s3_client: S3Client) -> None:
        """Initializes the transit service with an underlying active S3 client."""
        self._s3 = s3_client

    async def upload(self, records: list[CanonicalRecord]) -> str:
        """Serializes canonical records to Parquet format and uploads them to the transit bucket.

        Args:
            records: A list of structured CanonicalRecord instances to offload.

        Returns:
            The fully qualified S3 storage URI string pointing to the materialized batch file.
        """
        if not records:
            raise ValueError("Cannot upload an empty list of records.")

        key = f"staging/micro_batches/{uuid.uuid4()}.parquet"
        await self._s3.connect()

        raw_dicts = [r.model_dump() for r in records]
        with io.BytesIO() as buffer:
            try:
                table = await asyncio.to_thread(pa.Table.from_pylist, raw_dicts)
                await asyncio.to_thread(
                    pq.write_table, table, buffer, compression="snappy"
                )
                buffer.seek(0)
            except Exception as ser_exc:
                logger.error(
                    "s3_transit_serialization_failed",
                    key=key,
                    error=str(ser_exc),
                )
                raise

            try:
                await self._s3._client.put_object(
                    Bucket=self._s3.bucket,
                    Key=key,
                    Body=buffer,
                    ContentType="application/parquet",
                )
            except Exception as s3_exc:
                logger.error(
                    "s3_transit_upload_failed",
                    bucket=self._s3.bucket,
                    key=key,
                    error=str(s3_exc),
                )
                raise

        return f"s3://{self._s3.bucket}/{key}"
