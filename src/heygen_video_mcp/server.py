import hashlib
import json
import logging
import os
import re
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import quote

import httpx
from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel, ConfigDict, Field, JsonValue

MODEL = "heygen/heygen-video-1"
API = "https://openrouter.ai/api/v1"
mcp = FastMCP(
    "heygen-video",
    instructions="Estimate first. Obtain explicit user spending consent before submit. Never invent consent, retry uncertain submissions, or switch models. Poll and download the existing job.",
)


class VideoRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    prompt: Annotated[str, Field(min_length=1, max_length=10000)]
    duration: Annotated[int, Field(ge=5, le=15)] = 5
    resolution: Literal["480p", "768p", "2K"] = "768p"
    aspect_ratio: Literal["21:9", "16:9", "4:3", "1:1", "3:4", "9:16"] = "16:9"
    seed: int | None = None


class ModelInfo(BaseModel):
    id: str
    supported_durations: list[int]
    supported_resolutions: list[str]
    supported_aspect_ratios: list[str]
    pricing_skus: dict[str, str]


class Job(BaseModel):
    id: str
    status: Literal[
        "pending", "in_progress", "completed", "failed", "cancelled", "expired"
    ]
    generation_id: str | None = None
    usage: dict[str, JsonValue] | None = None
    # Deliberately omit URLs and upstream error text from output/logs.


class OpenRouterError(RuntimeError):
    def __init__(self, status: int, detail: str):
        self.status = status
        self.detail = detail
        super().__init__(f"OpenRouter HTTP {status}: {detail}. No automatic retry.")


def data_dir() -> Path:
    return Path(
        os.environ.get("HEYGEN_DATA_DIR", str(Path.home() / ".heygen-video-mcp"))
    ).resolve()


def headers() -> dict[str, str]:
    key = os.environ.get("OPENROUTER_API_KEY", "")
    if not key:
        raise ValueError("Set OPENROUTER_API_KEY in the MCP host environment.")
    return {"Authorization": f"Bearer {key}"}


async def api_json(
    method: str, path: str, payload: dict[str, JsonValue] | None = None
) -> object:
    async with httpx.AsyncClient(timeout=60, follow_redirects=False) as client:
        try:
            response = await client.request(
                method, API + path, headers=headers(), json=payload
            )
        except httpx.RequestError:
            raise RuntimeError(
                "OpenRouter transport failed; do not repeat a submission. Inspect its local receipt."
            ) from None
    if not response.is_success:
        # Keep actionable diagnostics, but never expose credentials or access-bearing URLs.
        detail = response.text.replace(os.environ["OPENROUTER_API_KEY"], "[REDACTED]")
        detail = re.sub(r'https?://[^\s"<>]+', "[URL REDACTED]", detail)
        detail = re.sub(r"(?i)Bearer\s+[^\s\"<>]+", "Bearer [REDACTED]", detail)
        raise OpenRouterError(response.status_code, detail[:2000])
    return response.json()


@mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": True})
async def heygen_estimate(request: VideoRequest) -> dict[str, JsonValue]:
    """Fetch current capabilities and conservative USD SKU estimate. No generation or charge. Text-to-video only."""
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.get(API + "/videos/models")
    response.raise_for_status()
    # Other models may have nullable capabilities: validate only the selected model.
    catalog = response.json()
    model = ModelInfo.model_validate(
        next(item for item in catalog["data"] if item["id"] == MODEL)
    )
    if (
        request.duration not in model.supported_durations
        or request.resolution not in model.supported_resolutions
        or request.aspect_ratio not in model.supported_aspect_ratios
    ):
        raise ValueError(
            "Requested settings are not in the current model capabilities."
        )
    rate = Decimal(model.pricing_skus[f"duration_seconds_{request.resolution.lower()}"])
    if not rate.is_finite() or rate < 0:
        raise ValueError("Invalid current price; submission disabled.")
    return {
        "model": MODEL,
        "duration": request.duration,
        "resolution": request.resolution,
        "usd_per_second": str(rate),
        "estimated_usd": str(rate * request.duration),
        "pricing_source": API + "/videos/models",
        "checked_at": datetime.now(UTC).isoformat(),
        "note": "Undiscounted SKU estimate; actual provider billing can differ. This is not a provider-enforced spending cap.",
    }


