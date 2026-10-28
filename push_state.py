#!/usr/bin/env python3
"""把 state.json 推去 monitor-state 分支，唔好寫入 main，以免 GitHub Pages 每 30 秒 rebuild。"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

STATE_BRANCH = "monitor-state"
STATE_PATH = Path(__file__).resolve().parent / "state.json"
API_VERSION = "2022-11-28"


def repo_from_env() -> str:
    repo = os.environ.get("GITHUB_REPOSITORY") or ""
    if "/" not in repo:
        raise SystemExit("GITHUB_REPOSITORY 未設定（例如 owner/iphone-monitor）")
    return repo


def token_from_env() -> str:
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN") or ""
    if not token:
        raise SystemExit("GH_TOKEN / GITHUB_TOKEN 未設定")
    return token


def encode_contents(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def decode_contents(content: str) -> bytes:
    return base64.b64decode(content.encode("ascii"))


class GitHub:
    def __init__(self, repo: str, token: str, opener: Any = None) -> None:
        self.repo = repo
        self.token = token
        self.opener = opener or urllib.request.urlopen

    def request(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        data = None if body is None else json.dumps(body).encode("utf-8")
        req = urllib.request.Request(
            f"https://api.github.com{path}",
            data=data,
            method=method,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": API_VERSION,
                "User-Agent": "iphone-monitor",
                "Content-Type": "application/json",
            },
        )
        try:
            with self.opener(req, timeout=30) as resp:
                raw = resp.read()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as exc:
            raw = exc.read() if exc.fp is not None else b""
            exc.detail = raw.decode("utf-8", errors="replace")  # type: ignore[attr-defined]
            raise

    def ref_sha(self, branch: str) -> str | None:
        try:
            data = self.request("GET", f"/repos/{self.repo}/git/ref/heads/{branch}")
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return None
            raise
        return str((data.get("object") or {}).get("sha") or "") or None

    def ensure_branch(self, branch: str, from_branch: str = "main") -> None:
        if self.ref_sha(branch):
            return
        source = self.ref_sha(from_branch)
        if not source:
            raise SystemExit(f"找不到來源分支 {from_branch}")
        try:
            self.request(
                "POST",
                f"/repos/{self.repo}/git/refs",
                {"ref": f"refs/heads/{branch}", "sha": source},
            )
        except urllib.error.HTTPError as exc:
            if exc.code in {409, 422} and self.ref_sha(branch):
                return
            raise

    def get_file(self, branch: str, path: str = "state.json") -> dict[str, Any] | None:
        try:
            return self.request(
                "GET",
                f"/repos/{self.repo}/contents/{path}?ref={branch}",
            )
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return None
            raise

    def put_file(
        self,
        *,
        branch: str,
        raw: bytes,
        message: str,
        sha: str | None,
        path: str = "state.json",
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "message": message,
            "content": encode_contents(raw),
            "branch": branch,
        }
        if sha:
            body["sha"] = sha
        return self.request("PUT", f"/repos/{self.repo}/contents/{path}", body)


def pull_state(gh: GitHub, dest: Path = STATE_PATH, branch: str = STATE_BRANCH) -> bool:
    info = gh.get_file(branch)
    if not info or not info.get("content"):
        print(f"{branch} 未有 state.json，沿用本地檔")
        return False
    dest.write_bytes(decode_contents(str(info["content"])))
    print(f"已由 {branch} 拉低 state.json")
    return True


def push_state(
    gh: GitHub,
    src: Path = STATE_PATH,
    branch: str = STATE_BRANCH,
    message: str = "chore: update inventory state",
) -> bool:
    if not src.exists():
        print("本地冇 state.json，跳過")
        return False
    raw = src.read_bytes()
    if b"<<<<<<<" in raw or b">>>>>>>" in raw:
        print("state.json 有衝突標記，跳過 push")
        return False
    gh.ensure_branch(branch)
    for attempt in range(3):
        current = gh.get_file(branch)
        sha = str(current["sha"]) if current and current.get("sha") else None
        if current and current.get("content") and decode_contents(str(current["content"])) == raw:
            print("state.json unchanged")
            return True
        try:
            gh.put_file(branch=branch, raw=raw, message=message, sha=sha)
            print(f"已推 state.json 去 {branch}")
            return True
        except urllib.error.HTTPError as exc:
            if exc.code in {409, 422} and attempt < 2:
                print(f"push 衝突 HTTP {exc.code}，重試")
                continue
            raise
    return False


def self_test() -> int:
    import tempfile
    from email.message import Message

    failures = 0

    def check(cond: bool, name: str) -> None:
        nonlocal failures
        if cond:
            print(f"  PASS  {name}")
        else:
            failures += 1
            print(f"  FAIL  {name}")

    check(STATE_BRANCH == "monitor-state", "狀態分支叫 monitor-state")
    check(encode_contents(b"abc") == "YWJj", "內容用 base64")
    check(decode_contents("YWJj") == b"abc", "可以解返 base64")

    class FakeResp:
        def __init__(self, payload: Any) -> None:
            self.payload = payload

        def read(self) -> bytes:
            return json.dumps(self.payload).encode("utf-8")

        def __enter__(self) -> "FakeResp":
            return self

        def __exit__(self, *args: object) -> None:
            return None

    calls: list[tuple[str, str]] = []
    created = {"monitor": False}

    def opener(req: urllib.request.Request, timeout: int = 30) -> FakeResp:
        calls.append((req.get_method() or "GET", req.full_url))
        method = req.get_method() or "GET"
        url = req.full_url
        if url.endswith("/git/ref/heads/monitor-state"):
            if created["monitor"]:
                return FakeResp({"object": {"sha": "bbb"}})
            raise urllib.error.HTTPError(url, 404, "gone", Message(), None)
        if url.endswith("/git/ref/heads/main"):
            return FakeResp({"object": {"sha": "aaa"}})
        if url.endswith("/git/refs") and method == "POST":
            created["monitor"] = True
            return FakeResp({"ref": "refs/heads/monitor-state"})
        if "contents/state.json" in url and method == "GET":
            return FakeResp({"sha": "file1", "content": encode_contents(b'{"a":1}\n')})
        if method == "PUT":
            return FakeResp({"content": {"sha": "file2"}})
        raise AssertionError(url)

    gh = GitHub("readbarapp/iphone-monitor", "token", opener=opener)
    gh.ensure_branch(STATE_BRANCH)
    check(created["monitor"], "未有分支就會由 main 開 monitor-state")
    with tempfile.NamedTemporaryFile(delete=False) as tmp:
        tmp.write(b'{"loop":{"tick_count":1}}\n')
        tmp_path = Path(tmp.name)
    try:
        check(push_state(gh, src=tmp_path), "可以把本地 state 推去 monitor-state")
        check(any(m == "PUT" and "contents/state.json" in url for m, url in calls), "push 用 Contents API")
    finally:
        tmp_path.unlink(missing_ok=True)
    return failures


def main() -> int:
    parser = argparse.ArgumentParser(description="同步 state.json 到 monitor-state 分支")
    parser.add_argument("--pull", action="store_true", help="由 monitor-state 拉低 state.json")
    parser.add_argument("--push", action="store_true", help="把本地 state.json 推去 monitor-state")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        failed = self_test()
        print(f"push_state 自我測試完成，失敗數： {failed}")
        return 1 if failed else 0
    if not args.pull and not args.push:
        parser.error("請指定 --pull 或 --push")
    gh = GitHub(repo_from_env(), token_from_env())
    if args.pull:
        pull_state(gh)
    if args.push:
        push_state(gh)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
