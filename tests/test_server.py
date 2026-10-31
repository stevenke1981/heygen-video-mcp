import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

import httpx
import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from pydantic import ValidationError

from heygen_video_mcp import server

CATALOG = {
    "data": [
        {
            "id": server.MODEL,
            "supported_durations": [5, 10],
            "supported_resolutions": ["480p", "768p"],
            "supported_aspect_ratios": ["16:9"],
            "pricing_skus": {
                "duration_seconds_768p": "0.036",
                "duration_seconds_480p": "0.02",
            },
        }
    ]
}
REQUEST = server.VideoRequest(prompt="A friendly mouse wedding")


@pytest.fixture
def api(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-placeholder")
    monkeypatch.setenv("HEYGEN_DATA_DIR", str(tmp_path))
    calls = []
    state = {
        "post": 202,
        "status": "completed",
        "timeout": False,
        "redirect": False,
        "invalid_media": False,
    }

    def handle(request):
        calls.append(request)
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json=CATALOG)
        if request.method == "POST":
            if state["timeout"]:
                raise httpx.ReadTimeout("private upstream information", request=request)
            if state["post"] != 202:
                return httpx.Response(
                    state["post"],
                    json={
                        "error": {
                            "message": "diagnostic test-placeholder https://example.org/private?token=secret"
                        }
                    },
                )
            return httpx.Response(
                state["post"], json={"id": "job-test", "status": "pending"}
            )
        if request.url.host == "cdn.example.org":
            assert "authorization" not in request.headers
            return httpx.Response(
                200, content=b"\x00\x00\x00\x18ftypisom" + b"mock" * 20
            )
        if request.url.path.endswith("/content"):
            if state["redirect"]:
                return httpx.Response(
                    302, headers={"Location": "https://cdn.example.org/clip"}
                )
            return httpx.Response(
                200,
                content=b"not a video"
                if state["invalid_media"]
                else b"\x00\x00\x00\x18ftypisom" + b"mock" * 20,
            )
        return httpx.Response(
            200,
            json={
                "id": "job-test",
                "status": state["status"],
                "usage": {"cost": 0.09},
                "urls": ["https://private.example/secret"],
            },
        )

    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        server.httpx,
        "AsyncClient",
        lambda **kwargs: real_client(transport=httpx.MockTransport(handle), **kwargs),
    )
    return calls, state, tmp_path


def submit(key="test-v1", request=REQUEST, limit=0.18):
    return asyncio.run(
        server.heygen_submit(
            request, key, "User explicitly approved this five second clip", limit
        )
    )


def test_estimate_and_capabilities(api):
    assert asyncio.run(server.heygen_estimate(REQUEST))["estimated_usd"] == "0.180"
    with pytest.raises(ValueError, match="capabilities"):
        asyncio.run(server.heygen_estimate(REQUEST.model_copy(update={"duration": 6})))
    assert all(call.method == "GET" for call in api[0])


@pytest.mark.parametrize(
    "field,value",
    [
        ("duration", 4),
        ("duration", 16),
        ("resolution", "1080p"),
        ("prompt", ""),
        ("aspect_ratio", "bad"),
    ],
)
def test_request_validation(field, value):
    with pytest.raises(ValidationError):
        server.VideoRequest.model_validate({**REQUEST.model_dump(), field: value})


def test_budget_blocks_post(api):
    with pytest.raises(ValueError, match="exceeds"):
        submit(limit=0.01)
    assert all(call.method != "POST" for call in api[0])


def test_submission_payload_and_deduplication(api):
    first = submit()
    second = submit()
    assert first == second
    posts = [call for call in api[0] if call.method == "POST"]
    assert len(posts) == 1
    payload = json.loads(posts[0].content)
    assert payload["model"] == server.MODEL
    assert "generate_audio" not in payload
    assert payload["duration"] == 5
    assert "seed" not in payload
    assert server.heygen_receipt("test-v1")["job"]["id"] == "job-test"
    with pytest.raises(ValueError, match="different parameters"):
        submit(request=REQUEST.model_copy(update={"prompt": "Different"}))


