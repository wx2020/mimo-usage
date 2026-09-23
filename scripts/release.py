#!/usr/bin/env python3
"""一键发版：版本号统一推进 + release notes 三处同步。

用法：
    python3 scripts/release.py 1.1.0 --dry-run          # 预览（不改任何东西）
    python3 scripts/release.py 1.1.0                    # 提交 + 打 tag（本地）
    python3 scripts/release.py 1.1.0 --push             # 再推送 main 与 tag
    python3 scripts/release.py 1.1.0 --push --github    # 再用 API 建 GitHub Release

约定（三处同步，避免"只有 tag 有 notes、commit 却空着"）：
    ① release 提交 body = notes（`--cleanup=verbatim`，否则 `#` 开头的标题会被删）
    ② annotated tag message = notes（同样 `verbatim`）
    ③ GitHub Release body = tag message（`--github`，需 OpenChamber 的 github-auth.json token）

notes 可按 conventional commits 自动生成（`## What's Changed` / `## Feature` /
`## Bugfix` / `## Contributors`），也可用 `--notes FILE` 指定现成 markdown。
"""

from __future__ import annotations

import argparse
import json
import pathlib
import re
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
#: 版本号需要同步的文件（旧 -> 新 的替换规则）
VERSION_FILES = [
    ("pyproject.toml", r'^version = ".*"$'),
    ("mimo_usage/__init__.py", r'^__version__ = ".*"$'),
    ("mimo_usage/app.py", r'^STATIC_VERSION = ".*"'),
    ("tests/test_entrypoint.py", r'^    assert mimo_usage\.__version__ == ".*"$'),
]
TOKEN_FILE = pathlib.Path.home() / ".config" / "openchamber" / "github-auth.json"


def run(*args: str, check: bool = True) -> str:
    proc = subprocess.run(args, cwd=ROOT, capture_output=True, text=True)
    if check and proc.returncode != 0:
        sys.exit(f"命令失败：{' '.join(args)}\n{proc.stderr.strip()}")
    return proc.stdout.strip()


def fail(message: str) -> None:
    sys.exit(f"✗ {message}")


def previous_tag() -> str | None:
    out = run("git", "describe", "--tags", "--abbrev=0", check=False)
    return out or None


def collect_commits(since: str | None) -> list[tuple[str, str]]:
    """返回 [(short_sha, subject)]，旧 -> 新。"""
    rev = f"{since}..HEAD" if since else "HEAD"
    log = run("git", "log", "--reverse", "--pretty=format:%h%x00%s", rev)
    rows = [line.split("\x00", 1) for line in log.splitlines() if line]
    # 过滤掉上一次的发版提交本身
    return [(sha, subj) for sha, subj in rows if not subj.startswith("chore(release):")]


def build_notes(version: str, commits: list[tuple[str, str]], author: str) -> str:
    def kind(subject: str) -> str | None:
        m = re.match(r"^(feat|fix)(\([^)]*\))?!?:", subject)
        return m.group(1) if m else None

    features = [f"* {s} ({sha})" for sha, s in commits if kind(s) == "feat"]
    bugfixes = [f"* {s} ({sha})" for sha, s in commits if kind(s) == "fix"]
    changed = [f"* {s} ({sha})" for sha, s in commits] or ["* 维护性发布"]
    lines = ["## What's Changed", *changed, "", "## Feature"]
    lines += features or ["- 无"]
    lines += ["", "## Bugfix"]
    lines += bugfixes or ["- 无"]
    lines += ["", "## Contributors", f"* @{author}"]
    return "\n".join(lines)


def bump_versions(version: str, dry_run: bool) -> list[str]:
    touched: list[str] = []
    for rel, pattern in VERSION_FILES:
        path = ROOT / rel
        text = path.read_text(encoding="utf-8")
        new_text, count = re.subn(
            pattern,
            lambda match: re.sub(r'"[^"]*"', f'"{version}"', match.group(0)),
            text,
            count=1,
            flags=re.M,
        )
        if count != 1:
            fail(f"{rel} 中未唯一匹配版本行（匹配 {count} 处），请检查 VERSION_FILES 规则")
        if new_text != text:
            touched.append(rel)
            if not dry_run:
                path.write_text(new_text, encoding="utf-8")
    return touched


