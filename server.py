
import logging
import sys
from typing import Annotated

from mcp.server.fastmcp import FastMCP
from pydantic import Field


# Keep application logs separate from MCP protocol messages.
logging.basicConfig(
    level=logging.INFO,
    stream=sys.stderr,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)


# Create the MCP server.
mcp = FastMCP("Customer Support Server")


# Customer IDs must look like CUST-A1234.
CustomerID = Annotated[
    str,
    Field(pattern=r"^CUST-[A-Z0-9]{5}$"),
]

# Reject zero, negative, NaN, and infinite refund amounts.
RefundAmount = Annotated[
    float,
    Field(gt=0, allow_inf_nan=False),
]

# Require a reason containing at least 10 characters.
RefundReason = Annotated[
    str,
    Field(min_length=10),
]


# Mock data keeps this assessment independent of real customer systems.
CUSTOMERS = {
    "CUST-A1234": {
        "name": "Sample Customer",
        "status": "active",
    },
    "CUST-B5678": {
        "name": "Another Customer",
        "status": "active",
    },
}


@mcp.tool()
def get_customer_record(customer_id: CustomerID) -> dict:
    """Return a customer record for a valid customer ID."""

    logger.info("Customer record lookup requested")

    customer = CUSTOMERS.get(customer_id)

    if customer is None:
        return {
            "customer_id": customer_id,
            "found": False,
            "message": "Customer record not found.",
        }

    return {
        "customer_id": customer_id,
        "found": True,
        **customer,
    }


@mcp.tool()
def trigger_refund(
    customer_id: CustomerID,
    amount: RefundAmount,
    reason: RefundReason,
) -> dict:
    """Validate and simulate a refund request."""

    logger.info("Refund request received")

    # This assessment does not connect to a payment provider.
    if customer_id not in CUSTOMERS:
        return {
            "status": "rejected",
            "message": "Customer record not found.",
        }

    return {
        "customer_id": customer_id,
        "amount": amount,
        "reason": reason,
        "status": "simulated",
        "message": "Refund request passed validation.",
    }


if __name__ == "__main__":
    mcp.run(transport="stdio")
