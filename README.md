
# Task 1: Custom MCP Server

## Problem Statement

Build a Python MCP server that exposes customer lookup and
refund tools. Validate tool inputs, use stdio transport, and
keep application logs separate from protocol messages.

## Tools

### 1. get_customer_record

Input:
- customer_id: string matching CUST-XXXXX

Behavior:
- Validates the customer ID format.
- Looks up the ID in mock customer data.
- Returns the customer record or a not-found result.

### 2. trigger_refund

Inputs:
- customer_id: valid customer ID
- amount: positive, finite number
- reason: string with at least 10 characters

Behavior:
- Validates the inputs.
- Checks whether the customer exists.
- Returns a simulated refund result.

No real refund is issued.

## Design Decisions

- FastMCP registers the Python functions as MCP tools.
- Pydantic constraints define the input validation rules.
- The SDK handles MCP protocol messages and stdio transport.
- Application logs are written to stderr.
- Mock data makes the assessment runnable without external services.

## Run the Server

Activate the virtual environment and install dependencies:

    python -m pip install -r requirements.txt

Start the server:

    python server.py

The server waits for an MCP client to connect over stdio.
A silent terminal is expected.

## Validation Examples

Valid customer ID:
    CUST-A1234

Invalid customer IDs:
    CUST-123
    cust-A1234
    CUST-ABCDE-extra

Valid refund:
    customer_id = CUST-A1234
    amount = 25.50
    reason = "Duplicate payment"

Invalid refunds:
    amount = 0
    amount = -10
    reason = "duplicate"

## Limitations

- Customer records are hard-coded mock data.
- Refunds are simulated.
- Authentication and authorization are not implemented here.
- SDK validation errors must be verified against the installed
  SDK's actual MCP response behavior.

## Interview Summary

I built an MCP server with two tools using FastMCP. I defined
input constraints with Pydantic, separated input validation
from customer lookup, used mock data to avoid external
dependencies, and directed application logs to stderr so
stdout remains available for MCP protocol communication.

# Task 2: HTTP JSON-RPC MCP Gateway

## Overview

For this task, I built an HTTP gateway that connects external clients to the MCP server created in Task 1. The gateway handles authentication, checks user permissions, and forwards valid requests to the MCP server.

The main goal was to make the MCP tools accessible over HTTP while ensuring that users can only access the tools allowed for their role.

## What I implemented

- Added a FastAPI endpoint at `/mcp` to receive JSON-RPC requests.
- Implemented Bearer-token authentication with two roles: `admin` and `viewer`.
- Forwarded `tools/list` requests to retrieve the available MCP tools.
- Forwarded authorized `tools/call` requests to execute MCP tools.
- Prevented viewers from calling tools whose names start with `admin_`.
- Returned JSON-RPC error code `-32001` with the message `Unauthorized Tool Call` when a viewer attempts to call a restricted tool.
- Added basic error handling for invalid requests, unsupported methods, and internal processing failures.

## How it works

When a request reaches the gateway, it first checks the Bearer token to identify the user's role. Requests without a valid token are rejected.

After authentication, the gateway checks the requested JSON-RPC method. Tool-list requests are forwarded to the MCP server. For tool calls, the gateway checks whether the tool name starts with `admin_`. If the caller is a viewer, the request is rejected before it reaches the MCP server. Otherwise, the gateway forwards the request and returns the result.

I used the official MCP Python SDK to communicate with the server over standard input/output rather than implementing the MCP communication protocol manually.

## Technologies used

- Python
- FastAPI
- MCP Python SDK
- Uvicorn
- Pytest

## Running the gateway

First, install the project dependencies:

```powershell
python -m pip install -r requirements.txt
```

Start the gateway with:

```powershell
python -m uvicorn gateway:app --host 127.0.0.1 --port 8000
```

The endpoint is available at `http://127.0.0.1:8000/mcp`.

For local testing, the gateway uses `local-admin-token` and `local-viewer-token` as its default tokens. These are development credentials and should be replaced with securely managed secrets in a real deployment.

