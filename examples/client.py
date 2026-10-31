"""Exercise the actual stdio MCP interface. Default mode cannot generate."""

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def run() -> None:
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--submit", action="store_true")
    mode.add_argument("--status")
    mode.add_argument("--download")
    parser.add_argument("--approval")
    parser.add_argument("--max-cost-usd", type=float)
    args = parser.parse_args()
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "heygen_video_mcp.server"],
        env=dict(os.environ),
    )
    async with (
        stdio_client(params) as (read, write),
        ClientSession(read, write) as session,
    ):
        await session.initialize()
        if args.submit:
            if not args.approval or not args.max_cost_usd:
                parser.error(
                    "--submit requires actual user --approval and --max-cost-usd"
                )
            request = json.loads(
                Path(__file__)
                .with_name("mouse-wedding.json")
                .read_text(encoding="utf-8")
            )
            result = await session.call_tool(
                "heygen_submit",
                {
                    "request": request,
                    "request_key": "mouse-wedding-v1",
                    "approval": args.approval,
                    "max_cost_usd": args.max_cost_usd,
                },
            )
        elif args.status:
            result = await session.call_tool("heygen_status", {"job_id": args.status})
        elif args.download:
            result = await session.call_tool(
                "heygen_download",
                {"job_id": args.download, "filename": "mouse-wedding-5s.mp4"},
            )
        else:
            print(
                json.dumps(
                    {
                        "tools": [
                            tool.name for tool in (await session.list_tools()).tools
                        ]
                    }
                )
            )
            request = json.loads(
                Path(__file__)
                .with_name("mouse-wedding.json")
                .read_text(encoding="utf-8")
            )
            result = await session.call_tool("heygen_estimate", {"request": request})
        print(result.model_dump_json(indent=2))
        if result.isError:
            raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(run())
