"""Demand prediction with a Random Forest regressor.

For every listing and day we build features from its own recent sales (last 7/14/28 days,
same-weekday average), calendar (weekday, month), category and price position, and learn
to predict units sold over the NEXT 7 days. The model is validated on the most recent
weeks (time-based split) against a naive baseline ("next week = last week"), so the
metrics show whether it actually adds value.

Predictions feed restock suggestions: days until stock-out and how much to reorder.
If the training data contains seeded demo sales, every output is flagged is_demo.

Gating (P7.3, plan section 8.9; thresholds in app/core/config.py): is_demo sales are left out when
NEARSHOP_MODELS_REAL_DATA_ONLY is on (the default in production). Forecasts are published only for eligible
listings (enough days of history, days with a sale and units), and only when, over the last
demand_backtest_days days, the eligible listings' MAE beats the naive baseline by demand_min_improvement_pct
percent; otherwise none are published and the run's metrics["gate"] says why.
"""
from __future__ import annotations

import math
import time
from datetime import timedelta

import numpy as np
import pandas as pd
from sklearn.ensemble import RandomForestRegressor
from sklearn.metrics import mean_absolute_error, r2_score

from app.core.config import settings
from app.core.database import Database, utcnow
from app.models import DemandForecast, ModelRun, Product
from app.services.analytics import (
    DEMAND_HISTORY_DAYS,
    demand_listing_eligible,
    demand_model_gate,
    real_sales_filter,
)
from app.services.loaders import with_catalog_items

HISTORY_DAYS = DEMAND_HISTORY_DAYS
HORIZON = 7
FEATURES = ["lag7", "lag14", "lag28", "same_dow_avg", "dow", "month", "category_id", "price_ratio", "trend"]


def _daily_matrix(db: Database) -> tuple[pd.DataFrame, bool]:
    since = utcnow() - timedelta(days=HISTORY_DAYS)
    # Summed per product and day inside MongoDB, so only the daily totals travel over the network.
    rows = db.sales_history.aggregate([
        {"$match": {"sold_at": {"$gte": since}, **real_sales_filter()}},
        {"$group": {"_id": {"p": "$product_id", "d": {"$dateTrunc": {"date": "$sold_at", "unit": "day"}}},
                    "quantity": {"$sum": "$quantity"}, "is_demo": {"$max": "$is_demo"}}},
    ])
    rows = [(r["_id"]["p"], r["_id"]["d"], r["quantity"], r["is_demo"]) for r in rows]
    if not rows:
        return pd.DataFrame(), False
    df = pd.DataFrame(rows, columns=["product_id", "sold_at", "quantity", "is_demo"])
    uses_demo = bool(df["is_demo"].any())
    df["day"] = pd.to_datetime(df["sold_at"]).dt.normalize()
    daily = df.groupby(["product_id", "day"])["quantity"].sum().unstack(fill_value=0)
    today = pd.Timestamp(utcnow()).normalize()
    full_range = pd.date_range(today - pd.Timedelta(days=HISTORY_DAYS - 1), today, freq="D")
    daily = daily.reindex(columns=full_range, fill_value=0)
    return daily, uses_demo


def _features_at(series: np.ndarray, t: int, day: pd.Timestamp, category_id: int, price_ratio: float) -> list[float]:
    """Features using only data up to and including index t (no leakage)."""
    lag7 = series[t - 6 : t + 1].sum()
    lag14 = series[t - 13 : t + 1].sum()
    lag28 = series[t - 27 : t + 1].sum()
    same_dow = series[t - 27 : t + 1 : 7].mean()
    trend = lag7 - (lag14 - lag7)
    return [lag7, lag14, lag28, same_dow, day.dayofweek, day.month, category_id, price_ratio, trend]


