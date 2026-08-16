from __future__ import annotations

from collections.abc import Mapping
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from .contracts import FINANCIAL_METRICS, require_string
from .snapshot import ValidatedSnapshot

CALCULATION_METRICS = (
    "revenue_growth",
    "operating_margin",
    "free_cash_flow_margin",
    "market_cap",
    "net_cash",
    "enterprise_value",
    "ev_to_revenue",
    "free_cash_flow_yield",
)

OK = "OK"
UNKNOWN = "UNKNOWN"
NOT_MEANINGFUL = "NOT_MEANINGFUL"
FACT_METRIC_ORDER = {metric: index for index, metric in enumerate(FINANCIAL_METRICS)}


def build_calculation_bundle(
    snapshot: ValidatedSnapshot,
    candidate: Mapping[str, Any],
    decision_at: datetime,
) -> dict[str, Any]:
    fact_refs = list(candidate.get("financial_fact_refs", []))
    eligible_fact_records: list[dict[str, Any]] = []
    cutoff_excluded_fact_ids: list[str] = []
    facts_by_metric: dict[str, list[dict[str, Any]]] = {}

    for fact_id in fact_refs:
        fact = dict(snapshot.financial_facts_by_id[fact_id])
        available_at = datetime.fromisoformat(fact["available_at"])
        if available_at <= decision_at:
            eligible_fact_records.append(fact)
            facts_by_metric.setdefault(fact["metric"], []).append(fact)
        else:
            cutoff_excluded_fact_ids.append(fact_id)

    for items in facts_by_metric.values():
        items.sort(key=_fact_order_key, reverse=True)

    # Canonical fact ordering is independent of candidate refs or snapshot list order:
    # group by the public contract metric order, then show the newest eligible revision first.
    eligible_fact_records.sort(key=_fact_order_key, reverse=True)
    eligible_fact_records.sort(key=_fact_metric_order_key)

    calculations = [
        _revenue_growth(facts_by_metric, cutoff_excluded_fact_ids),
        _ratio_metric(
            "operating_margin",
            "operating_income_ttm / revenue_ttm",
            "ratio",
            facts_by_metric,
            numerator_metric="operating_income_ttm",
            denominator_metric="revenue_ttm",
        ),
        _ratio_metric(
            "free_cash_flow_margin",
            "free_cash_flow_ttm / revenue_ttm",
            "ratio",
            facts_by_metric,
            numerator_metric="free_cash_flow_ttm",
            denominator_metric="revenue_ttm",
        ),
        _market_cap(facts_by_metric),
        _net_cash(facts_by_metric),
        _enterprise_value(facts_by_metric),
        _ev_to_revenue(facts_by_metric),
        _free_cash_flow_yield(facts_by_metric),
    ]

    return {
        "facts": [_fact_card(fact) for fact in eligible_fact_records],
        "calculations": calculations,
        "cutoff_excluded_fact_ids": cutoff_excluded_fact_ids,
    }


def build_calculations(
    snapshot: ValidatedSnapshot,
    candidate: Mapping[str, Any],
    decision_at: datetime,
) -> list[dict[str, Any]]:
    return build_calculation_bundle(snapshot, candidate, decision_at)["calculations"]


def _revenue_growth(
    facts_by_metric: Mapping[str, list[dict[str, Any]]],
    cutoff_excluded_fact_ids: list[str],
) -> dict[str, Any]:
    metric = "revenue_growth"
    formula = "(revenue_ttm_current - revenue_ttm_prior) / revenue_ttm_prior"
    revenues = _latest_distinct_period_facts(facts_by_metric.get("revenue_ttm", []), limit=2)
    if len(revenues) < 2:
        input_fact_ids = [fact["fact_id"] for fact in revenues[:1]]
        evidence_refs = _evidence_refs(revenues[:1])
        period_end = revenues[0]["period_end"] if revenues else None
        return _unknown(metric, formula, "ratio", input_fact_ids, evidence_refs, period_end)

    current_fact = revenues[0]
    prior_fact = revenues[1]
    current_value = _decimal_value(current_fact)
    prior_value = _decimal_value(prior_fact)
    input_facts = [current_fact, prior_fact]
    if current_value is None or prior_value is None:
        return _unknown(
            metric,
            formula,
            "ratio",
            [fact["fact_id"] for fact in input_facts if _decimal_value(fact) is not None],
            _evidence_refs(input_facts),
            current_fact["period_end"],
        )
    if prior_value == 0:
        return _not_meaningful(
            metric, formula, "ratio", input_facts, current_fact["period_end"], None
        )

    value = (current_value - prior_value) / prior_value
    _ = cutoff_excluded_fact_ids
    return _ok(
        metric, formula, float(value), "ratio", input_facts, current_fact["period_end"], None
    )


