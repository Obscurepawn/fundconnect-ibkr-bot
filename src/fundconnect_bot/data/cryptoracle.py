"""Standalone Cryptoracle BTC 1d sentiment & attention client and Parquet cache loader."""

from __future__ import annotations

import hashlib
import json
import math
import os
from collections.abc import Mapping
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

import pyarrow as pa
import pyarrow.parquet as pq

from ..config import SHANGHAI_TZ

CRYPTO_URL = "https://service.cryptoracle.network/openapi/v2.1/endpoint/klines"
CRYPTO_METRICS: Mapping[str, str] = {
    "CO-S-01-01": "emo_z",
    "CO-S-01-02": "ced",
    "CO-A-02-03": "net_sentiment",
    "CO-A-01-03": "message_count",
    "CO-A-01-04": "interaction_count",
}


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(
        self, req: Request, fp: Any, code: int, msg: str, headers: Any, newurl: str
    ) -> None:
        return None


def _sha256_file(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def parse_cryptoracle_daily(
    payload: Mapping[str, Any],
    token: str,
    start: date,
    end_exclusive: date,
    *,
    metrics: Mapping[str, str] = CRYPTO_METRICS,
) -> list[dict[str, Any]]:
    """Parse complete UTC daily periods; Cryptoracle timestamps are Asia/Shanghai (UTC+8)."""
    if payload.get("code") != 200:
        raise ValueError(f"Cryptoracle business error code={payload.get('code')}")
    data = payload.get("data")
    if not isinstance(data, list):
        raise TypeError("Cryptoracle response data must be a list")

    begin_utc = datetime.combine(start, datetime.min.time(), UTC)
    end_utc = datetime.combine(end_exclusive, datetime.min.time(), UTC)
    rows: list[dict[str, Any]] = []
    seen: set[datetime] = set()
    for item in data:
        if not isinstance(item, dict):
            raise TypeError("Cryptoracle item must be a dict")
        opening = (
            datetime.strptime(str(item["openTime"]), "%Y-%m-%d %H:%M:%S")
            .replace(tzinfo=SHANGHAI_TZ)
            .astimezone(UTC)
        )
        closing = (
            datetime.strptime(str(item["closeTime"]), "%Y-%m-%d %H:%M:%S")
            .replace(tzinfo=SHANGHAI_TZ)
            .astimezone(UTC)
        )
        if closing - opening != timedelta(days=1) or opening.time() != datetime.min.time():
            raise ValueError("Cryptoracle 1d bar must span an exact UTC day")
        if opening < begin_utc or closing > end_utc:
            continue
        if opening in seen:
            raise ValueError(f"Duplicate Cryptoracle daily bar at {opening.isoformat()}")
        seen.add(opening)
        row: dict[str, Any] = {
            "token": token,
            "period_start": opening,
            "period_end": closing,
        }
        for code, col in metrics.items():
            val = float(item[code])
            if not math.isfinite(val):
                raise ValueError(f"Non-finite Cryptoracle metric {code}")
            row[col] = val
        rows.append(row)
    return sorted(rows, key=lambda r: r["period_start"])


class CryptoracleClient:
    """Standalone HTTP client reading `CRYPTORACLE_API_KEY` strictly from `os.environ`."""

    def __init__(self, api_key: str, *, timeout: float = 35.0) -> None:
        if not api_key or any(not (33 <= ord(ch) <= 126) for ch in api_key):
            raise ValueError("CRYPTORACLE_API_KEY is absent or invalid")
        self._api_key = api_key
        self._timeout = timeout

    @classmethod
    def from_env(cls, env_var: str = "CRYPTORACLE_API_KEY") -> CryptoracleClient:
        key = os.environ.get(env_var, "").strip()
        if not key:
            raise RuntimeError(f"Required environment variable {env_var} is not set")
        return cls(key)

    def fetch_daily(
        self,
        token: str,
        start: date,
        end_exclusive: date,
        *,
        metrics: Mapping[str, str] = CRYPTO_METRICS,
    ) -> list[dict[str, Any]]:
        body = {
            "token": token,
            "interval": "1d",
            "endpoints": list(metrics.keys()),
            "startTime": f"{start.isoformat()} 08:00:00",
            "endTime": f"{end_exclusive.isoformat()} 08:00:00",
        }
        raw_body = json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")
        req = Request(
            CRYPTO_URL,
            data=raw_body,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "X-API-KEY": self._api_key,
            },
            method="POST",
        )
        try:
            with build_opener(_NoRedirect()).open(req, timeout=self._timeout) as resp:
                status = int(resp.status)
                payload_bytes = resp.read(32 * 1024 * 1024)
        except HTTPError as err:
            raise RuntimeError(f"Cryptoracle HTTP {err.code}") from None
        except (URLError, OSError):
            raise RuntimeError("Cryptoracle transport failed") from None
        if status != 200:
            raise RuntimeError(f"Cryptoracle returned HTTP {status}")
        parsed = json.loads(payload_bytes.decode("utf-8"))
        if not isinstance(parsed, dict):
            raise TypeError("Cryptoracle envelope must be an object")
        return parse_cryptoracle_daily(parsed, token, start, end_exclusive, metrics=metrics)


