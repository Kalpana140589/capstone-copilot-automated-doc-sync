#!/usr/bin/env python3
"""
Automated Documentation Sync - core script
Save as: scripts/sync_docs.py

Implements:
1) Config loading & validation via Pydantic
2) Run correlation IDs and structured logging
3) Idempotency guard (branch/PR existence)
4) Diff/context extractor + chunker/summarizer
5) Outdated-doc detector + prompt builder (strict allowed paths)
6) LLM client via secure proxy (tenacity retries)
7) Patch schema validation + clean application
8) Markdown formatting + PR creation (branch/commit/labels/summary)

Expected env:
- DOCSYNC_GITHUB_TOKEN (or GITHUB_TOKEN)
- LLM_PROXY_URL, LLM_PROXY_API_KEY (names per workflow; can be adapted)
"""

from __future__ import annotations

import argparse
import base64
import dataclasses
import datetime as dt
import fnmatch
import json
import os
import re
import sys
import textwrap
import time
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional, Tuple

import requests
import yaml
from pydantic import BaseModel, Field, ValidationError, field_validator
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

# Optional deps (fail gracefully if not installed)
try:
    import mdformat
except Exception:  # pragma: no cover
    mdformat = None

try:
    from unidiff import PatchSet
except Exception:  # pragma: no cover
    PatchSet = None


# --------------------------
# Logging / Observability
# --------------------------

REASON = Literal[
    "SUCCESS",
    "NO_OP",
    "FAILURE",
    "ALREADY_PROCESSED_OR_IN_PROGRESS",
    "CONFIG_INVALID",
    "CONTEXT_TOO_LARGE",
    "GITHUB_API_ERROR",
    "LLM_TIMEOUT",
    "LLM_ERROR",
    "LLM_OUTPUT_INVALID",
    "PATCH_APPLY_FAILED",
    "PATCH_POLICY_BLOCKED",
    "LINK_CHECK_FAILED",
]


@dataclasses.dataclass
class RunContext:
    run_id: str
    trigger: str
    repo: str  # owner/repo
    merge_commit_sha: str
    source_pr_number: Optional[int]
    started_at: str


def log_event(ctx: RunContext, level: str, message: str, **fields: Any) -> None:
    payload = {
        "ts": dt.datetime.utcnow().isoformat() + "Z",
        "level": level.upper(),
        "message": message,
        "run_id": ctx.run_id,
        "trigger": ctx.trigger,
        "repo": ctx.repo,
        "merge_commit_sha": ctx.merge_commit_sha,
        "source_pr_number": ctx.source_pr_number,
        **fields,
    }
    print(json.dumps(payload, ensure_ascii=False), flush=True)


def write_summary_line(line: str) -> None:
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not summary_path:
        return
    with open(summary_path, "a", encoding="utf-8") as f:
        f.write(line.rstrip() + "\n")


# --------------------------
# Config Models
# --------------------------

class PRBodyConfig(BaseModel):
    include_run_id: bool = True
    include_merge_commit_sha: bool = True
    include_source_pr_link: bool = True


class PRConfig(BaseModel):
    title: str
    labels: List[str] = Field(default_factory=list)
    body: PRBodyConfig = Field(default_factory=PRBodyConfig)


class CleanupPolicy(BaseModel):
    delete_lock_branch_on_failure_before_pr: bool = True


class DocsConfig(BaseModel):
    allowed_paths: List[str]


class LimitsConfig(BaseModel):
    max_changed_files: int = 200
    max_diff_lines: int = 2000
    max_prompt_chars: int = 45000
    max_generated_patch_chars: int = 80000


class LLMParameters(BaseModel):
    temperature: float = 0.2
    top_p: float = 1.0
    max_tokens: int = 1800


class RetryPolicy(BaseModel):
    max_attempts: int = 3
    backoff: Literal["exponential"] = "exponential"
    initial_delay_ms: int = 500
    max_delay_ms: int = 8000


class Timeouts(BaseModel):
    connect_seconds: int = 5
    read_seconds: int = 60
    total_seconds: int = 75


class LLMConfig(BaseModel):
    provider: str = "enterprise_proxy"
    model: str = "default"
    parameters: LLMParameters = Field(default_factory=LLMParameters)
    retry_policy: RetryPolicy = Field(default_factory=RetryPolicy)
    timeouts: Timeouts = Field(default_factory=Timeouts)


class SecretScrubbingConfig(BaseModel):
    enabled: bool = True
    additional_redaction_patterns: List[str] = Field(default_factory=list)


class SecretScanConfig(BaseModel):
    enabled: bool = True
    tool_preference: Literal["gitleaks", "regex"] = "regex"


class URLPolicyConfig(BaseModel):
    enforce_allowlist: bool = False
    allowed_domains: List[str] = Field(default_factory=list)
    blocked_domains: List[str] = Field(default_factory=list)


class SecurityConfig(BaseModel):
    secret_scrubbing: SecretScrubbingConfig = Field(default_factory=SecretScrubbingConfig)
    secret_scan: SecretScanConfig = Field(default_factory=SecretScanConfig)
    url_policy: URLPolicyConfig = Field(default_factory=URLPolicyConfig)


