from __future__ import annotations

import json
from dataclasses import asdict, replace
from datetime import date, timedelta

import numpy as np
import polars as pl
import pytest

import app.backtest.mining as mining_module
from app.backtest.mining import (
    CandidateEvaluation,
    CorrelationResult,
    FactorMetric,
    MiningBudget,
    MiningRequest,
    MiningService,
    NestedValidationConfig,
    _searchable_factors,
    beam_search_factor_combinations,
    compute_rank_correlation,
    generate_nested_folds,
    nested_fold_count,
    prune_correlated_factors,
    required_outer_folds,
    required_trading_bars,
    validation_config_for_profile,
)


def _panel(days: int = 12, assets: int = 6) -> pl.DataFrame:
    rows = []
    start = date(2024, 1, 2)
    for day_id in range(days):
        current = start + timedelta(days=day_id * 2)
        for asset_id in range(assets):
            target = float(asset_id + (day_id % 2) * 0.1)
            rows.append({
                "symbol": f"{asset_id:06d}.SZ",
                "date": current,
                "good": target,
                "inverse": -target,
                "copy": target * 10.0,
                "noise": float((asset_id * 7 + day_id * 3) % assets),
                "_next_return": target,
            })
    return pl.DataFrame(rows)


def _metrics() -> tuple[FactorMetric, ...]:
    return (
        FactorMetric("good", 1.0, 1.0, 1.0, 0.2, 1.0),
        FactorMetric("copy", 0.9, 0.9, 1.0, 0.1, 1.0),
        FactorMetric("inverse", 0.8, 0.8, 1.0, 0.1, -1.0),
        FactorMetric("noise", 0.2, 0.1, 0.9, 0.5, 0.0),
    )


def test_profiles_enforce_hard_limits_and_are_json_serializable():
    request = MiningRequest.for_profile("exploratory", ["good", "noise"])

    assert request.budget.beam_width == 8
    assert request.validation.outer_train_bars == 126
    assert request.validation.outer_test_bars == 63
    assert NestedValidationConfig.balanced().outer_train_bars == 504
    assert NestedValidationConfig.strict().outer_train_bars == 756
    assert json.loads(request.to_json())["factor_names"] == ["good", "noise"]
    assert asdict(request)["validation"]["purge_bars"] == 30
    with pytest.raises(ValueError, match="max_factors"):
        MiningBudget(max_factors=49)
    with pytest.raises(ValueError, match="beam_width"):
        MiningBudget(beam_width=33)
    with pytest.raises(ValueError, match="max_trials"):
        MiningBudget(max_trials=257)
    with pytest.raises(ValueError, match="existing strategy count"):
        MiningRequest(
            factor_names=("good",),
            existing_strategy_ids=tuple(str(index) for index in range(9)),
        )


@pytest.mark.parametrize(
    ("profile", "required_bars", "folds"),
    [
        ("exploratory", 219, 1),
        ("balanced", 786, 3),
        ("strict", 1164, 3),
    ],
)
def test_profile_trading_bar_requirements(profile, required_bars, folds):
    config = validation_config_for_profile(profile)

    assert required_outer_folds(profile) == folds
    assert required_trading_bars(config, folds) == required_bars
    assert nested_fold_count(required_bars - 1, config) == folds - 1
    assert nested_fold_count(required_bars, config) == folds


