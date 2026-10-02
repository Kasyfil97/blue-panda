"""
Pure-pandas metrics for the layered-resolver evaluation (no mage_flow, no
network) so the aggregation logic can be unit-tested in isolation.

Input: a "long" DataFrame with ONE ROW PER (test row, layer) and columns

    row_id      int   – index of the test row
    layer       str   – one of LAYER_ORDER (or the "kata_noctx" variant)
    covered     bool  – layer produced a non-empty description for this row
    tag         str   – resolver tag that produced it (exact_as400, kata_alias, ...)
    eval_desc   str   – similar | partial | unsimilar | no_gt | error | ""
    eval_title  str   – same, for business title
    truthfulness str  – "true" | "false" | "n/a" | "error" | ""   (KATA layers only)

Layers are run ISOLATED (every layer on every applicable row); the
sequential "waterfall" (first layer that covers a row wins) is derived
afterwards from those isolated outcomes.
"""

from __future__ import annotations

from typing import Dict, Iterable, List, Optional

import pandas as pd

LAYER_ORDER: List[str] = ["exact_match", "kata", "bm25", "fallback"]
VARIANT_LAYERS: List[str] = ["kata_noctx", "confluence_similar"]

LAYER_LABELS: Dict[str, str] = {
    "exact_match": "1. Exact Match (BM25 exact lookup)",
    "kata": "2. KATA (exact + similar; BM25 context on AS400 tables)",
    "kata_noctx": "2b. KATA without BM25 context (variant, not ranked)",
    "bm25": "3. BM25 (AS400 / Confluence / Informatica)",
    "fallback": "4. Fallback (Confluence fallback + Pure LLM)",
    "confluence_similar": "4b. Confluence + LLM synthesis (variant, not ranked; vs regex-only fallback)",
}

EVAL_LABELS = ("similar", "partial", "unsimilar")


def _frac(numerator: int, denominator: int) -> Optional[float]:
    return round(numerator / denominator, 4) if denominator else None


def _label_stats(labels: pd.Series) -> Dict[str, object]:
    """Counts + shares (of EVALUATED rows) for one eval column."""
    counts = {lab: int((labels == lab).sum()) for lab in EVAL_LABELS}
    evaluated = sum(counts.values())
    return {
        "evaluated": evaluated,
        "no_gt": int((labels == "no_gt").sum()),
        "error": int((labels == "error").sum()),
        **{f"{lab}_n": counts[lab] for lab in EVAL_LABELS},
        **{f"{lab}_pct": _frac(counts[lab], evaluated) for lab in EVAL_LABELS},
    }


