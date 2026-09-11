\# impl-plan.md — Automated Documentation Sync (v1) Implementation Plan



This plan is \*\*strictly dependency-ordered\*\* (later tasks are blocked until prerequisites complete) and incorporates design-review improvements:

\- Versioned policy/config: `.github/docs-sync.yml` + schema validation + workflow boot validation (\*\*Task 1.2, 2.0\*\*)

\- Run correlation IDs and structured observability (\*\*Task 1.4, 3.0\*\*)

\- LLM strict output schema validation (\*\*Task 1.3, 4.3\*\*)

\- Concurrency + atomic idempotency using \*\*merge\_commit\_sha\*\* (\*\*Task 2.2, 3.2\*\*)

\- Large-diff handling via deterministic \*\*chunking/summarization\*\* (\*\*Task 3.5\*\*)

\- Adjusted \*\*branch-lock lifecycle\*\*: lock branch created only after `UPDATE\_NEEDED` decision (prevents branch clutter) (\*\*Task 3.2 placement + 5.4\*\*)



\---



\## Phase 1 — Repo \& Contracts Foundations (must be first)



\### 1.1 Create repo structure + core module skeleton

\*\*Description:\*\* Establish code layout, entrypoint, shared utilities, and test scaffolding.



\*\*Acceptance criteria:\*\*

\- Repository contains a clear structure (example):

&#x20; - `src/` (implementation)

&#x20; - `tests/`

&#x20; - `.github/workflows/`

&#x20; - `docs/`

\- A runnable entrypoint exists (e.g., `python -m doc\_sync` or `node src/index.js`) that prints a stubbed run context.

\- Basic CI (lint/test) skeleton exists or placeholder documented.



\*\*Blocked tasks:\*\* All subsequent tasks depend on this.



\---



\### 1.2 Add `.github/docs-sync.yml` configuration + schema validation

\*\*Description:\*\* Add a versioned configuration file and validate it at runtime.



\*\*Acceptance criteria:\*\*

\- `.github/docs-sync.yml` exists with at least:

&#x20; - `allowed\_paths`: `README.md`, `docs/\*\*`

&#x20; - `branch\_prefix`: `docs-sync-`

&#x20; - `pr\_title`: `docs: automated documentation synchronization`

&#x20; - `labels`: `\["documentation","automated"]`

&#x20; - `max\_diff\_files`, `max\_diff\_lines`, `max\_prompt\_chars` (or tokens)

&#x20; - `link\_policy`: `{ internal: "block", external: "warn|block", external\_timeout\_seconds: N }`

&#x20; - `lock\_branch\_policy`: `{ create\_lock\_only\_if\_update\_needed: true }`

&#x20; - `cleanup\_policy`: `{ delete\_lock\_branch\_on\_noop: true, delete\_lock\_branch\_on\_failure\_before\_pr: true }`

\- Config validation is implemented (Pydantic/Zod) and fails fast with a clear error message if invalid.



\*\*Blocked tasks:\*\* Phase 2+ must rely on validated config.



\---



\### 1.3 Define the LLM output contract (strict schema)

\*\*Description:\*\* Define a machine-parseable LLM response schema for patch generation.



\*\*Acceptance criteria:\*\*

\- A documented schema exists and a validator is implemented.

\- Schema supports per-file edits, e.g.:

&#x20; - `\[{ "path": "README.md", "patch\_unified": "..." }, ...]` OR `new\_content`

\- Validator enforces:

&#x20; - paths are unique

&#x20; - paths match allowlist

&#x20; - max patch size per file and total output size

&#x20; - required fields present and correct types



\*\*Blocked tasks:\*\* Patch generation and application depend on this.



\---



\### 1.4 Define observability contract (log schema + reason codes)

\*\*Description:\*\* Standardize structured logging fields, reason codes, and summary output.



\*\*Acceptance criteria:\*\*

\- A log schema is defined (in code docs or `design-review.md`) including:

&#x20; - `run\_id`, `merge\_commit\_sha`, `source\_pr\_number` (if available), `trigger\_type`

&#x20; - `status` (`SUCCESS|NO\_OP|FAILURE`)