def test_rank_correlation_is_pairwise_finite_symmetric_and_average_ranked():
    panel = pl.DataFrame({
        "date": [date(2024, 1, 2)] * 5 + [date(2024, 1, 3)] * 5,
        "a": [1.0, 1.0, 3.0, np.nan, 5.0, 5.0, 4.0, 3.0, 2.0, 1.0],
        "b": [2.0, 2.0, 6.0, 8.0, np.inf, 1.0, 2.0, 3.0, 4.0, 5.0],
        "c": [1.0, None, 2.0, 3.0, 4.0, None, None, None, None, None],
        "constant": [7.0] * 10,
    })

    result = compute_rank_correlation(
        panel,
        ["a", "b", "c", "constant"],
        date(2024, 1, 2),
        date(2024, 1, 3),
    )

    matrix = np.asarray(result.matrix)
    counts = np.asarray(result.pair_counts)
    np.testing.assert_allclose(matrix, matrix.T)
    np.testing.assert_array_equal(np.diag(matrix), np.ones(4))
    assert counts[0, 1] == 2
    assert counts[0, 2] == 1
    assert counts[2, 2] == 1
    assert counts[3, 3] == 2
    assert counts[0, 3] == 0
    assert np.isnan(matrix[0, 3])
    assert result.n_dates == 2
    assert set(result.timing_ms) == {"filter", "rank_accumulate", "finalize", "total"}

    daily_correlations = []
    for daily in panel.partition_by("date"):
        valid = daily.filter(pl.col("a").is_finite() & pl.col("b").is_finite())
        daily_correlations.append(np.corrcoef(
            valid["a"].rank(method="average").to_numpy(),
            valid["b"].rank(method="average").to_numpy(),
        )[0, 1])
    assert daily_correlations == pytest.approx([1.0, -1.0])
    assert matrix[0, 1] == pytest.approx(np.mean(daily_correlations))


def test_rank_correlation_reranks_after_pairwise_finite_intersection():
    panel = pl.DataFrame({
        "date": [date(2024, 1, 2)] * 4,
        "a": [1.0, 2.0, 3.0, 4.0],
        "b": [10.0, None, 20.0, 30.0],
        "c": [None, 10.0, 20.0, 30.0],
    })

    result = compute_rank_correlation(panel, ["a", "b", "c"])
    matrix = np.asarray(result.matrix)

    assert matrix[0, 1] == pytest.approx(1.0)
    assert matrix[0, 2] == pytest.approx(1.0)
    assert matrix[1, 2] == pytest.approx(1.0)


def test_pruning_uses_deterministic_metric_order_and_reports_representative():
    correlation = CorrelationResult(
        factor_names=("a", "b", "c"),
        matrix=((1.0, 0.9, 0.1), (0.9, 1.0, 0.2), (0.1, 0.2, 1.0)),
        pair_counts=((10, 10, 10), (10, 10, 10), (10, 10, 10)),
        elapsed_ms=1.0,
        n_dates=2,
        n_rows=10,
    )
    metrics = (
        FactorMetric("b", 1.0, 2.0, 0.8, 0.1),
        FactorMetric("c", 0.5, 1.0, 1.0, 0.1),
        FactorMetric("a", 1.0, 2.0, 0.8, 0.2),
    )

    result = prune_correlated_factors(metrics, correlation, 0.8)

    assert result.selected == ("b", "c")
    assert result.excluded[0].factor_id == "a"
    assert result.excluded[0].representative == "b"
    assert result.excluded[0].rho == pytest.approx(0.9)


def test_pruning_ignores_unestimable_factor_pairs():
    correlation = CorrelationResult(
        factor_names=("a", "b"),
        matrix=((1.0, float("nan")), (float("nan"), 1.0)),
        pair_counts=((10, 0), (0, 10)),
        elapsed_ms=1.0,
        n_dates=2,
        n_rows=10,
    )
    metrics = (
        FactorMetric("a", 1.0, 1.0, 1.0, 0.1),
        FactorMetric("b", 0.9, 0.9, 1.0, 0.1),
    )

    result = prune_correlated_factors(metrics, correlation, 0.8)

    assert result.selected == ("a", "b")
    assert result.excluded == ()


def test_beam_search_learns_direction_from_train_and_honors_real_proxy_budget():
    panel = _panel()

    first = beam_search_factor_combinations(
        panel,
        ["noise", "inverse", "good"],
        max_combination_size=4,
        beam_width=32,
        max_trials=7,
    )
    second = beam_search_factor_combinations(
        panel,
        ["good", "noise", "inverse"],
        max_combination_size=4,
        beam_width=32,
        max_trials=7,
    )
    reversed_rows = beam_search_factor_combinations(
        panel.reverse(),
        ["good", "noise", "inverse"],
        max_combination_size=4,
        beam_width=32,
        max_trials=7,
    )

    assert first.trials_used == 7
    assert first.budget_exhausted is True
    assert first.candidates == second.candidates == reversed_rows.candidates
    inverse = next(
        candidate
        for candidate in first.candidates
        if candidate.factor_names == ("inverse",)
    )
    assert inverse.directions == (-1,)
    assert all(len(candidate.factor_names) <= 4 for candidate in first.candidates)
    assert all(
        set(candidate.weights) <= {1.0, 2.0}
        and sum(weight == 2.0 for weight in candidate.weights) <= 1
        for candidate in first.candidates
    )