def _ratio_metric(
    metric: str,
    formula: str,
    unit: str,
    facts_by_metric: Mapping[str, list[dict[str, Any]]],
    *,
    numerator_metric: str,
    denominator_metric: str,
) -> dict[str, Any]:
    numerator = _latest_fact(facts_by_metric, numerator_metric)
    denominator = _latest_fact(facts_by_metric, denominator_metric)
    if numerator is None or denominator is None:
        present_facts = [fact for fact in (numerator, denominator) if fact is not None]
        period_end = denominator["period_end"] if denominator is not None else None
        return _unknown(
            metric,
            formula,
            unit,
            [fact["fact_id"] for fact in present_facts],
            _evidence_refs(present_facts),
            period_end,
        )

    numerator_value = _decimal_value(numerator)
    denominator_value = _decimal_value(denominator)
    if numerator_value is None or denominator_value is None:
        return _unknown(
            metric,
            formula,
            unit,
            [
                fact["fact_id"]
                for fact in (numerator, denominator)
                if _decimal_value(fact) is not None
            ],
            _evidence_refs([numerator, denominator]),
            denominator["period_end"],
        )
    if denominator_value == 0:
        return _not_meaningful(
            metric, formula, unit, [numerator, denominator], denominator["period_end"], None
        )

    value = numerator_value / denominator_value
    return _ok(
        metric,
        formula,
        float(value),
        unit,
        [numerator, denominator],
        denominator["period_end"],
        None,
    )


def _market_cap(facts_by_metric: Mapping[str, list[dict[str, Any]]]) -> dict[str, Any]:
    metric = "market_cap"
    formula = "diluted_shares * close_price"
    shares = _latest_fact(facts_by_metric, "diluted_shares")
    price = _latest_fact(facts_by_metric, "close_price")
    if shares is None or price is None:
        present_facts = [fact for fact in (shares, price) if fact is not None]
        period_end = shares["period_end"] if shares is not None else None
        price_as_of = price["period_end"] if price is not None else None
        return _unknown(
            metric,
            formula,
            "USD",
            [fact["fact_id"] for fact in present_facts],
            _evidence_refs(present_facts),
            period_end,
            price_as_of,
        )

    shares_value = _decimal_value(shares)
    price_value = _decimal_value(price)
    if shares_value is None or price_value is None:
        return _unknown(
            metric,
            formula,
            "USD",
            [fact["fact_id"] for fact in (shares, price) if _decimal_value(fact) is not None],
            _evidence_refs([shares, price]),
            shares["period_end"],
            price["period_end"],
        )

    market_cap = shares_value * price_value
    if market_cap <= 0:
        return _not_meaningful(
            metric,
            formula,
            "USD",
            [shares, price],
            shares["period_end"],
            price["period_end"],
        )
    return _ok(
        metric,
        formula,
        float(market_cap),
        "USD",
        [shares, price],
        shares["period_end"],
        price["period_end"],
    )


def _net_cash(facts_by_metric: Mapping[str, list[dict[str, Any]]]) -> dict[str, Any]:
    metric = "net_cash"
    formula = "cash_and_equivalents - total_debt"
    cash = _latest_fact(facts_by_metric, "cash_and_equivalents")
    debt = _latest_fact(facts_by_metric, "total_debt")
    if cash is None or debt is None:
        present_facts = [fact for fact in (cash, debt) if fact is not None]
        period_end = (
            cash["period_end"]
            if cash is not None
            else debt["period_end"]
            if debt is not None
            else None
        )
        return _unknown(
            metric,
            formula,
            "USD",
            [fact["fact_id"] for fact in present_facts],
            _evidence_refs(present_facts),
            period_end,
        )

    cash_value = _decimal_value(cash)
    debt_value = _decimal_value(debt)
    if cash_value is None or debt_value is None:
        return _unknown(
            metric,
            formula,
            "USD",
            [fact["fact_id"] for fact in (cash, debt) if _decimal_value(fact) is not None],
            _evidence_refs([cash, debt]),
            cash["period_end"],
        )

    value = cash_value - debt_value
    return _ok(metric, formula, float(value), "USD", [cash, debt], cash["period_end"], None)


