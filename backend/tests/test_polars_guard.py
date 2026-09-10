"""polars collect 并发闸测试。

针对的缺陷: polars 共享执行器在多线程并发 collect 下可能死锁 (上游 #24448/
#25754 同族), 并发闸限制同时在飞的 collect 数量。此处验证两件事:
- 总闸与 background 车道确实约束并发上限;
- background 车道占满时 interactive 仍能拿到保留闸位 (页面不被后台计算饿死)。
"""
from __future__ import annotations

import threading
import time

import polars as pl

import app.polars_guard as guard
from app.polars_guard import collect_slot, guarded_collect


def _join_all(threads: list[threading.Thread]) -> None:
    for t in threads:
        t.join(timeout=10)
    assert all(not t.is_alive() for t in threads), "collect 闸内线程未结束 — 闸死锁了"


def test_total_permits_bound_background_concurrency(monkeypatch) -> None:
    monkeypatch.setattr(guard, "_TOTAL_GATE", threading.BoundedSemaphore(2))
    monkeypatch.setattr(guard, "_BACKGROUND_LANE", threading.BoundedSemaphore(1))

    counter = {"now": 0, "peak": 0, "bg_now": 0, "bg_peak": 0}
    lock = threading.Lock()

    def enter_interactive() -> None:
        with collect_slot("interactive"):
            with lock:
                counter["now"] += 1
                counter["peak"] = max(counter["peak"], counter["now"])
            time.sleep(0.1)
            with lock:
                counter["now"] -= 1

    def enter_background() -> None:
        with collect_slot("background"):
            with lock:
                counter["bg_now"] += 1
                counter["bg_peak"] = max(counter["bg_peak"], counter["bg_now"])
            time.sleep(0.1)
            with lock:
                counter["bg_now"] -= 1

    threads = [threading.Thread(target=enter_interactive, daemon=True) for _ in range(4)]
    threads += [threading.Thread(target=enter_background, daemon=True) for _ in range(4)]
    for t in threads:
        t.start()
    _join_all(threads)

    assert counter["peak"] <= 2  # 总闸上限
    assert counter["bg_peak"] <= 1  # background 车道上限


def test_interactive_survives_full_background_lane(monkeypatch) -> None:
    total, background = 3, 2
    monkeypatch.setattr(guard, "_TOTAL_GATE", threading.BoundedSemaphore(total))
    monkeypatch.setattr(guard, "_BACKGROUND_LANE", threading.BoundedSemaphore(background))

    holders: list = []
    holder_ready = threading.Event()

    def bg_holder() -> None:
        ctx = collect_slot("background")
        ctx.__enter__()
        holders.append(ctx)
        if len(holders) == background:
            holder_ready.set()

    bg_threads = [threading.Thread(target=bg_holder, daemon=True) for _ in range(background)]
    for t in bg_threads:
        t.start()
    assert holder_ready.wait(timeout=5), "后台线程未占满车道"

    # 车道被 background 占满时, interactive 仍应能在保留闸位内进入并退出。
    done = threading.Event()

    def interactive_probe() -> None:
        with collect_slot("interactive"):
            done.set()

    probe = threading.Thread(target=interactive_probe, daemon=True)
    probe.start()
    assert done.wait(timeout=2), "interactive 被占满的 background 车道饿死"
    probe.join(timeout=2)

    for ctx in holders:
        ctx.__exit__(None, None, None)
    _join_all(bg_threads)


def test_guarded_collect_executes_lazy_frame() -> None:
    lf = pl.LazyFrame({"a": [1, 2, 3]}).filter(pl.col("a") > 1)
    assert guarded_collect(lf)["a"].to_list() == [2, 3]
