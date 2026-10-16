# Vue administration panel migration

The Vue panel preserves the existing administration features while improving
layout, feedback and small-screen navigation. The backend API and deployment
remain the source of truth for authentication, permissions and configuration.

## Design contract

- Keep the musicdl brand assets, four accent palettes and light/dark appearance.
- Keep the existing navy/frost surface tokens: midnight `#061026`, navy `#122040`,
  frost `#F4F9FF`, blue `#2C5EB8`, and the existing success/warning/error ramps.
- Use the existing local system/CJK font stack; do not require external fonts.
- Make music search a visible navigation destination. Keep content left aligned,
  consistent page headings, clear form groups and visible operation results.
- Use an expandable navigation on small screens and keep keyboard focus visible.
- Preserve reduced-motion and reduced-transparency preferences.
- Read the previous Zustand-shaped `tgmusic.theme` value when initializing the
  Vue theme store, so upgrading does not reset a user's appearance.

## Route and feature contract

| Route | Features that must survive the migration |
| --- | --- |
| `/` | Session restoration, username persistence, remember-login option, login errors, theme control |
| `/dashboard` | Runtime overview, health/source summaries, recent-activity and search entries |
| `/dashboard/search` | `q` query parameter, source results and diagnostics, size probes, server-side download and feedback |
| `/dashboard/downloads` | History, pagination, detail refresh, running/completed/failed states and admission metrics |
| `/dashboard/sources` | Source CRUD, enable/priority/timeout, import/preview/install and permissions, health/lossless/diagnostic operations |
| `/dashboard/bots` | Bot CRUD and settings, Telegram phone/code/2FA login, status and logout |
| `/dashboard/config` | Typed fields, secret handling, deployment-value reset, save/validation, activation information and connection probes |
| `/dashboard/health` | Health and diagnostic evidence, refresh and actionable failures |
| `/dashboard/logs` | Events, audit and process-log tabs, filtering, cursor/generation reset and polling controls |
| `/dashboard/settings` | Forced credential change, validation, username/password updates and session invalidation |

## Architecture

The frontend uses Vue 3, TypeScript, Vite, Vue Router, Pinia and Tailwind CSS 4.
The framework-independent API, formatting and source-import modules retain their
existing public contracts. Vue composables replace React hooks. Global stores
contain cross-page state; page-specific data stays with its page or feature.

The service continues to host the built files on the same origin as `/admin/*`.
Only known frontend routes may fall back to the SPA entry document. Unknown
pages, missing assets and API routes must keep their real HTTP status. The
runtime image carries the built panel and does not run a frontend Node process.

## Migration sequence

1. Build the candidate in `web-vue/`, using `web/` as the behavior reference.
2. Deliver the application shell, session guard, theme migration and polling
   lifecycle before wiring feature pages.
3. Migrate all pages with disjoint file ownership; preserve operation semantics
   and provide meaningful behavior tests for risky transitions.
4. Adapt static hosting, Docker, CI and the existing regression scripts.
5. After all feature work settles, replace `web/` with the candidate. Retain the
   old frontend in Git history rather than shipping both frameworks.
6. Run the acceptance checks, obtain independent review, commit and push the
   work branch. A numbered release and production deployment are separate work.

## Acceptance checks

- Vue component/template type checking and production build complete.
- Meaningful frontend regressions retain the existing log cursor, search probe,
  channel metrics, import and diagnostic-polling coverage.
- Auth/session, forced credential change, old theme persistence and polling
  cancellation/resume semantics are exercised.
- Python panel-serving tests cover direct/deep links, query strings, API
  precedence, missing assets, unknown paths and unsupported methods.
- Real-service browser smoke covers search, download/history, source import,
  configuration, session behavior, navigation and responsive layout. Telegram
  interaction tests use fixtures and never initiate real account authentication.
- Existing API contracts, packaging checks and relevant backend tests pass.
- The settled diff and completed verification evidence receive independent Sol
  review before the work branch is committed and pushed.

## Rollback

No stored backend data is migrated by the frontend change. Roll back a deployment
to the previous complete application image; roll back source changes with the
reviewed migration commit. Keep API and static assets from the same image/commit.
Do not roll back only HTML while leaving mismatched hashed assets in place.

## Verification record

- The production Vue build and template/type checking passed. The full frontend
  run passed 18 suites and 72 tests; the final pagination-feedback adjustment also
  passed the seven service-log regression tests.
- The real-browser smoke passed against the production `web/dist` files and a
  disposable FastAPI service, covering authenticated searches and downloads,
  history/detail states, source installation, configuration persistence, theme
  restoration, forced credential changes, session rotation, logout and a
  390-pixel navigation/overflow check. Telegram responses were fixture-driven.
- The framework-independent API client passed 33 local and 18 unavailable-store
  contract checks against disposable real FastAPI applications. No remote
  service or real Telegram account was used.
- All 12 files under `web/public/` retain their original contents. The README
  files, release version and Compose topology are unchanged.
- The Windows backend suite reported 2,139 passed, 113 skipped and 9 failed.
  Those failures were checked separately: the release module and admission deadline
  case passed together (114 tests) with process-local Git signing disabled, and
  the path-separator assertion failed identically in an isolated original-HEAD
  checkout. No production behavior was changed to accommodate those fixtures.
- Docker, Redis and Deno checks requiring unavailable local runtimes remain
  skipped locally. GitHub Actions runs the Linux suite, production image build
  and browser smoke from the image's exported panel.
