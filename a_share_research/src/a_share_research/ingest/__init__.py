"""Optional, fail-closed market-data ingestion helpers.

The research engine does not import this package.  Callers opt in to a provider,
normalize the returned fragments, and then build a versioned research snapshot.
"""

from .baostock import BaoStockProvider
from .hithink_provider import HiThinkMarketDataProvider, HiThinkProvider
from .market_data import (
    BENCHMARK_SYMBOLS,
    DAILY_FIELDS,
    CollectionError,
    DailyBar,
    DailyMarketDataProvider,
    DailySeries,
    InstrumentMarketData,
    MarketDataCollection,
    ProviderUnavailableError,
    collect_cn_market_data,
    normalize_baostock_symbol,
    normalize_cn_symbol,
)
from .snapshot_builder import (
    SnapshotCollectionResult,
    collect_cn_snapshot,
    probe_hithink_provider,
    validate_research_seed,
)

__all__ = [
    "BENCHMARK_SYMBOLS",
    "DAILY_FIELDS",
    "BaoStockProvider",
    "CollectionError",
    "DailyBar",
    "DailyMarketDataProvider",
    "DailySeries",
    "HiThinkMarketDataProvider",
    "HiThinkProvider",
    "InstrumentMarketData",
    "MarketDataCollection",
    "ProviderUnavailableError",
    "SnapshotCollectionResult",
    "collect_cn_market_data",
    "collect_cn_snapshot",
    "normalize_baostock_symbol",
    "normalize_cn_symbol",
    "probe_hithink_provider",
    "validate_research_seed",
]
