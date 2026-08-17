"""Optional, fail-closed market-data ingestion helpers.

The research engine does not import this package.  Callers opt in to a provider,
normalize the returned fragments, and then build a versioned research snapshot.
"""

from .baostock import BaoStockProvider
from .market_data import (
    BENCHMARK_SYMBOLS,
    DAILY_FIELDS,
    CollectionError,
    DailyBar,
    DailyMarketDataProvider,
    InstrumentMarketData,
    MarketDataCollection,
    ProviderUnavailableError,
    collect_cn_market_data,
    normalize_baostock_symbol,
)
from .snapshot_builder import (
    SnapshotCollectionResult,
    collect_cn_snapshot,
    validate_research_seed,
)

__all__ = [
    "BENCHMARK_SYMBOLS",
    "DAILY_FIELDS",
    "BaoStockProvider",
    "CollectionError",
    "DailyBar",
    "DailyMarketDataProvider",
    "InstrumentMarketData",
    "MarketDataCollection",
    "ProviderUnavailableError",
    "SnapshotCollectionResult",
    "collect_cn_market_data",
    "collect_cn_snapshot",
    "normalize_baostock_symbol",
    "validate_research_seed",
]
