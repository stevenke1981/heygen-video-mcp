# HeyGen Video MCP

A stdio MCP server that lets Codex, Claude Desktop, and other MCP agents generate videos with **`heygen/heygen-video-1` on OpenRouter**. Python 3.11+ and `uv` are required. It uses the official MCP Python SDK.

## Install

```sh
git clone https://github.com/stevenke1981/heygen-video-mcp.git
cd heygen-video-mcp
uv sync --locked
uv run heygen-video-mcp
```

Set `OPENROUTER_API_KEY` in the launching process environment or your host's secret store. The server does not load `.env` automatically. Never paste secrets into prompts, commits, or command arguments. Optional `HEYGEN_DATA_DIR` selects persistent storage; its default is `~/.heygen-video-mcp`.

Codex registration (replace the path with your clone's absolute path):

```sh
codex mcp add heygen-video -- uv run --directory /absolute/path/heygen-video-mcp --locked heygen-video-mcp
```

Generic MCP host configuration:

```json
{
  "mcpServers": {
    "heygen-video": {
      "command": "uv",
      "args": ["run", "--directory", "/absolute/path/heygen-video-mcp", "--locked", "heygen-video-mcp"]
    }
  }
}
```

The host must inherit `OPENROUTER_API_KEY`. On Windows, use an absolute `uv.exe` path if the host cannot find it; JSON paths need escaped backslashes. Restart/reload your MCP host after registration. Local agents need access to the same data directory to resume jobs.

For a Windows NAS/mapped-drive checkout, put the virtual environment on a local disk: set `UV_PROJECT_ENVIRONMENT=C:\Users\YOUR_USER\.cache\heygen-video-mcp\venv` both when running `uv sync` and in your MCP host environment. This avoids native DLL loading failures from UNC paths.

## Agent workflow

1. Call `heygen_estimate` with `request` (prompt, duration, resolution, aspect_ratio, optional seed). This only reads the public model catalog.
2. Explain the selected model, purpose, USD rate and estimated total to the user. **Obtain explicit consent for this paid call.**
3. Call `heygen_submit` with the same request, a unique `request_key`, the actual user consent in `approval`, and their `max_cost_usd`. This checks current pricing again and sends one POST.
4. Poll `heygen_status(job_id)` about every 30 seconds. Continue an existing job after a timeout; never automatically regenerate.
5. Call `heygen_download(job_id, filename)` after completion. Inspect the actual MP4 before claiming creative approval.

`heygen_receipt(request_key)` recovers the locally saved job ID. Repeating submit with the same key and parameters returns the receipt without charging again. Changing parameters under a used key is rejected. Keep your ledger directory persistent. A different key is a different authorization responsibility, not provider-side idempotency.

Submission failure leaves an `UNKNOWN` receipt. Check OpenRouter activity/support before any retry; a timeout does not prove the provider rejected the job. The current API has no documented idempotency header or lookup-by-local-key endpoint. Receipts contain prompts and consent and should be kept private.

HTTP failures retain their status and redacted response text in the local receipt so parameter or account errors can be diagnosed without another paid request. API keys and response URLs are removed from these diagnostics. Reconciliation and renewed approval are still required before another submission.

## Cost and feature scope

This release supports **text-to-video**, 5–15 seconds, 480p/768p/2K and the six advertised aspect ratios. It validates capabilities against the live catalog. It intentionally exposes one model and no model-routing fallback. Reference inputs and separate voice generation are outside this release.

The current API reports `generate_audio: false`; this server explicitly requests `false` and does not promise a soundtrack. Even if the provider returns audio, listening review is separate from decoding.

Prices are fetched at execution time from `GET /videos/models`. On 2026-10-31, 768p was listed at $0.036/second ($0.18 for five seconds), while the web model page advertised a 50% discount. Estimates use the undiscounted SKU price. `max_cost_usd` is a **local estimate gate**, not a billing cap enforced by OpenRouter. It cannot guarantee final charges; use provider account/key spending controls as well. The consent field is a workflow record, not authentication of a human approval.

The tools do not log API keys or return signed asset URLs. Downloads use the fixed OpenRouter content endpoint; HTTPX removes Authorization across origins. Downloads refuse existing files, retain incomplete `.part` files, and return a SHA256. An MP4 signature is only a format check, not full media validation.

## Children's example: 老鼠娶親

[`examples/mouse-wedding.json`](examples/mouse-wedding.json) is a five-second 768p, 16:9 prompt: a friendly mouse wedding procession, red embroidered clothes, lanterns, flower petals and a sunny village. No frightening animals, chasing, violence, fireworks or readable generated text. It is a prompt, not a generated result.

Use the included MCP client to preview tools and estimate, without a paid call:

```sh
uv run python examples/client.py
```

After actual user approval, the same client can submit through MCP:

```sh
uv run python examples/client.py --submit --approval "ACTUAL USER CONSENT" --max-cost-usd 0.18
uv run python examples/client.py --status JOB_ID
uv run python examples/client.py --download JOB_ID
```

The example uses the stable request key `mouse-wedding-v1`. Do not change it to bypass an uncertain receipt.

## Development

```sh
uv sync --locked --group dev
uv run pytest
uv run ruff check .
uv build
```

Tests mock external generation; the stdio test launches the real server and checks MCP discovery and invalid arguments without charging. CI never receives an OpenRouter key. Live generation requires separate consent.

## Sources

- [OpenRouter model](https://openrouter.ai/heygen/heygen-video-1)
- [Video API guide](https://openrouter.ai/docs/guides/overview/multimodal/video-generation)
- [Live capabilities and SKU pricing](https://openrouter.ai/api/v1/videos/models)
- [MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk/tree/v1.x)

MIT license. This community project is not affiliated with HeyGen or OpenRouter. The license covers this code, not provider service terms or generated-media rights.