@mcp.tool(
    annotations={
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    }
)
async def heygen_submit(
    request: VideoRequest,
    request_key: Annotated[str, Field(pattern=r"^[a-zA-Z0-9_-]{1,80}$")],
    approval: Annotated[str, Field(min_length=10, max_length=2000)],
    max_cost_usd: Annotated[float, Field(gt=0, allow_inf_nan=False)],
) -> dict[str, JsonValue]:
    """PAID: submit once AFTER explicit user approval. Record the user's consent in approval and their USD limit. Never invent either. Reuse request_key to recover a receipt; uncertain submissions must be reconciled manually."""
    root = data_dir() / "jobs"
    root.mkdir(parents=True, exist_ok=True)
    receipt = root / f"{request_key}.json"
    payload = request.model_dump(exclude_none=True)
    # HeyGen always renders an audio track; it rejects generate_audio=False,
    # even though the discovery catalog currently reports generate_audio=False.
    payload["model"] = MODEL
    fingerprint = hashlib.sha256(
        json.dumps(payload, sort_keys=True).encode()
    ).hexdigest()
    if receipt.exists():
        previous = json.loads(receipt.read_text(encoding="utf-8"))
        if previous["request_sha256"] != fingerprint:
            raise ValueError("request_key already belongs to different parameters.")
        return previous
    headers()
    estimate = await heygen_estimate(request)
    if Decimal(str(estimate["estimated_usd"])) > Decimal(str(max_cost_usd)):
        raise ValueError(
            "Current estimated price exceeds the approved limit; no job submitted."
        )
    record = {
        "request_key": request_key,
        "request_sha256": fingerprint,
        "request": payload,
        "approval": approval,
        "max_cost_usd": max_cost_usd,
        "estimate": estimate,
        "state": "UNKNOWN",
        "actual_cost_usd": None,
        "created_at": datetime.now(UTC).isoformat(),
    }
    # Exclusive reservation blocks concurrent duplicate POSTs. UNKNOWN survives transport/process failures.
    with receipt.open("x", encoding="utf-8") as file:
        json.dump(record, file, ensure_ascii=False, indent=2)
    try:
        response = await api_json("POST", "/videos", payload)
    except OpenRouterError as error:
        record["error"] = {"http_status": error.status, "detail": error.detail}
        temporary = receipt.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        temporary.replace(receipt)
        raise
    job = Job.model_validate(response)
    record.update({"state": "SUBMITTED", "job": job.model_dump(mode="json")})
    temporary = receipt.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    temporary.replace(receipt)
    return record


@mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": False})
def heygen_receipt(
    request_key: Annotated[str, Field(pattern=r"^[a-zA-Z0-9_-]{1,80}$")],
) -> dict[str, JsonValue]:
    """Read the saved request and job ID without generating again. UNKNOWN requires dashboard reconciliation."""
    return json.loads(
        (data_dir() / "jobs" / f"{request_key}.json").read_text(encoding="utf-8")
    )


@mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": True})
async def heygen_status(
    job_id: Annotated[str, Field(pattern=r"^[a-zA-Z0-9_-]{1,200}$")],
) -> dict[str, JsonValue]:
    """Poll once. Wait about 30 seconds between polls. Terminal failures must not trigger automatic regeneration."""
    job = Job.model_validate(await api_json("GET", f"/videos/{quote(job_id, safe='')}"))
    return job.model_dump(mode="json")


@mcp.tool(
    annotations={"readOnlyHint": False, "destructiveHint": False, "openWorldHint": True}
)
async def heygen_download(
    job_id: Annotated[str, Field(pattern=r"^[a-zA-Z0-9_-]{1,200}$")],
    filename: Annotated[
        str, Field(pattern=r"^[a-zA-Z0-9_-]{1,100}\.mp4$")
    ] = "video.mp4",
) -> dict[str, JsonValue]:
    """Download an existing completed job into HEYGEN_DATA_DIR/outputs; never overwrite. Returns local path and SHA256. No new generation."""
    job = Job.model_validate(await api_json("GET", f"/videos/{quote(job_id, safe='')}"))
    if job.status != "completed":
        raise ValueError(f"Job is {job.status}, not completed.")
    folder = data_dir() / "outputs"
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / filename
    partial = target.with_suffix(".mp4.part")
    if target.exists() or partial.exists():
        raise ValueError(
            "Output or partial file exists; choose another filename or inspect it."
        )
    # HTTPX strips Authorization on cross-origin redirects.
    async with (
        httpx.AsyncClient(
            timeout=180, follow_redirects=True, max_redirects=5
        ) as client,
        client.stream(
            "GET", f"{API}/videos/{quote(job_id, safe='')}/content", headers=headers()
        ) as response,
    ):
        if not response.is_success:
            raise RuntimeError(f"Download HTTP {response.status_code}")
        digest = hashlib.sha256()
        count = 0
        first = b""
        with partial.open("xb") as file:
            async for chunk in response.aiter_bytes():
                if len(first) < 32:
                    first += chunk[: 32 - len(first)]
                digest.update(chunk)
                count += len(chunk)
                file.write(chunk)
    if count < 12 or first[4:8] != b"ftyp":
        raise ValueError("Output is not an MP4; retained .part for inspection.")
    partial.rename(target)
    return {
        "path": str(target),
        "bytes": count,
        "sha256": digest.hexdigest(),
        "usage": job.usage,
        "review": "NOT_RUN: inspect duration, decode, picture and audio before approving.",
    }


def main() -> None:
    logging.getLogger("httpx").setLevel(logging.WARNING)
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