def layer_summary(long_df: pd.DataFrame, n_rows: int, layers: Iterable[str]) -> pd.DataFrame:
    """One row per layer: coverage, description/title accuracy, yield, truthfulness."""
    rows = []
    for layer in layers:
        sub = long_df[long_df["layer"] == layer]
        covered = sub[sub["covered"]]
        not_covered = sub[~sub["covered"]]
        desc = _label_stats(covered["eval_desc"])
        title = _label_stats(covered["eval_title"])

        # Not-covered splits into two different things a reader must not
        # conflate: the resolver ran fine and legitimately found nothing
        # (e.g. KATA correctly rejecting every candidate) vs. it actually
        # threw/errored (exception, API failure, judge parse failure) — only
        # the latter is a "failure" in the engineering sense the resolver
        # failure-rate ticket asks about.
        if "error" in not_covered.columns:
            has_error = not_covered["error"].astype(str).str.len() > 0
        else:
            has_error = pd.Series([False] * len(not_covered), index=not_covered.index)
        n_errors = int(has_error.sum())
        n_no_match = int(len(not_covered)) - n_errors

        truth = covered["truthfulness"] if "truthfulness" in covered.columns else pd.Series([], dtype=str)
        t_true = int((truth == "true").sum())
        t_false = int((truth == "false").sum())

        rows.append({
            "Layer": LAYER_LABELS.get(layer, layer),
            "layer_key": layer,
            "Rows": n_rows,
            "Covered": int(len(covered)),
            "Coverage %": _frac(int(len(covered)), n_rows),
            # "Failure Rate" = execution errors only (exceptions/API/judge
            # failures) — NOT the same as 1 - Coverage, which also includes
            # rows the resolver legitimately chose not to answer.
            "Failure Rate (execution errors / rows)": _frac(n_errors, n_rows),
            "Execution Errors": n_errors,
            "No Match (legitimate, no error)": n_no_match,
            "Desc Evaluated": desc["evaluated"],
            "Desc Similar %": desc["similar_pct"],
            "Desc Partial %": desc["partial_pct"],
            "Desc Unsimilar %": desc["unsimilar_pct"],
            # "Yield" = share of ALL rows this layer serves with a similar
            # description = coverage x precision, the number that balances
            # "answers a lot" against "answers correctly".
            "Desc Yield (similar / all rows)": _frac(desc["similar_n"], n_rows),
            "Title Evaluated": title["evaluated"],
            "Title Similar %": title["similar_pct"],
            "Title Partial %": title["partial_pct"],
            "Title Unsimilar %": title["unsimilar_pct"],
            "Title Yield (similar / all rows)": _frac(title["similar_n"], n_rows),
            "Truthful % (true / judged)": _frac(t_true, t_true + t_false),
            "No GT (desc)": desc["no_gt"],
            "Judge errors (desc)": desc["error"],
        })
    return pd.DataFrame(rows)


def ranking_table(summary: pd.DataFrame, layer_order: Optional[List[str]] = None) -> pd.DataFrame:
    """Rank the "real" layers four ways; anything not in ``layer_order`` is
    listed unranked as a variant/comparison row.

    ``layer_order`` defaults to this module's own LAYER_ORDER (the v2,
    4-layer hierarchy) so existing callers (layer_eval.py) are unaffected;
    pass a different ordered list (e.g. hierarchy_v3_eval.py's flat 7-step
    STEP_ORDER) to rank THAT hierarchy's steps instead -- otherwise every
    step whose key isn't one of v2's 4 hardcoded layer names would silently
    fall into "variants" and never get ranked at all.

    Primary ranking = Desc Yield (coverage x precision). The other three are
    shown so the reader can see when the ordering depends on the metric:
    precision-only ranking would put a tiny-but-perfect layer on top,
    coverage-only would reward a layer that answers a lot but often wrong,
    and failure-rate-only would reward a layer that rarely THROWS even if it
    also rarely answers correctly (a layer that mostly abstains cleanly can
    have a near-zero failure rate and a poor yield at the same time).
    """
    order = list(layer_order) if layer_order is not None else LAYER_ORDER
    ranked = summary[summary["layer_key"].isin(order)].copy()
    ranked["Rank (yield)"] = ranked["Desc Yield (similar / all rows)"].rank(ascending=False, method="min")
    ranked["Rank (precision)"] = ranked["Desc Similar %"].rank(ascending=False, method="min")
    ranked["Rank (coverage)"] = ranked["Coverage %"].rank(ascending=False, method="min")
    ranked["Rank (failure rate, lower=better)"] = ranked["Failure Rate (execution errors / rows)"].rank(ascending=True, method="min")
    ranked = ranked.sort_values("Rank (yield)")
    variants = summary[~summary["layer_key"].isin(order)].copy()
    for col in ("Rank (yield)", "Rank (precision)", "Rank (coverage)", "Rank (failure rate, lower=better)"):
        variants[col] = None
    return pd.concat([ranked, variants], ignore_index=True)