def _enterprise_value(facts_by_metric: Mapping[str, list[dict[str, Any]]]) -> dict[str, Any]:
    metric = "enterprise_value"
    formula = "(diluted_shares * close_price) + total_debt - cash_and_equivalents"
    cash = _latest_fact(facts_by_metric, "cash_and_equivalents")
    debt = _latest_fact(facts_by_metric, "total_debt")
    shares = _latest_fact(facts_by_metric, "diluted_shares")
    price = _latest_fact(facts_by_metric, "close_price")
    required = [cash, debt, shares, price]
    if any(fact is None for fact in required):
        present_facts = [fact for fact in required if fact is not None]
        period_end = (
            cash["period_end"]
            if cash is not None
            else debt["period_end"]
            if debt is not None
            else shares["period_end"]
            if shares is not None
            else None
        )
        price_as_of = price["period_end"] if price is not None else None
        return _unknown(
            metric,
            formula,
            "USD",
            [fact["fact_id"] for fact in present_facts],
            _evidence_refs(present_facts),
            period_end,
            price_as_of,
        )

    assert cash is not None and debt is not None and shares is not None and price is not None
    cash_value = _decimal_value(cash)
    debt_value = _decimal_value(debt)
    shares_value = _decimal_value(shares)
    price_value = _decimal_value(price)
    if None in {cash_value, debt_value, shares_value, price_value}:
        return _unknown(
            metric,
            formula,
            "USD",
            [
                fact["fact_id"]
                for fact in (cash, debt, shares, price)
                if _decimal_value(fact) is not None
            ],
            _evidence_refs([cash, debt, shares, price]),
            cash["period_end"],
            price["period_end"],
        )

    market_cap = shares_value * price_value
    if market_cap <= 0:
        return _not_meaningful(
            metric,
            formula,
            "USD",
            [cash, debt, shares, price],
            cash["period_end"],
            price["period_end"],
        )

    value = market_cap + debt_value - cash_value
    return _ok(
        metric,
        formula,
        float(value),
        "USD",
        [cash, debt, shares, price],
        cash["period_end"],
        price["period_end"],
    )


def _ev_to_revenue(facts_by_metric: Mapping[str, list[dict[str, Any]]]) -> dict[str, Any]:
    metric = "ev_to_revenue"
    formula = "enterprise_value / revenue_ttm"
    revenue = _latest_fact(facts_by_metric, "revenue_ttm")
    cash = _latest_fact(facts_by_metric, "cash_and_equivalents")
    debt = _latest_fact(facts_by_metric, "total_debt")
    shares = _latest_fact(facts_by_metric, "diluted_shares")
    price = _latest_fact(facts_by_metric, "close_price")
    required = [revenue, cash, debt, shares, price]
    if any(fact is None for fact in required):
        present_facts = [fact for fact in required if fact is not None]
        period_end = revenue["period_end"] if revenue is not None else None
        price_as_of = price["period_end"] if price is not None else None
        return _unknown(
            metric,
            formula,
            "x",
            [fact["fact_id"] for fact in present_facts],
            _evidence_refs(present_facts),
            period_end,
            price_as_of,
        )

    assert (
        revenue is not None
        and cash is not None
        and debt is not None
        and shares is not None
        and price is not None
    )
    revenue_value = _decimal_value(revenue)
    cash_value = _decimal_value(cash)
    debt_value = _decimal_value(debt)
    shares_value = _decimal_value(shares)
    price_value = _decimal_value(price)
    if None in {revenue_value, cash_value, debt_value, shares_value, price_value}:
        return _unknown(
            metric,
            formula,
            "x",
            [
                fact["fact_id"]
                for fact in (revenue, cash, debt, shares, price)
                if _decimal_value(fact) is not None
            ],
            _evidence_refs([revenue, cash, debt, shares, price]),
            revenue["period_end"],
            price["period_end"],
        )

    market_cap = shares_value * price_value
    if market_cap <= 0 or revenue_value == 0:
        return _not_meaningful(
            metric,
            formula,
            "x",
            [revenue, cash, debt, shares, price],
            revenue["period_end"],
            price["period_end"],
        )

    enterprise_value = market_cap + debt_value - cash_value
    value = enterprise_value / revenue_value
    return _ok(
        metric,
        formula,
        float(value),
        "x",
        [revenue, cash, debt, shares, price],
        revenue["period_end"],
        price["period_end"],
    )


def _free_cash_flow_yield(facts_by_metric: Mapping[str, list[dict[str, Any]]]) -> dict[str, Any]:
    metric = "free_cash_flow_yield"
    formula = "free_cash_flow_ttm / market_cap"
    fcf = _latest_fact(facts_by_metric, "free_cash_flow_ttm")
    shares = _latest_fact(facts_by_metric, "diluted_shares")
    price = _latest_fact(facts_by_metric, "close_price")
    required = [fcf, shares, price]
    if any(fact is None for fact in required):
        present_facts = [fact for fact in required if fact is not None]
        period_end = fcf["period_end"] if fcf is not None else None
        price_as_of = price["period_end"] if price is not None else None
        return _unknown(
            metric,
            formula,
            "ratio",
            [fact["fact_id"] for fact in present_facts],
            _evidence_refs(present_facts),
            period_end,
            price_as_of,
        )

    assert fcf is not None and shares is not None and price is not None
    fcf_value = _decimal_value(fcf)
    shares_value = _decimal_value(shares)
    price_value = _decimal_value(price)
    if None in {fcf_value, shares_value, price_value}:
        return _unknown(
            metric,
            formula,
            "ratio",
            [fact["fact_id"] for fact in (fcf, shares, price) if _decimal_value(fact) is not None],
            _evidence_refs([fcf, shares, price]),
            fcf["period_end"],
            price["period_end"],
        )

    market_cap = shares_value * price_value
    if market_cap <= 0:
        return _not_meaningful(
            metric, formula, "ratio", [fcf, shares, price], fcf["period_end"], price["period_end"]
        )

    value = fcf_value / market_cap
    return _ok(
        metric,
        formula,
        float(value),
        "ratio",
        [fcf, shares, price],
        fcf["period_end"],
        price["period_end"],
    )