def test_beam_search_does_not_access_rows_outside_explicit_train_range():
    panel = _panel(days=10)
    train_end = sorted(panel["date"].unique().to_list())[5]
    changed = panel.with_columns(
        pl.when(pl.col("date") > train_end)
        .then(-pl.col("_next_return") * 1_000.0)
        .otherwise(pl.col("_next_return"))
        .alias("_next_return")
    )

    original_result = beam_search_factor_combinations(
        panel,
        ["good", "inverse", "noise"],
        train_end=train_end,
        max_trials=30,
    )
    changed_result = beam_search_factor_combinations(
        changed,
        ["good", "inverse", "noise"],
        train_end=train_end,
        max_trials=30,
    )

    assert original_result.candidates == changed_result.candidates


def test_nested_folds_use_trading_labels_with_explicit_purge_and_embargo():
    labels = [f"T{index:02d}" for index in range(22)]
    config = NestedValidationConfig(
        outer_train_bars=12,
        outer_test_bars=4,
        outer_step_bars=4,
        inner_train_bars=5,
        inner_test_bars=2,
        inner_step_bars=2,
        purge_bars=1,
        embargo_bars=2,
        min_train_bars=5,
    )

    folds = generate_nested_folds(labels, config)

    assert folds[0].outer.train_labels == tuple(labels[:12])
    assert folds[0].outer.purge_labels == ("T12",)
    assert folds[0].outer.test_labels == tuple(labels[13:17])
    assert folds[0].outer.embargo_labels == ("T17", "T18")
    assert folds[0].inner[0].train_labels == tuple(labels[:5])
    assert folds[0].inner[0].purge_labels == ("T05",)
    assert folds[0].inner[0].test_labels == ("T06", "T07")
    assert set(folds[0].inner[-1].test_labels).issubset(folds[0].outer.train_labels)
    with pytest.raises(ValueError, match="insufficient trading bars"):
        generate_nested_folds(labels[:10], config)


def _frame_labels(frame) -> tuple[str, ...]:
    return tuple(
        frame.select(pl.col("date").cast(pl.Utf8).str.slice(0, 10).unique().sort())
        .to_series()
        .to_list()
    )


class _Evaluator:
    def __init__(self) -> None:
        self.calls: list[tuple[tuple[str, ...], tuple[str, ...], dict]] = []

    def evaluate_candidate(self, train, test, definition):
        train_labels = _frame_labels(train)
        test_labels = _frame_labels(test)
        self.calls.append((train_labels, test_labels, dict(definition)))
        return {"score": len(definition.get("factor_names", ())) or 0.1}


class _AlternatingWinnerEvaluator(_Evaluator):
    """Prefer the good factor only in windows before 2024-01-25; every later window prefers the runner-up.

    With a 20-day panel (dates spaced two calendar days apart) and a
    non-overlapping outer step, every inner and outer window of the first
    fold falls in January while every window of the second fold falls after
    the boundary, so the per-fold winner flips and cross-fold evaluation
    has two distinct definitions to score.
    """

    def evaluate_candidate(self, train, test, definition):
        self.calls.append((_frame_labels(train), _frame_labels(test), dict(definition)))
        prefer_good = _frame_labels(test)[0] < "2024-01-25"
        names = definition.get("factor_names") or ()
        is_good = bool(names) and names[0] == "good"
        return {"score": 2.0 if is_good == prefer_good else 1.0}