&#x20; - `reason\_code` (e.g., `NO\_DOC\_IMPACT`, `PATCH\_POLICY\_BLOCKED`, `LLM\_TIMEOUT`, `PATCH\_APPLY\_FAILED`)

&#x20; - `docs\_sync\_pr\_url` when created

\- Logging library outputs JSON logs by default.

\- A run summary template is defined for `GITHUB\_STEP\_SUMMARY`.



\*\*Blocked tasks:\*\* Used by all runtime phases; implement early.



\---



\## Phase 2 — Workflow Trigger \& Boot Validation (platform integration)



\### 2.0 Workflow boot: load + validate `.github/docs-sync.yml` before any writes

\*\*Description:\*\* Ensure the workflow loads repo config and validates schema \*\*before\*\* any GitHub write operations.



\*\*Acceptance criteria:\*\*

\- On every run, config is read from the repository and validated first.

\- If config is missing/invalid, workflow fails with:

&#x20; - clear error

&#x20; - no branch creation

&#x20; - no commits/PRs



\*\*Blocked tasks:\*\* All GitHub write tasks (branch/commit/PR) depend on this.



\---



\### 2.1 Create GitHub Actions workflow with correct triggers

\*\*Description:\*\* Implement GitHub Actions workflow triggers:

\- `pull\_request` closed with `merged == true` into `main`

\- `workflow\_dispatch`



\*\*Acceptance criteria:\*\*

\- Workflow runs on PR merge to `main`.

\- Manual dispatch runs and uses the same code path.

\- Workflow extracts \*\*merge\_commit\_sha\*\* from the event payload:

&#x20; - `pull\_request.merge\_commit\_sha` (not `github.sha`).



\*\*Blocked tasks:\*\* Runtime pipeline execution depends on workflow correctness.



\---



\### 2.2 Add workflow concurrency keyed by `merge\_commit\_sha`

\*\*Description:\*\* Prevent parallel runs from processing the same merge commit concurrently.



\*\*Acceptance criteria:\*\*

\- Workflow uses `concurrency.group = docs-sync-<merge\_commit\_sha>`.

\- Two webhook deliveries or manual + webhook for the same merge commit result in only one active run (no duplicate PRs).



\*\*Blocked tasks:\*\* Supports correctness of idempotency (Phase 3+).



\---



\### 2.3 Establish GitHub authentication (least privilege)

\*\*Description:\*\* Configure GitHub App installation token (preferred) or minimal PAT for capstone.



\*\*Acceptance criteria:\*\*

\- Credentials stored in GitHub Secrets.

\- Documented minimum permissions:

&#x20; - Contents: Read/Write

&#x20; - Pull requests: Read/Write

&#x20; - Metadata: Read

\- A smoke test step can call GitHub API to read repo metadata.



\*\*Blocked tasks:\*\* All GitHub API operations depend on this.



\---



\## Phase 3 — Runtime Context, Diff Retrieval, and Pre-LLM Guardrails



\### 3.0 Run context initialization + correlation ID propagation

\*\*Description:\*\* Generate a `run\_id`, capture event context, and propagate to logs/summaries.



\*\*Acceptance criteria:\*\*

\- Every log line includes `run\_id` and `merge\_commit\_sha`.

\- `GITHUB\_STEP\_SUMMARY` includes:

&#x20; - `run\_id`, `merge\_commit\_sha`, trigger type, status, and reason code.

\- `run\_id` is stable within a run and passed through all components.



\*\*Blocked tasks:\*\* All downstream tasks should emit structured logs with correlation.



\---



\### 3.1 Implement GitHub API client wrapper (REST-first)

\*\*Description:\*\* Create a thin GitHub client layer for:

\- fetch merged PR metadata + changed files/diffs

\- fetch file contents (docs + relevant code snippets if needed)

\- create branch ref, commit, create PR, add labels

\- check for existing branches/PRs (idempotency support)



\*\*Acceptance criteria:\*\*

\- Client methods exist with unit tests using mocked HTTP responses.

\- Retries are implemented for transient GitHub failures (max 3, exponential backoff).