## Testing

I added automated tests in `test_gateway.py` to check the main gateway functionality.

The tests cover:

- Rejecting requests without a valid token.
- Forwarding requests to list available tools.
- Blocking viewers from calling tools that start with `admin_`.
- Allowing viewers to call regular tools.
- Checking that the gateway does not reject admin requests under the viewer-only restriction.

Run the tests using:

```powershell
python -m pytest test_gateway.py -v
```

All five tests passed.

One limitation is that the current MCP server does not have an actual `admin_` tool. The admin test verifies that the gateway allows the request past its role check, but it does not verify the successful execution of an administrative operation.

## Notes

For this assessment, the gateway starts the MCP server as a subprocess for each forwarded request. This keeps the implementation straightforward, although a production system would benefit from reusing connections and adding request timeouts, concurrency controls, and more robust logging.

Overall, this task gave me an opportunity to connect an MCP server to an HTTP interface and add authentication and role-based access control around its tools.


# Task 3 – Streaming LLM Gateway

## Overview

This task is about building an LLM gateway that streams the model's response and removes sensitive information before sending it to the client.

The gateway uses FastAPI and returns responses as Server-Sent Events (SSE). It checks for email addresses, SSNs, and credit card numbers and replaces them with `[REDACTED]`.

## How it works

- The client sends a prompt to the `/generate` endpoint.
- The gateway sends the prompt to the configured LLM API.
- The response is read in chunks as it arrives.
- Before sending each chunk to the client, the gateway checks for PII and redacts matching values.
- If a sensitive value is split across chunks, the redactor keeps part of the text in a buffer until it has enough information to check it.

The gateway also handles a missing API key and errors from the LLM provider.

## Setup

Install the project dependencies:

```powershell
pip install -r requirements.txt
```

Set the API key in PowerShell:

```powershell
$env:LLM_API_KEY = "your-api-key"
```

The gateway uses the OpenAI chat completions endpoint by default. You can change the endpoint and model using these environment variables if needed:

- `LLM_CHAT_COMPLETIONS_URL`
- `LLM_MODEL`

## Run the application

Start the server with:

```powershell
uvicorn task3_gateway:app --reload
```

The application runs at `http://127.0.0.1:8000`.

The endpoint for generating responses is `POST /generate`.

## Run the tests

```powershell
python -m pytest test_task3.py -v
```

The tests check PII redaction across chunks, multiple sensitive values in one chunk, incremental streaming, the HTTP endpoint, and behavior when the API key is missing.

## Notes

This implementation uses regular expressions to detect PII. It covers the formats tested in this task, but it may not catch every possible format of sensitive information. More testing would be needed before using it in production.


# Task 4: Rate-Limiting and Model Fallback Router

## Overview

This task implements a rate-limiting and model-routing layer for an LLM gateway. It controls token usage per tenant, routes completion requests to an upstream provider, and switches to a backup provider when the primary provider returns HTTP 429 or times out.

The gateway uses FastAPI for the API, SQLite for persistent rate-limit tracking, and HTTPX for asynchronous provider requests.

## Features

- **Token-aware rate limiting:** Tracks token reservations for each tenant API key within a rolling 60-second window.
- **Sliding window:** Removes expired usage records and checks current usage before accepting a request.
- **Concurrent request handling:** Uses SQLite transactions to prevent concurrent requests from incorrectly exceeding the configured quota.
- **Automatic fallback:** Retries through a configured backup provider when the primary provider returns HTTP 429 or times out.
- **Timeout handling:** Configures a 3-second timeout for primary-provider requests.
- **Sanitized errors:** Returns standardized error responses without exposing upstream response bodies, credentials, or internal trace details.
- **Persistent storage:** Stores rate-limit events in a local SQLite database.
- **Token estimation:** Uses `tiktoken` when available, with an approximate fallback when it is not.
- **Request validation:** Validates completion request fields using Pydantic.

## Architecture

