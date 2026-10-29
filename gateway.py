
import os
import sys
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Header, Request
from fastapi.responses import JSONResponse, Response
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


app = FastAPI(title="MCP HTTP Gateway")

SERVER_FILE = Path(__file__).with_name("server.py")

# These defaults are for local development only.
# Set environment variables for your own deployment.
TOKENS = {
    os.getenv("MCP_ADMIN_TOKEN", "local-admin-token"): "admin",
    os.getenv("MCP_VIEWER_TOKEN", "local-viewer-token"): "viewer",
}


def rpc_error(request_id: Any, code: int, message: str) -> dict:
    """Build a standard JSON-RPC error response."""
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {
            "code": code,
            "message": message,
        },
    }


def json_safe(model: Any) -> Any:
    """Convert an SDK model into JSON-compatible data."""
    if hasattr(model, "model_dump"):
        return model.model_dump(mode="json")
    return model


def get_role(authorization: str | None) -> str | None:
    """Validate the Authorization header and return the user's role."""
    if not authorization:
        return None

    scheme, separator, token = authorization.partition(" ")

    if not separator or scheme.lower() != "bearer" or not token:
        return None

    return TOKENS.get(token)


@app.post("/mcp")
async def handle_rpc(
    request: Request,
    authorization: str | None = Header(default=None),
):
    # Authenticate before processing the request.
    role = get_role(authorization)

    if role is None:
        return JSONResponse(
            status_code=401,
            content={"detail": "Missing or invalid Bearer token."},
            headers={"WWW-Authenticate": "Bearer"},
        )

    try:
        payload = await request.json()
    except Exception:
        return JSONResponse(
            status_code=400,
            content=rpc_error(None, -32700, "Parse error"),
        )

    if not isinstance(payload, dict):
        return JSONResponse(
            content=rpc_error(None, -32600, "Invalid Request")
        )

    request_id = payload.get("id")

    if payload.get("jsonrpc") != "2.0" or not isinstance(
        payload.get("method"), str
    ):
        return JSONResponse(
            content=rpc_error(request_id, -32600, "Invalid Request")
        )

    method = payload["method"]
    params = payload.get("params", {})

    if not isinstance(params, dict):
        return JSONResponse(
            content=rpc_error(request_id, -32600, "Invalid params")
        )

    # Enforce role-based access before forwarding the call.
    if method == "tools/call":
        tool_name = params.get("name")

        if not isinstance(tool_name, str):
            return JSONResponse(
                content=rpc_error(request_id, -32602, "Invalid params")
            )

        if tool_name.startswith("admin_") and role != "admin":
            return JSONResponse(
                content=rpc_error(
                    request_id,
                    -32001,
                    "Unauthorized Tool Call",
                )
            )

    if method not in {"tools/list", "tools/call"}:
        return JSONResponse(
            content=rpc_error(request_id, -32601, "Method not found")
        )

    server_params = StdioServerParameters(
        command=sys.executable,
        args=[str(SERVER_FILE)],
    )

    try:
        # Start the MCP server and forward the request through its SDK.
        async with stdio_client(server_params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()

                if method == "tools/list":
                    result = await session.list_tools()
                else:
                    arguments = params.get("arguments", {})

                    if not isinstance(arguments, dict):
                        return JSONResponse(
                            content=rpc_error(
                                request_id, -32602, "Invalid params"
                            )
                        )

                    result = await session.call_tool(
                        params["name"],
                        arguments,
                    )

                return JSONResponse(
                    content={
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "result": json_safe(result),
                    }
                )

    except Exception:
        # Keep internal exception details out of HTTP responses.
        return JSONResponse(
            content=rpc_error(
                request_id,
                -32603,
                "Internal error while processing the MCP request",
            )
        )
