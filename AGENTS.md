# Agent instructions

- Implement fixes and run targeted validation; do not start unsolicited review chains.
- Use `uv sync --locked`, `uv run pytest`, and `uv run ruff check .`.
- Never commit keys, `.env`, local receipts, private configuration, or signed download URLs.
- The only generation route is `heygen/heygen-video-1`. No fallback models.
- Before every paid generation, disclose model, purpose, rate and estimate, and obtain explicit user consent. An agent must not manufacture the approval field.
- `max_cost_usd` is an estimate guard, not a provider billing cap.
- Persist request keys/job IDs. UNKNOWN submissions require reconciliation, not automatic retries.
- Never call `google/gemini-4-argon` or `fish-audio/transcribe-1-pro`.
- Use Codex for verification; do not invoke external models for review. Unavailable listening/ASR is NOT_RUN.
- Keep technical decoding and creative review distinct. A prepared prompt is not a finished video.
