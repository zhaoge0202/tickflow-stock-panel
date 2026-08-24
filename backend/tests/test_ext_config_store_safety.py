"""ExtConfigStore 的 config_id 安全校验 — 拒绝路径穿越等非法 id (fail-closed)。

删除端点的 config_id 来自 URL path 参数, 不经创建端点的 pattern 校验;
若直接拼接路径, `../victim` 可让 rmtree 删除 ext_data 之外的目录。
"""
from __future__ import annotations

import json

from app.services.ext_data import ExtConfigStore


def _write_config(data_dir, config_id: str) -> None:
    d = data_dir / "ext_data" / config_id
    d.mkdir(parents=True, exist_ok=True)
    (d / "config.json").write_text(
        json.dumps({
            "id": config_id,
            "label": "测试",
            "mode": "snapshot",
            "fields": [{"name": "score"}],
        }),
        encoding="utf-8",
    )


def test_delete_rejects_path_traversal_ids(tmp_path):
    store = ExtConfigStore(tmp_path)
    _write_config(tmp_path, "ok_config")

    # 穿越目标: ext_data 之外、含 config.json 的目录 (满足旧实现 rmtree 的前置条件)
    victim = tmp_path / "victim"
    victim.mkdir()
    (victim / "config.json").write_text("{}", encoding="utf-8")

    for bad in ("../victim", "..\\victim", "a/b", "", "."):
        assert store.delete(bad) is False, bad
        assert store.get(bad) is None, bad
    assert (victim / "config.json").exists(), "穿越删除必须被拒绝"

    # 合法 id 不受影响
    assert store.get("ok_config") is not None
    assert store.delete("ok_config") is True
