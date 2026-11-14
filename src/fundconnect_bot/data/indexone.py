"""Standalone IndexOne HTTP client and Parquet cache loader (zero TradeMaster dependency)."""

from __future__ import annotations

import hashlib
import json
import os
import time
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from http.client import HTTPException
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

import pyarrow as pa
import pyarrow.parquet as pq

from ..config import ALL_HK_SYMBOLS, ALL_US_SYMBOLS, US_EQUITY_STOCKS

INDEXONE_BASE_URL = "https://api.indexone.io"
PRICE_UNIT = Decimal("0.00000001")

US_REFERENCE_FIELDS: tuple[str, ...] = (
    "id",
    "symbol",
    "name",
    "cik",
    "figi_composite",
    "mic",
    "currency",
    "sector",
    "industry",
    "security_type",
    "is_etf",
    "primary_listing",
    "listed_at",
    "delisted_at",
)
US_EOD_FIELDS: tuple[str, ...] = (
    "id",
    "date",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "adjClose",
    "splitAdjClose",
    "marketCap",
)
US_FUNDAMENTAL_FIELDS: tuple[str, ...] = (
    "id",
    "symbol",
    "date",
    "fiscalYear",
    "period",
    "reportedCurrency",
    "cik",
    "filingDate",
    "acceptedDate",
    "revenue",
    "grossProfit",
    "operatingIncome",
    "netIncome",
    "totalAssets",
    "totalStockholdersEquity",
    "operatingCashFlow",
    "freeCashFlow",
)
HK_REFERENCE_FIELDS: tuple[str, ...] = (
    "id",
    "symbol",
    "figi_composite",
    "mic",
    "currency",
    "listed_at",
    "delisted_at",
    "primary_listing",
    "security_type",
    "is_etf",
)
HK_EOD_FIELDS: tuple[str, ...] = (
    "id",
    "date",
    "open",
    "high",
    "low",
    "close",
    "adjOpen",
    "adjClose",
    "splitAdjClose",
    "volume",
)


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(
        self, req: Request, fp: Any, code: int, msg: str, headers: Any, newurl: str
    ) -> None:
        return None


