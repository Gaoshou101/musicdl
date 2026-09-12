# Phase 5 AI-assisted Ranking and Classification Design

## Goal

Add an optional OpenAI-compatible advisory layer that can disambiguate and
re-rank already deterministic search results and suggest one of the four media
language categories. The existing deterministic search and Phase 4 media
safety behavior remain authoritative whenever AI is disabled, unavailable, or
invalid.

## Requirements

- FR-011: configure an OpenAI-compatible base URL, API key, model, and timeout.
- FR-011: valid structured responses can change candidate order and recommend a
  language category.
- FR-006: the only accepted language values are `华语`, `欧美`, `日韩`, and
  `未知`.
- OPS-001: API keys, authorization data, provider response bodies, and raw
  exception text never appear in events or logs.
- AI never starts a download, selects a candidate on the user's behalf, changes
  candidate content, constructs filesystem paths, or bypasses confirmation and
  media validation.

## Architecture

Create a focused `musicdl.ai` package with three boundaries:

1. Immutable request, advice, event, and stable-error models.
2. An OpenAI-compatible HTTP client responsible only for one bounded
   `/chat/completions` request and strict response extraction.
3. Advisory orchestration that converts candidates into opaque local tokens,
   validates provider advice, and falls back to deterministic inputs.

The provider is called only after `search_sources()` has completed its existing
normalization, deduplication, and deterministic ordering. Re-ranking therefore
cannot restore rejected candidates or modify source results. Language advice is
requested independently and must pass both the AI response validator and Phase
4 `normalize_language()` before it can be supplied to the confirmed download
flow.

## Configuration

Add `AISettings` to `AppSettings`:

- `enabled`: defaults to `False`.
- `base_url`: defaults to `https://api.openai.com/v1` and accepts only HTTP or
  HTTPS URLs.
- `api_key`: optional `SecretStr` while disabled; required and non-empty while
  enabled.
- `model`: optional while disabled; required, stripped, and bounded while
  enabled.
- `timeout`: finite, positive, and bounded to at most 60 seconds.
- `max_candidates`: a bounded positive integer limiting metadata disclosure and
  response size.

The API key is used only to construct the authorization header. Configuration
representations and structured events use existing secret-redaction behavior.

## Provider Protocol

The client sends `POST <base_url>/chat/completions` with bearer authorization,
the configured model, deterministic sampling (`temperature: 0`), and a JSON
response-format request. No automatic retries occur; callers retain predictable
latency and immediately use fallback on failure.

Candidate ranking requests include only:

- an opaque per-request token generated from list position;
- normalized title, artist, optional album, duration, bitrate, and format.

They exclude source URLs, media bytes, credentials, sessions, headers, and
download capabilities. Language requests include only normalized title, artist,
and optional album. Provider response bodies and raw errors are never recorded.

The accepted JSON objects are:

```json
{"ordered_tokens":["candidate-2","candidate-1"]}
```

and:

```json
{"language":"华语"}
```

The outer OpenAI response must contain exactly one usable
`choices[0].message.content` value containing a JSON object. Markdown fences,
free-form prose, missing fields, unexpected fields, wrong types, and excessive
response size are invalid.

## Re-ranking Semantics

The AI request covers at most `max_candidates` candidates from the front of the
deterministic result. A valid response must contain every supplied opaque token
exactly once, with no duplicates or unknown tokens. The advised prefix replaces
only that same prefix; candidates beyond the disclosure limit retain their
original relative order.

Any invalid advice returns the original `SearchResult` unchanged, including its
version. A valid re-order produces a new `SearchResult` with unchanged statuses
and a version digest derived from the newly ordered public candidate data.
No advice object carries a download instruction or selected candidate.

## Language Semantics

A valid provider language is returned as an advisory value. Missing, malformed,
or unrecognized values yield the deterministic fallback supplied by the caller,
normalized through Phase 4 `normalize_language()`. The default fallback is
`未知`.

The AI layer has no filesystem access and does not call `download_candidate()`.
The confirmed-download caller may pass the returned language advice into the
existing Phase 4 API; Phase 4 remains the final path and category authority.

## Failure and Observability

Expected failures use stable codes such as `disabled`, `timeout`,
`provider_error`, `invalid_response`, and `invalid_advice`. The public advisory
result states whether AI was applied and contains the stable code when it was
not. Structured events contain operation, status, and stable error code only;
they exclude prompts, response bodies, authorization values, base URLs, and raw
exception text.

Cancellation is not converted into fallback: `CancelledError` propagates so
application shutdown and request cancellation remain correct.

## Testing

Use strict TDD and an injected `httpx` transport or in-memory fake transport.
Tests cover:

- configuration defaults, validation, and secret redaction;
- exact HTTP path and safe request fields without exposing the API key;
- valid full-prefix re-ranking and unchanged suffix/status values;
- valid language advice and normalization;
- disabled AI without a network call;
- timeout, provider error, malformed JSON, non-structured content, oversized
  content, missing fields, unexpected fields, duplicate/unknown/missing tokens,
  and invalid language;
- byte-for-byte deterministic ranking/version fallback on every ranking failure;
- four-category fallback on every classification failure;
- cancellation propagation and event redaction;
- regression tests proving the AI module cannot invoke download behavior.

Focused Phase 5 tests and the complete repository suite must finish with exit
status zero and warnings treated as errors. The real external AI provider is not
required for automated acceptance; compatibility is verified against the
documented OpenAI HTTP shape through a controlled mock endpoint.

## Scope Boundaries

Phase 5 does not add an admin UI, persist prompts or provider responses, analyze
media bytes, implement provider-specific extensions, retry requests, stream
responses, or allow AI to choose/download a candidate. Those capabilities are
outside the approved requirement and would require a separate design decision.

## Rollback

Revert the Phase 5 commits. Because the feature is disabled by default and does
not migrate stored data or own files, rollback requires no data conversion or
media cleanup.
