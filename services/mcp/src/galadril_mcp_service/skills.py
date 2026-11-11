"""Serve an integrity-checked catalogue of deployment-owned Agent Skills."""

from collections.abc import Iterator, Sequence
from hashlib import sha256
from importlib.resources import files
from importlib.resources.abc import Traversable
from pathlib import Path

import yaml
from mcp import MCPError
from mcp.server.context import ServerRequestContext
from mcp.server.extension import Extension, MethodBinding, ResourceBinding
from mcp.server.mcpserver.resources import TextResource
from mcp.types import INVALID_PARAMS, PaginatedRequestParams, RequestParams
from pydantic import Field, JsonValue, TypeAdapter

_LEGAL_ROOT = "skills/librejustice"
CLIENT_ROOT = Path(__file__).with_name("client_plugin")
_JSON_OBJECT = TypeAdapter(dict[str, JsonValue])
_MODERN = frozenset({"2026-07-28"})
_LEGAL_METHODS = ("recherche-jurisprudence", "recherche-normes")


def legal_root() -> Traversable:
    return files("galadril_plugins.law").joinpath(_LEGAL_ROOT)


def skill_folders(provider: str) -> Iterator[Traversable]:
    if provider in {"unified", "piste"}:
        yield CLIENT_ROOT / "skills" / "galadril-legal-research"
        for name in _LEGAL_METHODS:
            yield legal_root().joinpath(name)
    if provider in {"unified", "workspace"}:
        for name in ("galadril-email", "galadril-latex"):
            yield CLIENT_ROOT / "skills" / name


def skill_files(folder: Traversable) -> Iterator[tuple[str, Traversable]]:
    yield "SKILL.md", folder.joinpath("SKILL.md")
    for directory in ("agents", "references"):
        subtree = folder.joinpath(directory)
        if subtree.is_dir():
            for path in sorted(subtree.iterdir(), key=lambda item: item.name):
                if path.is_file() and path.name.endswith((".md", ".yaml")):
                    yield f"{directory}/{path.name}", path
    if folder.name == "galadril-legal-research":
        # Keep the provider references relative to their original procedure.
        for name in _LEGAL_METHODS:
            for relative, path in skill_files(legal_root().joinpath(name)):
                if relative.endswith(".md"):
                    filename = (
                        "procedure.md" if relative == "SKILL.md" else relative
                    )
                    yield f"references/{name}/{filename}", path


class SkillRead(RequestParams):
    uri: str = Field(min_length=1, max_length=1024)


class PackagedSkills(Extension):
    identifier = "io.modelcontextprotocol/skills"

    def __init__(self, provider: str) -> None:
        self._entries: dict[str, dict[str, JsonValue]] = {}
        self._resources: list[ResourceBinding] = []
        for folder in skill_folders(provider):
            prefix = f"skill://galadril/{folder.name}/"
            manifest: list[JsonValue] = []
            frontmatter: dict[str, JsonValue] = {}
            for relative, path in skill_files(folder):
                content = path.read_bytes()
                text = content.decode("utf-8")
                uri = prefix + relative
                description = None
                if relative == "SKILL.md":
                    _, start, remainder = text.partition("---\n")
                    metadata, end, _ = remainder.partition("\n---\n")
                    if not start or not end:
                        raise ValueError(
                            "Packaged Skill requires YAML frontmatter"
                        )
                    frontmatter = _JSON_OBJECT.validate_python(
                        yaml.safe_load(metadata)
                    )
                    description = frontmatter.get("description")
                    if (
                        frontmatter.get("name") != folder.name
                        or not isinstance(description, str)
                        or not description
                    ):
                        raise ValueError(
                            "Packaged Skill name or description is invalid"
                        )
                self._resources.append(
                    ResourceBinding(
                        TextResource(
                            uri=uri,
                            name=folder.name
                            if relative == "SKILL.md"
                            else f"{folder.name}/{relative}",
                            description=description,
                            mime_type="text/markdown"
                            if relative.endswith(".md")
                            else "application/yaml",
                            text=text,
                        )
                    )
                )
                manifest.append(
                    {
                        "uri": uri,
                        "digest": "sha256:" + sha256(content).hexdigest(),
                        "size": len(content),
                    }
                )
            uri = prefix + "SKILL.md"
            self._entries[uri] = {
                "uri": uri,
                "frontmatter": frontmatter,
                "resources": manifest,
            }

    def resources(self) -> Sequence[ResourceBinding]:
        return self._resources

    def methods(self) -> Sequence[MethodBinding]:
        return (
            MethodBinding(
                "skills/list", PaginatedRequestParams, self.list_skills, _MODERN
            ),
            MethodBinding("skills/get", SkillRead, self.get_skill, _MODERN),
        )

    async def list_skills(
        self,
        context: ServerRequestContext[None, object],
        params: PaginatedRequestParams,
    ) -> dict[str, JsonValue]:
        """The small bundled catalogue fits in a single response."""
        if params.cursor is not None:
            raise MCPError(code=INVALID_PARAMS, message="Unknown skills cursor")
        return {
            "resultType": "complete",
            "skills": list(self._entries.values()),
        }

    async def get_skill(
        self, context: ServerRequestContext[None, object], params: SkillRead
    ) -> dict[str, JsonValue]:
        if (skill := self._entries.get(params.uri)) is None:
            raise MCPError(code=INVALID_PARAMS, message="Unknown skill URI")
        return {"resultType": "complete", "skill": skill}