def sha256_file(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


@dataclass(frozen=True)
class UsBarRecord:
    session: date
    raw_open: float
    raw_close: float
    adj_close: float
    volume: float


@dataclass(frozen=True)
class UsMarketDataset:
    eod_by_symbol: dict[str, list[UsBarRecord]]
    fundamentals_rows: list[dict[str, Any]]
    eod_sha256: str
    fundamentals_sha256: str


@dataclass(frozen=True)
class HkBarRecord:
    session: date
    raw_open: float
    raw_close: float
    adj_open: Decimal
    adj_close: Decimal
    split_adj_close: float
    volume: float


class IndexOneClient:
    """Minimal read-only client for IndexOne `/execute` using `INDEXONE_API_KEY` from env."""

    def __init__(self, api_key: str, *, timeout: float = 45.0, max_attempts: int = 3) -> None:
        if not api_key or any(not (33 <= ord(ch) <= 126) for ch in api_key):
            raise ValueError("INDEXONE_API_KEY is absent or invalid")
        self._api_key = api_key
        self._timeout = timeout
        self._max_attempts = max_attempts

    @classmethod
    def from_env(cls, env_var: str = "INDEXONE_API_KEY") -> IndexOneClient:
        key = os.environ.get(env_var, "").strip()
        if not key:
            raise RuntimeError(f"Required environment variable {env_var} is not set")
        return cls(key)

    def execute(
        self,
        operation: str,
        *,
        params: Mapping[str, Any],
        fields: Sequence[str],
    ) -> list[dict[str, Any]]:
        payload_bytes = json.dumps(
            {
                "operations": [
                    {
                        "id": "data",
                        "operation": operation,
                        "input": [],
                        "parameters": {**dict(params), "attributes": list(fields)},
                    }
                ]
            },
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "x-api-key": self._api_key,
        }
        for attempt in range(self._max_attempts):
            req = Request(f"{INDEXONE_BASE_URL}/execute", data=payload_bytes, headers=headers)
            try:
                with build_opener(_NoRedirect()).open(req, timeout=self._timeout) as resp:
                    status = int(resp.status)
                    raw = resp.read(128 * 1024 * 1024)
            except HTTPError as err:
                status, raw = int(err.code), b""
            except (URLError, OSError, HTTPException):
                status, raw = 503, b""
            if status in {401, 403}:
                raise RuntimeError("IndexOne rejected API key or data entitlement")
            if status == 429 or 500 <= status <= 599:
                if attempt + 1 < self._max_attempts:
                    time.sleep(min(2.0**attempt, 4.0))
                    continue
                raise RuntimeError(f"IndexOne transient HTTP {status} exhausted retries")
            if status != 200:
                raise RuntimeError(f"IndexOne HTTP {status}")
            parsed = json.loads(raw.decode("utf-8"))
            node = parsed["data"] if "data" in parsed else parsed["operations"][0]
            output = node.get("output")
            if isinstance(output, list):
                return [dict(r) for r in output]
            if isinstance(output, dict):
                cols = list(fields)
                length = len(output[cols[0]]) if cols and cols[0] in output else 0
                return [{c: output[c][i] for c in cols} for i in range(length)]
            raise RuntimeError("Unexpected IndexOne output structure")
        raise RuntimeError("IndexOne request loop terminated unexpectedly")


def _resolve_us_mapping(
    ref_rows: Sequence[Mapping[str, Any]],
    symbols: Sequence[str],
) -> dict[str, str]:
    """Resolve each US symbol to its unique primary USD quote_id (composite FIGI or id)."""
    mapped: dict[str, str] = {}
    for sym in symbols:
        cands = [r for r in ref_rows if r.get("symbol") == sym and r.get("currency") == "USD"]
        if not cands:
            raise ValueError(f"Missing USD IndexOne reference row for {sym}")
        by_figi: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
        for r in cands:
            qid = str(r.get("figi_composite") or r.get("id") or "")
            if qid:
                by_figi[qid].append(r)
        if len(by_figi) == 1:
            mapped[sym] = next(iter(by_figi))
        else:
            prim = [r for r in cands if r.get("primary_listing") is True]
            prim_ids = {str(r.get("figi_composite") or r.get("id")) for r in prim}
            if len(prim_ids) != 1:
                raise ValueError(f"Ambiguous composite FIGI for {sym}")
            mapped[sym] = next(iter(prim_ids))
    return mapped


def refresh_us_indexone_data(
    data_dir: Path,
    *,
    end_date: date,
    history_start: date = date(2023, 1, 1),
    fundamental_start: date = date(2022, 1, 1),
    client: IndexOneClient | None = None,
) -> UsMarketDataset:
    """Pull live US EOD + SEC fundamentals via IndexOne and persist prepared Parquet tables."""
    idx_client = client or IndexOneClient.from_env()
    ref_rows = idx_client.execute(
        "core_securities_reference",
        params={
            "filters": [{"field": "symbol", "operator": "in", "value": list(ALL_US_SYMBOLS)}],
            "drop_duplicates": False,
            "is_active": False,
            "is_listed": False,
        },
        fields=US_REFERENCE_FIELDS,
    )
    sym_to_qid = _resolve_us_mapping(ref_rows, ALL_US_SYMBOLS)
    qid_to_sym = {qid: sym for sym, qid in sym_to_qid.items()}

    eod_rows: list[dict[str, Any]] = []
    qids = list(sym_to_qid.values())
    for idx in range(0, len(qids), 6):
        batch = qids[idx : idx + 6]
        rows = idx_client.execute(
            "i1_core_eod",
            params={
                "filters": [
                    {"field": "id", "operator": "in", "value": batch},
                    {"field": "date", "operator": "gte", "value": history_start.isoformat()},
                    {"field": "date", "operator": "lte", "value": end_date.isoformat()},
                ]
            },
            fields=US_EOD_FIELDS,
        )
        for r in rows:
            eod_rows.append({**r, "symbol": qid_to_sym[str(r["id"])]})

    fin_rows: list[dict[str, Any]] = []
    eq_qids = [sym_to_qid[s] for s in US_EQUITY_STOCKS]
    for idx in range(0, len(eq_qids), 12):
        batch = eq_qids[idx : idx + 12]
        rows = idx_client.execute(
            "i1_core_fundamentals",
            params={
                "storage_type": "quarterly",
                "filters": [
                    {"field": "id", "operator": "in", "value": batch},
                    {"field": "date", "operator": "gte", "value": fundamental_start.isoformat()},
                    {"field": "date", "operator": "lte", "value": end_date.isoformat()},
                ],
            },
            fields=US_FUNDAMENTAL_FIELDS,
        )
        for r in rows:
            fin_rows.append({**r, "mapped_symbol": qid_to_sym[str(r["id"])]})

    out_dir = data_dir / "us"
    out_dir.mkdir(parents=True, exist_ok=True)
    eod_path = out_dir / "us_eod.parquet"
    fin_path = out_dir / "us_fundamentals.parquet"
    pq.write_table(pa.Table.from_pylist(eod_rows), eod_path, compression="zstd")
    pq.write_table(pa.Table.from_pylist(fin_rows), fin_path, compression="zstd")
    return load_us_dataset_from_cache(out_dir, max_date=end_date)


def load_us_dataset_from_cache(
    us_cache_dir: Path,
    *,
    max_date: date | None = None,
) -> UsMarketDataset:
    """Load US EOD and SEC fundamentals from standalone `us/` or `fundconnect_us/prepared/`."""
    cand_eod = [
        us_cache_dir / "us_eod.parquet",
        us_cache_dir / "prepared" / "us_eod.parquet",
        us_cache_dir / "us" / "us_eod.parquet",
    ]
    cand_fin = [
        us_cache_dir / "us_fundamentals.parquet",
        us_cache_dir / "prepared" / "us_fundamentals.parquet",
        us_cache_dir / "us" / "us_fundamentals.parquet",
    ]
    eod_path = next((p for p in cand_eod if p.exists()), None)
    fin_path = next((p for p in cand_fin if p.exists()), None)
    if eod_path is None or fin_path is None:
        raise FileNotFoundError(
            f"US cached Parquet files (us_eod.parquet, us_fundamentals.parquet) not found under {us_cache_dir}"
        )

    eod_by_sym: dict[str, list[UsBarRecord]] = defaultdict(list)
    for r in pq.read_table(eod_path).to_pylist():
        d = date.fromisoformat(str(r["date"])[:10])
        if max_date is not None and d > max_date:
            continue
        eod_by_sym[str(r["symbol"])].append(
            UsBarRecord(
                session=d,
                raw_open=float(r["open"]),
                raw_close=float(r["close"]),
                adj_close=float(r["adjClose"]),
                volume=float(r["volume"] or 0.0),
            )
        )
    for bars in eod_by_sym.values():
        bars.sort(key=lambda b: b.session)

    fin_rows: list[dict[str, Any]] = []
    for r in pq.read_table(fin_path).to_pylist():
        rep_d = date.fromisoformat(str(r["date"])[:10])
        if max_date is not None and rep_d > max_date:
            continue
        fin_rows.append(dict(r))

    return UsMarketDataset(
        eod_by_symbol=dict(eod_by_sym),
        fundamentals_rows=fin_rows,
        eod_sha256=sha256_file(eod_path),
        fundamentals_sha256=sha256_file(fin_path),
    )


def refresh_hk_indexone_eod(
    data_dir: Path,
    *,
    hk_sessions: Sequence[date],
    end_date: date,
    warmup_start: date = date(2023, 6, 1),
    client: IndexOneClient | None = None,
) -> tuple[dict[str, dict[date, HkBarRecord]], str]:
    """Pull live XHKG EOD bars via IndexOne and persist `hk_eod.parquet`."""
    idx_client = client or IndexOneClient.from_env()
    ref_rows = idx_client.execute(
        "core_securities_reference",
        params={
            "filters": [{"field": "mic", "operator": "eq", "value": "XHKG"}],
            "drop_duplicates": False,
            "is_active": False,
            "is_listed": False,
        },
        fields=HK_REFERENCE_FIELDS,
    )
    target_set = set(ALL_HK_SYMBOLS)
    cands: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in ref_rows:
        sym = str(r.get("symbol") or "")
        if sym in target_set and r.get("currency") == "HKD" and r.get("delisted_at") is None:
            cands[sym].append(r)
    missing = sorted(target_set - set(cands))
    if missing:
        raise ValueError(f"IndexOne XHKG missing HKD reference rows for {missing}")

    chosen_id: dict[str, str] = {}
    for sym in sorted(target_set):
        prim = [r for r in cands[sym] if r.get("primary_listing")]
        chosen_id[sym] = str((prim[0] if prim else cands[sym][0])["id"])

    session_set = {d for d in hk_sessions if warmup_start <= d <= end_date}
    ordered_syms = sorted(target_set)
    flat_rows: list[dict[str, Any]] = []
    for idx in range(0, len(ordered_syms), 8):
        chunk_syms = ordered_syms[idx : idx + 8]
        chunk_ids = [chosen_id[s] for s in chunk_syms]
        id_to_sym = {chosen_id[s]: s for s in chunk_syms}
        rows = idx_client.execute(
            "i1_core_eod",
            params={
                "filters": [
                    {"field": "id", "operator": "in", "value": chunk_ids},
                    {"field": "date", "operator": "gte", "value": warmup_start.isoformat()},
                    {"field": "date", "operator": "lte", "value": end_date.isoformat()},
                ]
            },
            fields=HK_EOD_FIELDS,
        )
        for r in rows:
            d = date.fromisoformat(str(r["date"])[:10])
            if d not in session_set:
                continue
            flat_rows.append({**r, "symbol": id_to_sym[str(r["id"])]})

    out_dir = data_dir / "hk"
    out_dir.mkdir(parents=True, exist_ok=True)
    eod_path = out_dir / "hk_eod.parquet"
    pq.write_table(pa.Table.from_pylist(flat_rows), eod_path, compression="zstd")
    return load_hk_eod_from_cache(out_dir, hk_sessions=hk_sessions, max_date=end_date)


def _row_to_hk_bar(row: Mapping[str, Any]) -> HkBarRecord:
    d = date.fromisoformat(str(row["date"])[:10])
    raw_open = float(row["open"])
    raw_close = float(row["close"])
    adj_close = Decimal(str(row["adjClose"])).quantize(PRICE_UNIT)
    adj_open = (adj_close * Decimal(str(row["open"])) / Decimal(str(row["close"]))).quantize(
        PRICE_UNIT
    )
    return HkBarRecord(
        session=d,
        raw_open=raw_open,
        raw_close=raw_close,
        adj_open=adj_open,
        adj_close=adj_close,
        split_adj_close=float(row["splitAdjClose"]),
        volume=float(row.get("volume") or 0.0),
    )


def load_hk_eod_from_cache(
    hk_cache_dir: Path,
    *,
    hk_sessions: Sequence[date],
    max_date: date | None = None,
) -> tuple[dict[str, dict[date, HkBarRecord]], str]:
    """Load HKEX EOD bars from `hk_eod.parquet` or raw IndexOne Parquet chunks."""
    session_set = {d for d in hk_sessions if max_date is None or d <= max_date}
    direct_candidates = [
        hk_cache_dir / "hk_eod.parquet",
        hk_cache_dir / "hk" / "hk_eod.parquet",
    ]
    direct = next((p for p in direct_candidates if p.exists()), None)
    bars_by_sym: dict[str, dict[date, HkBarRecord]] = defaultdict(dict)

    if direct is not None:
        for r in pq.read_table(direct).to_pylist():
            d = date.fromisoformat(str(r["date"])[:10])
            if d not in session_set:
                continue
            sym = str(r["symbol"])
            bars_by_sym[sym][d] = _row_to_hk_bar(r)
        return dict(bars_by_sym), sha256_file(direct)

    # Support reading raw IndexOne content-addressed Parquet files under indexone/raw/indexone/
    raw_root = hk_cache_dir / "indexone" / "raw" / "indexone"
    ref_dir = raw_root / "core_securities_reference"
    eod_dir = raw_root / "i1_core_eod"
    if not ref_dir.exists() or not eod_dir.exists():
        raise FileNotFoundError(
            f"HK cached EOD Parquet files not found under {hk_cache_dir}"
        )

    ref_files = sorted(ref_dir.glob("*.parquet"))
    if not ref_files:
        raise FileNotFoundError(f"No reference Parquet files in {ref_dir}")

    target_set = set(ALL_HK_SYMBOLS)
    cands: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for rf in ref_files:
        for r in pq.read_table(rf).to_pylist():
            sym = str(r.get("symbol") or "")
            if sym in target_set and r.get("currency") == "HKD" and r.get("delisted_at") is None:
                cands[sym].append(dict(r))

    id_to_sym: dict[str, str] = {}
    for sym, rows in cands.items():
        prim = [r for r in rows if r.get("primary_listing")]
        chosen = prim[0] if prim else rows[0]
        id_to_sym[str(chosen["id"])] = sym

    hasher = hashlib.sha256()
    for ef in sorted(eod_dir.glob("*.parquet")):
        hasher.update(sha256_file(ef).encode("ascii"))
        for r in pq.read_table(ef).to_pylist():
            mapped_sym = id_to_sym.get(str(r["id"]))
            if mapped_sym is None:
                continue
            d = date.fromisoformat(str(r["date"])[:10])
            if d not in session_set:
                continue
            bars_by_sym[mapped_sym][d] = _row_to_hk_bar(r)

    return dict(bars_by_sym), hasher.hexdigest()