def resolver_breakdown(long_df: pd.DataFrame, n_rows: int, n_as400: int, layers: Iterable[str]) -> pd.DataFrame:
    """Per (layer, resolver tag): how much each individual resolver contributes."""
    rows = []
    for layer in layers:
        sub = long_df[(long_df["layer"] == layer) & (long_df["covered"])]
        for tag, grp in sub.groupby("tag"):
            desc = _label_stats(grp["eval_desc"])
            # The AS400 BM25 resolver only applies to AS400 tables, so its
            # coverage is reported against those rows, not against all rows.
            applicable = n_as400 if tag == "bm25_as400" else n_rows
            rows.append({
                "Layer": LAYER_LABELS.get(layer, layer),
                "Resolver tag": tag,
                "Covered": int(len(grp)),
                "Applicable rows": applicable,
                "Coverage % (of applicable)": _frac(int(len(grp)), applicable),
                "Desc Evaluated": desc["evaluated"],
                "Desc Similar %": desc["similar_pct"],
                "Desc Partial %": desc["partial_pct"],
                "Desc Unsimilar %": desc["unsimilar_pct"],
            })
    return pd.DataFrame(rows)


def winner_by_row(long_df: pd.DataFrame, order: List[str]) -> pd.Series:
    """First layer (in production order) that covers each row; '' if none."""
    covered = long_df[long_df["covered"] & long_df["layer"].isin(order)]
    rank = {layer: i for i, layer in enumerate(order)}
    covered = covered.assign(_rank=covered["layer"].map(rank))
    first = covered.sort_values("_rank").groupby("row_id").first()["layer"]
    all_ids = pd.Index(sorted(long_df["row_id"].unique()), name="row_id")
    return first.reindex(all_ids).fillna("")


def waterfall(long_df: pd.DataFrame, n_rows: int, order: Optional[List[str]] = None) -> pd.DataFrame:
    """Sequential cascade: each row is served by the first layer that covers it.

    Reports, per layer, how many rows it newly takes over and how accurate
    those specific rows are, plus running totals and a final overall line.
    """
    order = order or LAYER_ORDER
    winners = winner_by_row(long_df, order)
    rows = []
    cum_covered = 0
    cum_similar = 0
    overall_desc = {lab: 0 for lab in EVAL_LABELS}
    for step, layer in enumerate(order, start=1):
        ids = winners[winners == layer].index
        served = long_df[(long_df["layer"] == layer) & (long_df["row_id"].isin(ids))]
        desc = _label_stats(served["eval_desc"])
        title = _label_stats(served["eval_title"])
        cum_covered += len(ids)
        cum_similar += desc["similar_n"]
        for lab in EVAL_LABELS:
            overall_desc[lab] += desc[f"{lab}_n"]
        rows.append({
            "Step": step,
            "Layer": LAYER_LABELS.get(layer, layer),
            "Newly covered": int(len(ids)),
            "Cumulative covered": cum_covered,
            "Cumulative coverage %": _frac(cum_covered, n_rows),
            "Desc Evaluated": desc["evaluated"],
            "Desc Similar %": desc["similar_pct"],
            "Desc Partial %": desc["partial_pct"],
            "Desc Unsimilar %": desc["unsimilar_pct"],
            "Cumulative similar (desc)": cum_similar,
            "Cumulative yield %": _frac(cum_similar, n_rows),
            "Title Similar %": title["similar_pct"],
        })
    uncovered = int((winners == "").sum())
    evaluated_total = sum(overall_desc.values())
    rows.append({
        "Step": "cascade total",
        "Layer": f"All layers combined ({uncovered} rows uncovered)",
        "Newly covered": cum_covered,
        "Cumulative covered": cum_covered,
        "Cumulative coverage %": _frac(cum_covered, n_rows),
        "Desc Evaluated": evaluated_total,
        "Desc Similar %": _frac(overall_desc["similar"], evaluated_total),
        "Desc Partial %": _frac(overall_desc["partial"], evaluated_total),
        "Desc Unsimilar %": _frac(overall_desc["unsimilar"], evaluated_total),
        "Cumulative similar (desc)": cum_similar,
        "Cumulative yield %": _frac(cum_similar, n_rows),
        "Title Similar %": None,
    })
    return pd.DataFrame(rows)