1. The client sends a completion request with a tenant API key.
2. The gateway validates the request and derives a tenant identifier from a hash of the API key.
3. The token limiter estimates prompt tokens and reserves capacity for the requested output.
4. SQLite checks the tenant's usage in the previous 60 seconds.
5. The gateway sends the request to the primary provider.
6. If the primary provider returns HTTP 429 or times out, the gateway attempts the configured backup provider.
7. The gateway returns the successful provider response or a sanitized error.
8. The token reservation is updated or released according to the request outcome.

## Requirements

- Python 3.10 or later
- FastAPI
- HTTPX
- Pydantic
- python-dotenv
- tiktoken (optional; used for token estimation)
- SQLite (included with Python)

Install dependencies:

```bash
pip install -r requirements.txt
```

## Environment Configuration

Create a `.env` file in the project root:

```dotenv
PRIMARY_LLM_API_KEY=your-primary-api-key
PRIMARY_LLM_URL=https://api.openai.com/v1/chat/completions
PRIMARY_LLM_MODEL=gpt-4o-mini

BACKUP_LLM_API_KEY=your-backup-api-key
BACKUP_LLM_URL=your-backup-provider-chat-completions-url
BACKUP_LLM_MODEL=your-backup-model

RATE_LIMIT_DB_PATH=rate_limits.sqlite3
```

Replace the example values with valid credentials and endpoints for your providers. The backup provider must be configured separately for actual failover to work.

Do not commit `.env` or expose API keys in source control.

## API Endpoint

### `POST /v1/completions`

Example request:

```json
{
  "messages": [
    {
      "role": "user",
      "content": "Explain rate limiting in simple terms."
    }
  ],
  "max_tokens": 256,
  "temperature": 0.7
}
```

Include the tenant API key in the request header:

```http
Authorization: Bearer your-tenant-api-key
```

The gateway forwards the request to the configured provider and returns its completion response when successful.

## Rate-Limiting Behavior

The default configuration allows up to **50,000 reserved tokens per tenant in a rolling 60-second window**.

The reservation includes estimated prompt tokens and the requested maximum output tokens. Requests that exceed the remaining quota are rejected with HTTP 429.

SQLite transactions and a database busy timeout help coordinate concurrent reservations.

## Fallback Behavior

The gateway attempts the backup provider when the primary provider:

- Returns HTTP 429 (rate limit exceeded).
- Times out according to the configured primary timeout.

Other primary-provider errors are returned as sanitized gateway errors rather than automatically triggering fallback.

If the backup provider is not configured or also fails, the gateway returns a standardized error response.

## Error Handling

Errors use a consistent JSON structure:

```json
{
  "error": {
    "type": "rate_limit_error",
    "code": "rate_limit_exceeded",
    "message": "The tenant token limit has been exceeded."
  }
}
```

The actual error type, code, and message depend on the failure. Internal stack traces and raw upstream error details are not returned to the client.

## Running the Gateway

From the project directory, activate the virtual environment and start the application:

```powershell
.\.venv\Scripts\Activate.ps1
uvicorn task4_router:app --reload
```

The API will be available at:

`http://127.0.0.1:8000`

## Running Tests

Run the Task 4 tests:

```powershell
python -m pytest test_task4.py -v
```

Run the complete assessment test suite:

```powershell
python -m pytest -v
```

The Task 4 tests cover missing API keys, successful primary responses, fallback after HTTP 429, fallback after a timeout, rate-limit enforcement, and sanitized upstream errors. The provider calls are mocked in these tests, so they do not require real API credentials.

## Implementation Notes

The token limiter uses a local SQLite database and asynchronous thread offloading for database operations. The primary-provider timeout is configured to three seconds. A production deployment should additionally verify strict end-to-end timeout behavior, reconcile actual token usage accurately, and test concurrent requests against the configured quota.

The gateway is intended as a lightweight assessment implementation. Production use would also require stronger tenant authentication, provider-specific error handling, observability, and deployment-level security controls.