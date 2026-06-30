"""S3 transit layer for batch offloading."""

from __future__ import annotations

import asyncio
import io
import json
from typing import Any, Literal
import pyarrow as pa
import pyarrow.parquet as pq
import structlog

from galadril_vision.connectors.s3.client import S3Client

logger = structlog.get_logger(__name__)


class S3TransitService:
    """Handles data streaming and staging into S3 transit zones."""

    def __init__(self, s3_client: S3Client) -> None:
        """Initializes the transit service with an underlying active S3 client."""
        self._s3 = s3_client

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
        await self._s3.connect()

        with io.BytesIO() as buffer:
            try:
                if format_type == "parquet":
                    if records:
                        table = await asyncio.to_thread(
                            pa.Table.from_pylist, records
                        )
                        await asyncio.to_thread(
                            pq.write_table, table, buffer, compression="snappy"
                        )
                else:
                    serialized_data = await asyncio.to_thread(
                        lambda: json.dumps(records).encode("utf-8")
                    )
                    buffer.write(serialized_data)

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
