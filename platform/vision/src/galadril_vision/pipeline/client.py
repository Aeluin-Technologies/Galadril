"""GraphQL client for Dagster orchestration execution."""

from __future__ import annotations

import aiohttp
import structlog

logger = structlog.get_logger(__name__)


class DagsterAsyncClient:
    """Dispatches pipeline execution mutations to the Dagster webserver daemon."""

    def __init__(
        self, endpoint_url: str = "http://localhost:3000/graphql"
    ) -> None:
        self._endpoint_url = endpoint_url

    async def trigger_job(self, job_name: str, batch_storage_path: str) -> bool:
        """Fires a non-blocking request to execute a specific Dagster job target.

        Args:
            job_name: Target Dagster job name mapping.
            batch_storage_path: S3 URI containing the target data batch payload.

        Returns:
            True if the execution mutation was accepted by the control plane.
        """
        query = """
        mutation LaunchPipelineExecution($jobName: String!, $runConfig: RunConfigData!) {
          launchPipelineExecution(executionParams: {
            selector: { pipelineName: $jobName }
            runConfigData: $runConfig
          }) {
            __typename
            ... on LaunchRunSuccess { run { runId } }
            ... on PipelineNotFoundError { message }
            ... on RunConfigValidationInvalid { errors { message } }
          }
        }
        """

        run_config = {
            "ops": {
                "vision_pipeline_batch": {
                    "config": {"batch_storage_path": batch_storage_path}
                }
            }
        }

        variables = {"jobName": job_name, "runConfig": run_config}

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    self._endpoint_url,
                    json={"query": query, "variables": variables},
                    timeout=aiohttp.ClientTimeout(total=5.0),
                ) as response:
                    if response.status != 200:
                        return False

                    res_json = await response.json()
                    data = res_json.get("data", {}).get(
                        "launchPipelineExecution", {}
                    )

                    if data.get("__typename") == "LaunchRunSuccess":
                        logger.info(
                            "dagster_run_launched_async",
                            run_id=data["run"]["runId"],
                        )
                        return True

                    logger.error("dagster_mutation_rejected", response=data)
                    return False
        except Exception as exc:
            logger.exception(
                "dagster_async_client_communication_failure", error=str(exc)
            )
            return False
