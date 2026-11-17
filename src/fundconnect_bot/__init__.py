"""Standalone FundConnect HK/US weekly composite portfolio planner & IBKR execution bot."""

from __future__ import annotations

from .config import (
    BOOTSTRAP_SOURCE_SYMBOL,
    HK_DEFENSIVE_SYMBOL,
    US_DEFENSIVE_SYMBOL,
    ExecutionPolicyConfig,
    IbkrConnectionConfig,
    SleeveCadences,
    SleeveWeights,
)

__all__ = [
    "BOOTSTRAP_SOURCE_SYMBOL",
    "ExecutionPolicyConfig",
    "HK_DEFENSIVE_SYMBOL",
    "IbkrConnectionConfig",
    "SleeveCadences",
    "SleeveWeights",
    "US_DEFENSIVE_SYMBOL",
    "__version__",
]

__version__ = "0.1.0"