\- Handles GitHub rate limiting signals (e.g., secondary rate limit) with controlled backoff/fail behavior.



\*\*Blocked tasks:\*\* 3.3+ and Phase 5 depend on this.



\---



\### 3.3 Implement diff \& context extractor (bounded)

\*\*Description:\*\* Retrieve changed files and diffs and extract initial context needed for detection and generation.



\*\*Acceptance criteria:\*\*

\- Given merge event context, returns:

&#x20; - changed files list (path, status)

&#x20; - bounded diff summary (respects `max\_diff\_files`, `max\_diff\_lines`)

&#x20; - current contents of in-scope docs (`README.md`, `/docs/\*\*/\*.md` subset)

\- If caps are exceeded, returns a truncated/structured summary and flags `context\_truncated=true`.



\*\*Blocked tasks:\*\* 3.4/3.5 and Phase 4 depend on this.



\---



\### 3.4 Implement secret scrubber + data minimizer (pre-LLM)

\*\*Description:\*\* Scrub secret-like patterns from diffs/docs and minimize payload.



\*\*Acceptance criteria:\*\*

\- Scrubber runs on all LLM inputs and removes/redacts:

&#x20; - token/key patterns

&#x20; - credential-like strings

\- Logs do not contain raw diffs or raw LLM payloads.

\- Unit tests demonstrate scrubbing behavior and no sensitive leakage in logs.



\*\*Blocked tasks:\*\* 4.2/4.3 must use scrubbed context.



\---



\### 3.5 Implement diff chunker/summarizer (deterministic)

\*\*Description:\*\* Ensure large PRs can be handled within LLM context limits and SLA via chunking/summarization.



\*\*Acceptance criteria:\*\*

\- Deterministically transforms raw diff into:

&#x20; - per-file summaries and/or categorized change summaries

&#x20; - capped chunks sized to `max\_prompt\_chars` (or tokens)

\- Provides a stable output given the same inputs (no randomness).

\- If still oversized after chunking, fails gracefully with a clear reason code (e.g., `CONTEXT\_TOO\_LARGE`) and no GitHub writes.



\*\*Blocked tasks:\*\* 4.3 prompt builder depends on chunked/summarized context.



\---



\## Phase 4 — Decisioning + LLM Integration + Patch Application



\### 4.1 Implement “outdated doc detection” (deterministic)

\*\*Description:\*\* Decide `UPDATE\_NEEDED` vs `NO\_OP` with a deterministic reason code.



\*\*Acceptance criteria:\*\*

\- Produces one of:

&#x20; - `NO\_OP` with reason (e.g., `NO\_DOC\_IMPACT`)

&#x20; - `UPDATE\_NEEDED` with reason (e.g., `PUBLIC\_API\_CHANGED`)

\- If `NO\_OP`, workflow exits successfully and writes a summary.

\- No GitHub writes occur in `NO\_OP` path.



\*\*Blocked tasks:\*\* 3.2 lock branch creation is gated on `UPDATE\_NEEDED` (see next task).



\---



\### 3.2 Implement atomic idempotency lock (branch-create lock) — \*\*moved here by lifecycle policy\*\*

\*\*Description:\*\* Only after `UPDATE\_NEEDED`, acquire atomic lock by creating branch ref `docs-sync-<merge\_commit\_sha>`.



\*\*Acceptance criteria:\*\*

\- On first `UPDATE\_NEEDED` run for a commit:

&#x20; - branch is created successfully off `main`

\- On concurrent/duplicate run for same commit:

&#x20; - branch create returns “already exists”; run exits cleanly with reason code (e.g., `ALREADY\_PROCESSED\_OR\_IN\_PROGRESS`)

\- This branch-create step is the atomic lock (prevents duplicate PRs).



\*\*Blocked tasks:\*\* 4.2+ must not proceed without lock acquisition.



\---



\### 4.2 Implement LLM client (secure proxy)

\*\*Description:\*\* Implement LLM invocation with timeouts, retries, and payload size enforcement.



\*\*Acceptance criteria:\*\*

\- Requests go through the secure proxy endpoint.

\- Retries with exponential backoff occur for transient network/5xx errors (max 3).