class LinkMode(BaseModel):
    mode: Literal["block", "warn", "off"] = "warn"


class ExternalLinksConfig(BaseModel):
    mode: Literal["block", "warn", "off"] = "warn"
    timeout_seconds: int = 5
    max_concurrency: int = 10


class LinksConfig(BaseModel):
    internal: LinkMode = Field(default_factory=lambda: LinkMode(mode="block"))
    external: ExternalLinksConfig = Field(default_factory=ExternalLinksConfig)


class IdempotencyConfig(BaseModel):
    key: Literal["merge_commit_sha"] = "merge_commit_sha"
    on_existing_lock: Literal["exit", "update_existing"] = "exit"


class SyncConfig(BaseModel):
    branch_prefix: str = "docs-sync-"
    pr: PRConfig
    cleanup_policy: CleanupPolicy = Field(default_factory=CleanupPolicy)


class ObservabilityConfig(BaseModel):
    structured_logging: bool = True
    include_run_summary: bool = True
    log_level: Literal["DEBUG", "INFO", "WARN", "ERROR"] = "INFO"
    redact_sensitive: bool = True
    reason_codes_enabled: bool = True


class RepoConfig(BaseModel):
    default_branch: str = "main"


class AppConfig(BaseModel):
    version: int = 1
    repo: RepoConfig = Field(default_factory=RepoConfig)
    docs: DocsConfig
    sync: SyncConfig
    idempotency: IdempotencyConfig = Field(default_factory=IdempotencyConfig)
    limits: LimitsConfig = Field(default_factory=LimitsConfig)
    llm: LLMConfig = Field(default_factory=LLMConfig)
    security: SecurityConfig = Field(default_factory=SecurityConfig)
    links: LinksConfig = Field(default_factory=LinksConfig)
    observability: ObservabilityConfig = Field(default_factory=ObservabilityConfig)

    @field_validator("docs")
    @classmethod
    def validate_allowed_paths(cls, v: DocsConfig) -> DocsConfig:
        if not v.allowed_paths:
            raise ValueError("docs.allowed_paths must not be empty")
        # ensure docs patterns include README.md and docs/**/*.md for v1 baseline
        return v


# --------------------------
# GitHub Client (REST)
# --------------------------

class GitHubAPIError(RuntimeError):
    pass