class _LabelEvaluator(_Evaluator):
    def __init__(self) -> None:
        super().__init__()
        self.label_calls: list[tuple[tuple[str, ...], tuple[str, ...], dict]] = []

    def evaluate_candidate(self, train, test, definition):
        raise AssertionError("label evaluator must not receive fold DataFrames")

    def evaluate_candidate_labels(self, train_labels, test_labels, definition):
        self.label_calls.append((
            tuple(train_labels),
            tuple(test_labels),
            dict(definition),
        ))
        return {"score": len(definition.get("factor_names", ())) or 0.1}


@pytest.mark.parametrize("evaluator_type", [_Evaluator, _LabelEvaluator])
def test_mining_service_reselects_per_inner_fold_and_keeps_tests_out_of_selection(
    evaluator_type,
):
    panel = _panel(days=10)
    validation = NestedValidationConfig(
        outer_train_bars=8,
        outer_test_bars=2,
        outer_step_bars=2,
        inner_train_bars=4,
        inner_test_bars=2,
        inner_step_bars=2,
        purge_bars=0,
        embargo_bars=0,
        min_train_bars=4,
    )
    request = MiningRequest(
        factor_names=("good", "copy", "inverse", "noise"),
        correlation_threshold=0.95,
        budget=MiningBudget(
            max_combination_size=2,
            beam_width=4,
            max_proxy_trials=18,
            max_trials=3,
        ),
        validation=validation,
        profile="exploratory",
    )
    folds = generate_nested_folds(
        sorted(str(value) for value in panel["date"].unique().to_list()),
        validation,
    )
    assert len(folds) == 1
    metric_calls: list[tuple[str, ...]] = []

    def metric_provider(train, factor_names):
        assert tuple(factor_names) == request.factor_names
        labels = tuple(
            train.select(pl.col("date").cast(pl.Utf8).str.slice(0, 10).unique().sort())
            .to_series()
            .to_list()
        )
        metric_calls.append(labels)
        return _metrics()

    evaluator = evaluator_type()
    result = MiningService().run(
        panel,
        request,
        metric_provider=metric_provider,
        evaluator=evaluator,
    )

    nested = folds[0]
    expected_selection_labels = [
        *(inner.train_labels for inner in nested.inner),
        nested.outer.train_labels,
    ]
    assert metric_calls == expected_selection_labels
    assert result.trials_used == request.budget.max_trials
    assert result.proxy_trials_used <= request.budget.max_proxy_trials
    evaluator_calls = (
        evaluator.label_calls
        if isinstance(evaluator, _LabelEvaluator)
        else evaluator.calls
    )
    assert len(evaluator_calls) == len(nested.inner) + 1
    for call, inner in zip(evaluator_calls[:-1], nested.inner, strict=True):
        train_labels, test_labels, _ = call
        assert train_labels == inner.train_labels
        assert test_labels == inner.test_labels
        assert not set(nested.outer.test_labels).intersection(train_labels + test_labels)
    outer_train, outer_test, outer_definition = evaluator_calls[-1]
    assert outer_train == nested.outer.train_labels
    assert outer_test == nested.outer.test_labels
    assert result.folds[0].selected_candidate_id is not None
    selected = next(
        candidate
        for candidate in result.folds[0].candidates
        if candidate.candidate_id == result.folds[0].selected_candidate_id
    )
    assert outer_definition == selected.definition()

    changed = panel.with_columns(
        pl.when(
            pl.col("date")
            .cast(pl.Utf8)
            .str.slice(0, 10)
            .is_in(nested.outer.test_labels)
        )
        .then(-pl.col("good") * 999.0)
        .otherwise(pl.col("good"))
        .alias("good")
    )
    without_evaluation = MiningService().run(
        panel,
        request,
        factor_metrics=_metrics(),
    )
    changed_result = MiningService().run(
        changed,
        request,
        factor_metrics=_metrics(),
    )
    assert without_evaluation.folds[0].selected_factors == (
        changed_result.folds[0].selected_factors
    )
    assert without_evaluation.folds[0].candidates == changed_result.folds[0].candidates

    with pytest.raises(ValueError, match="requires at least 3 outer folds"):
        MiningService().run(
            panel,
            replace(request, profile="balanced"),
            factor_metrics=_metrics(),
        )


