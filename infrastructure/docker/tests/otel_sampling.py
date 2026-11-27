"""Exercises the deployed tail sampler with real OTLP and Tempo."""

from __future__ import annotations

import base64
import time
from pathlib import Path
from typing import cast

import httpx
from testcontainers.core.container import DockerContainer
from testcontainers.core.network import Network

from infrastructure.docker.tests.test_models import model

ROOT = Path(__file__).absolute().parents[2]


def _send_span(
    client: httpx.Client,
    trace_id: str,
    span_id: str,
    *,
    code: str | None = None,
    parent: str = "",
    slow: bool = False,
) -> None:
    end = time.time_ns()
    attributes = (
        [{"key": "galadril.error.code", "value": {"stringValue": code}}]
        if code is not None
        else []
    )
    attributes.extend(
        {
            "key": key,
            "value": {"stringValue": "must-not-reach-tempo"},
        }
        for key in (
            "galadril.audit.principal_id",
            "db.query.text",
            "http.request.header.authorization",
        )
    )
    response = client.post(
        "/v1/traces",
        json={
            "resourceSpans": [
                {
                    "resource": {
                        "attributes": [
                            {
                                "key": "service.name",
                                "value": {"stringValue": "sampling-test"},
                            }
                        ]
                    },
                    "scopeSpans": [
                        {
                            "spans": [
                                {
                                    "traceId": trace_id,
                                    "spanId": span_id,
                                    "parentSpanId": parent,
                                    "name": "sampling.test",
                                    "startTimeUnixNano": str(
                                        end
                                        - (6_000_000_000 if slow else 1_000_000)
                                    ),
                                    "endTimeUnixNano": str(end),
                                    "attributes": attributes,
                                }
                            ]
                        }
                    ],
                }
            ]
        },
    )
    response.raise_for_status()


def _endpoint(container: DockerContainer, port: int) -> str:
    return f"http://{container.get_container_host_ip()}:{container.get_exposed_port(port)}"


def _eventually(
    client: httpx.Client, path: str, expected_span: str = ""
) -> str:
    deadline = time.monotonic() + 40
    while time.monotonic() < deadline:
        try:
            response = client.get(path)
            if response.is_success and (
                not expected_span
                or base64.b64encode(bytes.fromhex(expected_span)).decode()
                in response.text
                or expected_span in response.text
            ):
                return response.text
        except httpx.TransportError:
            pass
        time.sleep(0.25)
    raise AssertionError(f"Timed out waiting for {path}")


def _wait_for_export_failure(health: httpx.Client) -> None:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        response = health.get("/metrics")
        response.raise_for_status()
        for sample in response.text.splitlines():
            if (
                sample.startswith("otelcol_exporter_send_failed_spans")
                and float(sample.rsplit(" ", 1)[-1]) > 0
            ):
                return
        time.sleep(0.25)
    raise AssertionError(
        "Collector did not account for the failed trace export"
    )


def check_tail_sampling() -> None:
    """Checks cross-request affinity, priority, ordinary sampling, and late spans."""
    services = model("infrastructure/docker/docker-compose.yaml")
    config = ROOT / "observability"
    with Network() as network:
        tempo = (
            DockerContainer(cast(str, services["tempo"]["image"]))
            .with_network(network)
            .with_network_aliases("tempo")
            .with_command(["-config.file=/etc/tempo.yaml"])
            .with_volume_mapping(
                str(config / "tempo.yaml"), "/etc/tempo.yaml", "ro"
            )
            .with_exposed_ports(3200)
        )
        collector = (
            DockerContainer(cast(str, services["otel-collector"]["image"]))
            .with_network(network)
            .with_command(["--config=/etc/otelcol-contrib/config.yaml"])
            .with_env("GALADRIL_TRACE_DECISION_WAIT", "2s")
            .with_env("GALADRIL_TRACE_SUCCESS_SAMPLE_PERCENT", "0")
            .with_env("GALADRIL_TRACE_EXPORT_RETRY_LIMIT", "6s")
            .with_volume_mapping(
                str(config / "otel-collector.yaml"),
                "/etc/otelcol-contrib/config.yaml",
                "ro",
            )
            .with_exposed_ports(4318, 8888)
        )
        with (
            tempo,
            collector,
            httpx.Client(base_url=_endpoint(tempo, 3200), timeout=3) as traces,
            httpx.Client(
                base_url=_endpoint(collector, 4318), timeout=3
            ) as ingest,
            httpx.Client(
                base_url=_endpoint(collector, 8888), timeout=3
            ) as health,
        ):
            _eventually(traces, "/ready")
            try:
                _eventually(health, "/metrics")
            except AssertionError as error:
                logs = (
                    collector.get_wrapped_container()
                    .logs()
                    .decode(errors="replace")
                )
                raise AssertionError(logs[-3000:]) from error
            root = "0102030405060708"
            child = "0203040506070809"
            late = "0304050607080910"
            failed = "01" * 16
            slow = "02" * 16
            security = "03" * 16
            success = "04" * 16
            denied = "05" * 16
            _send_span(ingest, failed, root)
            _send_span(
                ingest, failed, child, code="invalid_result", parent=root
            )
            _send_span(ingest, slow, root, slow=True)
            _send_span(ingest, security, root, code="ifc_violation")
            _send_span(ingest, success, root)
            _send_span(ingest, denied, root, code="access_denied")
            for trace_id in (failed, slow, security):
                _eventually(traces, f"/api/traces/{trace_id}")
            retained = _eventually(traces, f"/api/traces/{failed}", child)
            assert "must-not-reach-tempo" not in retained
            _eventually(traces, f"/api/traces/{failed}", root)
            _send_span(ingest, failed, late, parent=root)
            _eventually(traces, f"/api/traces/{failed}", late)
            for trace_id in (success, denied):
                assert traces.get(f"/api/traces/{trace_id}").status_code == 404
            tempo.get_wrapped_container().stop(timeout=1)
            _send_span(ingest, "06" * 16, root, code="internal")
            _wait_for_export_failure(health)
