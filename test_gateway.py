
from fastapi.testclient import TestClient

from gateway import app


client = TestClient(app)

VIEWER_HEADERS = {
    "Authorization": "Bearer local-viewer-token"
}

ADMIN_HEADERS = {
    "Authorization": "Bearer local-admin-token"
}


def test_missing_token_is_rejected():
    response = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/list",
        },
    )

    assert response.status_code == 401


def test_tools_list_is_forwarded():
    response = client.post(
        "/mcp",
        headers=VIEWER_HEADERS,
        json={
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/list",
        },
    )

    assert response.status_code == 200

    body = response.json()
    assert body["id"] == 2
    assert "result" in body

    tool_names = [
        tool["name"] for tool in body["result"]["tools"]
    ]

    assert "get_customer_record" in tool_names
    assert "trigger_refund" in tool_names


def test_viewer_cannot_call_admin_tool():
    response = client.post(
        "/mcp",
        headers=VIEWER_HEADERS,
        json={
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {
                "name": "admin_delete_customer",
                "arguments": {},
            },
        },
    )

    assert response.status_code == 200

    body = response.json()
    assert body["id"] == 3
    assert body["error"]["code"] == -32001
    assert body["error"]["message"] == "Unauthorized Tool Call"


def test_viewer_can_call_regular_tool():
    response = client.post(
        "/mcp",
        headers=VIEWER_HEADERS,
        json={
            "jsonrpc": "2.0",
            "id": 4,
            "method": "tools/call",
            "params": {
                "name": "get_customer_record",
                "arguments": {
                    "customer_id": "CUST-A1234",
                },
            },
        },
    )

    assert response.status_code == 200

    body = response.json()
    assert body["id"] == 4
    assert "result" in body
    assert body["result"]["isError"] is False


def test_admin_request_is_not_blocked_by_role_check():
    # This verifies the gateway's authorization rule.
    # The mock tool does not exist on the current MCP server.
    response = client.post(
        "/mcp",
        headers=ADMIN_HEADERS,
        json={
            "jsonrpc": "2.0",
            "id": 5,
            "method": "tools/call",
            "params": {
                "name": "admin_delete_customer",
                "arguments": {},
            },
        },
    )

    assert response.status_code == 200
    assert response.json()["id"] == 5

    # The request was forwarded, so it should not receive the
    # gateway's specific -32001 viewer-authorization error.
    body = response.json()
    assert body.get("error", {}).get("code") != -32001
