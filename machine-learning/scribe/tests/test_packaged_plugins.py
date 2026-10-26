"""Exercise real packaged discovery and factories in a fresh runtime process."""

import os
import shutil
import subprocess
import sys
from pathlib import Path

import galadril_plugins
import pytest


def test_official_bundle_is_discovered_and_executed_across_runtimes(
    tmp_path: Path,
) -> None:
    package = tmp_path / "galadril_plugins"
    shutil.copytree(
        Path(galadril_plugins.__file__).parent,
        package,
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    fixture = Path(__file__).parents[3] / "plugins/tests/fixtures/fixture"
    shutil.copytree(fixture, package / "fixture")
    script = tmp_path / "check_bundle.py"
    script.write_text("""
import asyncio
import sys
from uuid import uuid4
import httpx
from galadril_plugins import PluginRegistry, PluginScope
from galadril_plugins.worker import serve
from galadril_pipeline.config import PipelineStep
from galadril_pipeline.events import PipelineCommand
from galadril_scribe.contracts import Settings
from galadril_scribe.runtime import Runtime, create_app
from galadril_vision.actors.processor import VisionCommandProcessor
from galadril_vision.common.config import VisionConfig

async def main():
    registry = PluginRegistry.discover()
    templates = registry.document_templates()
    assert len(templates) == 1
    assert templates[0]["plugin_id"] == "fixture"
    assert templates[0]["template"]["scope"] == "plugin"
    assert "owner" not in templates[0]["template"]
    assert any(asset["path"] == "letter.cls" for asset in templates[0]["template"]["assets"])
    assert len([item for item in registry.catalog.capabilities if item.plugin_id == "fixture"]) == 5
    assert {item.id for item in registry.catalog.capabilities if item.plugin_id == "utils"} == {"utils.input.email", "utils.mcp.email", "utils.skill.email"}
    assert "galadril_plugins.utils.service" not in sys.modules
    assert "galadril_plugins.fixture.scribe" not in sys.modules
    assert "galadril_plugins.fixture.handlers" not in sys.modules
    await serve("fixture", "listener", {})
    assert "galadril_plugins.fixture.scribe" not in sys.modules
    step = PipelineStep.model_validate({"step":"count","type":"algorithm","input_from":["input"],"capability_id":"fixture.algorithm.count","capability_version":"1.0.0","params":{}})
    handler = registry.handler(PluginScope("tenant","pipeline","revision","execution"),step)
    command = PipelineCommand(correlation_id=uuid4(),tenant_id="tenant",pipeline="execution",step="count",step_type="algorithm",resource_class="cpu")
    assert await handler.execute(command,step) == {"count":2,"tenant":"tenant"}
    config = VisionConfig.model_validate({"name":"execution","runtime_tenant_id":"tenant","runtime_pipeline_id":"pipeline","runtime_revision_id":"r"*32,"connectors":{
      "kafka":{"brokers":["localhost:9092"],"schema_registry":"http://localhost:8081","consumer_group":"test"},
      "s3":{"endpoint":"http://localhost:9000","access_key":"test","secret_key":"test","region":"us-east-1","bucket":"raw"},
      "postgres":{"database":"test","host":"localhost:5432","user":"test","password":"test"},
      "spicedb":{"endpoint":"localhost:50051","token":"test"}},
      "sources":[{"id":"input","topic":"raw","schema_path":"text.avsc","match_pattern":".*","capability_id":"fixture.input.text","capability_version":"1.0.0"}],"pipeline":[step.model_dump(mode="json")]})
    processor = VisionCommandProcessor(config)
    assert await processor.process(command) == {"count":2,"tenant":"tenant"}
    async with httpx.AsyncClient() as http:
        runtime = Runtime(Settings(gateway_tools_url="http://gateway/internal/chat/tools",default_model="fixture.model.text"),http)
        assert runtime.catalog.default_model == "fixture.model.text"
        assert {item.id for item in runtime.capabilities.capabilities}.issuperset({"fixture.model.text","fixture.skill.count","fixture.mcp.tools"})
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(runtime)),base_url="http://scribe") as api:
            catalog = await api.get("/pipeline-capabilities")
            assert any(item["id"] == "fixture.input.text" for item in catalog.json()["capabilities"])
            response = await api.post("/pipeline/route",json={"model_alias":"fixture.model.text","text":"document","labels":["match","other"],"instructions":"Choose"})
            assert response.status_code == 200, response.text
            assert response.json()["branch"] == "match"
asyncio.run(main())
""")
    result = subprocess.run(
        [sys.executable, str(script)],
        env={
            **os.environ,
            "PYTHONPATH": os.pathsep.join((str(tmp_path), *sys.path)),
        },
        capture_output=True,
        text=True,
        timeout=45,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__]))