\- Payload-size enforcement rejects oversize requests before sending.

\- Observability: logs include latency, status, retry count (no raw content).



\*\*Blocked tasks:\*\* 4.3 depends on this.



\---



\### 4.3 Implement prompt builder + schema-constrained output parsing/validation

\*\*Description:\*\* Construct prompts that require structured patch outputs and parse/validate responses.



\*\*Acceptance criteria:\*\*

\- Prompt includes:

&#x20; - explicit allowed paths

&#x20; - instruction to return strictly valid JSON in the defined schema

&#x20; - instruction to avoid adding out-of-scope links/domains if policy requires

\- Response is parsed and validated:

&#x20; - rejects invalid JSON

&#x20; - rejects paths outside allowlist

&#x20; - rejects oversized patches

&#x20; - rejects duplicate path entries

\- On validation failure: run fails safely with reason code (e.g., `LLM\_OUTPUT\_INVALID`) and no commit/PR.



\*\*Blocked tasks:\*\* 4.4 depends on valid patches.



\---



\### 4.4 Implement patch applier (deterministic)

\*\*Description:\*\* Apply patches to a working directory with strict scope controls.



\*\*Acceptance criteria:\*\*

\- Applies patches cleanly to target files.

\- Produces a list of modified files and a computed diff.

\- Fails with clear reason code if patch cannot be applied (e.g., `PATCH\_APPLY\_FAILED`).

\- Ensures only Markdown targets are touched.



\*\*Blocked tasks:\*\* Phase 5 policy gate requires final file changes.



\---



\## Phase 5 — Policy Gate, Formatting/Links, Commit \& PR Creation



\### 5.1 Implement validation \& policy gate (required)

\*\*Description:\*\* Enforce security/repo guardrails \*\*before\*\* committing.



\*\*Acceptance criteria:\*\*

\- \*\*Path allowlist enforcement (hard fail):\*\*

&#x20; - modified files must be `README.md` or under `/docs/\*\*/\*.md`

\- \*\*Secret scan (hard fail):\*\*

&#x20; - use a scanner (preferred) or hardened regex to detect secret-like strings in generated output

\- Optional but recommended:

&#x20; - URL/domain allowlist for newly introduced external links

\- On failure: run aborts, branch cleanup behavior follows policy (Task 5.4).



\*\*Blocked tasks:\*\* 5.2/5.3 cannot proceed unless policy gate passes.



\---



\### 5.2 Implement Markdown formatter + link update/check

\*\*Description:\*\* Run formatting cleanup and link checks according to config policy.



\*\*Acceptance criteria:\*\*

\- Markdown formatting produces stable output (idempotent on re-run).

\- Link policy enforced:

&#x20; - internal links: blocking

&#x20; - external links: warn or block per config with timeouts

\- Failures produce clear reason codes and appear in run summary.



\*\*Blocked tasks:\*\* 5.3 depends on formatted output.



\---



\### 5.3 Implement commit writer + PR creator + labels

\*\*Description:\*\* Commit changes to the lock branch and create the standardized PR.



\*\*Acceptance criteria:\*\*

\- Commit is created on `docs-sync-<merge\_commit\_sha>`.

\- PR is created targeting `main` with:

&#x20; - Title: `docs: automated documentation synchronization`

&#x20; - Labels: `documentation`, `automated`

\- PR body includes:

&#x20; - source PR reference (if available)

&#x20; - `merge\_commit\_sha`

&#x20; - brief summary of docs changed (file list)

&#x20; - warnings if non-blocking checks failed (if configured)

\- No direct commits to `main`.



\*\*Blocked tasks:\*\* Phase 6 E2E tests depend on this.



\---



\### 5.4 Implement deterministic branch-lock lifecycle + cleanup policy

\*\*Description:\*\* Ensure lock branch behavior is deterministic and does not cause clutter or block legitimate retries.



\*\*Acceptance criteria:\*\*

\- Because lock branches are created \*\*only if `UPDATE\_NEEDED`\*\*, no-op runs do not create lock branches.

\- On \*\*failure before PR creation\*\*, cleanup follows config:

&#x20; - default: delete lock branch to allow manual re-run

