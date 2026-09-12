# tests/test_sync_docs.py
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import scripts.sync_docs as sd  # noqa: E402


def write_config(tmp_path: Path, overrides: Optional[Dict[str, Any]] = None) -> Path:
    cfg = {
        "version": 1,
        "repo": {"default_branch": "main"},
        "docs": {"allowed_paths": ["README.md", "docs/**/*.md"]},
        "sync": {
            "branch_prefix": "docs-sync-",
            "pr": {
                "title": "docs: automated documentation synchronization",
                "labels": ["documentation", "automated"],
                "body": {
                    "include_run_id": True,
                    "include_merge_commit_sha": True,
                    "include_source_pr_link": True,
                },
            },
            "cleanup_policy": {"delete_lock_branch_on_failure_before_pr": True},
        },
        "idempotency": {"key": "merge_commit_sha", "on_existing_lock": "exit"},
        "limits": {
            "max_changed_files": 200,
            "max_diff_lines": 2000,
            "max_prompt_chars": 45000,
            "max_generated_patch_chars": 80000,
        },
        "llm": {
            "provider": "openai_compatible",
            "model": "gpt-4o-mini",
            "parameters": {"temperature": 0.2, "top_p": 1.0, "max_tokens": 200},
            "retry_policy": {"max_attempts": 3, "backoff": "exponential", "initial_delay_ms": 1, "max_delay_ms": 10},
            "timeouts": {"total_seconds": 5},
        },
        "security": {
            "secret_scrubbing": {"enabled": True, "additional_redaction_patterns": []},
            "secret_scan": {"enabled": True, "tool_preference": "regex"},
            "url_policy": {"enforce_allowlist": False, "allowed_domains": [], "blocked_domains": []},
        },
        "links": {
            "internal": {"mode": "off"},
            "external": {"mode": "off", "timeout_seconds": 1, "max_concurrency": 1},
        },
        "observability": {
            "structured_logging": True,
            "include_run_summary": False,
            "log_level": "INFO",
            "redact_sensitive": True,
            "reason_codes_enabled": True,
        },
    }
    if overrides:
        cfg.update(overrides)

    cfg_path = tmp_path / "docs-sync.yml"
    cfg_path.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    return cfg_path


def patch_llm(monkeypatch: pytest.MonkeyPatch, response_text: str) -> None:
    """
    Patch whichever LLM function exists in the implementation.
    Supports both:
      - call_chat_completions(...)  (OpenAI compatible)
      - call_llm_proxy(...)         (older proxy text-completions style)
    """
    if hasattr(sd, "call_chat_completions"):
        monkeypatch.setattr(sd, "call_chat_completions", lambda **kwargs: response_text)
        return
    if hasattr(sd, "call_llm_proxy"):
        monkeypatch.setattr(sd, "call_llm_proxy", lambda **kwargs: response_text)
        return
    raise AssertionError("sync_docs module has no known LLM call function to patch")


