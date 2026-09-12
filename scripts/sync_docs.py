#!/usr/bin/env python3
"""
scripts/sync_docs.py

Automated Documentation Sync (v1)

Implements:
1) Config Loading & Validation (Pydantic)
2) Run Initialization & Correlation IDs (structured JSON logs)
3) Idempotency Guard (branch/PR exists)
4) Diff & Context Extractor (GitHub REST) + chunker/summarizer
5) Outdated-Doc Detector + Prompt Builder (strict allowlist)
6) LLM Client (OpenAI-compatible /v1/chat/completions OR proxy fallback) w/ tenacity retries
7) Patch Generator & Validator (strict JSON schema + apply cleanly)
8) Markdown Formatter + PR Creator (branch/commit/labels/summary)

Key updates (per your request):
- Robust allowlist matching across OS using PurePosixPath (docs/**/*.md matches docs/guide.md on Windows).
- Remove datetime.utcnow() deprecation warnings by using timezone-aware UTC timestamps.
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
import time
from pathlib import Path, PurePosixPath
from typing import Any, Dict, List, Literal, Optional, Tuple

import requests
import yaml
from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

try:
    import mdformat
except Exception:  # pragma: no cover
    mdformat = None

try:
    from unidiff import PatchSet
except Exception:  # pragma: no cover
    PatchSet = None


# --------------------------
# Time helpers (UTC, tz-aware)
# --------------------------

def utc_now_iso() -> str:
    """Timezone-aware UTC ISO timestamp (avoids datetime.utcnow() deprecation)."""
    return dt.datetime.now(dt.UTC).isoformat().replace("+00:00", "Z")


# --------------------------
# Observability / Logging
# --------------------------

ReasonCode = Literal[
    "SUCCESS",
    "NO_OP",
    "FAILURE",
    "ALREADY_PROCESSED_OR_IN_PROGRESS",
    "CONFIG_INVALID",
    "CONTEXT_TOO_LARGE",
    "GITHUB_API_ERROR",
    "LLM_ERROR",
    "LLM_TIMEOUT",
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
        "ts": utc_now_iso(),
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


def write_summary(lines: List[str]) -> None:
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not summary_path:
        return
    with open(summary_path, "a", encoding="utf-8") as f:
        for ln in lines:
            f.write(ln.rstrip() + "\n")


# --------------------------
# Config Models (.github/docs-sync.yml)
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
    total_seconds: int = 75


class LLMConfig(BaseModel):
    provider: str = "openai_compatible"
    model: str = "gpt-4o-mini"
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
    def validate_docs_paths(cls, v: DocsConfig) -> DocsConfig:
        if not v.allowed_paths:
            raise ValueError("docs.allowed_paths must not be empty")
        return v


# --------------------------
# GitHub REST Client
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
        if resp.status_code >= 400:
            raise GitHubAPIError(f"{resp.status_code}: {resp.text[:500]}")
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
                raise GitHubAPIError(f"Expected list response for {path}")
            out.extend(batch)
            link = resp.headers.get("Link", "")
            if 'rel="next"' not in link:
                break
            page += 1
        return out

    def find_pr_by_merge_commit(self, owner: str, repo: str, merge_commit_sha: str) -> Optional[int]:
        pulls = self._request(
            "GET",
            f"/repos/{owner}/{repo}/commits/{merge_commit_sha}/pulls",
            headers={"Accept": "application/vnd.github.groot-preview+json"},
        ).json()
        if isinstance(pulls, list) and pulls:
            return pulls[0].get("number")
        return None

    def list_pr_files(self, owner: str, repo: str, pr_number: int) -> List[Dict[str, Any]]:
        return self.get_paginated(f"/repos/{owner}/{repo}/pulls/{pr_number}/files")

    def get_ref(self, owner: str, repo: str, ref: str) -> Optional[Dict[str, Any]]:
        try:
            return self.get(f"/repos/{owner}/{repo}/git/ref/{ref}")
        except Exception:
            return None

    def create_ref(self, owner: str, repo: str, ref: str, sha: str) -> None:
        self._request("POST", f"/repos/{owner}/{repo}/git/refs", json={"ref": ref, "sha": sha})

    def delete_ref(self, owner: str, repo: str, ref: str) -> None:
        self._request("DELETE", f"/repos/{owner}/{repo}/git/refs/{ref}")

    def get_file_content(self, owner: str, repo: str, path: str, ref: str) -> str:
        data = self.get(f"/repos/{owner}/{repo}/contents/{path}", params={"ref": ref})
        content_b64 = data.get("content", "")
        return base64.b64decode(content_b64).decode("utf-8", errors="replace")

    def get_content_sha(self, owner: str, repo: str, path: str, ref: str) -> Optional[str]:
        try:
            data = self.get(f"/repos/{owner}/{repo}/contents/{path}", params={"ref": ref})
            return data.get("sha")
        except Exception:
            return None

    def put_file(
        self, owner: str, repo: str, path: str, branch: str, message: str, content: str, sha: Optional[str]
    ) -> None:
        payload: Dict[str, Any] = {
            "message": message,
            "content": base64.b64encode(content.encode("utf-8")).decode("ascii"),
            "branch": branch,
        }
        if sha:
            payload["sha"] = sha
        self._request("PUT", f"/repos/{owner}/{repo}/contents/{path}", json=payload)

    def create_pull_request(self, owner: str, repo: str, title: str, head: str, base: str, body: str) -> Dict[str, Any]:
        return self._request(
            "POST",
            f"/repos/{owner}/{repo}/pulls",
            json={"title": title, "head": head, "base": base, "body": body},
        ).json()

    def add_labels(self, owner: str, repo: str, issue_number: int, labels: List[str]) -> None:
        self._request("POST", f"/repos/{owner}/{repo}/issues/{issue_number}/labels", json={"labels": labels})

    def find_existing_open_pr_by_head(self, owner: str, repo: str, head_branch: str) -> Optional[Dict[str, Any]]:
        prs = self.get_paginated(f"/repos/{owner}/{repo}/pulls", params={"state": "open"})
        for pr in prs:
            if pr.get("head", {}).get("ref") == head_branch:
                return pr
        return None


# --------------------------
# LLM Patch Output Schema
# --------------------------

class LLMFilePatch(BaseModel):
    path: str
    new_content: Optional[str] = None
    patch_unified: Optional[str] = None

    @model_validator(mode="after")
    def one_of_new_or_patch(self) -> "LLMFilePatch":
        if (self.new_content is None) == (self.patch_unified is None):
            raise ValueError("Each patch must include exactly one of new_content or patch_unified")
        return self

    @field_validator("path")
    @classmethod
    def normalize_path(cls, v: str) -> str:
        return v.strip().lstrip("/")


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
# Security / Allowlist / Chunking Helpers
# --------------------------

SECRET_REGEXES = [
    re.compile(r"ghp_[A-Za-z0-9]{30,}"),  # GitHub PAT
    re.compile(r"-----BEGIN (RSA|DSA|EC|OPENSSH) PRIVATE KEY-----"),
    re.compile(r"(?i)aws(.{0,20})?(secret|access)[^A-Za-z0-9]{0,3}[A-Za-z0-9/+=]{20,}"),
    re.compile(r"(?i)api[_-]?key[^A-Za-z0-9]{0,3}[A-Za-z0-9_\-]{16,}"),
]


def _to_posix(p: str) -> str:
    """Normalize any path/pattern to POSIX-style for consistent matching across OS."""
    p = p.strip().replace("\\", "/").lstrip("/")
    return str(PurePosixPath(p))


def is_allowed_path(path: str, allowed_patterns: List[str]) -> bool:
    """
    Cross-platform allowlist matching.

    Supports patterns:
      - README.md
      - docs/*.md
      - docs/**/*.md (MUST match docs/guide.md and docs/a/b.md)

    Uses POSIX normalization + fnmatchcase and special-cases ** semantics.
    """
    norm = _to_posix(path)
    for pat in allowed_patterns:
        pat_norm = _to_posix(pat)

        if fnmatch.fnmatchcase(norm, pat_norm):
            return True

        # Special-case: "docs/**/*.md" should match both "docs/x.md" and deeper.
        if pat_norm.endswith("/**/*.md"):
            prefix = pat_norm[: -len("/**/*.md")]
            if norm.startswith(prefix + "/") and norm.endswith(".md"):
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
            pass
    return out


def extract_patch_hunks(files: List[Dict[str, Any]], max_lines: int) -> str:
    used = 0
    parts: List[str] = []
    for f in files:
        fn = f.get("filename", "")
        patch = f.get("patch") or ""
        if not patch:
            continue
        patch_lines = patch.splitlines()
        remaining = max_lines - used
        if remaining <= 0:
            break
        patch_lines = patch_lines[:remaining]
        used += len(patch_lines)
        parts.append(f"--- file: {fn}\n" + "\n".join(patch_lines))
    if used >= max_lines:
        parts.append("--- [TRUNCATED: max_diff_lines reached]")
    return "\n\n".join(parts)


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


def deterministic_diff_summary(
    files: List[Dict[str, Any]], max_files: int, max_lines: int
) -> Tuple[List[Dict[str, Any]], str, bool]:
    truncated = False
    if len(files) > max_files:
        files = files[:max_files]
        truncated = True
    diff = extract_patch_hunks(files, max_lines=max_lines)
    if "--- [TRUNCATED" in diff:
        truncated = True
    return files, diff, truncated


def md_format(text: str) -> str:
    if mdformat is None:
        return text
    return mdformat.text(text)


def secret_scan(text: str) -> bool:
    return any(rx.search(text) for rx in SECRET_REGEXES)


# --------------------------
# Internal link checking (deterministic, best-effort)
# --------------------------

_LINK_RX = re.compile(r"\[[^\]]+\]\(([^)]+)\)")


def internal_link_issues(markdown: str, current_path: str, repo_root: Path) -> List[str]:
    issues: List[str] = []
    for m in _LINK_RX.finditer(markdown):
        target = m.group(1).strip()
        if target.startswith(("http://", "https://", "mailto:")):
            continue
        if target.startswith("#"):
            continue
        target = target.split("#", 1)[0]
        if not target:
            continue

        base = (repo_root / current_path).parent
        resolved = (base / target).resolve()
        try:
            resolved.relative_to(repo_root.resolve())
        except Exception:
            issues.append(f"{current_path}: link escapes repo root: {target}")
            continue
        if not resolved.exists():
            issues.append(f"{current_path}: broken internal link: {target}")
    return issues


# --------------------------
# LLM Client (OpenAI-compatible chat completions) + legacy proxy fallback
# --------------------------

class LLMError(RuntimeError):
    pass


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=0.5, min=0.5, max=8),
    retry=retry_if_exception_type((requests.RequestException, LLMError)),
)
def call_chat_completions(
    base_url: str,
    api_key: str,
    model: str,
    system_prompt: str,
    user_prompt: str,
    max_tokens: int,
    temperature: float,
    top_p: float,
    timeout_total: int,
) -> str:
    url = base_url.rstrip("/") + "/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    payload = {
        "model": model,
        "temperature": temperature,
        "top_p": top_p,
        "max_tokens": max_tokens,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
    }
    resp = requests.post(url, headers=headers, json=payload, timeout=timeout_total)
    if resp.status_code >= 500:
        raise LLMError(f"LLM 5xx: {resp.status_code}")
    if resp.status_code == 429:
        raise LLMError("LLM rate limited (429)")
    if resp.status_code >= 400:
        raise LLMError(f"LLM error {resp.status_code}: {resp.text[:300]}")

    data = resp.json()
    try:
        return data["choices"][0]["message"]["content"]
    except Exception as e:
        raise LLMError(f"Unexpected chat completion response shape: {e}")


# Legacy fallback kept for compatibility with earlier workflow naming.
# If you only use OpenAI-compatible chat, you can remove this.
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
    if "text" in data and isinstance(data["text"], str):
        return data["text"]
    if "choices" in data and data["choices"] and "text" in data["choices"][0]:
        return data["choices"][0]["text"]
    if "output" in data and isinstance(data["output"], str):
        return data["output"]
    raise LLMError("LLM proxy response missing text field")


# --------------------------
# Prompting + Decision + Patch Application
# --------------------------

def detect_outdated_docs(pr_files: List[Dict[str, Any]]) -> Tuple[bool, str]:
    non_doc = []
    for f in pr_files:
        fn = f.get("filename", "")
        if fn == "README.md" or fn.startswith("docs/"):
            continue
        non_doc.append(fn)
    if non_doc:
        return True, "PUBLIC_API_CHANGED_OR_CODE_CHANGED"
    return False, "NO_DOC_IMPACT"


def build_prompts(
    cfg: AppConfig,
    ctx: RunContext,
    diff_chunks: List[str],
    docs_snapshot: Dict[str, str],
) -> Tuple[str, str]:
    allowed_list = "\n".join([f"- {p}" for p in cfg.docs.allowed_paths])
    system_prompt = (
        "You are a precise documentation update agent. "
        "You must follow the output contract strictly and never modify files outside the allowlist."
    )

    docs_excerpt = "\n\n".join(
        [f"## FILE: {path}\n{content[:2500]}\n" for path, content in sorted(docs_snapshot.items())]
    )
    contract_example = {
        "patches": [
            {"path": "README.md", "new_content": "<full markdown content>"},
            {"path": "docs/example.md", "patch_unified": "<unified diff against current file>"},
        ]
    }
    diff_summary = diff_chunks[0] if diff_chunks else ""

    user_prompt = f"""