class GitHubClient:
    def __init__(self, token: str, api_url: str = "https://api.github.com") -> None:
        self.api_url = api_url.rstrip("/")
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "docs-sync-bot",
            }
        )

    def _url(self, path: str) -> str:
        return f"{self.api_url}{path}"

    def _request(self, method: str, path: str, **kwargs: Any) -> requests.Response:
        url = self._url(path)
        resp = self.session.request(method, url, timeout=30, **kwargs)
        if resp.status_code in (401, 403) and "rate limit" in resp.text.lower():
            # still raise but caller may classify it; keep text minimal
            raise GitHubAPIError(f"GitHub rate limit: {resp.status_code}")
        if resp.status_code >= 400:
            raise GitHubAPIError(f"GitHub API error {resp.status_code}: {resp.text[:500]}")
        return resp

    def get(self, path: str, **kwargs: Any) -> Dict[str, Any]:
        return self._request("GET", path, **kwargs).json()

    def get_paginated(self, path: str, params: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        page = 1
        while True:
            p = dict(params or {})
            p.update({"per_page": 100, "page": page})
            resp = self._request("GET", path, params=p)
            batch = resp.json()
            if not isinstance(batch, list):
                raise GitHubAPIError(f"Expected list response for {path}, got {type(batch)}")
            out.extend(batch)
            # Link header pagination
            link = resp.headers.get("Link", "")
            if 'rel="next"' not in link:
                break
            page += 1
        return out

    def find_pr_by_merge_commit(self, owner: str, repo: str, merge_commit_sha: str) -> Optional[int]:
        # Search PRs by commit SHA (works for commits that belong to PRs)
        # GET /repos/{owner}/{repo}/commits/{commit_sha}/pulls
        pulls = self._request(
            "GET",
            f"/repos/{owner}/{repo}/commits/{merge_commit_sha}/pulls",
            headers={"Accept": "application/vnd.github.groot-preview+json"},
        ).json()
        if isinstance(pulls, list) and pulls:
            # Prefer merged PR targeting main; take first as default
            return pulls[0].get("number")
        return None

    def list_pr_files(self, owner: str, repo: str, pr_number: int) -> List[Dict[str, Any]]:
        return self.get_paginated(f"/repos/{owner}/{repo}/pulls/{pr_number}/files")

    def get_repo(self, owner: str, repo: str) -> Dict[str, Any]:
        return self.get(f"/repos/{owner}/{repo}")

    def get_branch(self, owner: str, repo: str, branch: str) -> Optional[Dict[str, Any]]:
        try:
            return self.get(f"/repos/{owner}/{repo}/branches/{branch}")
        except GitHubAPIError:
            return None

    def get_ref(self, owner: str, repo: str, ref: str) -> Optional[Dict[str, Any]]:
        try:
            return self.get(f"/repos/{owner}/{repo}/git/ref/{ref}")
        except GitHubAPIError:
            return None

    def create_ref(self, owner: str, repo: str, ref: str, sha: str) -> None:
        self._request(
            "POST",
            f"/repos/{owner}/{repo}/git/refs",
            json={"ref": ref, "sha": sha},
        )

    def delete_ref(self, owner: str, repo: str, ref: str) -> None:
        self._request("DELETE", f"/repos/{owner}/{repo}/git/refs/{ref}")

    def get_file_content(self, owner: str, repo: str, path: str, ref: str) -> str:
        data = self.get(f"/repos/{owner}/{repo}/contents/{path}", params={"ref": ref})
        if data.get("type") != "file":
            raise GitHubAPIError(f"Not a file: {path}")
        content_b64 = data.get("content", "")
        return base64.b64decode(content_b64).decode("utf-8", errors="replace")

    def create_or_update_file(
        self,
        owner: str,
        repo: str,
        path: str,
        message: str,
        content_text: str,
        branch: str,
        sha: Optional[str] = None,
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "message": message,
            "content": base64.b64encode(content_text.encode("utf-8")).decode("ascii"),
            "branch": branch,
        }
        if sha:
            payload["sha"] = sha
        return self._request("PUT", f"/repos/{owner}/{repo}/contents/{path}", json=payload).json()

    def get_content_sha(self, owner: str, repo: str, path: str, ref: str) -> Optional[str]:
        try:
            data = self.get(f"/repos/{owner}/{repo}/contents/{path}", params={"ref": ref})
            return data.get("sha")
        except GitHubAPIError:
            return None

    def create_pull_request(
        self, owner: str, repo: str, title: str, head: str, base: str, body: str
    ) -> Dict[str, Any]:
        return self._request(
            "POST",
            f"/repos/{owner}/{repo}/pulls",
            json={"title": title, "head": head, "base": base, "body": body},
        ).json()

    def add_labels(self, owner: str, repo: str, issue_number: int, labels: List[str]) -> None:
        self._request(
            "POST",
            f"/repos/{owner}/{repo}/issues/{issue_number}/labels",
            json={"labels": labels},
        )

    def find_existing_pr_by_head(self, owner: str, repo: str, head: str) -> Optional[Dict[str, Any]]:
        # List PRs (open) and match head.ref. For larger repos, use search API; fine for v1.
        prs = self.get_paginated(f"/repos/{owner}/{repo}/pulls", params={"state": "open"})
        for pr in prs:
            if pr.get("head", {}).get("ref") == head:
                return pr
        return None


# --------------------------
# LLM Output Schema
# --------------------------

class LLMFilePatch(BaseModel):
    path: str
    # One of these must be present
    new_content: Optional[str] = None
    patch_unified: Optional[str] = None

    @field_validator("path")
    @classmethod
    def normalize_path(cls, v: str) -> str:
        return v.strip().lstrip("/")

    @field_validator("patch_unified")
    @classmethod
    def patch_size_guard(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        if len(v) < 5:
            raise ValueError("patch_unified too short to be valid")
        return v

    @field_validator("new_content")
    @classmethod
    def content_not_empty(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and v.strip() == "":
            raise ValueError("new_content provided but empty")
        return v


class LLMPatchResponse(BaseModel):
    patches: List[LLMFilePatch]

    @field_validator("patches")
    @classmethod
    def no_duplicate_paths(cls, v: List[LLMFilePatch]) -> List[LLMFilePatch]:
        paths = [p.path for p in v]
        if len(paths) != len(set(paths)):
            raise ValueError("Duplicate paths in patches")
        return v


# --------------------------
# Helpers: allowlist, scrubbing, chunking
# --------------------------

SECRET_REGEXES = [
    re.compile(r"ghp_[A-Za-z0-9]{30,}"),  # GitHub PAT
    re.compile(r"AIza[0-9A-Za-z\-_]{35}"),  # Google API key
    re.compile(r"(?i)aws(.{0,20})?(secret|access)[^A-Za-z0-9]{0,3}[A-Za-z0-9/+=]{20,}"),
    re.compile(r"-----BEGIN (RSA|DSA|EC|OPENSSH) PRIVATE KEY-----"),
    re.compile(r"(?i)apikey[^A-Za-z0-9]{0,3}[A-Za-z0-9_\-]{16,}"),
    re.compile(r"(?i)api_key[^A-Za-z0-9]{0,3}[A-Za-z0-9_\-]{16,}"),
]


def is_allowed_path(path: str, allowed_patterns: List[str]) -> bool:
    norm = path.strip().lstrip("/")
    for pat in allowed_patterns:
        # normalize docs/** glob expectations
        if fnmatch.fnmatch(norm, pat):
            return True
    return False


def scrub_secrets(text: str, additional_patterns: List[str]) -> str:
    out = text
    for rx in SECRET_REGEXES:
        out = rx.sub("[REDACTED]", out)
    for pat in additional_patterns:
        try:
            out = re.sub(pat, "[REDACTED]", out)
        except re.error:
            # ignore invalid regex; config validation could enforce later
            pass
    return out


def chunk_text(text: str, max_chars: int) -> List[str]:
    if len(text) <= max_chars:
        return [text]
    chunks: List[str] = []
    start = 0
    while start < len(text):
        end = min(len(text), start + max_chars)
        chunks.append(text[start:end])
        start = end
    return chunks


def summarize_pr_files(files: List[Dict[str, Any]], max_files: int) -> List[Dict[str, Any]]:
    if len(files) <= max_files:
        return files
    # Keep first N files; note truncation
    return files[:max_files]


def extract_patch_hunks(files: List[Dict[str, Any]], max_lines: int) -> str:
    """
    Build a bounded diff-ish summary using `patch` field from PR files API.
    """
    lines_used = 0
    parts: List[str] = []
    for f in files:
        filename = f.get("filename")
        patch = f.get("patch") or ""
        if not patch:
            continue
        patch_lines = patch.splitlines()
        # truncate patch to fit
        remaining = max_lines - lines_used
        if remaining <= 0:
            break
        patch_lines = patch_lines[:remaining]
        lines_used += len(patch_lines)
        parts.append(f"--- file: {filename}\n" + "\n".join(patch_lines))
    if lines_used >= max_lines:
        parts.append("\n--- [TRUNCATED: max_diff_lines reached]\n")
    return "\n\n".join(parts)


def internal_link_check(markdown_text: str, current_path: str, repo_root: Path) -> List[str]:
    """
    Best-effort internal relative link checks:
    - extracts (./, ../, docs/...) links
    - ensures the target exists on disk (after modifications) ignoring anchors.
    """
    issues: List[str] = []
    link_rx = re.compile(r"\[[^\]]+\]\(([^)]+)\)")
    for m in link_rx.finditer(markdown_text):
        target = m.group(1).strip()
        if target.startswith("http://") or target.startswith("https://") or target.startswith("mailto:"):
            continue
        if target.startswith("#"):
            continue
        target_no_anchor = target.split("#", 1)[0]
        if target_no_anchor == "":
            continue
        # resolve relative to current file
        base_dir = (repo_root / current_path).parent
        resolved = (base_dir / target_no_anchor).resolve()
        try:
            resolved.relative_to(repo_root.resolve())
        except Exception:
            issues.append(f"{current_path}: link escapes repo root: {target}")
            continue
        if not resolved.exists():
            issues.append(f"{current_path}: broken internal link: {target}")
    return issues


# --------------------------
# LLM Client (secure proxy)
# --------------------------

class LLMError(RuntimeError):
    pass


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=0.5, min=0.5, max=8),
    retry=retry_if_exception_type((requests.RequestException, LLMError)),
)
def call_llm_proxy(
    proxy_url: str,
    api_key: str,
    model: str,
    prompt: str,
    max_tokens: int,
    temperature: float,
    top_p: float,
    timeout_total: int,
) -> str:
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    payload = {
        "model": model,
        "prompt": prompt,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "top_p": top_p,
    }
    resp = requests.post(proxy_url, headers=headers, json=payload, timeout=timeout_total)
    if resp.status_code >= 500:
        raise LLMError(f"LLM proxy 5xx: {resp.status_code}")
    if resp.status_code >= 400:
        raise LLMError(f"LLM proxy error {resp.status_code}: {resp.text[:300]}")
    data = resp.json()
    # Support common shapes: {"text": "..."} or {"choices":[{"text":"..."}]}
    if "text" in data and isinstance(data["text"], str):
        return data["text"]
    if "choices" in data and data["choices"] and "text" in data["choices"][0]:
        return data["choices"][0]["text"]
    if "output" in data and isinstance(data["output"], str):
        return data["output"]
    raise LLMError("LLM proxy response missing text field")


# --------------------------
# Main Logic
# --------------------------

def load_config(config_path: str) -> AppConfig:
    with open(config_path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    return AppConfig.model_validate(raw)


def parse_owner_repo(repo: str) -> Tuple[str, str]:
    if "/" not in repo:
        raise ValueError(f"Invalid repo format (expected owner/repo): {repo}")
    owner, name = repo.split("/", 1)
    return owner, name


def build_prompt(
    cfg: AppConfig,
    ctx: RunContext,
    diff_summary: str,
    docs_snapshot: Dict[str, str],
) -> str:
    allowed = "\n".join([f"- {p}" for p in cfg.docs.allowed_paths])
    docs_excerpt = "\n\n".join(
        [f"## FILE: {path}\n{content[:4000]}\n" for path, content in docs_snapshot.items()]
    )
    # Strict JSON output contract
    contract = {
        "patches": [
            {"path": "README.md", "new_content": "<full markdown content>"},
            {"path": "docs/example.md", "patch_unified": "<unified diff against current file>"},
        ]
    }

    prompt = f"""
You are an automated documentation sync agent.

GOAL:
Update ONLY the repository documentation to reflect the merged code changes.

STRICT SCOPE:
You MUST ONLY modify these Markdown files (no others):
{allowed}

INPUTS:
- Merge commit SHA: {ctx.merge_commit_sha}
- Diff summary (may be truncated):
{diff_summary}

- Current docs snapshot (excerpts):
{docs_excerpt}

OUTPUT REQUIREMENTS (CRITICAL):
1) Return ONLY valid JSON (no markdown fences, no commentary).
2) JSON MUST match this schema shape:
{json.dumps(contract, indent=2)}
3) Each patch item MUST include:
   - "path" (string) in allowlist
   - either "new_content" (string) OR "patch_unified" (string)
4) Do NOT include duplicate paths.
5) Do NOT change files outside scope.
6) Ensure Markdown is clean and links in README.md and docs are updated where needed.

Now produce the JSON response.
""".strip()
    return prompt


def detect_outdated_docs(files_changed: List[Dict[str, Any]]) -> Tuple[bool, str]:
    """
    Deterministic heuristic:
    - If code/config files changed (not docs-only), assume docs may need update.
    - If only docs changed, no-op.
    """
    non_doc = []
    for f in files_changed:
        fn = f.get("filename", "")
        if fn == "README.md" or fn.startswith("docs/"):
            continue
        non_doc.append(fn)
    if non_doc:
        return True, "PUBLIC_API_CHANGED_OR_CODE_CHANGED"
    return False, "NO_DOC_IMPACT"


def apply_patches(
    cfg: AppConfig,
    repo_root: Path,
    existing_files: Dict[str, str],
    patches: LLMPatchResponse,
) -> Dict[str, str]:
    """
    Returns updated file contents for modified files.
    """
    updated: Dict[str, str] = dict(existing_files)

    for p in patches.patches:
        if not is_allowed_path(p.path, cfg.docs.allowed_paths):
            raise ValueError(f"Patch path out of allowlist: {p.path}")

        if p.new_content is not None:
            updated[p.path] = p.new_content
            continue

        if p.patch_unified is not None:
            if PatchSet is None:
                raise RuntimeError("unidiff not installed; cannot apply patch_unified")
            original = updated.get(p.path)
            if original is None:
                raise ValueError(f"patch_unified refers to unknown file: {p.path}")

            # Apply unified diff using unidiff (line-based). We'll do a simple apply.
            patchset = PatchSet(p.patch_unified.splitlines(True))
            # Find file patch matching path (best effort)
            file_patch = None
            for fp in patchset:
                # fp.path is usually "a/..." or "b/..." stripped by lib
                if fp.path.endswith(p.path) or fp.path == p.path:
                    file_patch = fp
                    break
            if file_patch is None:
                raise ValueError(f"No matching file in patch_unified for path={p.path}")

            new_lines = original.splitlines(True)
            # naive apply: walk hunks; use target line numbers (may fail on drift)
            try:
                for hunk in file_patch:
                    # unidiff uses 1-based line numbers
                    idx = hunk.target_start - 1
                    # remove hunk.target_length lines and replace with added/kept lines
                    # build replacement from hunk lines that are not removed
                    replacement: List[str] = []
                    for line in hunk:
                        if line.is_added or line.is_context:
                            replacement.append(line.value)
                        # removed lines are skipped
                    # splice
                    new_lines[idx : idx + hunk.target_length] = replacement
                updated[p.path] = "".join(new_lines)
            except Exception as e:
                raise RuntimeError(f"Failed applying unified diff for {p.path}: {e}") from e

            continue

        raise ValueError(f"Patch must include new_content or patch_unified for path={p.path}")

    return updated


def md_format_if_available(text: str) -> str:
    if mdformat is None:
        return text
    return mdformat.text(text)


def secret_scan_text(text: str) -> bool:
    for rx in SECRET_REGEXES:
        if rx.search(text):
            return True
    return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True, help="Path to .github/docs-sync.yml")
    ap.add_argument("--repo", required=True, help="owner/repo")
    ap.add_argument("--merge-commit-sha", required=True)
    ap.add_argument("--run-id", required=False)
    ap.add_argument("--trigger", required=False, default=os.environ.get("DOCSYNC_TRIGGER", "unknown"))
    ap.add_argument("--github-api-url", required=False, default=os.environ.get("GITHUB_API_URL", "https://api.github.com"))
    args = ap.parse_args()

    run_id = args.run_id or os.environ.get("DOCSYNC_RUN_ID") or f"local-{int(time.time())}"
    ctx = RunContext(
        run_id=run_id,
        trigger=args.trigger,
        repo=args.repo,
        merge_commit_sha=args.merge_commit_sha,
        source_pr_number=None,
        started_at=dt.datetime.utcnow().isoformat() + "Z",
    )

    # Phase 1: Config load/validate
    try:
        cfg = load_config(args.config)
    except (OSError, ValidationError, yaml.YAMLError) as e:
        # minimal logging; no secrets
        log_event(ctx, "ERROR", "Config invalid or unreadable", reason_code="CONFIG_INVALID", error=str(e)[:300])
        write_summary_line("## Automated Documentation Sync")
        write_summary_line(f"- Run ID: `{ctx.run_id}`")
        write_summary_line(f"- Merge Commit SHA: `{ctx.merge_commit_sha}`")
        write_summary_line(f"- Status: `FAILURE`")
        write_summary_line(f"- Reason: `CONFIG_INVALID`")
        return 2

    owner, repo_name = parse_owner_repo(args.repo)

    token = os.environ.get("DOCSYNC_GITHUB_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if not token:
        log_event(ctx, "ERROR", "Missing GitHub token (DOCSYNC_GITHUB_TOKEN/GITHUB_TOKEN)")
        return 2
    gh = GitHubClient(token=token, api_url=args.github_api_url)

    # Resolve source PR number (best effort)
    try:
        pr_num = gh.find_pr_by_merge_commit(owner, repo_name, ctx.merge_commit_sha)
        ctx.source_pr_number = pr_num
    except Exception as e:
        log_event(ctx, "WARN", "Could not resolve source PR number from merge commit", error=str(e)[:200])

    log_event(ctx, "INFO", "Run initialized", run_started_at=ctx.started_at)

    # Phase 3: Idempotency guard (branch/PR existence)
    branch_name = f"{cfg.sync.branch_prefix}{ctx.merge_commit_sha}"
    existing_branch = gh.get_ref(owner, repo_name, f"heads/{branch_name}")
    existing_pr = None
    try:
        existing_pr = gh.find_existing_pr_by_head(owner, repo_name, head=branch_name)
    except Exception:
        existing_pr = None

    if existing_branch or existing_pr:
        pr_url = existing_pr.get("html_url") if existing_pr else None
        log_event(
            ctx,
            "INFO",
            "Idempotency guard: already processed or in progress",
            reason_code="ALREADY_PROCESSED_OR_IN_PROGRESS",
            branch=branch_name,
            existing_pr_url=pr_url,
        )
        write_summary_line("## Automated Documentation Sync")
        write_summary_line(f"- Run ID: `{ctx.run_id}`")
        write_summary_line(f"- Merge Commit SHA: `{ctx.merge_commit_sha}`")
        write_summary_line(f"- Status: `NO_OP`")
        write_summary_line(f"- Reason: `ALREADY_PROCESSED_OR_IN_PROGRESS`")
        if pr_url:
            write_summary_line(f"- Existing PR: {pr_url}")
        return 0

    # Phase 4.1: Diff & context extraction
    if ctx.source_pr_number is None:
        log_event(ctx, "ERROR", "Unable to determine source PR for merge commit; cannot extract PR files", reason_code="GITHUB_API_ERROR")
        return 3

    try:
        pr_files = gh.list_pr_files(owner, repo_name, ctx.source_pr_number)
    except Exception as e:
        log_event(ctx, "ERROR", "Failed to fetch PR files", reason_code="GITHUB_API_ERROR", error=str(e)[:300])
        return 3

    pr_files = summarize_pr_files(pr_files, cfg.limits.max_changed_files)
    diff_summary = extract_patch_hunks(pr_files, cfg.limits.max_diff_lines)

    # Secret scrubbing + minimization (pre-LLM)
    if cfg.security.secret_scrubbing.enabled:
        diff_summary = scrub_secrets(diff_summary, cfg.security.secret_scrubbing.additional_redaction_patterns)

    # Outdated-doc detection
    update_needed, decision_reason = detect_outdated_docs(pr_files)
    log_event(ctx, "INFO", "Outdated-doc decision computed", update_needed=update_needed, decision_reason=decision_reason)

    if not update_needed:
        write_summary_line("## Automated Documentation Sync")
        write_summary_line(f"- Run ID: `{ctx.run_id}`")
        write_summary_line(f"- Merge Commit SHA: `{ctx.merge_commit_sha}`")
        write_summary_line(f"- Status: `NO_OP`")
        write_summary_line(f"- Reason: `{decision_reason}`")
        return 0

    # Acquire atomic lock by creating branch NOW (after UPDATE_NEEDED decision)
    try:
        main_branch = cfg.repo.default_branch
        main_ref = gh.get_ref(owner, repo_name, f"heads/{main_branch}")
        main_sha = main_ref["object"]["sha"]
        gh.create_ref(owner, repo_name, f"refs/heads/{branch_name}", main_sha)
        log_event(ctx, "INFO", "Lock branch created (atomic idempotency lock acquired)", branch=branch_name)
    except Exception as e:
        log_event(ctx, "INFO", "Lock branch already exists or cannot be created", reason_code="ALREADY_PROCESSED_OR_IN_PROGRESS", error=str(e)[:200])
        return 0

    # Load docs snapshot (full content for in-scope files)
    docs_paths = ["README.md"]
    # Add docs/**/*.md by listing via git tree? For v1, load only existing files we can discover.
    # We'll attempt to read docs index files referenced by config by scanning local checkout if available.
    repo_root = Path.cwd()
    discovered_docs = []
    docs_dir = repo_root / "docs"
    if docs_dir.exists():
        discovered_docs = [str(p.relative_to(repo_root)) for p in docs_dir.rglob("*.md")]
    docs_paths.extend(discovered_docs)

    # Deduplicate and enforce allowlist
    docs_paths = sorted({p for p in docs_paths if is_allowed_path(p, cfg.docs.allowed_paths)})

    docs_snapshot: Dict[str, str] = {}
    for p in docs_paths:
        try:
            docs_snapshot[p] = gh.get_file_content(owner, repo_name, p, ref=cfg.repo.default_branch)
        except Exception:
            # ignore missing files; keep snapshot partial
            continue

    # Chunking / prompt size enforcement
    # Build a single prompt with bounded components; if too big, chunk diff_summary.
    prompt_base = build_prompt(cfg, ctx, diff_summary, docs_snapshot)
    if len(prompt_base) > cfg.limits.max_prompt_chars:
        # chunk the diff summary and keep docs excerpts small; simplest strategy:
        # reduce docs excerpts and diff_summary further.
        truncated_docs_snapshot = {k: v[:1500] for k, v in docs_snapshot.items()}
        chunks = chunk_text(diff_summary, max_chars=max(2000, cfg.limits.max_prompt_chars // 3))
        # Use only first chunk for v1; could iterate in future
        diff_chunk = chunks[0]
        prompt_base = build_prompt(cfg, ctx, diff_chunk, truncated_docs_snapshot)

    if len(prompt_base) > cfg.limits.max_prompt_chars:
        log_event(ctx, "ERROR", "Prompt still too large after chunking", reason_code="CONTEXT_TOO_LARGE", prompt_chars=len(prompt_base))
        # cleanup lock branch on failure-before-PR
        if cfg.sync.cleanup_policy.delete_lock_branch_on_failure_before_pr:
            try:
                gh.delete_ref(owner, repo_name, f"heads/{branch_name}")
            except Exception:
                pass
        return 4

    # Phase 4.2: Call LLM via secure proxy
    proxy_url = os.environ.get("LLM_PROXY_URL")
    proxy_key = os.environ.get("LLM_PROXY_API_KEY")
    if not proxy_url or not proxy_key:
        log_event(ctx, "ERROR", "Missing LLM proxy credentials", reason_code="LLM_ERROR")
        if cfg.sync.cleanup_policy.delete_lock_branch_on_failure_before_pr:
            try:
                gh.delete_ref(owner, repo_name, f"heads/{branch_name}")
            except Exception:
                pass
        return 5

    try:
        llm_text = call_llm_proxy(
            proxy_url=proxy_url,
            api_key=proxy_key,
            model=cfg.llm.model,
            prompt=prompt_base,
            max_tokens=cfg.llm.parameters.max_tokens,
            temperature=cfg.llm.parameters.temperature,
            top_p=cfg.llm.parameters.top_p,
            timeout_total=cfg.llm.timeouts.total_seconds,
        )
    except Exception as e:
        log_event(ctx, "ERROR", "LLM call failed", reason_code="LLM_ERROR", error=str(e)[:300])
        if cfg.sync.cleanup_policy.delete_lock_branch_on_failure_before_pr:
            try:
                gh.delete_ref(owner, repo_name, f"heads/{branch_name}")
            except Exception:
                pass
        return 5

    # Phase 4.3: Parse and validate LLM output
    llm_text = llm_text.strip()
    if len(llm_text) > cfg.limits.max_generated_patch_chars:
        log_event(ctx, "ERROR", "LLM output exceeds configured size cap", reason_code="LLM_OUTPUT_INVALID", output_chars=len(llm_text))
        if cfg.sync.cleanup_policy.delete_lock_branch_on_failure_before_pr:
            try:
                gh.delete_ref(owner, repo_name, f"heads/{branch_name}")
            except Exception:
                pass
        return 6

    try:
        obj = json.loads(llm_text)
        patches = LLMPatchResponse.model_validate(obj)
    except Exception as e:
        log_event(ctx, "ERROR", "LLM output invalid JSON or schema", reason_code="LLM_OUTPUT_INVALID", error=str(e)[:300])
        if cfg.sync.cleanup_policy.delete_lock_branch_on_failure_before_pr:
            try:
                gh.delete_ref(owner, repo_name, f"heads/{branch_name}")
            except Exception:
                pass
        return 6

    # Enforce allowlist again and cap sizes
    for p in patches.patches:
        if not is_allowed_path(p.path, cfg.docs.allowed_paths):
            log_event(ctx, "ERROR", "Patch path violates allowlist", reason_code="PATCH_POLICY_BLOCKED", path=p.path)
            if cfg.sync.cleanup_policy.delete_lock_branch_on_failure_before_pr:
                try:
                    gh.delete_ref(owner, repo_name, f"heads/{branch_name}")
                except Exception:
                    pass
            return 7

    # Fetch current contents for patched files (from main)
    existing_files: Dict[str, str] = {}
    for p in patches.patches:
        existing_files[p.path] = gh.get_file_content(owner, repo_name, p.path, ref=cfg.repo.default_branch)

    # Phase 4.4: Apply patches
    try:
        updated_files = apply_patches(cfg, repo_root=repo_root, existing_files=existing_files, patches=patches)
    except Exception as e:
        log_event(ctx, "ERROR", "Failed to apply patches", reason_code="PATCH_APPLY_FAILED", error=str(e)[:300])
        if cfg.sync.cleanup_policy.delete_lock_branch_on_failure_before_pr:
            try:
                gh.delete_ref(owner, repo_name, f"heads/{branch_name}")
            except Exception:
                pass
        return 7

    # Phase 5.1: Policy gate (paths + secret scan)
    modified_paths = sorted(updated_files.keys())
    for path in modified_paths:
        if not is_allowed_path(path, cfg.docs.allowed_paths):
            log_event(ctx, "ERROR", "Out-of-scope file modification detected", reason_code="PATCH_POLICY_BLOCKED", path=path)
            if cfg.sync.cleanup_policy.delete_lock_branch_on_failure_before_pr:
                try:
                    gh.delete_ref(owner, repo_name, f"heads/{branch_name}")
                except Exception:
                    pass
            return 8

    if cfg.security.secret_scan.enabled:
        for path, content in updated_files.items():
            if secret_scan_text(content):
                log_event(ctx, "ERROR", "Secret-like pattern detected in generated docs", reason_code="PATCH_POLICY_BLOCKED", path=path)
                if cfg.sync.cleanup_policy.delete_lock_branch_on_failure_before_pr:
                    try:
                        gh.delete_ref(owner, repo_name, f"heads/{branch_name}")
                    except Exception:
                        pass
                return 8

    # Phase 5.2: Markdown formatting + internal link check (deterministic)
    formatted_files: Dict[str, str] = {}
    for path, content in updated_files.items():
        formatted = md_format_if_available(content)
        formatted_files[path] = formatted

    link_issues: List[str] = []
    if cfg.links.internal.mode != "off":
        # Write formatted files locally for link resolution checks
        for path, content in formatted_files.items():
            local_path = repo_root / path
            local_path.parent.mkdir(parents=True, exist_ok=True)
            local_path.write_text(content, encoding="utf-8")
        for path, content in formatted_files.items():
            link_issues.extend(internal_link_check(content, current_path=path, repo_root=repo_root))

        if link_issues and cfg.links.internal.mode == "block":
            log_event(ctx, "ERROR", "Internal link check failed", reason_code="LINK_CHECK_FAILED", issues=link_issues[:20])
            if cfg.sync.cleanup_policy.delete_lock_branch_on_failure_before_pr:
                try:
                    gh.delete_ref(owner, repo_name, f"heads/{branch_name}")
                except Exception:
                    pass
            return 9
        elif link_issues:
            log_event(ctx, "WARN", "Internal link issues detected (non-blocking)", reason_code="LINK_CHECK_FAILED", issues=link_issues[:20])

    # Phase 5.3: Commit files to lock branch using Contents API
    committed: List[str] = []
    try:
        for path, content in formatted_files.items():
            sha = gh.get_content_sha(owner, repo_name, path, ref=branch_name)
            msg = f"docs: sync for {ctx.merge_commit_sha}"
            gh.create_or_update_file(owner, repo_name, path, msg, content, branch=branch_name, sha=sha)
            committed.append(path)
    except Exception as e:
        log_event(ctx, "ERROR", "Failed to commit documentation updates", reason_code="GITHUB_API_ERROR", error=str(e)[:300])
        if cfg.sync.cleanup_policy.delete_lock_branch_on_failure_before_pr:
            try:
                gh.delete_ref(owner, repo_name, f"heads/{branch_name}")
            except Exception:
                pass
        return 10

    # Create PR
    pr_body_lines = []
    pr_body_lines.append("This PR was generated automatically to synchronize documentation with merged code changes.")
    if cfg.sync.pr.body.include_run_id:
        pr_body_lines.append(f"- Run ID: `{ctx.run_id}`")
    if cfg.sync.pr.body.include_merge_commit_sha:
        pr_body_lines.append(f"- Merge commit: `{ctx.merge_commit_sha}`")
    if cfg.sync.pr.body.include_source_pr_link and ctx.source_pr_number:
        pr_body_lines.append(f"- Source PR: #{ctx.source_pr_number}")
    pr_body_lines.append("")
    pr_body_lines.append("### Files updated")
    for p in committed:
        pr_body_lines.append(f"- `{p}`")
    if link_issues:
        pr_body_lines.append("")
        pr_body_lines.append("### Link check warnings")
        pr_body_lines.extend([f"- {i}" for i in link_issues[:20]])

    pr_body = "\n".join(pr_body_lines).strip() + "\n"

    try:
        pr = gh.create_pull_request(
            owner=owner,
            repo=repo_name,
            title=cfg.sync.pr.title,
            head=branch_name,
            base=cfg.repo.default_branch,
            body=pr_body,
        )
        pr_number = pr.get("number")
        pr_url = pr.get("html_url")
        if pr_number and cfg.sync.pr.labels:
            gh.add_labels(owner, repo_name, pr_number, cfg.sync.pr.labels)

        log_event(ctx, "INFO", "PR created successfully", reason_code="SUCCESS", pr_url=pr_url, pr_number=pr_number)
        write_summary_line("## Automated Documentation Sync")
        write_summary_line(f"- Run ID: `{ctx.run_id}`")
        write_summary_line(f"- Merge Commit SHA: `{ctx.merge_commit_sha}`")
        write_summary_line(f"- Status: `SUCCESS`")
        write_summary_line(f"- PR: {pr_url}")
        return 0
    except Exception as e:
        log_event(ctx, "ERROR", "Failed to create PR", reason_code="GITHUB_API_ERROR", error=str(e)[:300])
        if cfg.sync.cleanup_policy.delete_lock_branch_on_failure_before_pr:
            try:
                gh.delete_ref(owner, repo_name, f"heads/{branch_name}")
            except Exception:
                pass
        return 11


if __name__ == "__main__":
    raise SystemExit(main())