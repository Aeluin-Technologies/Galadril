"""S3 storage framework connectivity resource for staging raw data packets."""

import asyncio
from typing import Optional
import dagster as dg
from pydantic import PrivateAttr, Field
from galadril_vision.connectors.s3.client import S3Client


class S3ClientResource(dg.ConfigurableResource):
    """Shared resource managing persistent connection allocations for S3 storage frameworks."""

    bucket: str = Field(description="S3 staging bucket name.")
    endpoint_url: str = Field(description="S3 endpoint URL.")
    aws_access_key: str = Field(description="AWS access key.")
    aws_secret_key: str = Field(description="AWS secret key.", exclude=True)
    aws_region: str = Field(description="AWS region.")

    _client: Optional[S3Client] = PrivateAttr(default=None)

    def setup_for_execution(self, context: dg.InitResourceContext) -> None:
        """Initializes the S3Client with bucket and credential configurations."""
        self._client = S3Client(
            bucket=self.bucket,
            endpoint_url=self.endpoint_url,
            aws_access_key=self.aws_access_key,
            aws_secret_key=self.aws_secret_key,
            aws_region=self.aws_region,
        )

    def teardown_after_execution(self, context: dg.InitResourceContext) -> None:
        """Closes the active S3 client network context."""
        if self._client:
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(self._client.close())
            except RuntimeError:
                asyncio.run(self._client.close())

    @property
    def client(self) -> S3Client:
        """Returns the active S3Client instance."""
        if self._client is None:
            raise RuntimeError("S3ClientResource client accessed before setup.")
        return self._client
