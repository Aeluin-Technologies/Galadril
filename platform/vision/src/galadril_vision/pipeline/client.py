"""GraphQL client for Dagster orchestration execution."""

from __future__ import annotations

import asyncio
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
                        response_text = await response.text()
                        logger.error(
                            "dagster_http_error",
                            status=response.status,
                            response=response_text,
                        )
                        return False

                    try:
                        res_json = await response.json()
                    except (aiohttp.ContentTypeError, ValueError) as json_err:
                        logger.error(
                            "dagster_invalid_json_response", error=str(json_err)
                        )
                        return False

                    errors = res_json.get("errors")
                    if errors:
                        logger.error("dagster_graphql_errors", errors=errors)
                        return False

                    data = (res_json.get("data") or {}).get(
                        "launchPipelineExecution"
                    ) or {}

                    if data.get("__typename") == "LaunchRunSuccess":
                        run_data = data.get("run") or {}
                        logger.info(
                            "dagster_run_launched_async",
                            run_id=run_data.get("runId"),
                        )
                        return True

                    logger.error("dagster_mutation_rejected", response=data)
                    return False
        except asyncio.TimeoutError as timeout_exc:
            logger.error(
                "dagster_client_timeout",
                error=str(timeout_exc),
                endpoint=self._endpoint_url,
            )
            return False
        except aiohttp.ClientError as client_exc:
            logger.error(
                "dagster_client_connection_error",
                error=str(client_exc),
                endpoint=self._endpoint_url,
            )
            return False
        except Exception as exc:
            logger.exception(
                "dagster_async_client_communication_failure", error=str(exc)
            )
            return False
