"""S3 transit layer for batch offloading."""

from __future__ import annotations

import io
import json
import pyarrow as pa
import pyarrow.parquet as pq
from typing import Any, Literal
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
        buffer = io.BytesIO()

        if format_type == "parquet":
            if records:
                table = pa.Table.from_pylist(records)
                pq.write_table(table, buffer, compression="snappy")
        else:
            buffer.write(json.dumps(records).encode("utf-8"))

        buffer.seek(0)

        await self._s3._client.put_object(
            Bucket=self._s3.bucket,
            Key=key,
            Body=buffer,
            ContentType=f"application/{format_type}",
        )

        buffer.close()
        return f"s3://{self._s3.bucket}/{key}"
