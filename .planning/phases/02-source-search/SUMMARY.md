# Phase 2 — Source abstraction and deterministic search

## Status

Implementation and automated verification completed on 2026-09-12; this
summary was written on 2026-09-16 against the shipped code, after the phase's
deliverables had been re-read on `origin/main`. The phase never had a recorded
summary, so the claims below are derived from the implementation and its tests
rather than from a contemporaneous note.

## Exit criteria and evidence

| Exit criterion | Implementation and test evidence |
| --- | --- |
| Two controlled sources return unified candidates concurrently | `src/musicdl/sources/search.py`: `search_sources`, `_invoke`; `src/musicdl/sources/registry.py`: `MusicSource`, `SourceEntry`, `SourceRegistry`; `tests/unit/test_source_search.py::test_sources_overlap_timeout_and_bad_source_isolated` |
| Ordering is stable across repeated runs | `src/musicdl/sources/search.py`: `ordering`, `quality_key`, `search_result_version`; `tests/unit/test_source_search.py::test_dedup_sort_and_version_are_completion_order_independent` and `::test_reversed_completion_order_has_identical_candidates_version_and_statuses` |
| The WeCom output carries version-identifying fields and the size | `src/musicdl/wecom/results.py`: `format_results` renders `{source_id}@{source_version}` plus an IEC size through `_size`; `tests/unit/test_wecom_results.py` |
| One slow source does not block the others | `src/musicdl/sources/search.py`: per-source `timeout` inside `_invoke`, isolated as a `SourceStatus` rather than an exception; `tests/unit/test_source_search.py::test_sources_overlap_timeout_and_bad_source_isolated` and `::test_mismatched_candidate_isolated_and_error_safe` |

The unified candidate model is `src/musicdl/sources/models.py::Candidate` with
`normalize_text`, `canonical_version_key`, and `public_representation`; the
cross-source identity and the ordered public representation are what
`search_result_version` hashes, so the version changes exactly when the public
candidate set changes (`tests/unit/test_source_search.py::test_normalized_query_exact_match_and_material_digest_change`).

## Delivered behavior

- Bounded `Candidate` fields with strict types, optional blank values
  normalized to `None`, and normalized public text
  (`tests/unit/test_source_models.py`).
- An explicit enabled/priority registry that rejects duplicate or invalid
  identity and caps the number of sources
  (`tests/unit/test_source_registry.py`, `tests/unit/test_plugin_source.py::test_source_registry_registration`).
- Concurrency with independent per-source timeouts; malformed, mismatched,
  failed, and timed-out sources are isolated into status records that never
  carry the query, payload, or exception text
  (`tests/unit/test_source_search.py::test_mismatched_candidate_isolated_and_error_safe`).
- Cross-source deduplication that keeps quality-distinct versions apart while
  collapsing the same version, and deterministic selection between duplicates
  (`::test_cross_source_same_version_collapses_but_quality_versions_remain`,
  `::test_duplicate_identity_selects_richer_candidate`).
- Deterministic ranking by query relevance first, then quality, then registry
  priority (`::test_exact_query_relevance_outranks_lexical_order`,
  `::test_quality_sort_prefers_higher_mp3_bitrate_over_lexical_title`,
  `::test_priority_resolves_equal_relevance_and_version`).
- Bounded WeCom result formatting with `max_items` and `max_bytes` enforced
  (`::test_source_result_boundaries_and_max_results_argument`,
  `::test_public_search_result_version_matches_search_output`).

## Commits

- `fbb3053` feat: add deterministic multi-source search (2026-09-12)
- `4d8f634` Harden source validation and bound search result formatting (2026-09-12)

## Verification evidence

- Focused Phase 2 suite (`tests/unit/test_source_models.py`,
  `tests/unit/test_source_registry.py`, `tests/unit/test_source_search.py`,
  `tests/unit/test_plugin_source.py`): 29 passed, exit 0.
- Phase 2 is exercised end to end by the vertical slice in
  `tests/integration/test_plugin_download_vertical_slice.py`, which searches two
  fixture sources concurrently, deduplicates to the primary candidate, and
  pins `runner.calls` and the published path.
- Repository-wide suite on the merged `main`: 937 passed, 25 skipped in CI, and
  892 passed / 70 skipped against the live test-server Redis.

## Known limits

- The phase plan deferred runtime wiring, and that wiring landed later: the
  registry is assembled in `src/musicdl/app.py::_build_runtime`, not here.
- No real source adapter exists beyond the fixture plugins; every Phase 2
  source in use today either is a plugin or wraps one.
- `deferred`: Telegram-sourced candidates exist as bot adapters but are not
  registered into the runtime registry (see the release gap list in
  `.planning/ROADMAP.md`).
