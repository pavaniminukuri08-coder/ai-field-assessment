
import asyncio
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def main():
    # Start the MCP server using the current Python environment.
    server_file = Path(__file__).with_name("server.py")

    server_params = StdioServerParameters(
        command=sys.executable,
        args=[str(server_file)],
    )

    async with stdio_client(server_params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            # 1. Verify that both tools are registered.
            tools = await session.list_tools()

            print("Available tools:")
            for tool in tools.tools:
                print(f"- {tool.name}: {tool.description}")

            # 2. Test a valid customer lookup.
            result = await session.call_tool(
                "get_customer_record",
                {"customer_id": "CUST-A1234"},
            )

            print("\nValid customer lookup:")
            print(result)

            # 3. Test invalid inputs through the MCP client.
            test_cases = [
                {
                    "name": "Invalid customer ID",
                    "tool": "get_customer_record",
                    "arguments": {"customer_id": "INVALID"},
                },
                {
                    "name": "Negative refund amount",
                    "tool": "trigger_refund",
                    "arguments": {
                        "customer_id": "CUST-A1234",
                        "amount": -10,
                        "reason": "Customer requested refund",
                    },
                },
                {
                    "name": "Short refund reason",
                    "tool": "trigger_refund",
                    "arguments": {
                        "customer_id": "CUST-A1234",
                        "amount": 10,
                        "reason": "Too short",
                    },
                },
            ]

            for test in test_cases:
                result = await session.call_tool(
                    test["tool"],
                    test["arguments"],
                )

                print(f"\n{test['name']}:")
                print(result)
                print(f"Tool reported error: {result.isError}")


if __name__ == "__main__":
    asyncio.run(main())
