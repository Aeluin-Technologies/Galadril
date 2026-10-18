import os
from typing import List, Tuple

import boto3
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
from openai import DefaultHttpxClient, OpenAI

from ..utils.config_utils import BaseConfig
from ..utils.llm_utils import TextChatMessage
from ..utils.logging_utils import get_logger
from ..utils.openai_utils import validate_openai_base_url
from .base import BaseLLM, LLMConfig, normalize_generation_token_params
from .openai_gpt import cache_response

logger = get_logger(__name__)


class BedrockMantleSigV4Auth:
    def __init__(self, profile_name: str, region_name: str) -> None:
        self.session = boto3.Session(profile_name=profile_name, region_name=region_name)
        self.region_name = region_name

    def sign_request(self, request) -> None:
        credentials = self.session.get_credentials()
        if credentials is None:
            raise ValueError("No AWS credentials were found for Bedrock Mantle SigV4 authentication.")
        aws_request = AWSRequest(method=request.method, url=str(request.url), data=request.content, headers=dict(request.headers))
        SigV4Auth(credentials.get_frozen_credentials(), "bedrock-mantle", self.region_name).add_auth(aws_request)
        request.headers.update(dict(aws_request.headers.items()))

    def auth_flow(self, request):
        self.sign_request(request)
        yield request


class BedrockMantleLLM(BaseLLM):
    """Amazon Bedrock Mantle implementation using the OpenAI Responses API."""

    prefix = "bedrock-mantle/"

    def __init__(self, global_config: BaseConfig) -> None:
        super().__init__(global_config)
        if not self.llm_name.startswith(self.prefix) or len(self.llm_name) == len(self.prefix):
            raise ValueError(f"Bedrock Mantle model names must use {self.prefix}<model-id>.")
        if not self.global_config.llm_base_url:
            raise ValueError("llm_base_url is required for Bedrock Mantle, for example https://bedrock-mantle.us-east-2.api.aws/openai/v1.")
        if self.global_config.response_format is not None:
            raise ValueError("Bedrock Mantle Responses does not accept Chat Completions response_format; set response_format=None.")
        self.llm_base_url = validate_openai_base_url(self.global_config.llm_base_url, "responses", "llm_base_url")
        self.cache_dir = os.path.join(global_config.save_dir, "llm_cache")
        os.makedirs(self.cache_dir, exist_ok=True)
        self.cache_file_name = os.path.join(self.cache_dir, f"{self.llm_name.replace('/', '_')}_cache.sqlite")
        self.max_retries = global_config.max_retry_attempts
        self._init_llm_config()
        if self.global_config.bedrock_mantle_auth == "api_key":
            api_key = os.getenv("AWS_BEARER_TOKEN_BEDROCK")
            if not api_key:
                raise ValueError("AWS_BEARER_TOKEN_BEDROCK is required when bedrock_mantle_auth is api_key.")
            self.openai_client = OpenAI(base_url=self.llm_base_url, api_key=api_key, max_retries=self.max_retries, timeout=5 * 60)
        elif self.global_config.bedrock_mantle_auth == "aws_credentials":
            if not self.global_config.bedrock_region:
                raise ValueError("bedrock_region is required when bedrock_mantle_auth is aws_credentials.")
            auth = BedrockMantleSigV4Auth(self.global_config.bedrock_aws_profile, self.global_config.bedrock_region)
            client = DefaultHttpxClient(timeout=5 * 60, event_hooks={"request": [auth.sign_request]})
            try:
                self.openai_client = OpenAI(
                    base_url=self.llm_base_url,
                    api_key="bedrock-sigv4",
                    http_client=client,
                    max_retries=self.max_retries,
                )
            except Exception:
                client.close()
                raise
        else:
            raise ValueError(f"Unsupported Bedrock Mantle authentication method: {self.global_config.bedrock_mantle_auth}")

    def _init_llm_config(self) -> None:
        self.llm_config = LLMConfig.from_dict({
            "llm_name": self.llm_name,
            "llm_base_url": self.llm_base_url,
            "generate_params": {
                "model": self.llm_name[len(self.prefix):],
                "max_output_tokens": self.global_config.max_new_tokens,
                "store": False,
            },
        })

    @cache_response
    def infer(self, messages: List[TextChatMessage], **kwargs) -> Tuple[str, dict, bool]:
        if "response_format" in kwargs:
            raise ValueError("Bedrock Mantle Responses does not accept Chat Completions response_format.")
        params = normalize_generation_token_params(self.llm_config.generate_params, kwargs, "max_output_tokens")
        params["input"] = messages
        logger.debug(f"Calling Amazon Bedrock Mantle Responses API with model {params['model']}")
        response = self.openai_client.responses.create(**params)
        if response.status == "failed":
            error = getattr(response, "error", None)
            error_message = getattr(error, "message", None) or "unknown error"
            raise RuntimeError(f"Bedrock Mantle Responses API failed: {error_message}")
        if response.status not in {"completed", "incomplete"}:
            raise RuntimeError(f"Bedrock Mantle returned unexpected response status: {response.status}")
        message = response.output_text
        if not isinstance(message, str) or not message:
            raise ValueError("Bedrock Mantle response did not contain non-empty output text.")
        usage = response.usage
        if usage is None:
            raise ValueError("Bedrock Mantle response omitted usage; HippoRAG cannot account for this request safely.")
        incomplete_details = getattr(response, "incomplete_details", None)
        incomplete_reason = getattr(incomplete_details, "reason", None)
        if response.status == "incomplete" and incomplete_reason != "max_output_tokens":
            raise RuntimeError(f"Bedrock Mantle response was incomplete: {incomplete_reason or 'unknown reason'}")
        finish_reason = "length" if incomplete_reason == "max_output_tokens" else response.status
        total_tokens = getattr(usage, "total_tokens", None)
        metadata = {
            "prompt_tokens": usage.input_tokens,
            "completion_tokens": usage.output_tokens,
            "total_tokens": total_tokens if total_tokens is not None else usage.input_tokens + usage.output_tokens,
            "finish_reason": finish_reason,
            "response_id": response.id,
            "status": response.status,
        }
        for key, value in (("model", getattr(response, "model", None)), ("request_id", getattr(response, "_request_id", None))):
            if value is not None:
                metadata[key] = value
        input_details = getattr(usage, "input_tokens_details", None)
        output_details = getattr(usage, "output_tokens_details", None)
        cached_tokens = getattr(input_details, "cached_tokens", None)
        reasoning_tokens = getattr(output_details, "reasoning_tokens", None)
        if cached_tokens is not None:
            metadata["cached_tokens"] = cached_tokens
        if reasoning_tokens is not None:
            metadata["reasoning_tokens"] = reasoning_tokens
        return message, metadata

    def close(self) -> None:
        self.openai_client.close()