def test_mining_service_requires_fold_local_metrics_with_evaluator():
    panel = _panel(days=10)
    request = MiningRequest(
        factor_names=("good",),
        budget=MiningBudget(max_combination_size=1, max_proxy_trials=12, max_trials=3),
        validation=NestedValidationConfig(
            outer_train_bars=8,
            outer_test_bars=2,
            outer_step_bars=2,
            inner_train_bars=4,
            inner_test_bars=2,
            inner_step_bars=2,
            purge_bars=0,
            embargo_bars=0,
            min_train_bars=4,
        ),
        profile="exploratory",
    )

    with pytest.raises(ValueError, match="metric_provider is required"):
        MiningService().run(panel, request, evaluator=_Evaluator())
    with pytest.raises(ValueError, match="factor_metrics must not be supplied"):
        MiningService().run(
            panel,
            request,
            factor_metrics=(FactorMetric("good", 1.0, 1.0, 1.0, 0.1),),
            metric_provider=lambda _train, _names: (
                FactorMetric("good", 1.0, 1.0, 1.0, 0.1),
            ),
            evaluator=_Evaluator(),
        )


def test_target_endpoint_is_removed_from_every_train_phase_but_not_tests(monkeypatch):
    panel = _panel(days=10)
    labels = sorted(panel["date"].unique().to_list())
    target_dates = pl.DataFrame({
        "date": labels,
        "_target_date": [*labels[1:], labels[-1] + timedelta(days=2)],
    })
    panel = panel.join(target_dates, on="date", how="left")
    validation = NestedValidationConfig(
        outer_train_bars=8,
        outer_test_bars=2,
        outer_step_bars=2,
        inner_train_bars=4,
        inner_test_bars=2,
        inner_step_bars=2,
        purge_bars=0,
        embargo_bars=0,
        min_train_bars=4,
    )
    request = MiningRequest(
        factor_names=("good",),
        budget=MiningBudget(max_combination_size=1, max_proxy_trials=12, max_trials=3),
        validation=validation,
        profile="exploratory",
    )
    nested = generate_nested_folds([str(label) for label in labels], validation)[0]
    train_ends = [*(inner.train_end for inner in nested.inner), nested.outer.train_end]
    metric_frames = []
    correlation_frames = []
    beam_frames = []

    def assert_target_bounded(frame, train_end):
        assert frame.filter(
            pl.col("_target_date").cast(pl.Utf8).str.slice(0, 10) > train_end
        ).is_empty()

    def metric_provider(train, factor_names):
        train_end = train_ends[len(metric_frames)]
        assert tuple(factor_names) == request.factor_names
        assert_target_bounded(train, train_end)
        metric_frames.append(train)
        return (FactorMetric("good", 1.0, 1.0, 1.0, 0.1),)

    original_correlation = mining_module.compute_rank_correlation
    original_beam = mining_module.beam_search_factor_combinations

    def checked_correlation(frame, *args, **kwargs):
        train_end = train_ends[len(correlation_frames)]
        assert_target_bounded(frame, train_end)
        correlation_frames.append(frame)
        return original_correlation(frame, *args, **kwargs)

    def checked_beam(frame, *args, **kwargs):
        train_end = train_ends[len(beam_frames)]
        assert_target_bounded(frame, train_end)
        beam_frames.append(frame)
        return original_beam(frame, *args, **kwargs)

    class EndpointEvaluator(_Evaluator):
        def __init__(self):
            super().__init__()
            self.test_frames = []

        def evaluate_candidate(self, train, test, definition):
            self.test_frames.append(test)
            return super().evaluate_candidate(train, test, definition)

    monkeypatch.setattr(mining_module, "compute_rank_correlation", checked_correlation)
    monkeypatch.setattr(mining_module, "beam_search_factor_combinations", checked_beam)
    evaluator = EndpointEvaluator()

    result = MiningService().run(
        panel,
        request,
        metric_provider=metric_provider,
        evaluator=evaluator,
    )

    assert result.folds[0].error is None
    assert len(metric_frames) == len(correlation_frames) == len(beam_frames) == 3
    evaluation_folds = [*nested.inner, nested.outer]
    assert len(evaluator.calls) == len(evaluation_folds)
    for (train_labels, test_labels, _), test_frame, fold in zip(
        evaluator.calls,
        evaluator.test_frames,
        evaluation_folds,
        strict=True,
    ):
        assert train_labels == fold.train_labels[:-1]
        assert test_labels == fold.test_labels
        assert test_frame.height == len(fold.test_labels) * 6
        assert not test_frame.filter(
            pl.col("_target_date").cast(pl.Utf8).str.slice(0, 10) > fold.test_end
        ).is_empty()


