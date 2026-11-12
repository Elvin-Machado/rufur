"""Wire service + MCP + REST into one ASGI app (what `rufur serve` runs)."""

from __future__ import annotations

from fastapi import FastAPI

from .api.mcp_server import LocalBackend, build_mcp
from .api.rest import create_app
from .config import Config
from .service import MemoryService


def build_app(cfg: Config, service: MemoryService | None = None) -> FastAPI:
    service = service or MemoryService(cfg)
    mcp = build_mcp(LocalBackend(service), default_agent="mcp-client", resolve_key=service.agent_for_key)
    # Stateless JSON mode: each MCP request is handled inside its HTTP request, so no
    # long-lived session state has to survive daemon restarts.
    mcp_app = mcp.streamable_http_app(streamable_http_path="/mcp", stateless_http=True, json_response=True,
                                      host=cfg.host)  # fmt: skip
    return create_app(service, mcp_app=mcp_app, mcp_session_manager=mcp.session_manager)
