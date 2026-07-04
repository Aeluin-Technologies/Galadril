"""S3 transit layer for batch offloading."""

from __future__ import annotations

import asyncio
import io
import json
import uuid
from typing import TYPE_CHECKING, Any, Literal

import pyarrow as pa
import pyarrow.parquet as pq
import structlog

if TYPE_CHECKING:
    from galadril_vision.common.schemas import CanonicalRecord
    from galadril_vision.connectors.s3.client import S3Client

logger = structlog.get_logger(__name__)


class S3TransitService:
    """Encapsulates Arrow serialization and multi-part remote storage transit tracking."""

    def __init__(self, s3_client: S3Client) -> None:
        """Initializes the transit service with an underlying active S3 client."""
        self._s3 = s3_client

    async def upload(
        self,
        records: list[CanonicalRecord] | list[dict[str, Any]],
        key: str | None = None,
        format_type: Literal["json", "parquet"] = "parquet",
    ) -> str:
        """Serializes canonical records to Parquet format and uploads them to the transit bucket.

        Args:
            records: A list of structured CanonicalRecord instances to offload.
            key: Target S3 object key destination.
            format_type: Serialization format choice ('json' or 'parquet').

        Returns:
            The fully qualified S3 storage URI string pointing to the materialized batch file.
        """
        if not records:
            raise ValueError("Cannot upload an empty list of records.")

        if key is None:
            key = f"staging/micro_batches/{uuid.uuid4()}.parquet"

        await self._s3.connect()

        raw_dicts = [
            r if isinstance(r, dict) else r.model_dump() for r in records
        ]

        with io.BytesIO() as buffer:
            try:
                if format_type == "parquet":
                    table = await asyncio.to_thread(
                        pa.Table.from_pylist, raw_dicts
                    )
                    await asyncio.to_thread(
                        pq.write_table, table, buffer, compression="snappy"
                    )
                elif format_type == "json":
                    serialized_data = await asyncio.to_thread(
                        lambda: json.dumps(raw_dicts).encode("utf-8")
                    )
                    buffer.write(serialized_data)
                else:
                    raise ValueError(f"Unsupported format_type: {format_type}")

                buffer.seek(0)
            except Exception as ser_exc:
                logger.error(
                    "s3_transit_serialization_failed",
                    key=key,
                    format_type=format_type,
                    error=str(ser_exc),
                )
                raise

            try:
                await self._s3._client.put_object(
                    Bucket=self._s3.bucket,
                    Key=key,
                    Body=buffer,
                    ContentType=f"application/{format_type}",
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

    async def upload_batch(
        self,
        key: str,
        records: list[dict[str, Any]],
        format_type: Literal["json", "parquet"] = "parquet",
    ) -> str:
        """Serializes and uploads records to S3.

        Args:
            key: Target S3 object key destination.
            format_type: Serialization format choice ('json' or 'parquet').
            records: List of structured dictionary payloads.

        Returns:
            The fully qualified S3 URI pointer of the uploaded batch.
        """
        return await self.upload(
            records=records, key=key, format_type=format_type
        )