def test_outer_refit_fails_when_selected_factor_structure_is_missing():
    panel = _panel(days=10)
    request = MiningRequest(
        factor_names=("good", "copy"),
        correlation_threshold=0.8,
        budget=MiningBudget(max_combination_size=1, max_proxy_trials=24, max_trials=8),
        validation=NestedValidationConfig(
            outer_train_bars=8,
            outer_test_bars=2,
            outer_step_bars=2,
            inner_train_bars=4,
            inner_test_bars=2,
            inner_step_bars=2,
            purge_bars=0,
            embargo_bars=0,
            min_train_bars=4,
        ),
        profile="exploratory",
    )

    def metric_provider(train, _factor_names):
        if train["date"].n_unique() < 8:
            return (
                FactorMetric("good", 1.0, 1.0, 1.0, 0.1),
                FactorMetric("copy", 0.5, 0.5, 1.0, 0.1),
            )
        return (
            FactorMetric("good", 0.5, 0.5, 1.0, 0.1),
            FactorMetric("copy", 1.0, 1.0, 1.0, 0.1),
        )

    evaluator = _Evaluator()
    result = MiningService().run(
        panel,
        request,
        metric_provider=metric_provider,
        evaluator=evaluator,
    )

    fold = result.folds[0]
    assert len(evaluator.calls) == 2
    assert fold.selected_candidate_id is None
    assert fold.outer_evaluation is None
    assert fold.error == "outer retraining did not reproduce selected candidate structure"
    assert {candidate.factor_names for candidate in fold.candidates} == {("copy",)}


def test_candidate_evaluation_dataclass_score_must_be_finite():
    panel = _panel(days=10)
    request = MiningRequest(
        factor_names=("good",),
        budget=MiningBudget(max_combination_size=1, max_proxy_trials=12, max_trials=8),
        validation=NestedValidationConfig(
            outer_train_bars=8,
            outer_test_bars=2,
            outer_step_bars=2,
            inner_train_bars=4,
            inner_test_bars=2,
            inner_step_bars=2,
            purge_bars=0,
            embargo_bars=0,
            min_train_bars=4,
        ),
        profile="exploratory",
    )

    class NonFiniteEvaluator:
        def evaluate_candidate(self, train, test, definition):
            return CandidateEvaluation(score=float("nan"), metrics={"source": "test"})

    result = MiningService().run(
        panel,
        request,
        metric_provider=lambda _train, _names: (
            FactorMetric("good", 1.0, 1.0, 1.0, 0.1),
        ),
        evaluator=NonFiniteEvaluator(),
    )

    assert result.folds[0].selected_candidate_id is None
    assert result.folds[0].error == "no candidate completed inner validation within budget"


def test_beam_search_skips_factors_without_sufficient_valid_observations():
    panel = _panel().with_columns(
        pl.lit(None).cast(pl.Float64).alias("all_null"),
        pl.lit(7.0).alias("constant"),
        pl.col("inverse").alias("valid_inverse"),
        pl.when(pl.col("symbol").is_in(["000000.SZ", "000001.SZ"]))
        .then(pl.col("good"))
        .otherwise(None)
        .alias("sparse"),
    )

    result = beam_search_factor_combinations(
        panel,
        ["all_null", "constant", "good", "sparse", "valid_inverse"],
        max_combination_size=2,
        max_trials=30,
    )
    invalid_only = beam_search_factor_combinations(
        panel,
        ["all_null", "constant", "sparse"],
        max_combination_size=2,
        max_trials=30,
    )

    assert result.candidates
    assert all(
        set(candidate.factor_names) <= {"good", "valid_inverse"}
        for candidate in result.candidates
    )
    assert {candidate.factor_names for candidate in result.candidates} >= {
        ("good",),
        ("valid_inverse",),
        ("good", "valid_inverse"),
    }
    assert all(candidate.dates > 0 and candidate.observations >= 3 for candidate in result.candidates)
    assert invalid_only.candidates == ()