def create_github_release(tag: str, notes: str, dry_run: bool) -> None:
    if not TOKEN_FILE.exists():
        fail(f"未找到 {TOKEN_FILE}，无法创建 GitHub Release（可去掉 --github 手工建）")
    auth = json.loads(TOKEN_FILE.read_text(encoding="utf-8"))
    tokens = [
        item["accessToken"]
        for item in auth
        if isinstance(item, dict) and item.get("accessToken") and item.get("current")
    ]
    if not tokens:
        fail("github-auth.json 中没有 current: true 的 token")
    slug = run("git", "remote", "get-url", "origin")
    m = re.search(r"github\.com[:/](?P<slug>[^/]+/[^/.]+)", slug)
    if not m:
        fail(f"无法从 origin 解析 owner/repo：{slug}")
    repo = m.group("slug")
    if dry_run:
        print(f"  [dry-run] 会创建 GitHub Release：{repo} @ {tag}（body 取 tag message）")
        return
    import urllib.request

    payload = json.dumps({"tag_name": tag, "name": tag, "body": notes,
                          "draft": False, "prerelease": False}).encode()
    req = urllib.request.Request(
        f"https://api.github.com/repos/{repo}/releases", data=payload, method="POST",
        headers={"Authorization": f"Bearer {tokens[0]}", "Accept": "application/vnd.github+json",
                 "Content-Type": "application/json", "X-GitHub-Api-Version": "2022-11-28"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.load(resp)
        print(f"  ✓ GitHub Release: {data.get('html_url')}")
    except Exception as exc:  # noqa: BLE001 - 打印可读原因即可
        fail(f"创建 GitHub Release 失败：{exc}")


def main() -> None:
    parser = argparse.ArgumentParser(description="mimo-usage 发版")
    parser.add_argument("version", help="语义化版本号，如 1.1.0")
    parser.add_argument("--notes", help="现成的 notes markdown 文件（默认按 conventional commits 自动生成）")
    parser.add_argument("--push", action="store_true", help="推送 main 与 tag 到 origin")
    parser.add_argument("--github", action="store_true", help="用 API 创建 GitHub Release（需 GitHub token）")
    parser.add_argument("--dry-run", action="store_true", help="只打印将要做的改动")
    args = parser.parse_args()

    version = args.version.lstrip("v")
    tag = f"v{version}"

    if run("git", "status", "--porcelain"):
        fail("工作区不干净，请先提交或 stash")
    if run("git", "rev-parse", "--abbrev-ref", "HEAD") != "main":
        fail("请在 main 分支发版")
    if run("git", "rev-parse", "-q", "--verify", f"refs/tags/{tag}", check=False):
        fail(f"tag {tag} 已存在")

    since = previous_tag()
    commits = collect_commits(since)
    author = run("git", "config", "user.name") or "wx2020"
    if args.notes:
        notes = pathlib.Path(args.notes).read_text(encoding="utf-8").rstrip("\n")
    else:
        notes = build_notes(version, commits, author)
    if not notes.strip():
        fail("notes 为空")

    print(f"发版 {tag}（自 {since or '仓库起始'}，{len(commits)} 个提交）")
    print("── notes ─────────────────────────────────")
    print("\n".join(f"  {line}" for line in notes.splitlines()))
    print("──────────────────────────────────────────")

    touched = bump_versions(version, args.dry_run)
    for rel in touched:
        print(f"  {'[dry-run] ' if args.dry_run else ''}版本号 → {version}: {rel}")

    if args.dry_run:
        print("  [dry-run] 会提交：chore(release): " + version + "（body = 上面 notes，cleanup=verbatim）")
        print(f"  [dry-run] 会打 annotated tag {tag}（message = notes，cleanup=verbatim）")
        if args.push:
            print("  [dry-run] 会推送 main 与 tag")
        if args.github:
            create_github_release(tag, notes, dry_run=True)
        return

    # 提交：subject + 空行 + notes；必须 verbatim，否则 "#" 开头的标题会被删掉
    msg_file = ROOT / ".git" / "MIMO_RELEASE_MSG"
    msg_file.write_text(f"chore(release): {version}\n\n{notes}\n", encoding="utf-8")
    run("git", "add", "-A")
    run("git", "commit", "--cleanup=verbatim", "-F", str(msg_file))
    msg_file.unlink(missing_ok=True)
    print(f"  ✓ 已提交（body {len(notes.splitlines())} 行）")

    tag_file = ROOT / ".git" / "MIMO_RELEASE_TAGMSG"
    tag_file.write_text(notes + "\n", encoding="utf-8")
    run("git", "tag", "-a", "--cleanup=verbatim", "-F", str(tag_file), tag)
    tag_file.unlink(missing_ok=True)
    print(f"  ✓ 已打 tag {tag}")

    if args.push:
        run("git", "push", "origin", "main")
        run("git", "push", "origin", tag)
        print("  ✓ 已推送 main 与 tag")
    if args.github:
        create_github_release(tag, notes, dry_run=False)


if __name__ == "__main__":
    main()
