"""Walk-forward folds with purging and embargo (ADR 0015, PRD §11 "no look-ahead").

Fold k tests on a block of `test_size` consecutive sessions. It trains on every earlier row
whose *label window has closed* before the test block starts (`label_end < test_start`,
the exact exit date stored with each label), minus an extra `embargo` of sessions. The
training window expands: nothing from the test block or after it is ever used to fit.
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd


@dataclass(frozen=True)
class Fold:
    index: int
    train_end: pd.Timestamp  # last trade_date allowed in training (before purge by label_end)
    test_start: pd.Timestamp
    test_end: pd.Timestamp


def make_folds(dates: pd.Series, *, min_train: int = 60, test_size: int = 20, embargo: int = 2) -> list[Fold]:
    """Fold boundaries over the sorted unique sessions in `dates`."""
    days = sorted(pd.to_datetime(pd.Series(dates).unique()))
    folds: list[Fold] = []
    start = min_train + embargo
    k = 0
    while start + test_size <= len(days):
        folds.append(Fold(k, days[start - embargo - 1], days[start], days[start + test_size - 1]))
        start += test_size
        k += 1
    return folds


def split(df: pd.DataFrame, fold: Fold) -> tuple[pd.DataFrame, pd.DataFrame]:
    """(train, test) rows of `df` (needs trade_date and label_end) for `fold`."""
    train = df[(df["trade_date"] <= fold.train_end) & (df["label_end"] < fold.test_start)]
    test = df[(df["trade_date"] >= fold.test_start) & (df["trade_date"] <= fold.test_end)]
    return train, test


__all__ = ["Fold", "make_folds", "split"]