def test_small_real_allowance_evaluates_factor_and_finalists_stay_capped():
    panel = _panel(days=10)
    request = MiningRequest(
        factor_names=("good", "noise"),
        existing_strategy_ids=tuple(f"existing-{index}" for index in range(8)),
        budget=MiningBudget(max_combination_size=1, max_proxy_trials=12, max_trials=1),
        validation=NestedValidationConfig(
            outer_train_bars=8,
            outer_test_bars=2,
            outer_step_bars=2,
            inner_train_bars=4,
            inner_test_bars=2,
            inner_step_bars=2,
            purge_bars=0,
            embargo_bars=0,
            min_train_bars=4,
        ),
        profile="exploratory",
    )
    evaluator = _Evaluator()

    result = MiningService().run(
        panel,
        request,
        metric_provider=lambda _train, _names: (
            FactorMetric("good", 1.0, 1.0, 1.0, 0.1),
            FactorMetric("noise", 0.5, 0.5, 1.0, 0.1),
        ),
        evaluator=evaluator,
    )

    assert len(evaluator.calls) == 1
    assert evaluator.calls[0][2]["kind"] == "factor_rank"
    finalists = result.folds[0].candidates
    assert all(candidate.kind == "factor_rank" for candidate in finalists)
    assert len(finalists) == 2
    assert result.folds[0].selected_candidate_id is not None


def test_existing_strategies_are_benchmarked_on_every_outer_fold():
    panel = _panel(days=18)
    request = MiningRequest(
        factor_names=("good",),
        existing_strategy_ids=("alpha", "beta"),
        budget=MiningBudget(max_combination_size=1, max_proxy_trials=24, max_trials=64),
        validation=NestedValidationConfig(
            outer_train_bars=8,
            outer_test_bars=2,
            outer_step_bars=4,
            inner_train_bars=4,
            inner_test_bars=2,
            inner_step_bars=2,
            purge_bars=0,
            embargo_bars=0,
            min_train_bars=4,
        ),
        profile="exploratory",
    )
    evaluator = _Evaluator()

    result = MiningService().run(
        panel,
        request,
        metric_provider=lambda _train, _names: (FactorMetric("good", 1.0, 1.0, 1.0, 0.1),),
        evaluator=evaluator,
    )

    assert len(result.folds) == 3
    for fold in result.folds:
        assert all(candidate.kind == "factor_rank" for candidate in fold.candidates)
        signatures = [candidate_id for candidate_id, _ in fold.benchmark_evaluations]
        assert signatures == ["strategy:alpha", "strategy:beta"]
        for _, evaluation in fold.benchmark_evaluations:
            assert evaluation.error is None
    evaluated_strategy_kinds = [
        definition["kind"]
        for *_, definition in evaluator.calls
        if definition["kind"] == "existing_strategy"
    ]
    assert len(evaluated_strategy_kinds) == 6


def test_winner_definitions_are_cross_evaluated_on_all_outer_folds():
    panel = _panel(days=20)
    request = MiningRequest(
        factor_names=("good", "noise"),
        budget=MiningBudget(max_combination_size=1, max_proxy_trials=24, max_trials=64),
        validation=NestedValidationConfig(
            outer_train_bars=8,
            outer_test_bars=2,
            outer_step_bars=10,
            inner_train_bars=4,
            inner_test_bars=2,
            inner_step_bars=2,
            purge_bars=0,
            embargo_bars=0,
            min_train_bars=4,
        ),
        profile="exploratory",
    )
    evaluator = _AlternatingWinnerEvaluator()

    result = MiningService().run(
        panel,
        request,
        metric_provider=lambda _train, _names: (
            FactorMetric("good", 1.0, 1.0, 1.0, 0.1),
            FactorMetric("noise", 0.9, 0.9, 1.0, 0.1),
        ),
        evaluator=evaluator,
    )

    assert len(result.folds) == 2
    winners = {fold.selected_candidate_id for fold in result.folds}
    assert len(winners) == 2
    for fold in result.folds:
        cross_ids = [candidate_id for candidate_id, _ in fold.cross_evaluations]
        assert cross_ids == sorted(winners - {fold.selected_candidate_id})
        for _, evaluation in fold.cross_evaluations:
            assert evaluation.error is None