GOAL:
Synchronize documentation with the merged code changes.

STRICT SCOPE:
You MUST ONLY modify these Markdown files (no others):
{allowed_list}

INPUTS:
- Merge commit SHA: {ctx.merge_commit_sha}

- Diff summary (may be truncated):
{diff_summary}

- Current docs snapshot (excerpts):
{docs_excerpt}

OUTPUT REQUIREMENTS (CRITICAL):
1) Return ONLY valid JSON (no markdown fences, no commentary).
2) JSON MUST match this schema shape:
{json.dumps(contract_example, indent=2)}
3) Each patch MUST include:
   - "path" (string) in allowlist
   - exactly one of "new_content" OR "patch_unified"
4) Do NOT include duplicate paths.
5) Ensure Markdown is clean and update links where needed.
""".strip()

    return system_prompt, user_prompt


def apply_patches(existing: Dict[str, str], patches: LLMPatchResponse) -> Dict[str, str]:
    updated = dict(existing)

    for p in patches.patches:
        if p.new_content is not None:
            updated[p.path] = p.new_content
            continue

        if p.patch_unified is not None:
            if PatchSet is None:
                raise RuntimeError("unidiff not installed; cannot apply patch_unified")

            original = updated.get(p.path)
            if original is None:
                raise ValueError(f"patch_unified references unknown file: {p.path}")

            patchset = PatchSet(p.patch_unified.splitlines(True))
            file_patch = None
            for fp in patchset:
                if fp.path.endswith(p.path) or fp.path == p.path:
                    file_patch = fp
                    break
            if file_patch is None:
                raise ValueError(f"No matching file patch found for path={p.path}")

            new_lines = original.splitlines(True)
            for hunk in file_patch:
                idx = hunk.target_start - 1
                replacement: List[str] = []
                for line in hunk:
                    if line.is_added or line.is_context:
                        replacement.append(line.value)
                new_lines[idx : idx + hunk.target_length] = replacement
            updated[p.path] = "".join(new_lines)
            continue

        raise ValueError(f"Patch missing new_content/patch_unified for path={p.path}")

    return updated


# --------------------------
# Utilities
# --------------------------

def load_config(path: str) -> AppConfig:
    with open(path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    return AppConfig.model_validate(raw)


def parse_owner_repo(repo: str) -> Tuple[str, str]:
    if "/" not in repo:
        raise ValueError(f"Invalid repo format (expected owner/repo): {repo}")
    owner, name = repo.split("/", 1)
    return owner, name


def cleanup_lock_branch(
    cfg: AppConfig, gh: GitHubClient, owner: str, repo: str, branch_name: str, ctx: RunContext
) -> None:
    if not cfg.sync.cleanup_policy.delete_lock_branch_on_failure_before_pr:
        return
    try:
        gh.delete_ref(owner, repo, f"heads/{branch_name}")
        log_event(ctx, "INFO", "Deleted lock branch due to failure-before-PR", branch=branch_name)
    except Exception as e:
        log_event(ctx, "WARN", "Failed deleting lock branch during cleanup", branch=branch_name, error=str(e)[:200])


# --------------------------
# Main
# --------------------------

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True)
    ap.add_argument("--repo", required=True, help="owner/repo")
    ap.add_argument("--merge-commit-sha", required=True)
    ap.add_argument("--run-id", required=False)
    ap.add_argument("--trigger", required=False, default=os.environ.get("DOCSYNC_TRIGGER", "unknown"))
    ap.add_argument("--github-api-url", required=False, default=os.environ.get("GITHUB_API_URL", "https://api.github.com"))
    args = ap.parse_args()

    ctx = RunContext(
        run_id=args.run_id or os.environ.get("DOCSYNC_RUN_ID") or f"local-{int(time.time())}",
        trigger=args.trigger,
        repo=args.repo,
        merge_commit_sha=args.merge_commit_sha,
        source_pr_number=None,
        started_at=utc_now_iso(),
    )

    # 1) Config load/validate
    try:
        cfg = load_config(args.config)
    except (OSError, ValidationError, yaml.YAMLError) as e:
        log_event(ctx, "ERROR", "Config invalid/unreadable", reason_code="CONFIG_INVALID", error=str(e)[:300])
        write_summary(
            [
                "## Automated Documentation Sync",
                f"- Run ID: `{ctx.run_id}`",
                f"- Merge Commit SHA: `{ctx.merge_commit_sha}`",
                "- Status: `FAILURE`",
                "- Reason: `CONFIG_INVALID`",
            ]
        )
        return 2

    token = os.environ.get("DOCSYNC_GITHUB_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if not token:
        log_event(ctx, "ERROR", "Missing GitHub token (DOCSYNC_GITHUB_TOKEN/GITHUB_TOKEN)", reason_code="GITHUB_API_ERROR")
        return 2

    owner, repo_name = parse_owner_repo(args.repo)
    gh = GitHubClient(token=token, api_url=args.github_api_url)

    # 2) Determine source PR number (best-effort)
    try:
        ctx.source_pr_number = gh.find_pr_by_merge_commit(owner, repo_name, ctx.merge_commit_sha)
    except Exception as e:
        log_event(ctx, "WARN", "Unable to resolve source PR number from merge commit", error=str(e)[:200])

    log_event(ctx, "INFO", "Run initialized", run_started_at=ctx.started_at)

    if ctx.source_pr_number is None:
        log_event(ctx, "ERROR", "Source PR number not found for merge commit", reason_code="GITHUB_API_ERROR")
        return 3

    # 3) Idempotency guard (branch OR open PR)
    branch_name = f"{cfg.sync.branch_prefix}{ctx.merge_commit_sha}"
    if gh.get_ref(owner, repo_name, f"heads/{branch_name}") is not None:
        log_event(ctx, "INFO", "Idempotency: branch exists", reason_code="ALREADY_PROCESSED_OR_IN_PROGRESS", branch=branch_name)
        write_summary(
            [
                "## Automated Documentation Sync",
                f"- Run ID: `{ctx.run_id}`",
                f"- Merge Commit SHA: `{ctx.merge_commit_sha}`",
                "- Status: `NO_OP`",
                "- Reason: `ALREADY_PROCESSED_OR_IN_PROGRESS`",
            ]
        )
        return 0

    existing_pr = gh.find_existing_open_pr_by_head(owner, repo_name, branch_name)
    if existing_pr:
        log_event(
            ctx,
            "INFO",
            "Idempotency: PR already exists",
            reason_code="ALREADY_PROCESSED_OR_IN_PROGRESS",
            existing_pr_url=existing_pr.get("html_url"),
        )
        write_summary(
            [
                "## Automated Documentation Sync",
                f"- Run ID: `{ctx.run_id}`",
                f"- Merge Commit SHA: `{ctx.merge_commit_sha}`",
                "- Status: `NO_OP`",
                "- Reason: `ALREADY_PROCESSED_OR_IN_PROGRESS`",
                f"- Existing PR: {existing_pr.get('html_url')}",
            ]
        )
        return 0

    # 4) Diff & context extractor + deterministic summarization
    try:
        pr_files_raw = gh.list_pr_files(owner, repo_name, ctx.source_pr_number)
        pr_files, diff_summary, truncated = deterministic_diff_summary(
            pr_files_raw,
            max_files=cfg.limits.max_changed_files,
            max_lines=cfg.limits.max_diff_lines,
        )
    except Exception as e:
        log_event(ctx, "ERROR", "Failed to fetch PR files/diff", reason_code="GITHUB_API_ERROR", error=str(e)[:300])
        return 3

    if cfg.security.secret_scrubbing.enabled:
        diff_summary = scrub_secrets(diff_summary, cfg.security.secret_scrubbing.additional_redaction_patterns)

    # 5) Outdated-doc detector
    update_needed, decision_reason = detect_outdated_docs(pr_files)
    log_event(ctx, "INFO", "Outdated-doc decision computed", update_needed=update_needed, decision_reason=decision_reason, diff_truncated=truncated)

    if not update_needed:
        write_summary(
            [
                "## Automated Documentation Sync",
                f"- Run ID: `{ctx.run_id}`",
                f"- Merge Commit SHA: `{ctx.merge_commit_sha}`",
                "- Status: `NO_OP`",
                f"- Reason: `{decision_reason}`",
            ]
        )
        return 0

    # Acquire atomic lock AFTER UPDATE_NEEDED decision
    try:
        main_ref = gh.get_ref(owner, repo_name, f"heads/{cfg.repo.default_branch}")
        if not main_ref:
            raise GitHubAPIError(f"Default branch ref not found: {cfg.repo.default_branch}")
        main_sha = main_ref["object"]["sha"]  # type: ignore[index]
        gh.create_ref(owner, repo_name, f"refs/heads/{branch_name}", main_sha)
        log_event(ctx, "INFO", "Lock branch created (atomic idempotency lock acquired)", branch=branch_name)
    except Exception as e:
        log_event(ctx, "INFO", "Lock branch exists or cannot be created", reason_code="ALREADY_PROCESSED_OR_IN_PROGRESS", error=str(e)[:200])
        return 0

    # Collect docs snapshot from main
    docs_snapshot: Dict[str, str] = {}
    repo_root = Path.cwd()

    # Always include README.md
    if is_allowed_path("README.md", cfg.docs.allowed_paths):
        try:
            docs_snapshot["README.md"] = gh.get_file_content(owner, repo_name, "README.md", ref=cfg.repo.default_branch)
        except Exception:
            pass

    # Discover docs/**/*.md from local checkout if present
    docs_dir = repo_root / "docs"
    if docs_dir.exists():
        for p in sorted(docs_dir.rglob("*.md")):
            rel = str(p.relative_to(repo_root)).replace("\\", "/")
            if is_allowed_path(rel, cfg.docs.allowed_paths):
                try:
                    docs_snapshot[rel] = gh.get_file_content(owner, repo_name, rel, ref=cfg.repo.default_branch)
                except Exception:
                    continue

    # Chunk diff to fit prompt caps
    diff_chunks = chunk_text(diff_summary, max_chars=max(2000, min(cfg.limits.max_prompt_chars // 2, 15000)))
    system_prompt, user_prompt = build_prompts(cfg, ctx, diff_chunks, docs_snapshot)

    prompt_chars = len(system_prompt) + len(user_prompt)
    if prompt_chars > cfg.limits.max_prompt_chars:
        log_event(ctx, "ERROR", "Prompt too large after chunking", reason_code="CONTEXT_TOO_LARGE", prompt_chars=prompt_chars)
        cleanup_lock_branch(cfg, gh, owner, repo_name, branch_name, ctx)
        return 4

    # 6) LLM call
    # Preferred env for OpenAI-compatible chat:
    # - LLM_BASE_URL (base, no /v1/chat/completions suffix)
    # - LLM_API_KEY
    llm_base_url = os.environ.get("LLM_BASE_URL") or os.environ.get("LLM_PROXY_URL")
    llm_api_key = os.environ.get("LLM_API_KEY") or os.environ.get("LLM_PROXY_API_KEY")
    if not llm_base_url or not llm_api_key:
        log_event(ctx, "ERROR", "Missing LLM credentials", reason_code="LLM_ERROR")
        cleanup_lock_branch(cfg, gh, owner, repo_name, branch_name, ctx)
        return 5

    try:
        llm_out = call_chat_completions(
            base_url=llm_base_url,
            api_key=llm_api_key,
            model=cfg.llm.model,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            max_tokens=cfg.llm.parameters.max_tokens,
            temperature=cfg.llm.parameters.temperature,
            top_p=cfg.llm.parameters.top_p,
            timeout_total=cfg.llm.timeouts.total_seconds,
        ).strip()
    except Exception as e:
        log_event(ctx, "ERROR", "LLM call failed", reason_code="LLM_ERROR", error=str(e)[:300])
        cleanup_lock_branch(cfg, gh, owner, repo_name, branch_name, ctx)
        return 5

    if len(llm_out) > cfg.limits.max_generated_patch_chars:
        log_event(ctx, "ERROR", "LLM output exceeds size cap", reason_code="LLM_OUTPUT_INVALID", output_chars=len(llm_out))
        cleanup_lock_branch(cfg, gh, owner, repo_name, branch_name, ctx)
        return 6

    # 7) Parse and validate patches
    try:
        parsed = json.loads(llm_out)
        patch_resp = LLMPatchResponse.model_validate(parsed)
    except Exception as e:
        log_event(ctx, "ERROR", "LLM output invalid JSON/schema", reason_code="LLM_OUTPUT_INVALID", error=str(e)[:300])
        cleanup_lock_branch(cfg, gh, owner, repo_name, branch_name, ctx)
        return 6

    for p in patch_resp.patches:
        if not is_allowed_path(p.path, cfg.docs.allowed_paths):
            log_event(ctx, "ERROR", "Patch path violates allowlist", reason_code="PATCH_POLICY_BLOCKED", path=p.path)
            cleanup_lock_branch(cfg, gh, owner, repo_name, branch_name, ctx)
            return 7

    # Fetch current contents from main for referenced files
    existing: Dict[str, str] = {}
    try:
        for p in patch_resp.patches:
            existing[p.path] = gh.get_file_content(owner, repo_name, p.path, ref=cfg.repo.default_branch)
    except Exception as e:
        log_event(ctx, "ERROR", "Failed to fetch file content for patch application", reason_code="GITHUB_API_ERROR", error=str(e)[:300])
        cleanup_lock_branch(cfg, gh, owner, repo_name, branch_name, ctx)
        return 7

    try:
        updated = apply_patches(existing, patch_resp)
    except Exception as e:
        log_event(ctx, "ERROR", "Patch apply failed", reason_code="PATCH_APPLY_FAILED", error=str(e)[:300])
        cleanup_lock_branch(cfg, gh, owner, repo_name, branch_name, ctx)
        return 7

    # 8) Policy gate: only allowlisted paths + secret scan
    for path in updated.keys():
        if not is_allowed_path(path, cfg.docs.allowed_paths):
            log_event(ctx, "ERROR", "Out-of-scope file change detected", reason_code="PATCH_POLICY_BLOCKED", path=path)
            cleanup_lock_branch(cfg, gh, owner, repo_name, branch_name, ctx)
            return 8

    if cfg.security.secret_scan.enabled:
        for path, content in updated.items():
            if secret_scan(content):
                log_event(ctx, "ERROR", "Secret-like pattern detected in generated output", reason_code="PATCH_POLICY_BLOCKED", path=path)
                cleanup_lock_branch(cfg, gh, owner, repo_name, branch_name, ctx)
                return 8

    formatted: Dict[str, str] = {p: md_format(c) for p, c in updated.items()}

    link_issues: List[str] = []
    if cfg.links.internal.mode != "off":
        # write files for deterministic relative resolution
        for path, content in formatted.items():
            local = repo_root / path
            local.parent.mkdir(parents=True, exist_ok=True)
            local.write_text(content, encoding="utf-8")

        for path, content in formatted.items():
            link_issues.extend(internal_link_issues(content, path, repo_root))

        if link_issues and cfg.links.internal.mode == "block":
            log_event(ctx, "ERROR", "Internal link check failed", reason_code="LINK_CHECK_FAILED", issues=link_issues[:20])
            cleanup_lock_branch(cfg, gh, owner, repo_name, branch_name, ctx)
            return 9
        if link_issues:
            log_event(ctx, "WARN", "Internal link issues detected", reason_code="LINK_CHECK_FAILED", issues=link_issues[:20])

    # Commit to lock branch
    try:
        for path, content in formatted.items():
            sha = gh.get_content_sha(owner, repo_name, path, ref=branch_name)
            gh.put_file(
                owner, repo_name, path,
                branch=branch_name,
                message=f"docs: sync for {ctx.merge_commit_sha}",
                content=content,
                sha=sha,
            )
    except Exception as e:
        log_event(ctx, "ERROR", "Failed to commit changes", reason_code="GITHUB_API_ERROR", error=str(e)[:300])
        cleanup_lock_branch(cfg, gh, owner, repo_name, branch_name, ctx)
        return 10

    # Create PR
    body_lines: List[str] = [
        "This PR was generated automatically to synchronize documentation with merged code changes.",
    ]
    if cfg.sync.pr.body.include_run_id:
        body_lines.append(f"- Run ID: `{ctx.run_id}`")
    if cfg.sync.pr.body.include_merge_commit_sha:
        body_lines.append(f"- Merge commit: `{ctx.merge_commit_sha}`")
    if cfg.sync.pr.body.include_source_pr_link and ctx.source_pr_number:
        body_lines.append(f"- Source PR: #{ctx.source_pr_number}")
    body_lines += ["", "### Files updated"]
    body_lines += [f"- `{p}`" for p in sorted(formatted.keys())]
    if link_issues:
        body_lines += ["", "### Link warnings", *[f"- {i}" for i in link_issues[:20]]]
    pr_body = "\n".join(body_lines).strip() + "\n"

    try:
        pr = gh.create_pull_request(
            owner, repo_name,
            title=cfg.sync.pr.title,
            head=branch_name,
            base=cfg.repo.default_branch,
            body=pr_body,
        )
        pr_number = pr.get("number")
        pr_url = pr.get("html_url")

        if pr_number and cfg.sync.pr.labels:
            gh.add_labels(owner, repo_name, pr_number, cfg.sync.pr.labels)

        log_event(ctx, "INFO", "PR created", reason_code="SUCCESS", pr_url=pr_url, pr_number=pr_number)
        write_summary(
            [
                "## Automated Documentation Sync",
                f"- Run ID: `{ctx.run_id}`",
                f"- Merge Commit SHA: `{ctx.merge_commit_sha}`",
                "- Status: `SUCCESS`",
                f"- PR: {pr_url}",
            ]
        )
        return 0
    except Exception as e:
        log_event(ctx, "ERROR", "Failed to create PR", reason_code="GITHUB_API_ERROR", error=str(e)[:300])
        cleanup_lock_branch(cfg, gh, owner, repo_name, branch_name, ctx)
        return 11


if __name__ == "__main__":
    raise SystemExit(main())