def load_or_fetch_cryptoracle_btc(
    cache_dir: Path,
    *,
    end_date: date,
    refresh: bool = False,
    crypto_start: date = date(2025, 1, 1),
    client: CryptoracleClient | None = None,
) -> tuple[list[dict[str, Any]], str]:
    """Load cached `btc_cryptoracle_1d.parquet` or refresh live BTC 1d bars through `end_date`."""
    cand_paths = [
        cache_dir / "btc_cryptoracle_1d.parquet",
        cache_dir / "prepared" / "btc_cryptoracle_1d.parquet",
        cache_dir / "us" / "btc_cryptoracle_1d.parquet",
    ]
    existing = next((p for p in cand_paths if p.exists()), None)
    end_exclusive = end_date + timedelta(days=1)

    if not refresh:
        if existing is None:
            raise FileNotFoundError(
                f"btc_cryptoracle_1d.parquet not found under {cache_dir}"
            )
        rows = [
            dict(r)
            for r in pq.read_table(existing).to_pylist()
            if r["period_end"].astimezone(UTC)
            <= datetime.combine(end_exclusive, datetime.min.time(), UTC)
        ]
        rows.sort(key=lambda r: r["period_end"])
        return rows, _sha256_file(existing)

    co_client = client or CryptoracleClient.from_env()
    dedup: dict[datetime, dict[str, Any]] = {}
    cursor = crypto_start
    if existing is not None:
        for r in pq.read_table(existing).to_pylist():
            ps = r["period_start"].astimezone(UTC)
            dedup[ps] = {
                **dict(r),
                "period_start": ps,
                "period_end": r["period_end"].astimezone(UTC),
            }
        if dedup:
            cursor = max(crypto_start, max(dedup).date() - timedelta(days=3))

    while cursor < end_exclusive:
        chunk_end = min(cursor + timedelta(days=90), end_exclusive)
        for r in co_client.fetch_daily("BTC", cursor, chunk_end):
            dedup[r["period_start"]] = r
        cursor = chunk_end

    out_dir = cache_dir / "us"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "btc_cryptoracle_1d.parquet"
    sorted_rows = [dedup[k] for k in sorted(dedup)]
    schema_fields: list[tuple[str, pa.DataType]] = [
        ("token", pa.string()),
        ("period_start", pa.timestamp("us", tz="UTC")),
        ("period_end", pa.timestamp("us", tz="UTC")),
        *[(name, pa.float64()) for name in CRYPTO_METRICS.values()],
    ]
    pq.write_table(
        pa.Table.from_pylist(sorted_rows, schema=pa.schema(schema_fields)),
        out_path,
        compression="zstd",
    )
    filtered = [
        r
        for r in sorted_rows
        if r["period_end"] <= datetime.combine(end_exclusive, datetime.min.time(), UTC)
    ]
    return filtered, _sha256_file(out_path)