def test_benchmarks_run_even_when_factor_track_fails_on_a_fold():
    panel = _panel(days=18)
    request = MiningRequest(
        factor_names=("good",),
        existing_strategy_ids=("alpha",),
        budget=MiningBudget(max_combination_size=1, max_proxy_trials=24, max_trials=64),
        validation=NestedValidationConfig(
            outer_train_bars=8,
            outer_test_bars=2,
            outer_step_bars=4,
            inner_train_bars=4,
            inner_test_bars=2,
            inner_step_bars=2,
            purge_bars=0,
            embargo_bars=0,
            min_train_bars=4,
        ),
        profile="exploratory",
    )

    class _FailingEvaluator(_Evaluator):
        def evaluate_candidate(self, train, test, definition):
            raise RuntimeError("backtest exploded")

    result = MiningService().run(
        panel,
        request,
        metric_provider=lambda _train, _names: (FactorMetric("good", 1.0, 1.0, 1.0, 0.1),),
        evaluator=_FailingEvaluator(),
    )

    assert len(result.folds) == 1
    fold = result.folds[0]
    assert fold.error == "no candidate completed inner validation within budget"
    assert fold.selected_candidate_id is None
    assert [candidate_id for candidate_id, _ in fold.benchmark_evaluations] == [
        "strategy:alpha"
    ]
    benchmark = fold.benchmark_evaluations[0][1]
    assert benchmark.error == "backtest exploded"


def test_searchable_factors_are_capped_by_beam_width():
    budget = MiningBudget(beam_width=3)
    assert _searchable_factors(("a", "b", "c", "d", "e"), budget) == ("a", "b", "c")
    assert _searchable_factors(("a",), MiningBudget(beam_width=8)) == ("a",)


def test_beam_search_only_receives_capped_factor_inputs():
    names = tuple(f"factor_{index:02d}" for index in range(20))
    rows = []
    start = date(2024, 1, 2)
    for day_id in range(12):
        for asset_id in range(6):
            row = {
                "symbol": f"{asset_id:06d}.SZ",
                "date": start + timedelta(days=day_id),
                "_next_return": float(asset_id),
            }
            for factor_id, name in enumerate(names):
                row[name] = float((asset_id * (factor_id + 1)) % 7) + factor_id * 0.5
            rows.append(row)
    panel = pl.DataFrame(rows)
    request = MiningRequest(
        factor_names=names,
        correlation_threshold=1.0,
        budget=MiningBudget(
            max_combination_size=2,
            beam_width=4,
            max_proxy_trials=96,
            max_trials=8,
        ),
        validation=NestedValidationConfig(
            outer_train_bars=8,
            outer_test_bars=2,
            outer_step_bars=4,
            inner_train_bars=4,
            inner_test_bars=2,
            inner_step_bars=2,
            purge_bars=0,
            embargo_bars=0,
            min_train_bars=4,
        ),
        profile="exploratory",
    )
    evaluator = _Evaluator()

    result = MiningService().run(
        panel,
        request,
        metric_provider=lambda _train, factor_names: tuple(
            FactorMetric(name, 1.0 - index * 0.01, 1.0, 1.0, 0.1)
            for index, name in enumerate(factor_names)
        ),
        evaluator=evaluator,
    )

    searched = {
        factor_name
        for candidate in result.folds[0].candidates
        for factor_name in candidate.factor_names
    }
    assert searched <= set(names[:4])
    assert any(len(candidate.factor_names) == 2 for candidate in result.folds[0].candidates)
