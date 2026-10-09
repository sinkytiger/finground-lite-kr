"""Plugin entry point: run the MCP server from the plugin root without installing the package."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from finground_kr.mcp_server import serve  # noqa: E402

if __name__ == "__main__":
    serve()
