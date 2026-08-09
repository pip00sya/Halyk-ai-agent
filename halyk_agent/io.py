from __future__ import annotations

import csv
import hashlib
import json
import re
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from .models import Transaction


SCENARIO_RE = re.compile(r"^TXN-([A-Za-z][A-Za-z0-9]*)-", re.IGNORECASE)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().lower()


def read_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8-sig") as stream:
        return json.load(stream)


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write("\n")


def _parse_decimal(value: str | None) -> Decimal | None:
    if value is None or not value.strip():
        return None
    normalized = value.strip().replace("\u00a0", "").replace(",", "")
    try:
        return Decimal(normalized)
    except InvalidOperation as exc:
        raise ValueError(f"Invalid amount: {value!r}") from exc


class Ledger:
    def __init__(self, transactions: list[Transaction]) -> None:
        self.transactions = transactions
        self.by_id = {txn.txn_id: txn for txn in transactions}
        if len(self.by_id) != len(transactions):
            raise ValueError("Duplicate txn_id values in ledger")

    @classmethod
    def from_csv(cls, path: Path) -> "Ledger":
        with path.open("r", encoding="utf-8-sig", newline="") as stream:
            reader = csv.DictReader(stream)
            required = {
                "txn_id", "date", "account_id", "counterparty",
                "description", "amount", "currency",
            }
            missing = required - set(reader.fieldnames or [])
            if missing:
                raise ValueError(f"Ledger columns missing: {sorted(missing)}")
            rows = [
                Transaction(
                    txn_id=row["txn_id"].strip(),
                    date=row["date"].strip(),
                    account_id=row["account_id"].strip(),
                    counterparty=row["counterparty"].strip(),
                    description=row["description"].strip(),
                    amount=_parse_decimal(row["amount"]),
                    currency=row["currency"].strip().upper(),
                )
                for row in reader
            ]
        return cls(rows)

    def scenario_ids(self) -> list[str]:
        values: set[str] = set()
        for txn in self.transactions:
            match = SCENARIO_RE.match(txn.txn_id)
            if match:
                values.add(match.group(1))
        return sorted(values, key=scenario_sort_key)

    def for_scenario(self, scenario_id: str) -> list[Transaction]:
        prefix = f"TXN-{scenario_id}-"
        return [txn for txn in self.transactions if txn.txn_id.startswith(prefix)]

    def context_for_scenario(
        self,
        scenario_id: str,
        account_id: str | None,
        referenced_txn_ids: set[str] | None = None,
    ) -> list[Transaction]:
        """Include primary, same-account and explicitly cross-referenced ledger rows."""
        prefix = f"TXN-{scenario_id}-"
        referenced_txn_ids = referenced_txn_ids or set()
        return [
            txn for txn in self.transactions
            if txn.txn_id.startswith(prefix)
            or (account_id is not None and txn.account_id == account_id)
            or txn.txn_id in referenced_txn_ids
        ]

    def account_for_scenario(self, scenario_id: str) -> str | None:
        counts: dict[str, int] = {}
        for txn in self.for_scenario(scenario_id):
            counts[txn.account_id] = counts.get(txn.account_id, 0) + 1
        return max(counts, key=counts.get) if counts else None


def scenario_sort_key(value: str) -> tuple[str, int, str]:
    match = re.fullmatch(r"([A-Za-z]+)(\d+)", value)
    if not match:
        return value, 0, value
    return match.group(1), int(match.group(2)), value
