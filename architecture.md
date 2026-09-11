```markdown

\# architecture.md — Automated Documentation Sync (High-Level Architecture)



\## 1. Architecture Overview

Automated Documentation Sync is an event-driven automation that runs when a Pull Request is merged into the `main` branch of a single GitHub repository (`capstone-copilot-automated-doc-sync`). It analyzes code changes, determines whether Markdown documentation is outdated, uses an enterprise-approved hosted LLM (via secure proxy) to generate documentation updates, applies formatting/link quality steps, and always opens a GitHub Pull Request containing the proposed changes for human review.



Key constraints from requirements:

\- \*\*Single repo (v1)\*\*, \*\*docs scope\*\* limited to `README.md` and `/docs/\*\*/\*.md`.

\- \*\*Trigger\*\* on \*\*PR merged into `main`\*\*, plus \*\*manual dispatch\*\* for testing.

\- \*\*LLM required\*\* via secure proxy.

\- \*\*Always PR output\*\*; no direct commits to `main`; no issues.

\- \*\*SLA\*\*: target end-to-end completion within \*\*2–5 minutes\*\*.

\- \*\*Retries\*\*: exponential backoff, \*\*max 3\*\* for transient GitHub/LLM/network errors.

\- \*\*Idempotency\*\*: no duplicate PRs for the same merge commit hash.



\---



\## 2. High-Level Component Diagram (Mermaid)



```mermaid

flowchart LR

&#x20; dev\[Developers] -->|Merge PR| gh\[(GitHub Repo<br/>capstone-copilot-automated-doc-sync)]



&#x20; subgraph github\[GitHub Platform]

&#x20;   gh -->|PR merged event<br/>(pull\_request closed=merged)| ga\[GitHub Actions Workflow<br/>Trigger + Orchestration]

&#x20;   ghapi\[GitHub API<br/>(REST/GraphQL)]

&#x20; end



&#x20; subgraph runner\[Automation Runtime]

&#x20;   ga --> job\[Sync Job Runner<br/>(container or hosted runner)]

&#x20;   job --> orch\[Doc Sync Orchestrator]

&#x20;   orch --> idemp\[Idempotency Guard<br/>(commit-hash ledger)]

&#x20;   orch --> diff\[Diff \& Context Extractor]

&#x20;   diff --> det\[Outdated-Doc Detector]

&#x20;   det --> prompt\[Prompt Builder]

&#x20;   prompt --> llm\[Hosted LLM via Secure Proxy]

&#x20;   llm --> gen\[Doc Patch Generator<br/>(apply edits)]

&#x20;   gen --> fmt\[Markdown Formatter<br/>+ Link Updater/Checker]

&#x20;   fmt --> commit\[Branch/Commit Writer]

&#x20;   commit --> pr\[PR Creator + Labeler]

&#x20; end



&#x20; idemp <--> store\[(State Store<br/>Artifacts/Cache/DB)]

&#x20; pr -->|create branch/commit/PR| ghapi

&#x20; diff -->|fetch diffs, files| ghapi

&#x20; commit --> ghapi



&#x20; subgraph obs\[Observability]

&#x20;   logs\[Structured Logs]:::obs

&#x20;   metrics\[Metrics/Run Summary]:::obs

&#x20; end



&#x20; orch --> logs

&#x20; orch --> metrics

&#x20; ga --> logs



&#x20; classDef obs fill:#f5f5f5,stroke:#999,color:#333;