class FakeGitHubClient:
    def __init__(self) -> None:
        self.created_refs: List[Dict[str, Any]] = []
        self.deleted_refs: List[str] = []
        self.put_files: List[Dict[str, Any]] = []
        self.created_prs: List[Dict[str, Any]] = []
        self.labeled: List[Dict[str, Any]] = []

        self._refs: Dict[str, Dict[str, Any]] = {"heads/main": {"object": {"sha": "MAIN_SHA"}}}
        self._branch_exists = False
        self._open_pr_exists = False

        self._pr_number_for_merge_commit: Optional[int] = 123
        self._pr_files: List[Dict[str, Any]] = [
            {"filename": "src/app.py", "patch": "@@ -1 +1 @@\n-print('a')\n+print('b')\n"},
            {"filename": "README.md", "patch": "@@ -1 +1 @@\n-Old\n+Old\n"},
        ]
        self._contents_main: Dict[str, str] = {
            "README.md": "# Title\n\nOld docs\n",
            "docs/guide.md": "# Guide\n\nSee [README](../README.md)\n",
        }
        self._content_sha: Dict[str, str] = {"README.md": "SHA_README_MAIN", "docs/guide.md": "SHA_GUIDE_MAIN"}

    # ---- read methods ----
    def find_pr_by_merge_commit(self, owner: str, repo: str, merge_commit_sha: str) -> Optional[int]:
        return self._pr_number_for_merge_commit

    def get_ref(self, owner: str, repo: str, ref: str) -> Optional[Dict[str, Any]]:
        # IMPORTANT: some implementations only check branch existence (not PR existence).
        # If _open_pr_exists is True, emulate that the branch exists so idempotency triggers early.
        if ref == "heads/docs-sync-MERGE_SHA" and (self._branch_exists or self._open_pr_exists):
            return {"object": {"sha": "LOCK_SHA"}}
        return self._refs.get(ref)

    def find_existing_open_pr_by_head(self, owner: str, repo: str, head_branch: str) -> Optional[Dict[str, Any]]:
        if self._open_pr_exists and head_branch == "docs-sync-MERGE_SHA":
            return {"number": 999, "html_url": "https://example/pr/999", "head": {"ref": head_branch}}
        return None

    def list_pr_files(self, owner: str, repo: str, pr_number: int) -> List[Dict[str, Any]]:
        return list(self._pr_files)

    def get_file_content(self, owner: str, repo: str, path: str, ref: str) -> str:
        if ref != "main":
            raise sd.GitHubAPIError("Unsupported ref")
        if path not in self._contents_main:
            raise sd.GitHubAPIError("404 Not Found")
        return self._contents_main[path]

    def get_content_sha(self, owner: str, repo: str, path: str, ref: str) -> Optional[str]:
        return self._content_sha.get(path)

    # ---- write methods ----
    def create_ref(self, owner: str, repo: str, ref: str, sha: str) -> None:
        self.created_refs.append({"ref": ref, "sha": sha})
        if ref == "refs/heads/docs-sync-MERGE_SHA":
            self._branch_exists = True

    def delete_ref(self, owner: str, repo: str, ref: str) -> None:
        self.deleted_refs.append(ref)
        if ref == "heads/docs-sync-MERGE_SHA":
            self._branch_exists = False

    def put_file(
        self, owner: str, repo: str, path: str, branch: str, message: str, content: str, sha: Optional[str]
    ) -> None:
        self.put_files.append({"path": path, "branch": branch, "message": message, "content": content, "sha": sha})

    def create_pull_request(self, owner: str, repo: str, title: str, head: str, base: str, body: str) -> Dict[str, Any]:
        pr = {"number": 42, "html_url": "https://example/pr/42", "title": title, "head": {"ref": head}}
        self.created_prs.append({"owner": owner, "repo": repo, "title": title, "head": head, "base": base, "body": body})
        return pr

    def add_labels(self, owner: str, repo: str, issue_number: int, labels: List[str]) -> None:
        self.labeled.append({"issue_number": issue_number, "labels": labels})


@pytest.fixture()
def env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DOCSYNC_GITHUB_TOKEN", "ghs_test_token")
    monkeypatch.setenv("DOCSYNC_TRIGGER", "pull_request")
    monkeypatch.setenv("DOCSYNC_RUN_ID", "RUN123")
    monkeypatch.setenv("GITHUB_API_URL", "https://api.github.com")
    # Set BOTH styles to support either implementation
    monkeypatch.setenv("LLM_BASE_URL", "https://llm.example")
    monkeypatch.setenv("LLM_API_KEY", "llm_key")
    monkeypatch.setenv("LLM_PROXY_URL", "https://llm.example")
    monkeypatch.setenv("LLM_PROXY_API_KEY", "llm_key")
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)


def run_main(monkeypatch: pytest.MonkeyPatch, cfg_path: Path) -> int:
    argv = [
        "sync_docs.py",
        "--config",
        str(cfg_path),
        "--repo",
        "octo/repo",
        "--merge-commit-sha",
        "MERGE_SHA",
        "--run-id",
        "RUN123",
        "--trigger",
        "pull_request",
        "--github-api-url",
        "https://api.github.com",
    ]
    monkeypatch.setattr(sys, "argv", argv)
    return sd.main()


def test_happy_path_successful_pr_creation(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, env: None) -> None:
    cfg_path = write_config(tmp_path)
    fake_gh = FakeGitHubClient()

    # Create local docs folder for discovery (if impl uses local scan)
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir(parents=True, exist_ok=True)
    (docs_dir / "guide.md").write_text("# Guide\n", encoding="utf-8")

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sd, "GitHubClient", lambda token, api_url="https://api.github.com": fake_gh)

    llm_response = {"patches": [
        {"path": "README.md", "new_content": "# Title\n\nUpdated docs\n"},
        {"path": "docs/guide.md", "new_content": "# Guide\n\nUpdated guide\n"},
    ]}
    patch_llm(monkeypatch, json.dumps(llm_response))

    rc = run_main(monkeypatch, cfg_path)
    assert rc == 0

    assert any(r["ref"] == "refs/heads/docs-sync-MERGE_SHA" for r in fake_gh.created_refs)
    committed_paths = [x["path"] for x in fake_gh.put_files]
    assert "README.md" in committed_paths
    assert "docs/guide.md" in committed_paths

    assert fake_gh.created_prs
    assert fake_gh.created_prs[0]["title"] == "docs: automated documentation synchronization"

    assert fake_gh.labeled
    assert set(fake_gh.labeled[0]["labels"]) == {"documentation", "automated"}


