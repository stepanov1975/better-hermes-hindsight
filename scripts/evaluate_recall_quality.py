#!/usr/bin/env python3
"""Evaluate labeled Better Hindsight recall results without printing private text."""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import NoReturn, cast

from better_hermes_hindsight.client import RecallResponse, RecallResult, create_hindsight_client
from better_hermes_hindsight.config import BetterHindsightConfig, load_config
from better_hermes_hindsight.formatting import (
    TEXT_TRUNCATION_MARKER,
    format_recall_context_with_selected_results,
    project_query,
)

_SCHEMA_VERSION = 1
_VARIANTS = ("baseline", "prefer_observations")
_CASE_KEYS = {
    "id",
    "query",
    "expect_recall",
    "useful_result_ids",
    "redundant_result_ids",
    "irrelevant_result_ids",
    "responses",
}
_RESPONSE_KEYS = {"elapsed_ms", "results"}
_RESULT_KEYS = {"id", "text"}


class EvaluationInputError(ValueError):
    """Raised when a quality corpus is not strict, complete JSON."""


@dataclass(frozen=True, slots=True)
class LabeledResult:
    result_id: str
    text: str


@dataclass(frozen=True, slots=True)
class VariantResponse:
    results: tuple[LabeledResult, ...]
    elapsed_ms: float | None = None


@dataclass(frozen=True, slots=True)
class QualityCase:
    case_id: str
    query: str
    expect_recall: bool
    useful_result_ids: frozenset[str]
    redundant_result_ids: frozenset[str]
    irrelevant_result_ids: frozenset[str]
    responses: Mapping[str, VariantResponse]


class _DuplicateJsonKey(ValueError):
    pass


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateJsonKey(key)
        result[key] = value
    return result


def _fail(message: str) -> NoReturn:
    raise EvaluationInputError(message)