@pytest.mark.parametrize("failure", ["timeout", "http"])
def test_unknown_submission_never_retries(api, failure):
    if failure == "timeout":
        api[1]["timeout"] = True
    else:
        api[1]["post"] = 503
    with pytest.raises(RuntimeError):
        submit()
    assert submit()["state"] == "UNKNOWN"
    assert len([call for call in api[0] if call.method == "POST"]) == 1


def test_missing_key_never_posts(api, monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY")
    with pytest.raises(ValueError, match="OPENROUTER_API_KEY"):
        submit()
    assert not api[0]


def test_rejected_request_preserves_redacted_diagnostics(api):
    api[1]["post"] = 400
    with pytest.raises(server.OpenRouterError, match="HTTP 400") as raised:
        submit()
    assert "test-placeholder" not in str(raised.value)
    assert "token=secret" not in str(raised.value)
    record = server.heygen_receipt("test-v1")
    assert record["state"] == "UNKNOWN"
    assert record["error"]["http_status"] == 400
    assert "diagnostic" in record["error"]["detail"]
    assert "test-placeholder" not in json.dumps(record)
    assert submit() == record
    assert len([call for call in api[0] if call.method == "POST"]) == 1


def test_status_does_not_expose_signed_urls(api):
    result = asyncio.run(server.heygen_status("job-test"))
    assert result["usage"]["cost"] == 0.09
    assert "private.example" not in json.dumps(result)


def test_download_and_no_overwrite(api):
    result = asyncio.run(server.heygen_download("job-test", "wedding.mp4"))
    assert Path(result["path"]).read_bytes()[4:8] == b"ftyp"
    assert len(result["sha256"]) == 64
    with pytest.raises(ValueError, match="exists"):
        asyncio.run(server.heygen_download("job-test", "wedding.mp4"))


def test_download_redirect_strips_secret(api):
    api[1]["redirect"] = True
    asyncio.run(server.heygen_download("job-test", "redirect.mp4"))
    assert api[0][-1].url.host == "cdn.example.org"
    assert "authorization" not in api[0][-1].headers


def test_download_requires_completed_and_real_mp4(api):
    api[1]["status"] = "pending"
    with pytest.raises(ValueError, match="not completed"):
        asyncio.run(server.heygen_download("job-test"))
    api[1]["status"] = "completed"
    api[1]["invalid_media"] = True
    with pytest.raises(ValueError, match="not an MP4"):
        asyncio.run(server.heygen_download("job-test"))
    assert not (api[2] / "outputs/video.mp4").exists()


def test_stdio_discovery_and_schema(tmp_path):
    async def check():
        env = dict(os.environ)
        env.pop("OPENROUTER_API_KEY", None)
        env["HEYGEN_DATA_DIR"] = str(tmp_path)
        params = StdioServerParameters(
            command=sys.executable, args=["-m", "heygen_video_mcp.server"], env=env
        )
        async with (
            stdio_client(params) as (read, write),
            ClientSession(read, write) as session,
        ):
            await session.initialize()
            tools = await session.list_tools()
            assert len(tools.tools) == 5
            invalid = await session.call_tool(
                "heygen_submit",
                {
                    "request": {"prompt": "test"},
                    "request_key": "../escape",
                    "approval": "approved",
                    "max_cost_usd": -1,
                },
            )
            assert invalid.isError
            missing = await session.call_tool(
                "heygen_submit",
                {"request": {"prompt": "test"}, "request_key": "sample"},
            )
            assert missing.isError
            assert not (tmp_path / "jobs").exists()

    asyncio.run(check())


def test_example_error_exits_cleanly_without_credentials(tmp_path):
    env = dict(os.environ)
    env.pop("OPENROUTER_API_KEY", None)
    env["HEYGEN_DATA_DIR"] = str(tmp_path)
    result = subprocess.run(
        [sys.executable, "examples/client.py", "--status", "job-test"],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 1
    assert "OPENROUTER_API_KEY" in result.stdout
    assert "ExceptionGroup" not in result.stderr
