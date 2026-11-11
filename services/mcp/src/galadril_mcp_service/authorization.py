"""Represent missing tool scopes separately from Gateway authorization denials."""

from mcp.server.mcpserver.exceptions import ToolError


class ToolScopeRequired(ToolError):
    def __init__(self, scope: str) -> None:
        super().__init__("Additional Galadril permissions are required")
        self.scope = scope