```



\---



\## 3. Recommended Technology Choices



\### 3.1 Triggering \& Orchestration

\- \*\*GitHub Actions\*\* (primary orchestrator)

&#x20; - Event trigger: `pull\_request` with `types: \[closed]` and guard `merged == true` and `base.ref == "main"`.

&#x20; - Manual trigger: `workflow\_dispatch`.



\### 3.2 Sync Logic Runtime / Language

\- \*\*Python\*\* (recommended for v1)

&#x20; - Strong ecosystem for text processing and markdown tooling.

&#x20; - Suggested libraries:

&#x20;   - GitHub API: `PyGithub` or `requests`

&#x20;   - Retries/backoff: `tenacity`

&#x20;   - Config validation: `pydantic`

\- Alternative: \*\*Node.js/TypeScript\*\*

&#x20; - GitHub integration: `@actions/github`, `octokit`

&#x20; - Validation: `zod`

&#x20; - Retries: `p-retry`



\### 3.3 GitHub PR/Branch/Commit Automation

\- \*\*GitHub REST API\*\* (sufficient for v1)

&#x20; - Read PR files/diffs

&#x20; - Create branch refs

&#x20; - Create commits

&#x20; - Create PRs

&#x20; - Apply labels (`documentation`, `automated`)

\- Optional: \*\*GitHub GraphQL\*\* for efficient PR/branch lookup, but not required.



\### 3.4 LLM Integration

\- \*\*Hosted LLM via enterprise secure proxy\*\* (required)

&#x20; - Enforce:

&#x20;   - request timeouts

&#x20;   - bounded retries (max 3)

&#x20;   - request/response size limits

&#x20;   - minimal necessary context sharing per enterprise guidelines



\### 3.5 State / Idempotency Storage

Recommended options (choose one):

\- \*\*GitHub-native (simplest for capstone):\*\*

&#x20; - Determine idempotency by searching for existing PR/branch named `docs-sync-<commit-hash>`.

&#x20; - Optionally persist a small ledger via GitHub Actions artifacts/caches.

\- \*\*External store (more “service-like”):\*\*

&#x20; - DynamoDB / Redis / Postgres to store:

&#x20;   - merge commit hash

&#x20;   - status (success/no-op/failure)

&#x20;   - PR URL/id

&#x20;   - timestamps



\### 3.6 Formatting \& Link Quality Gates

\- Markdown formatting:

&#x20; - `prettier` (Markdown formatting) and/or `markdownlint-cli2`

\- Link check/update:

&#x20; - `lychee` or `markdown-link-check`

&#x20; - Recommended: prioritize \*\*internal relative links\*\* for determinism; treat external link failures as “flag” rather than hard fail if needed.



\### 3.7 Observability

\- GitHub Actions step summary:

&#x20; - Use `GITHUB\_STEP\_SUMMARY` to publish run output (decision, PR URL, changed files).

\- Structured logs:

&#x20; - JSON logs to stdout.

\- Metrics (v1):

&#x20; - log-derived counts and durations (success/failure/no-op, retries, run time).



\---



\## 4. End-to-End Data Flow (Step-by-Step)



1\. \*\*PR merge event occurs\*\*

&#x20;  - A PR is merged into `main` in `capstone-copilot-automated-doc-sync`.



2\. \*\*Workflow trigger\*\*

&#x20;  - GitHub Actions workflow starts from the merge event.

&#x20;  - Manual `workflow\_dispatch` can start the same flow for testing/debugging.



3\. \*\*Run initialization\*\*

&#x20;  - Orchestrator loads:

&#x20;    - merge commit hash (primary idempotency key)

&#x20;    - repository identifier

&#x20;    - source PR number (if available)

&#x20;    - current `main` reference



4\. \*\*Idempotency check\*\*

&#x20;  - System checks if `docs-sync-<commit-hash>` already exists as:

&#x20;    - a branch ref, or

&#x20;    - an open/closed PR head branch

&#x20;  - If already processed, system exits cleanly (or updates the existing branch/PR if that policy is chosen).



5\. \*\*Diff \& context extraction\*\*

&#x20;  - Pull changed files and/or diff hunks from GitHub API for the merged PR.

&#x20;  - Load current contents of:

&#x20;    - `README.md`

&#x20;    - relevant `/docs/\*\*/\*.md` (entire files or selected excerpts)



6\. \*\*Outdated documentation detection\*\*

&#x20;  - Determine whether docs likely require changes based on:

&#x20;    - the diffs (e.g., changed public interfaces/config/endpoints)

&#x20;    - heuristics and/or LLM-assisted classification

&#x20;  - Output a deterministic decision: \*\*update needed\*\* vs \*\*no-op\*\*.



7\. \*\*Prompt build \& LLM call\*\*

&#x20;  - Prompt Builder packages:

&#x20;    - summarized changes

&#x20;    - relevant doc excerpts

&#x20;    - constraints: modify only `README.md` and `/docs/\*\*/\*.md`, produce valid Markdown, update links as needed

&#x20;  - LLM Client calls hosted LLM via secure proxy with retries.



8\. \*\*Patch generation and application\*\*

&#x20;  - Convert LLM output into file edits (recommended: unified diff or structured per-file patches).

&#x20;  - Apply edits to working tree.

&#x20;  - Enforce scope guardrail: no files outside `README.md` and `/docs/\*\*/\*.md` are modified.



9\. \*\*Formatting and link quality steps\*\*

&#x20;  - Run Markdown formatting cleanup.

&#x20;  - Update and/or validate links in modified Markdown.

&#x20;  - If link updates cannot be resolved deterministically:

&#x20;    - fail the run, or

&#x20;    - create PR and clearly flag in PR body (implementation policy decision).



10\. \*\*Branch, commit, PR creation\*\*

&#x20;  - Create branch: `docs-sync-<commit-hash>` off `main`.

&#x20;  - Commit changes with consistent commit message.

&#x20;  - Create PR targeting `main`:

&#x20;    - Title: `docs: automated documentation synchronization`

&#x20;    - Labels: `documentation`, `automated`



11\. \*\*Reporting\*\*

&#x20;  - Write PR URL and run summary to `GITHUB\_STEP\_SUMMARY`.

&#x20;  - Emit structured logs including retries and outcomes.



\---



\## 5. Key Components \& Responsibilities



\### 5.1 GitHub Actions Workflow (Trigger + Orchestration)

\*\*Responsibilities\*\*

\- Trigger on PR merged into `main`.

\- Support `workflow\_dispatch`.

\- Provide runtime environment and required secrets.

\- Gate execution to intended branch and event type.



\### 5.2 Doc Sync Orchestrator (Main Entry Point)

\*\*Responsibilities\*\*

\- Coordinate end-to-end execution steps.

\- Maintain run context (repo, merge commit hash, timestamps).

\- Enforce scope: only `README.md` and `/docs/\*\*/\*.md`.

\- Produce final run outcome: success / no-op / failure.



\### 5.3 Idempotency Guard (Commit-Hash Ledger)

\*\*Responsibilities\*\*

\- Prevent duplicate PRs per merge commit hash.

\- Check existing `docs-sync-<commit-hash>` branch/PR and/or external ledger.

\- Decide behavior on re-run:

&#x20; - exit cleanly, or

&#x20; - update existing PR branch (must be consistent and logged).



\### 5.4 Diff \& Context Extractor

\*\*Responsibilities\*\*

\- Fetch PR file list, diffs, and relevant metadata from GitHub API.

\- Load current Markdown docs in-scope.

\- Reduce/summarize context to fit LLM token and policy constraints.



\### 5.5 Outdated-Doc Detector

\*\*Responsibilities\*\*

\- Determine if documentation updates are needed.

\- Ensure deterministic decision output per run (record rationale in logs/summary).



\### 5.6 Prompt Builder

\*\*Responsibilities\*\*

\- Build structured prompts with:

&#x20; - change summaries/diff excerpts

&#x20; - existing doc excerpts

&#x20; - formatting/link constraints

&#x20; - allowed file list enforcement

\- Apply redaction/minimization if required by enterprise policy.



\### 5.7 LLM Client (Secure Proxy Integration)

\*\*Responsibilities\*\*

\- Execute hosted LLM calls via secure proxy.

\- Implement timeouts and retries (exponential backoff, max 3).

\- Capture diagnostic metadata without leaking secrets or sensitive content.



\### 5.8 Doc Patch Generator / Applier

\*\*Responsibilities\*\*

\- Translate LLM output to deterministic file edits.

\- Apply patches safely; detect conflicts/un-applicable diffs.

\- Validate resulting Markdown files are syntactically valid and within scope.



\### 5.9 Markdown Formatter + Link Updater/Checker

\*\*Responsibilities\*\*

\- Apply Markdown formatting cleanup consistently.

\- Update internal relative links when paths/anchors change.

\- Validate links (at minimum internal links; external optional with a defined policy).



\### 5.10 PR Automation Service (GitHub Writer)

\*\*Responsibilities\*\*

\- Create branch `docs-sync-<commit-hash>`.

\- Commit file changes.

\- Create PR with standardized metadata:

&#x20; - title: `docs: automated documentation synchronization`

&#x20; - labels: `documentation`, `automated`

\- Include PR body summary (source PR, summary of doc changes, known limitations).



\### 5.11 Observability \& Reporting

\*\*Responsibilities\*\*

\- Emit structured logs for each run:

&#x20; - trigger type, merge commit hash, decision, retries, PR URL

\- Produce a GitHub Actions run summary (`GITHUB\_STEP\_SUMMARY`).

\- Track basic metrics (duration, success/no-op/failure counts, retry counts).



\---



\## 6. Notes / Implementation Guardrails (Recommended)

\- Enforce “in-scope only” edits by hard-checking the modified file list before commit.

\- Prefer a machine-applicable LLM output format (e.g., per-file unified diffs) to reduce risk of malformed updates.

\- Keep prompts minimal and contextual to comply with enterprise data guidelines and to improve performance within the 2–5 minute SLA.

```