def train_and_forecast(db: Database) -> ModelRun:
    started = time.time()
    daily, uses_demo = _daily_matrix(db)
    products = {p.id: p for p in with_catalog_items(
        db, db.products.find({"is_active": True}, projection={"description": 0, "specs": 0}))}

    run = ModelRun(name="demand", algorithm="RandomForestRegressor", uses_demo_data=uses_demo)
    dates = list(daily.columns)
    n_days = len(dates)
    eligible: set[int] = set()
    X, y, day_idx, row_pid = [], [], [], []
    for pid, row in daily.iterrows():
        p = products.get(pid)
        if p is None:
            continue
        series = row.to_numpy(dtype=float)
        if listing_eligible(series):
            eligible.add(pid)
        ratio = _price_ratio(p)
        for t in range(27, n_days - HORIZON):
            X.append(_features_at(series, t, dates[t], p.category_id, ratio))
            y.append(series[t + 1 : t + 1 + HORIZON].sum())
            day_idx.append(t)
            row_pid.append(pid)
    if not X:
        run.data_note = "No sales history yet. Forecasts appear once reservations and orders are completed."
        run.metrics = {"gate": demand_model_gate(None, None, 0)}

        def publish_empty() -> None:  # no forecasts from an older run stay on show
            db.demand_forecasts.delete_many({})
            db.model_runs.insert(run)

        db.transaction(publish_empty)
        return run
    X = np.array(X, dtype=float)
    y = np.array(y, dtype=float)
    day_idx = np.array(day_idx)
    is_eligible = np.isin(np.array(row_pid), list(eligible))

    # Time-based split: validate on the last demand_backtest_days usable days (the gate's backtest).
    split = (n_days - HORIZON) - settings.demand_backtest_days
    train_mask, test_mask = day_idx < split, day_idx >= split
    rng = np.random.default_rng(7)
    train_rows = np.flatnonzero(train_mask)
    if len(train_rows) > 80_000:
        train_rows = rng.choice(train_rows, 80_000, replace=False)

    model = RandomForestRegressor(n_estimators=120, max_depth=14, min_samples_leaf=4, n_jobs=-1, random_state=7)
    model.fit(X[train_rows], y[train_rows])

    # The backtest the gate reads covers the eligible listings only; with none, every listing (for the card).
    backtest = test_mask & is_eligible if eligible else test_mask
    metrics: dict = {"train_rows": int(len(train_rows)), "test_rows": int(backtest.sum()),
                     "listings": len(products.keys() & set(daily.index)), "eligible_listings": len(eligible),
                     "backtest_scope": "eligible listings" if eligible else "all listings",
                     "backtest_days": settings.demand_backtest_days}
    mae = naive_mae = None
    if backtest.any():
        pred = model.predict(X[backtest])
        naive = X[backtest][:, 0]  # lag7: "next week sells like last week"
        mae = float(mean_absolute_error(y[backtest], pred))
        naive_mae = float(mean_absolute_error(y[backtest], naive))
        metrics.update(
            mae_7d=round(mae, 3),
            naive_mae_7d=round(naive_mae, 3),
            improvement_pct=round((naive_mae - mae) / naive_mae * 100, 1) if naive_mae else None,
            r2=round(float(r2_score(y[backtest], pred)), 3),
        )
    model_gate = demand_model_gate(mae, naive_mae, len(eligible))
    metrics["gate"] = model_gate
    metrics["feature_importance"] = {
        f: round(float(v), 3) for f, v in sorted(zip(FEATURES, model.feature_importances_), key=lambda x: -x[1])
    }

    run.n_samples = int(len(X))
    run.metrics = metrics
    run.data_note = (
        "Trained on seeded demo sales plus any real completed pickups and deliveries."
        if uses_demo
        else "Trained on real completed pickups and deliveries."
    )
    run.id = db.next_id("model_runs")

    # Forecast the next 7 days for every eligible listing, and only when the model passes its gate.
    forecasts = []
    t = n_days - 1
    for pid, row in daily.iterrows():
        p = products.get(pid)
        if p is None or pid not in eligible or not model_gate["shown"]:
            continue
        series = row.to_numpy(dtype=float)
        active_days = int((series > 0).sum())
        feats = _features_at(series, t, dates[t], p.category_id, _price_ratio(p))
        pred7 = max(0.0, float(model.predict(np.array([feats]))[0]))
        daily_rate = pred7 / HORIZON
        last7, prev7 = series[-7:].sum(), series[-14:-7].sum()
        trend = "rising" if last7 > prev7 * 1.25 + 1 else "falling" if last7 < prev7 * 0.75 - 1 else "steady"
        history_days = int(np.flatnonzero(series)[0]) if active_days else 0
        confidence = "high" if active_days >= 40 else "medium" if active_days >= 15 else "low"
        forecasts.append(
            DemandForecast(
                product_id=pid,
                shop_id=p.shop_id,
                predicted_7d=round(pred7, 2),
                daily_rate=round(daily_rate, 3),
                days_to_stockout=round(p.quantity / daily_rate, 1) if daily_rate > 0.01 else None,
                recommended_restock=max(0, math.ceil(pred7 * 2 - p.quantity)),  # cover two weeks
                trend=trend,
                confidence=confidence,
                history_days=n_days - history_days if active_days else 0,
                is_demo=uses_demo,
                model_run_id=run.id,
            )
        )
    run.duration_ms = int((time.time() - started) * 1000)

    def publish() -> None:  # readers see the old forecasts or the new ones, never a half-written set
        db.demand_forecasts.delete_many({})
        db.demand_forecasts.insert_many(forecasts)
        db.model_runs.insert(run)

    db.transaction(publish)
    return run


def listing_eligible(series: np.ndarray) -> bool:
    """A listing's daily units (oldest first, ending today): eligible for forecasts with enough days since its
    first sale, days with a sale and units in total."""
    sold = np.flatnonzero(series)
    if not len(sold):
        return False
    return demand_listing_eligible(len(series) - int(sold[0]), len(sold), int(series.sum()))


def _price_ratio(p: Product) -> float:
    ref = p.catalog_item.typical_price if p.catalog_item and p.catalog_item.typical_price else p.price
    return float(p.price / ref) if ref else 1.0