\- On \*\*PR created\*\*, lock branch is retained as PR head branch.

\- Cleanup actions are logged with `run\_id` and appear in summary.



\*\*Blocked tasks:\*\* Recommended before Phase 6 to avoid flaky E2E behavior.



\---



\## Phase 6 — Testing, Verification, and Operational Readiness



\### 6.1 Unit tests for critical modules

\*\*Description:\*\* Unit test config parsing, correlation logging, chunker, scrubber, schema validation, patch apply, policy gate.



\*\*Acceptance criteria:\*\*

\- Tests cover edge cases:

&#x20; - invalid/missing config

&#x20; - oversize diff chunking behavior

&#x20; - invalid LLM JSON/schema

&#x20; - out-of-scope file changes

&#x20; - secret detection triggers failure

&#x20; - branch already exists idempotency path

\- Coverage target defined and met for critical modules (team-defined threshold).



\*\*Blocked tasks:\*\* Requires Phases 1–5 implementations.



\---



\### 6.2 Integration tests with mocked GitHub API + mocked LLM

\*\*Description:\*\* Simulate end-to-end pipeline behavior with deterministic fixtures.



\*\*Acceptance criteria:\*\*

\- Mocked run produces correct sequence of calls:

&#x20; - read config → fetch diffs → detect update → create lock branch → LLM → apply → validate → format → commit → PR

\- Retry paths tested:

&#x20; - GitHub transient errors

&#x20; - LLM transient timeouts

\- Rate-limit handling tested (at least one simulated 403 secondary rate limit scenario).



\*\*Blocked tasks:\*\* Depends on 3.1, 4.2, 5.3.



\---



\### 6.3 End-to-end test in a sandbox repo

\*\*Description:\*\* Validate real GitHub event flow and PR creation.



\*\*Acceptance criteria:\*\*

\- Merge to `main` triggers workflow and completes within target SLA for typical PR size.

\- Generated PR:

&#x20; - correct branch naming `docs-sync-<merge\_commit\_sha>`

&#x20; - correct title and labels

&#x20; - changes only in `README.md` and/or `/docs/\*\*/\*.md`

\- Re-running (manual dispatch) for the same merge commit does not create a duplicate PR.



\*\*Blocked tasks:\*\* Requires working workflow + auth + PR creation (Phase 2–5).



\---



\### 6.4 Observability and safety verification

\*\*Description:\*\* Verify log redaction, correlation, and summaries are correct and safe.



\*\*Acceptance criteria:\*\*

\- Logs contain `run\_id`, `merge\_commit\_sha`, status, reason codes, and timing.

\- No raw diffs, prompts, LLM responses, or secrets appear in logs or summaries.

\- `GITHUB\_STEP\_SUMMARY` includes:

&#x20; - run identifiers

&#x20; - decision

&#x20; - PR URL (if created)

&#x20; - warnings/errors summary (if any)



\*\*Blocked tasks:\*\* Requires end-to-end wiring of orchestrator and reporting.



\---



\## Blocked Tasks (explicit dependency highlights)

\- \*\*Phase 2\*\* is blocked by \*\*Phase 1 (1.1–1.2)\*\* because the workflow must run a validated codebase and config.

\- \*\*Task 2.0\*\* must complete before any tasks that can perform GitHub writes (\*\*3.2\*\*, \*\*5.3\*\*).

\- \*\*Phase 3\*\* is blocked by \*\*2.1–2.3\*\* (workflow, concurrency, and auth).

\- \*\*Task 3.5\*\* is blocked by \*\*3.3\*\* (needs extracted diff/context).

\- \*\*Phase 4\*\* is blocked by \*\*Phase 3\*\* (context + scrubbing + chunking).

\- \*\*Task 3.2 (lock branch)\*\* is blocked by \*\*4.1\*\* (`UPDATE\_NEEDED`) and \*\*3.1\*\* (GitHub client).

\- \*\*Phase 5\*\* is blocked by \*\*4.4\*\* (must have applied patches before validation/format/commit).

\- \*\*Phase 6\*\* is blocked by \*\*Phases 1–5\*\* completion.



\---

