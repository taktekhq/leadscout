"""Tiny CSV read/write helpers used by the CLI."""

from __future__ import annotations

import csv
from typing import List, Sequence


def write_csv(rows: List[dict], fields: Sequence[str], path: str) -> None:
    with open(path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(fields), extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({k: row.get(k, "") for k in fields})


def read_csv(path: str) -> List[dict]:
    with open(path, "r", encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))
