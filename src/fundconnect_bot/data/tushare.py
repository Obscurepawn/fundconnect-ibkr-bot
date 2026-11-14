"""Standalone Tushare Pro HK calendar, board-lot, Southbound CCASS & GGT client/cache loader."""

from __future__ import annotations

import hashlib
import json
import os
import time
import urllib.request
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from ..config import DEFAULT_HK_STOCK_BOARD_LOTS, HK_BLUECHIP_UNIVERSE

TUSHARE_API_URL = "http://api.tushare.pro"
HK_HOLD_ROW_CEILING = 4150


def indexone_to_tushare_hk(symbol: str) -> str:
    """Convert 4-digit HK ticker ('0700.HK') to 5-digit Tushare code ('00700.HK')."""
    code, sep, suffix = symbol.partition(".")
    if sep != "." or suffix != "HK" or not code.isdigit():
        raise ValueError(f"Invalid HK symbol: {symbol}")
    number = int(code)
    if number <= 0 or number >= 80000:
        raise ValueError(f"Unsupported or non-HKD HK code: {symbol}")
    return f"{number:05d}.HK"


def tushare_to_indexone_hk(ts_code: str) -> str:
    """Convert 5-digit Tushare HK code ('00700.HK') to 4-digit canonical ticker ('0700.HK')."""
    code, sep, suffix = ts_code.partition(".")
    if sep != "." or suffix != "HK" or not code.isdigit():
        raise ValueError(f"Invalid Tushare HK ts_code: {ts_code}")
    number = int(code)
    if number <= 0 or number >= 80000:
        raise ValueError(f"Unsupported or non-HKD HK ts_code: {ts_code}")
    return f"{number:04d}.HK"


