```markdown
# architecture.md — Automated Documentation Sync (High-Level Architecture)

## 1. Architecture Overview
Automated Documentation Sync is an event-driven automation that runs when a Pull Request is merged into the `main` branch of a single GitHub repository (`capstone-copilot-automated-doc-sync`). It analyzes code changes, determines whether Markdown documentation is outdated, uses an enterprise-approved hosted LLM (via secure proxy) to generate documentation updates, applies formatting/link quality steps, and always opens a GitHub Pull Request containing the proposed changes for human review.

Key constraints from requirements:
- **Single repo (v1)**, docs scope limited to `README.md` and `/docs/**/*.md`.
- **Trigger** on **PR merged into `main`**, plus **manual dispatch** for testing.
- **LLM required** via secure proxy.
- **Always PR output**; no direct commits to `main`; no issues.
- **SLA**: target end-to-end completion within **2–5 minutes**.
- **Retries**: exponential backoff, **max 3** for transient GitHub/LLM/network errors.
- **Idempotency**: no duplicate PRs for the same merge commit hash.

Security and reliability guardrails added (per design review):
- Use **least-privilege GitHub App** credentials (preferred) instead of PATs.
- Perform **secret scrubbing/data minimization** before sending any context to the LLM.
- Enforce **strict path allowlist validation** so only in-scope files can be modified.
- Ensure **atomic idempotency** using **branch creation as a lock** + workflow concurrency.

---

## 2. High-Level Component Diagram (Mermaid)

```mermaid
flowchart LR
  dev[Developers] -->|Merge PR| gh[(GitHub Repo<br/>capstone-copilot-automated-doc-sync)]

  subgraph github[GitHub Platform]
    gh -->|PR merged event<br/>(pull_request closed=merged)| ga[GitHub Actions Workflow<br/>Trigger + Orchestration]
    ghapi[GitHub API<br/>(REST/GraphQL)]
  end

  subgraph runner[Automation Runtime]
    ga --> job[Sync Job Runner<br/>(container or hosted runner)]
    job --> orch[Doc Sync Orchestrator]

    orch --> conc[Concurrency Control<br/>(workflow-level)]
    orch --> lock[Atomic Idempotency Lock<br/>(branch-create lock)]

    orch --> diff[Diff & Context Extractor]
    diff --> scrub[Secret Scrubber + Data Minimizer]
    scrub --> det[Outdated-Doc Detector]

    det --> prompt[Prompt Builder<br/>(schema-constrained)]
    prompt --> llm[Hosted LLM via Secure Proxy]

    llm --> gen[Doc Patch Generator<br/>(schema validated)]
    gen --> gate[Validation & Policy Gate<br/>(path allowlist, URL rules, secret scan)]
    gate --> fmt[Markdown Formatter<br/>+ Link Update/Check]
    fmt --> commit[Branch/Commit Writer]
    commit --> pr[PR Creator + Labeler]
  end

  pr -->|create PR| ghapi
  diff -->|fetch diffs, files| ghapi
  lock -->|create branch ref as lock| ghapi
  commit --> ghapi

  subgraph obs[Observability]
    logs[Structured Logs (redacted)]:::obs
    summary[Run Summary (GITHUB_STEP_SUMMARY)]:::obs
  end

  orch --> logs
  orch --> summary
  ga --> logs

  classDef obs fill:#f5f5f5,stroke:#999,color:#333;
