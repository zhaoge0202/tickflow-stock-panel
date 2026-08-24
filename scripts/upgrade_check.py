#!/usr/bin/env python3
"""Read-only Git overlap and conflict preview for secondary-development branches."""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path


def _git(root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["git", *args],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    if check and result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise RuntimeError(detail or f"git {' '.join(args)} failed")
    return result


def _lines(result: subprocess.CompletedProcess[str]) -> set[str]:
    return {line.strip() for line in result.stdout.splitlines() if line.strip()}


def _resolve_repo(path: Path) -> Path:
    result = _git(path, "rev-parse", "--show-toplevel")
    return Path(result.stdout.strip()).resolve()


def _resolve_commit(root: Path, ref: str) -> str:
    return _git(root, "rev-parse", "--verify", f"{ref}^{{commit}}").stdout.strip()


def _merge_tree_conflicts(root: Path, base: str, current: str, target: str) -> list[str]:
    result = _git(root, "merge-tree", base, current, target)
    lines = result.stdout.splitlines()
    conflicts: list[str] = []
    current_path: str | None = None
    current_conflicted = False
    for line in [*lines, ""]:
        if line in {"changed in both", "added in both", "removed in both"}:
            if current_path and current_conflicted:
                conflicts.append(current_path)
            current_path = None
            current_conflicted = False
            continue
        parts = line.strip().split()
        if current_path is None and len(parts) >= 4 and parts[0] in {"base", "our", "their"}:
            current_path = parts[-1]
        if line.startswith("+<<<<<<< ") or line.startswith("+=======") or line.startswith("+>>>>>>> "):
            current_conflicted = True
    if current_path and current_conflicted:
        conflicts.append(current_path)
    return sorted(set(conflicts))


def _print_paths(title: str, paths: list[str]) -> None:
    print(f"\n{title}: {len(paths)}")
    for path in paths:
        print(f"  - {path}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="只读检查当前二开分支升级到目标 Git ref 时的源码重叠与文本冲突",
    )
    parser.add_argument("target", help="目标 Tag/分支/commit, 例如 v0.3.1")
    parser.add_argument(
        "--repo",
        type=Path,
        default=Path.cwd(),
        help="仓库路径, 默认当前目录",
    )
    args = parser.parse_args(argv)

    try:
        root = _resolve_repo(args.repo.resolve())
        current = _resolve_commit(root, "HEAD")
        target = _resolve_commit(root, args.target)
        base = _git(root, "merge-base", current, target).stdout.strip()
        if not base:
            raise RuntimeError("当前分支与目标版本没有共同基线")

        current_paths = _lines(_git(root, "diff", "--name-only", f"{base}..{current}"))
        target_paths = _lines(_git(root, "diff", "--name-only", f"{base}..{target}"))
        overlap = sorted(current_paths & target_paths)
        conflicts = _merge_tree_conflicts(root, base, current, target)
        dirty = sorted(_lines(_git(root, "status", "--short")))

        print("Tick Stock Panel 二开升级预检")
        print(f"仓库: {root}")
        print(f"当前: {current[:12]}")
        print(f"目标: {args.target} ({target[:12]})")
        print(f"共同基线: {base[:12]}")
        print(f"当前分支修改文件: {len(current_paths)}")
        print(f"目标版本修改文件: {len(target_paths)}")
        _print_paths("双方修改的同一文件", overlap)
        _print_paths("Git 三方预演识别的文本冲突", conflicts)

        if dirty:
            print(f"\n未提交工作区条目: {len(dirty)}")
            print("  注意: 未提交内容不会进入 Git 三方预演, 请先提交到临时分支后再复检。")
        if conflicts:
            print("\n结论: 需要人工解决文本冲突并执行完整回归。")
            return 2
        if overlap:
            print("\n结论: Git 未发现文本冲突, 但重叠文件需要检查语义兼容和测试。")
            return 0
        print("\n结论: 未发现源码重叠; 仍需执行目标版本的验证矩阵。")
        return 0
    except (OSError, RuntimeError) as exc:
        print(f"升级预检失败: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