def _mapping(value: object, field: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or not all(isinstance(key, str) for key in value):
        _fail(f"{field} must be a JSON object")
    return cast(Mapping[str, object], value)


def _exact_keys(value: Mapping[str, object], allowed: set[str], field: str) -> None:
    unknown = sorted(set(value) - allowed)
    if unknown:
        _fail(f"{field} has unknown key(s): {', '.join(unknown)}")


def _nonempty_string(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        _fail(f"{field} must be a non-empty string")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        _fail(f"{field} must contain valid Unicode text")
    return value


def _id_set(value: object, field: str) -> frozenset[str]:
    if not isinstance(value, list):
        _fail(f"{field} must be a JSON array")
    items = [_nonempty_string(item, f"{field}[]") for item in value]
    if len(items) != len(set(items)):
        _fail(f"{field} must not contain duplicate IDs")
    return frozenset(items)


def _parse_result(value: object, field: str) -> LabeledResult:
    result = _mapping(value, field)
    if set(result) != _RESULT_KEYS:
        _fail(f"{field} must contain exactly id and text")
    return LabeledResult(
        result_id=_nonempty_string(result["id"], f"{field}.id"),
        text=_nonempty_string(result["text"], f"{field}.text"),
    )


def _parse_response(value: object, field: str) -> VariantResponse:
    response = _mapping(value, field)
    _exact_keys(response, _RESPONSE_KEYS, field)
    if "results" not in response:
        _fail(f"{field}.results is required")
    raw_results = response["results"]
    if not isinstance(raw_results, list):
        _fail(f"{field}.results must be a JSON array")
    results = tuple(
        _parse_result(result, f"{field}.results[{index}]")
        for index, result in enumerate(raw_results)
    )
    result_ids = [result.result_id for result in results]
    if len(result_ids) != len(set(result_ids)):
        _fail(f"{field}.results must not contain duplicate IDs")
    elapsed = response.get("elapsed_ms")
    if elapsed is not None and (
        isinstance(elapsed, bool)
        or not isinstance(elapsed, (int, float))
        or not math.isfinite(elapsed)
        or elapsed < 0
    ):
        _fail(f"{field}.elapsed_ms must be a finite non-negative number")
    return VariantResponse(
        results=results,
        elapsed_ms=None if elapsed is None else float(elapsed),
    )


def _parse_case(value: object, index: int) -> QualityCase:
    field = f"cases[{index}]"
    case = _mapping(value, field)
    _exact_keys(case, _CASE_KEYS, field)
    required = _CASE_KEYS - {"responses"}
    missing = sorted(required - set(case))
    if missing:
        _fail(f"{field} is missing key(s): {', '.join(missing)}")
    expect_recall = case["expect_recall"]
    if not isinstance(expect_recall, bool):
        _fail(f"{field}.expect_recall must be a boolean")
    useful = _id_set(case["useful_result_ids"], f"{field}.useful_result_ids")
    redundant = _id_set(case["redundant_result_ids"], f"{field}.redundant_result_ids")
    irrelevant = _id_set(case["irrelevant_result_ids"], f"{field}.irrelevant_result_ids")
    if (useful & redundant) or (useful & irrelevant) or (redundant & irrelevant):
        _fail(f"{field} result-label ID sets must be disjoint")
    if not expect_recall and useful:
        _fail(f"{field} cannot label useful results when expect_recall is false")
    raw_responses = case.get("responses", {})
    responses_mapping = _mapping(raw_responses, f"{field}.responses")
    unknown_variants = sorted(set(responses_mapping) - set(_VARIANTS))
    if unknown_variants:
        _fail(f"{field}.responses has unknown variant(s): {', '.join(unknown_variants)}")
    responses = {
        variant: _parse_response(response, f"{field}.responses.{variant}")
        for variant, response in responses_mapping.items()
    }
    return QualityCase(
        case_id=_nonempty_string(case["id"], f"{field}.id"),
        query=_nonempty_string(case["query"], f"{field}.query"),
        expect_recall=expect_recall,
        useful_result_ids=useful,
        redundant_result_ids=redundant,
        irrelevant_result_ids=irrelevant,
        responses=responses,
    )


def load_corpus(path: Path) -> tuple[QualityCase, ...]:
    try:
        payload = json.loads(
            path.read_text(encoding="utf-8"), object_pairs_hook=_reject_duplicate_keys
        )
    except FileNotFoundError:
        raise EvaluationInputError("corpus file does not exist") from None
    except (OSError, UnicodeError):
        raise EvaluationInputError("corpus file could not be read as UTF-8") from None
    except _DuplicateJsonKey as error:
        raise EvaluationInputError(f"corpus contains duplicate JSON key: {error.args[0]}") from None
    except json.JSONDecodeError as error:
        raise EvaluationInputError(
            f"corpus is not valid JSON at line {error.lineno}, column {error.colno}"
        ) from None
    root = _mapping(payload, "corpus")
    if set(root) != {"schema_version", "cases"}:
        _fail("corpus must contain exactly schema_version and cases")
    schema_version = root["schema_version"]
    if type(schema_version) is not int or schema_version != _SCHEMA_VERSION:
        _fail(f"corpus.schema_version must be the integer {_SCHEMA_VERSION}")
    raw_cases = root["cases"]
    if not isinstance(raw_cases, list) or not raw_cases:
        _fail("corpus.cases must be a non-empty JSON array")
    cases = tuple(_parse_case(case, index) for index, case in enumerate(raw_cases))
    case_ids = [case.case_id for case in cases]
    if len(case_ids) != len(set(case_ids)):
        _fail("corpus.cases must not contain duplicate IDs")
    return cases


def _safe_ratio(numerator: int, denominator: int) -> float | None:
    return None if denominator == 0 else round(numerator / denominator, 6)


def _has_substantive_memory_text(text: str) -> bool:
    prefix = text[: -len(TEXT_TRUNCATION_MARKER)] if text.endswith(TEXT_TRUNCATION_MARKER) else text
    return bool(prefix.strip())


def evaluate_variant(
    cases: Sequence[QualityCase],
    responses: Mapping[str, VariantResponse],
) -> dict[str, int | float | None]:
    expected_recall_cases = sum(case.expect_recall for case in cases)
    negative_cases = len(cases) - expected_recall_cases
    expected_memory_count = sum(len(case.useful_result_ids) for case in cases)
    expected_memory_hits = 0
    useful_at_3_hits = 0
    useful_at_3_slots = 0
    irrelevant_returns = 0
    redundant_returns = 0
    unlabeled_returns = 0
    no_context_correct = 0
    returned_records = 0
    returned_text_bytes = 0
    fully_truncated_returns = 0
    elapsed_values: list[float] = []

    for case in cases:
        response = responses[case.case_id]
        result_ids = [result.result_id for result in response.results]
        usable_result_ids = {
            result.result_id
            for result in response.results
            if _has_substantive_memory_text(result.text)
        }
        expected_memory_hits += len(case.useful_result_ids & usable_result_ids)
        top_3 = response.results[:3]
        useful_at_3_hits += sum(
            result.result_id in case.useful_result_ids and _has_substantive_memory_text(result.text)
            for result in top_3
        )
        useful_at_3_slots += len(top_3)
        irrelevant_returns += sum(
            result_id in case.irrelevant_result_ids for result_id in result_ids
        )
        redundant_returns += sum(result_id in case.redundant_result_ids for result_id in result_ids)
        labeled_ids = (
            case.useful_result_ids | case.redundant_result_ids | case.irrelevant_result_ids
        )
        unlabeled_returns += sum(result_id not in labeled_ids for result_id in result_ids)
        if not case.expect_recall and not result_ids:
            no_context_correct += 1
        returned_records += len(response.results)
        returned_text_bytes += sum(len(result.text.encode("utf-8")) for result in response.results)
        fully_truncated_returns += sum(
            not _has_substantive_memory_text(result.text) for result in response.results
        )
        if response.elapsed_ms is not None:
            elapsed_values.append(response.elapsed_ms)

    return {
        "case_count": len(cases),
        "elapsed_ms_total": (
            round(sum(elapsed_values), 3) if len(elapsed_values) == len(cases) else None
        ),
        "expected_memory_count": expected_memory_count,
        "expected_memory_coverage": _safe_ratio(expected_memory_hits, expected_memory_count),
        "expected_memory_hits": expected_memory_hits,
        "expected_recall_cases": expected_recall_cases,
        "fully_truncated_returns": fully_truncated_returns,
        "irrelevant_returns": irrelevant_returns,
        "negative_cases": negative_cases,
        "no_context_correct": no_context_correct,
        "no_context_rate": _safe_ratio(no_context_correct, negative_cases),
        "redundant_returns": redundant_returns,
        "returned_records": returned_records,
        "returned_text_bytes": returned_text_bytes,
        "unlabeled_returns": unlabeled_returns,
        "useful_at_3_hits": useful_at_3_hits,
        "useful_at_3_precision": _safe_ratio(useful_at_3_hits, useful_at_3_slots),
    }


def _offline_responses(
    cases: Sequence[QualityCase], variants: Sequence[str]
) -> dict[str, dict[str, VariantResponse]]:
    by_variant: dict[str, dict[str, VariantResponse]] = {variant: {} for variant in variants}
    for case in cases:
        missing = [variant for variant in variants if variant not in case.responses]
        if missing:
            _fail(f"case {case.case_id!r} has no fixture response for: {', '.join(missing)}")
        for variant in variants:
            by_variant[variant][case.case_id] = case.responses[variant]
    return by_variant


def comparison_configs(
    config: BetterHindsightConfig,
) -> tuple[BetterHindsightConfig, BetterHindsightConfig]:
    if config.recall.prefer_observations is True:
        _fail("configured baseline already sets recall.prefer_observations=true")
    preferred_recall = replace(config.recall, prefer_observations=True)
    return config, replace(config, recall=preferred_recall)


def _response_for_evaluation(
    response: RecallResponse,
    elapsed_ms: float,
    *,
    max_bytes: int,
) -> VariantResponse:
    _context, records, selected_results = format_recall_context_with_selected_results(
        response,
        max_bytes=max_bytes,
    )
    return VariantResponse(
        results=tuple(
            LabeledResult(
                result_id=cast(RecallResult, result).id,
                text=cast(str, record["memory"]),
            )
            for record, result in zip(records, selected_results, strict=True)
        ),
        elapsed_ms=round(elapsed_ms, 3),
    )


async def collect_live_responses(
    cases: Sequence[QualityCase],
    config: BetterHindsightConfig,
    *,
    compare_prefer_observations: bool,
) -> dict[str, dict[str, VariantResponse]]:
    configs = comparison_configs(config) if compare_prefer_observations else (config,)
    variants = _VARIANTS if compare_prefer_observations else (_VARIANTS[0],)
    responses: dict[str, dict[str, VariantResponse]] = {}
    for variant, variant_config in zip(variants, configs, strict=True):
        client = create_hindsight_client(variant_config)
        variant_responses: dict[str, VariantResponse] = {}
        try:
            for case in cases:
                projected = project_query(
                    case.query,
                    max_chars=variant_config.recall.input_max_chars,
                    max_tokens=variant_config.recall.input_max_tokens,
                )
                if not projected.strip():
                    variant_responses[case.case_id] = VariantResponse(results=(), elapsed_ms=0.0)
                    continue
                started = time.perf_counter()
                response = await asyncio.wait_for(
                    client.recall(projected),
                    timeout=variant_config.recall.timeout_seconds,
                )
                elapsed_ms = (time.perf_counter() - started) * 1000
                variant_responses[case.case_id] = _response_for_evaluation(
                    response,
                    elapsed_ms,
                    max_bytes=variant_config.recall.context_max_bytes,
                )
        finally:
            await client.close()
        responses[variant] = variant_responses
    return responses


def evaluate(
    cases: Sequence[QualityCase],
    responses: Mapping[str, Mapping[str, VariantResponse]],
) -> dict[str, object]:
    return {
        "result": "ok",
        "schema_version": _SCHEMA_VERSION,
        "variants": {variant: evaluate_variant(cases, responses[variant]) for variant in responses},
    }


def main() -> int:
    parser = argparse.ArgumentParser(allow_abbrev=False)
    parser.add_argument("corpus", type=Path)
    parser.add_argument(
        "--hermes-home",
        type=Path,
        help="Explicit absolute Hermes home for read-only live recall; omit for fixture responses.",
    )
    parser.add_argument(
        "--compare-prefer-observations",
        action="store_true",
        help="Compare the configured baseline with only prefer_observations set true.",
    )
    args = parser.parse_args()
    try:
        cases = load_corpus(args.corpus)
        if args.hermes_home is None:
            variants = _VARIANTS if args.compare_prefer_observations else (_VARIANTS[0],)
            responses = _offline_responses(cases, variants)
        else:
            if not args.hermes_home.is_absolute():
                _fail("--hermes-home must be an explicit absolute path")
            config = load_config(args.hermes_home)
            if not config.authorize_cli().recall_enabled:
                _fail("configured CLI principal is not authorized for recall")
            responses = asyncio.run(
                collect_live_responses(
                    cases,
                    config,
                    compare_prefer_observations=args.compare_prefer_observations,
                )
            )
        print(json.dumps(evaluate(cases, responses), sort_keys=True, separators=(",", ":")))
    except EvaluationInputError as error:
        parser.error(str(error))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
