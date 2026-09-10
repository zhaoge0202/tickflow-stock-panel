"""复盘推送触发方式 review_push_mode 最小 demo — 验证 auto/manual 白名单与默认值。

运行方式(在 backend/ 目录下, 已安装依赖):
    python -m scripts.demo_recap_push_mode

隔离到临时偏好文件, 不污染真实偏好。
"""
from __future__ import annotations

import tempfile
from pathlib import Path

from app.services import preferences


def main() -> None:
    prefs_path = Path(tempfile.mkdtemp()) / "preferences.json"
    preferences._path = lambda: prefs_path  # noqa: SLF001 - demo 隔离
    preferences._invalidate_cache()

    print("== 复盘推送模式 review_push_mode ==")
    print(f"   默认 = {preferences.get_review_push_mode()} (期望 manual)")
    print(f"   设为 auto = {preferences.set_review_push_mode('auto')}")
    print(f"   非法值回退 = {preferences.set_review_push_mode('bogus')} (期望 manual)")

    print("\n全部通过")


if __name__ == "__main__":
    main()