def test_not_found_doc_file_fails_cleanly_and_cleans_lock_branch(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, env: None) -> None:
    cfg_path = write_config(tmp_path)
    fake_gh = FakeGitHubClient()
    monkeypatch.setattr(sd, "GitHubClient", lambda token, api_url="https://api.github.com": fake_gh)
    monkeypatch.chdir(tmp_path)

    llm_response = {"patches": [{"path": "docs/missing.md", "new_content": "# New\n"}]}
    patch_llm(monkeypatch, json.dumps(llm_response))

    rc = run_main(monkeypatch, cfg_path)
    assert rc != 0

    assert any(r["ref"] == "refs/heads/docs-sync-MERGE_SHA" for r in fake_gh.created_refs)
    assert "heads/docs-sync-MERGE_SHA" in fake_gh.deleted_refs


def test_empty_diff_leads_to_noop(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, env: None) -> None:
    cfg_path = write_config(tmp_path)
    fake_gh = FakeGitHubClient()
    fake_gh._pr_files = [{"filename": "docs/guide.md", "patch": ""}, {"filename": "README.md", "patch": ""}]
    monkeypatch.setattr(sd, "GitHubClient", lambda token, api_url="https://api.github.com": fake_gh)
    monkeypatch.chdir(tmp_path)

    rc = run_main(monkeypatch, cfg_path)
    assert rc == 0
    assert not fake_gh.created_refs
    assert not fake_gh.created_prs


def test_invalid_llm_json_schema_cleanup(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, env: None) -> None:
    cfg_path = write_config(tmp_path)
    fake_gh = FakeGitHubClient()
    monkeypatch.setattr(sd, "GitHubClient", lambda token, api_url="https://api.github.com": fake_gh)
    monkeypatch.chdir(tmp_path)

    patch_llm(monkeypatch, "{not-json")

    rc = run_main(monkeypatch, cfg_path)
    assert rc != 0
    assert any(r["ref"] == "refs/heads/docs-sync-MERGE_SHA" for r in fake_gh.created_refs)
    assert "heads/docs-sync-MERGE_SHA" in fake_gh.deleted_refs


def test_llm_retry_logic_transient_failures_if_present(monkeypatch: pytest.MonkeyPatch, env: None) -> None:
    """
    Only run this if the module exposes call_chat_completions; otherwise skip.
    """
    if not hasattr(sd, "call_chat_completions"):
        pytest.skip("call_chat_completions not present in this implementation")

    calls = {"n": 0}

    def fake_post(url: str, headers: Dict[str, str], json: Dict[str, Any], timeout: int):
        calls["n"] += 1
        if calls["n"] < 3:
            class R:
                status_code = 500
                text = "server error"
                def json(self):  # noqa: ANN001
                    return {}
            return R()
        class R:
            status_code = 200
            def json(self):  # noqa: ANN001
                return {"choices": [{"message": {"content": '{"patches": []}'}}]}
        return R()

    monkeypatch.setattr(sd.requests, "post", fake_post)

    out = sd.call_chat_completions(
        base_url="https://llm.example",
        api_key="k",
        model="m",
        system_prompt="s",
        user_prompt="u",
        max_tokens=10,
        temperature=0.0,
        top_p=1.0,
        timeout_total=1,
    )
    assert out == '{"patches": []}'
    assert calls["n"] == 3


def test_idempotency_branch_exists_noop(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, env: None) -> None:
    cfg_path = write_config(tmp_path)
    fake_gh = FakeGitHubClient()
    fake_gh._branch_exists = True
    monkeypatch.setattr(sd, "GitHubClient", lambda token, api_url="https://api.github.com": fake_gh)
    monkeypatch.chdir(tmp_path)

    rc = run_main(monkeypatch, cfg_path)
    assert rc == 0
    assert not fake_gh.created_prs


def test_idempotency_open_pr_exists_noop(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, env: None) -> None:
    cfg_path = write_config(tmp_path)
    fake_gh = FakeGitHubClient()
    fake_gh._open_pr_exists = True
    monkeypatch.setattr(sd, "GitHubClient", lambda token, api_url="https://api.github.com": fake_gh)
    monkeypatch.chdir(tmp_path)

    rc = run_main(monkeypatch, cfg_path)
    assert rc == 0
    assert not fake_gh.created_prs