```

---

## 3. Recommended Technology Choices

### 3.1 Triggering & Orchestration
- **GitHub Actions** (primary orchestrator)
  - Event trigger: `pull_request` with `types: [closed]` and guard `merged == true` and `base.ref == "main"`.
  - Manual trigger: `workflow_dispatch`.
  - **Concurrency guardrail (required):**
    - Configure `concurrency.group` keyed by merge commit hash to avoid parallel runs generating duplicate PRs.

### 3.2 Sync Logic Runtime / Language
- **Python** (recommended for v1)
  - Suggested libraries:
    - GitHub API: `PyGithub` or `requests`
    - Retries/backoff: `tenacity`
    - Config validation/schemas: `pydantic`
- Alternative: **Node.js/TypeScript**
  - GitHub integration: `@actions/github`, `octokit`
  - Validation: `zod`
  - Retries: `p-retry`

### 3.3 GitHub Authentication (Least Privilege)
- **Preferred:** **GitHub App** installation token (short-lived) rather than PAT.
  - Recommended permissions (adjust to minimum necessary):
    - **Contents:** Read/Write (needed to create branch + commit)
    - **Pull requests:** Read/Write (needed to open PR)
    - **Metadata:** Read
- Secrets storage: **GitHub Actions Secrets** (or equivalent) only.
- Logging: never log tokens or raw headers; redact any accidental occurrences.

### 3.4 LLM Integration (Secure Proxy)
- **Hosted LLM via enterprise secure proxy** (required)
  - Enforce:
    - request timeouts
    - bounded retries (max 3)
    - request/response size limits
    - **data minimization** (send smallest viable context)
    - optional “no retention/zero log” mode if available

### 3.5 State / Idempotency Storage (Atomic Locking)
- **Primary idempotency mechanism (required):** **Branch-create lock**
  - Attempt to create branch ref: `docs-sync-<commit-hash>` off `main`.
  - If branch already exists, treat as “already processed / in progress” and:
    - exit cleanly, or
    - update existing PR branch (if policy allows; must be consistent).
- **Optional additional ledger:** GitHub-native artifacts/caches or external DB for richer audit trails.

### 3.6 Formatting & Link Quality Gates
- Markdown formatting:
  - `prettier` (Markdown) and/or `markdownlint-cli2`
- Link check/update:
  - Prefer deterministic internal link checks; external links optionally flagged with timeouts.

### 3.7 Observability
- **Structured logs (JSON)** to stdout with **redaction**.
- GitHub Actions step summary via `GITHUB_STEP_SUMMARY`.
- Track log-derived metrics: duration, outcome, retry counts, files changed.

---

## 4. End-to-End Data Flow (Step-by-Step)

1. **PR merge event occurs**
   - A PR is merged into `main`.

2. **Workflow trigger & concurrency**
   - GitHub Actions workflow starts.
   - Workflow applies **concurrency grouping** to prevent two runs for the same merge commit hash.

3. **Run initialization**
   - Orchestrator loads:
     - merge commit hash (idempotency key)
     - repository identifier
     - source PR number (if available)

4. **Atomic idempotency lock (branch-create lock)**
   - System attempts to create branch ref `docs-sync-<commit-hash>` off `main`.
   - Outcomes:
     - **Success:** lock acquired; proceed.
     - **Already exists:** treat as already processed/in-progress; exit cleanly (or optionally update PR branch based on defined policy).

5. **Diff & context extraction**
   - Fetch changed files and diffs via GitHub API.
   - Load current in-scope docs content:
     - `README.md`
     - `/docs/**/*.md` (full file or relevant excerpts)

6. **Secret scrubbing & data minimization (before LLM)**
   - Apply a **Secret Scrubber** to all candidate LLM inputs:
     - remove/replace secret-like patterns (tokens, keys, credentials)
     - redact high-risk literals and credential formats
   - Minimize context:
     - include only relevant diff hunks/summaries and doc excerpts required for the update

7. **Outdated documentation detection**
   - Decide “update needed” vs “no-op” deterministically.
   - If no update needed:
     - write run summary, release/close lock context (branch may remain unused depending on policy; recommended to delete the lock branch if no-op, see Notes).

8. **Prompt build & LLM call**
   - Prompt Builder constructs a schema-constrained request specifying:
     - allowed file paths
     - required output format (structured patches)
     - formatting/link expectations
   - LLM Client calls hosted LLM via secure proxy with retries.

9. **Patch generation and application**
   - Patch Generator validates output against strict schema.
   - Apply edits only to in-scope files.

10. **Validation & Policy Gate (required)**
   - Hard validations before commit:
     - **Path allowlist enforcement:** modified files must be exactly `README.md` or under `/docs/**/*.md`.
     - **No out-of-scope writes:** fail if any other file changes are present.
     - **Secret scan of generated content:** fail if secret-like patterns appear post-generation.
     - Optional: URL/domain allowlist (prevent malicious outbound links).

11. **Formatting and link quality steps**
   - Run Markdown formatter.
   - Update and/or validate links in modified Markdown.
   - Apply defined policy for failures (fail run vs flag in PR body).

12. **Commit and PR creation**
   - Commit changes on existing lock branch `docs-sync-<commit-hash>`.
   - Create PR targeting `main`:
     - Title: `docs: automated documentation synchronization`
     - Labels: `documentation`, `automated`

13. **Reporting**
   - Write PR URL and summary to `GITHUB_STEP_SUMMARY`.
   - Emit structured logs (redacted) including retries and outcomes.

---

## 5. Key Components & Responsibilities

### 5.1 GitHub Actions Workflow (Trigger + Orchestration)
**Responsibilities**
- Trigger on PR merged into `main`.
- Support `workflow_dispatch`.
- Provide runtime environment and required secrets.
- Enforce workflow **concurrency** keyed by merge commit hash.

### 5.2 Doc Sync Orchestrator (Main Entry Point)
**Responsibilities**
- Coordinate end-to-end execution steps.
- Maintain run context (repo, merge commit hash, timestamps).
- Enforce scope: only `README.md` and `/docs/**/*.md`.
- Produce final run outcome: success / no-op / failure.

### 5.3 Atomic Idempotency Lock (Branch-Create Lock)
**Responsibilities**
- Acquire idempotency lock by attempting to create `docs-sync-<commit-hash>` branch ref.
- Provide atomic protection against concurrent runs.
- Define consistent behavior when branch already exists (exit vs update).

### 5.4 Diff & Context Extractor
**Responsibilities**
- Fetch PR file list, diffs, and metadata from GitHub API.
- Load current Markdown docs in-scope.
- Reduce context for downstream processing.

### 5.5 Secret Scrubber + Data Minimizer
**Responsibilities**
- Scrub secret-like strings from diffs/docs before LLM calls.
- Enforce data minimization: send only required excerpts/summaries to LLM.
- Ensure logs and summaries never include raw secrets or raw token-like artifacts.

### 5.6 Outdated-Doc Detector
**Responsibilities**
- Determine if documentation updates are needed.
- Output deterministic decision + rationale (logged/redacted).

### 5.7 Prompt Builder (Schema-Constrained)
**Responsibilities**
- Build prompts that:
  - specify allowed paths and formatting/link constraints
  - require machine-parseable output (structured patch format)
- Apply redaction/minimization rules.

### 5.8 LLM Client (Secure Proxy Integration)
**Responsibilities**
- Execute hosted LLM calls via secure proxy.
- Implement timeouts and retries (exponential backoff, max 3).
- Record diagnostics without leaking sensitive content.

### 5.9 Doc Patch Generator / Applier (Schema Validated)
**Responsibilities**
- Validate LLM output against strict schema.
- Apply patches deterministically to working tree.
- Fail safely if patch is malformed or unapplicable.

### 5.10 Validation & Policy Gate (Required)
**Responsibilities**
- Enforce **path allowlist** (only `README.md` and `/docs/**/*.md`).
- Ensure no out-of-scope file modifications.
- Run secret scan on generated content (block secrets from being introduced into docs).
- Optional: restrict new URLs/domains and reject suspicious patterns.

### 5.11 Markdown Formatter + Link Updater/Checker
**Responsibilities**
- Apply Markdown formatting cleanup consistently.
- Update internal relative links when necessary.
- Validate links per defined policy (internal required; external optional/flagged).

### 5.12 PR Automation Service (GitHub Writer)
**Responsibilities**
- Commit changes to `docs-sync-<commit-hash>` branch.
- Create PR with standardized metadata:
  - title: `docs: automated documentation synchronization`
  - labels: `documentation`, `automated`
- Keep PR body minimal and safe (avoid including large diffs or sensitive content).

### 5.13 Observability & Reporting
**Responsibilities**
- Emit **structured, redacted** logs:
  - trigger type, merge commit hash, decision, retries, PR URL
- Produce GitHub Actions run summary (`GITHUB_STEP_SUMMARY`).
- Track basic metrics (duration, success/no-op/failure counts, retry counts).

---

## 6. Notes / Implementation Guardrails (Required)
- **Least privilege:** Use GitHub App tokens where possible; avoid PATs.
- **Atomic idempotency:** Use branch-create as lock + Actions concurrency to prevent duplicates under concurrent deliveries.
- **Strict allowlist:** Fail the run if any file outside `README.md` and `/docs/**/*.md` is modified.
- **Secret safety:** Scrub secrets before LLM calls and scan generated output for secret-like patterns before committing.
- **Logging hygiene:** Do not log raw diffs/prompts/responses. Log identifiers, counts, and redacted summaries.
- **Optional cleanup:** If lock branch is created but run results in no-op or hard failure before PR creation, consider deleting the lock branch to avoid clutter (ensure behavior remains idempotent and documented).
```