def _ok(
    metric: str,
    formula: str,
    value: float,
    unit: str,
    input_facts: list[dict[str, Any]],
    period_end: str | None,
    price_as_of: str | None,
) -> dict[str, Any]:
    return {
        "metric": metric,
        "status": OK,
        "formula": formula,
        "value": value,
        "unit": unit,
        "input_fact_ids": [fact["fact_id"] for fact in input_facts],
        "period_end": period_end,
        "price_as_of": price_as_of,
        "available_at_cutoff_passed": True,
        "evidence_refs": _evidence_refs(input_facts),
    }


def _unknown(
    metric: str,
    formula: str,
    unit: str,
    input_fact_ids: list[str],
    evidence_refs: list[str],
    period_end: str | None,
    price_as_of: str | None = None,
) -> dict[str, Any]:
    return {
        "metric": metric,
        "status": UNKNOWN,
        "formula": formula,
        "value": None,
        "unit": unit,
        "input_fact_ids": input_fact_ids,
        "period_end": period_end,
        "price_as_of": price_as_of,
        "available_at_cutoff_passed": False,
        "evidence_refs": evidence_refs,
    }


def _not_meaningful(
    metric: str,
    formula: str,
    unit: str,
    input_facts: list[dict[str, Any]],
    period_end: str | None,
    price_as_of: str | None,
) -> dict[str, Any]:
    return {
        "metric": metric,
        "status": NOT_MEANINGFUL,
        "formula": formula,
        "value": None,
        "unit": unit,
        "input_fact_ids": [fact["fact_id"] for fact in input_facts],
        "period_end": period_end,
        "price_as_of": price_as_of,
        "available_at_cutoff_passed": True,
        "evidence_refs": _evidence_refs(input_facts),
    }


def _latest_fact(
    facts_by_metric: Mapping[str, list[dict[str, Any]]],
    metric: str,
) -> dict[str, Any] | None:
    items = facts_by_metric.get(metric, [])
    return items[0] if items else None


def _latest_distinct_period_facts(
    facts: list[dict[str, Any]],
    *,
    limit: int,
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    seen_periods: set[str] = set()
    for fact in facts:
        period_end = fact["period_end"]
        if period_end in seen_periods:
            continue
        seen_periods.add(period_end)
        results.append(fact)
        if len(results) == limit:
            break
    return results


def _evidence_refs(facts: list[dict[str, Any]]) -> list[str]:
    seen: set[str] = set()
    refs: list[str] = []
    for fact in facts:
        evidence_ref = require_string(fact["evidence_ref"], "fact.evidence_ref", strip=False)
        if evidence_ref not in seen:
            seen.add(evidence_ref)
            refs.append(evidence_ref)
    return refs


def _fact_card(fact: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "fact_id": fact["fact_id"],
        "metric": fact["metric"],
        "value": fact["value"],
        "unit": fact["unit"],
        "period_end": fact["period_end"],
        "available_at": fact["available_at"],
        "evidence_ref": fact["evidence_ref"],
    }


def _decimal_value(fact: Mapping[str, Any]) -> Decimal | None:
    try:
        value = Decimal(str(fact["value"]))
    except (InvalidOperation, ValueError):
        return None
    if not value.is_finite():
        return None
    return value


def _parse_date(value: Any) -> date:
    return date.fromisoformat(require_string(value, "period_end", strip=False))


def _fact_order_key(fact: Mapping[str, Any]) -> tuple[date, datetime, str]:
    return (
        _parse_date(fact["period_end"]),
        datetime.fromisoformat(require_string(fact["available_at"], "available_at", strip=False)),
        require_string(fact["fact_id"], "fact_id", strip=False),
    )


def _fact_metric_order_key(fact: Mapping[str, Any]) -> int:
    metric = require_string(fact["metric"], "metric", strip=False)
    return FACT_METRIC_ORDER.get(metric, len(FACT_METRIC_ORDER))