def _sha256_file(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _parse_ymd(ymd: str) -> date:
    return date(int(ymd[:4]), int(ymd[4:6]), int(ymd[6:8]))


@dataclass(frozen=True)
class HkTushareDataset:
    open_dates: tuple[date, ...]
    board_lots: dict[str, int]
    ggt_rows: list[dict[str, Any]]
    hk_hold_rows: list[dict[str, Any]]
    digests: dict[str, str]


class TushareClient:
    """Minimal standalone Tushare Pro client reading `TUSHARE_TOKEN` strictly from env."""

    def __init__(self, token: str, *, timeout: float = 40.0, max_attempts: int = 4) -> None:
        if not token or any(not (33 <= ord(ch) <= 126) for ch in token):
            raise ValueError("TUSHARE_TOKEN is absent or invalid")
        self._token = token
        self._timeout = timeout
        self._max_attempts = max_attempts

    @classmethod
    def from_env(cls, env_var: str = "TUSHARE_TOKEN") -> TushareClient:
        tok = os.environ.get(env_var, "").strip()
        if not tok:
            raise RuntimeError(f"Required environment variable {env_var} is not set")
        return cls(tok)

    def query(
        self,
        api_name: str,
        params: Mapping[str, str],
        fields: str = "",
    ) -> list[dict[str, Any]]:
        body_bytes = json.dumps(
            {
                "api_name": api_name,
                "token": self._token,
                "params": dict(params),
                "fields": fields,
            },
            separators=(",", ":"),
        ).encode("utf-8")
        for attempt in range(self._max_attempts):
            req = urllib.request.Request(
                TUSHARE_API_URL,
                data=body_bytes,
                headers={"Content-Type": "application/json"},
            )
            try:
                with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                    payload = json.loads(resp.read().decode("utf-8"))
            except OSError:
                if attempt + 1 < self._max_attempts:
                    time.sleep(1.5 * (attempt + 1))
                    continue
                raise RuntimeError(f"Tushare transport error on {api_name}") from None
            code = payload.get("code")
            if code == 0:
                data = payload.get("data") or {}
                cols = list(data.get("fields") or [])
                items = list(data.get("items") or [])
                return [dict(zip(cols, row, strict=False)) for row in items]
            msg = str(payload.get("msg", "unknown error"))
            if attempt + 1 < self._max_attempts and (
                "每分钟" in msg or "频繁" in msg or code == -2002
            ):
                time.sleep(3.0 * (attempt + 1))
                continue
            raise RuntimeError(f"Tushare {api_name} failed (code={code})")
        raise RuntimeError(f"Tushare {api_name} exhausted retries")

    def fetch_hk_hold_range(self, start_ymd: str, end_ymd: str) -> list[dict[str, Any]]:
        rows = self.query(
            "hk_hold",
            {"start_date": start_ymd, "end_date": end_ymd, "exchange": "HK"},
            "code,trade_date,ts_code,vol,ratio,exchange",
        )
        if len(rows) >= HK_HOLD_ROW_CEILING and start_ymd < end_ymd:
            s_dt = _parse_ymd(start_ymd)
            e_dt = _parse_ymd(end_ymd)
            mid_dt = s_dt + (e_dt - s_dt) // 2
            left = self.fetch_hk_hold_range(start_ymd, mid_dt.strftime("%Y%m%d"))
            right_start = (mid_dt + timedelta(days=1)).strftime("%Y%m%d")
            right = self.fetch_hk_hold_range(right_start, end_ymd)
            return left + right
        return rows


def refresh_hk_tushare_data(
    data_dir: Path,
    *,
    end_date: date,
    warmup_start: date = date(2023, 6, 1),
    client: TushareClient | None = None,
) -> HkTushareDataset:
    """Pull HK calendar, `hk_basic`, `ggt_daily`, and `hk_hold` into `data_dir/hk/tushare/`."""
    ts_client = client or TushareClient.from_env()
    out_dir = data_dir / "hk" / "tushare"
    out_dir.mkdir(parents=True, exist_ok=True)

    cal_end = (end_date + timedelta(days=30)).strftime("%Y%m%d")
    start_ymd = warmup_start.strftime("%Y%m%d")
    end_ymd = end_date.strftime("%Y%m%d")

    cal_rows = [
        {"cal_date": str(r["cal_date"]), "is_open": int(r["is_open"])}
        for r in ts_client.query(
            "hk_tradecal",
            {"start_date": start_ymd, "end_date": cal_end, "is_open": "1"},
            "cal_date,is_open",
        )
    ]
    pq.write_table(
        pa.Table.from_pylist(cal_rows),
        out_dir / "hk_tradecal.parquet",
        compression="zstd",
    )

    basic_rows = [
        {
            "ts_code": str(r["ts_code"]),
            "name": str(r.get("name") or ""),
            "trade_unit": float(r["trade_unit"]) if r.get("trade_unit") is not None else None,
            "curr_type": str(r.get("curr_type") or ""),
            "list_date": str(r.get("list_date") or ""),
        }
        for r in ts_client.query(
            "hk_basic",
            {"list_status": "L"},
            "ts_code,name,trade_unit,curr_type,list_date",
        )
    ]
    pq.write_table(
        pa.Table.from_pylist(basic_rows),
        out_dir / "hk_basic_listed.parquet",
        compression="zstd",
    )

    ggt_rows = [
        {
            "trade_date": str(r["trade_date"]),
            "buy_amount": float(r["buy_amount"] or 0.0),
            "buy_volume": float(r["buy_volume"] or 0.0),
            "sell_amount": float(r["sell_amount"] or 0.0),
            "sell_volume": float(r["sell_volume"] or 0.0),
        }
        for r in ts_client.query(
            "ggt_daily",
            {"start_date": start_ymd, "end_date": end_ymd},
            "trade_date,buy_amount,buy_volume,sell_amount,sell_volume",
        )
    ]
    pq.write_table(
        pa.Table.from_pylist(ggt_rows),
        out_dir / "ggt_daily.parquet",
        compression="zstd",
    )

    # Incremental support for hk_hold if an existing hk_hold*.parquet already covers earlier dates
    existing_hold_files = sorted(out_dir.glob("hk_hold*.parquet"))
    dedup: dict[tuple[str, str], dict[str, Any]] = {}
    fetch_start_ymd = start_ymd
    if existing_hold_files:
        for r in pq.read_table(existing_hold_files[-1]).to_pylist():
            td = str(r["trade_date"])
            tc = str(r["ts_code"])
            dedup[(td, tc)] = {
                "trade_date": td,
                "ts_code": tc,
                "vol": int(r["vol"]),
                "ratio": float(r["ratio"]),
            }
        if dedup:
            max_cached = max(k[0] for k in dedup)
            overlap_dt = max(warmup_start, _parse_ymd(max_cached) - timedelta(days=7))
            fetch_start_ymd = overlap_dt.strftime("%Y%m%d")

    open_ymds: list[str] = sorted(
        str(r["cal_date"])
        for r in cal_rows
        if fetch_start_ymd <= str(r["cal_date"]) <= end_ymd
    )
    chunks: list[tuple[str, str]] = []
    step = 3
    for idx in range(0, len(open_ymds), step):
        seg = open_ymds[idx : idx + step]
        if idx + step < len(open_ymds):
            nxt_dt = _parse_ymd(open_ymds[idx + step])
            chunks.append((seg[0], (nxt_dt - timedelta(days=1)).strftime("%Y%m%d")))
        else:
            chunks.append((seg[0], end_ymd))

    if chunks:
        with ThreadPoolExecutor(max_workers=6) as pool:
            futs = [pool.submit(ts_client.fetch_hk_hold_range, s, e) for s, e in chunks]
            for fut in futs:
                for r in fut.result():
                    if r.get("vol") is None or r.get("ratio") is None:
                        continue
                    td = str(r["trade_date"])
                    tc = str(r["ts_code"])
                    dedup[(td, tc)] = {
                        "trade_date": td,
                        "ts_code": tc,
                        "vol": int(r["vol"]),
                        "ratio": float(r["ratio"]),
                    }

    hold_path = out_dir / "hk_hold_full.parquet"
    pq.write_table(
        pa.Table.from_pylist([dedup[k] for k in sorted(dedup)]),
        hold_path,
        compression="zstd",
    )
    return load_hk_tushare_from_cache(data_dir / "hk", max_date=end_date)


def _find_latest_parquet(tushare_dir: Path, prefix: str) -> Path:
    matches = sorted(tushare_dir.glob(f"{prefix}*.parquet"))
    if not matches:
        raise FileNotFoundError(f"Missing {prefix}*.parquet in {tushare_dir}")
    return matches[-1]


def load_hk_tushare_from_cache(
    hk_cache_dir: Path,
    *,
    max_date: date | None = None,
) -> HkTushareDataset:
    """Load Tushare HK calendar, board lots, `ggt_daily`, and `hk_hold` from Parquet cache."""
    cand_dirs = [
        hk_cache_dir / "tushare",
        hk_cache_dir / "hk" / "tushare",
        hk_cache_dir,
    ]
    ts_dir = next(
        (d for d in cand_dirs if d.exists() and list(d.glob("hk_tradecal*.parquet"))),
        None,
    )
    if ts_dir is None:
        raise FileNotFoundError(f"Could not locate Tushare HK Parquet cache under {hk_cache_dir}")

    cal_path = _find_latest_parquet(ts_dir, "hk_tradecal")
    basic_path = _find_latest_parquet(ts_dir, "hk_basic")
    ggt_path = _find_latest_parquet(ts_dir, "ggt_daily")
    hold_path = _find_latest_parquet(ts_dir, "hk_hold")

    open_dates = tuple(
        sorted(
            _parse_ymd(str(r["cal_date"]))
            for r in pq.read_table(cal_path).to_pylist()
            if int(r["is_open"]) == 1
        )
    )

    basic_map: dict[str, int] = {}
    for r in pq.read_table(basic_path).to_pylist():
        if r.get("trade_unit") is not None:
            basic_map[str(r["ts_code"])] = round(float(r["trade_unit"]))

    board_lots: dict[str, int] = dict(DEFAULT_HK_STOCK_BOARD_LOTS)
    for sym in HK_BLUECHIP_UNIVERSE:
        ts5 = indexone_to_tushare_hk(sym)
        if ts5 in basic_map and basic_map[ts5] >= 1:
            board_lots[sym] = basic_map[ts5]

    max_ymd = max_date.strftime("%Y%m%d") if max_date is not None else None
    ggt_rows = [
        dict(r)
        for r in pq.read_table(ggt_path).to_pylist()
        if max_ymd is None or str(r["trade_date"]) <= max_ymd
    ]
    hold_rows = [
        dict(r)
        for r in pq.read_table(hold_path).to_pylist()
        if max_ymd is None or str(r["trade_date"]) <= max_ymd
    ]

    digests = {
        "hk_tradecal_sha256": _sha256_file(cal_path),
        "hk_basic_sha256": _sha256_file(basic_path),
        "ggt_daily_sha256": _sha256_file(ggt_path),
        "hk_hold_sha256": _sha256_file(hold_path),
    }
    return HkTushareDataset(
        open_dates=open_dates,
        board_lots=board_lots,
        ggt_rows=ggt_rows,
        hk_hold_rows=hold_rows,
        digests=digests,
    )
