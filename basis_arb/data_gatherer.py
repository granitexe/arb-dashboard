"""
Continuous market data gatherer for Hyperliquid, Binance, Bybit, OKX, and CoinGecko.

Collects price, volume, funding rate, and open interest data from all exchanges
and stores them in a unified SQLite database via DataStore.
"""

import asyncio
import json
import logging
import math
import signal
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

import aiohttp

from .data_store import DataStore

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("data_gatherer")


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class TickerData:
    exchange: str
    symbol: str
    price: float
    volume_24h: Optional[float] = None
    funding_rate: Optional[float] = None
    open_interest: Optional[float] = None
    premium_index: Optional[float] = None
    bid: Optional[float] = None
    ask: Optional[float] = None
    mid: Optional[float] = None
    last: Optional[float] = None
    timestamp: float = field(default_factory=time.time)


# ---------------------------------------------------------------------------
# Base exchange collector
# ---------------------------------------------------------------------------

class BaseCollector:
    """Abstract base for exchange data collectors."""

    def __init__(self, session: aiohttp.ClientSession, timeout: int = 10):
        self.session = session
        self.timeout = timeout
        self._log = logger.getChild(self.name)

    @property
    def name(self) -> str:
        raise NotImplementedError

    async def fetch(self) -> List[TickerData]:
        raise NotImplementedError

    async def _get_json(self, url: str, headers: Optional[Dict] = None) -> Any:
        async with self.session.get(url, headers=headers, timeout=self.timeout) as resp:
            resp.raise_for_status()
            return await resp.json()


# ---------------------------------------------------------------------------
# Hyperliquid
# ---------------------------------------------------------------------------

class HyperliquidCollector(BaseCollector):
    """Collector for Hyperliquid perpetuals."""

    name = "hyperliquid"

    async def fetch(self) -> List[TickerData]:
        try:
            # Fetch ticker info
            payload = {"type": "tickerAll"}
            async with self.session.post(
                "https://api.hyperliquid.xyz/info",
                json=payload,
                timeout=self.timeout,
            ) as resp:
                resp.raise_for_status()
                data = await resp.json()

            results: List[TickerData] = []
            now = time.time()
            # data is a dict with key "universe" containing list of perpetuals
            universe = data.get("universe", [])
            tickers = data.get("ticker", {})

            for item in universe:
                sym = item.get("szSymbol", "")
                if not sym:
                    continue
                ticker = tickers.get(sym, {})
                try:
                    price = float(ticker.get("lastPrice", 0))
                except (TypeError, ValueError):
                    price = 0.0

                try:
                    volume_24h = float(ticker.get("volume", 0))
                except (TypeError, ValueError):
                    volume_24h = None

                try:
                    open_interest = float(ticker.get("openInterest", 0))
                except (TypeError, ValueError):
                    open_interest = None

                try:
                    funding_rate = float(ticker.get("fundingRate", 0))
                except (TypeError, ValueError):
                    funding_rate = None

                results.append(
                    TickerData(
                        exchange=self.name,
                        symbol=sym,
                        price=price,
                        volume_24h=volume_24h,
                        funding_rate=funding_rate,
                        open_interest=open_interest,
                        timestamp=now,
                    )
                )
            return results
        except Exception as e:
            self._log.warning("Fetch failed: %s", e)
            return []


# ---------------------------------------------------------------------------
# Binance
# ---------------------------------------------------------------------------

class BinanceCollector(BaseCollector):
    """Collector for Binance spot + USDT-mapped perpetuals."""

    name = "binance"

    # Map exchange symbols to normalized symbols
    SYMBOL_MAP = {
        "BTCUSDT": "BTC/USDT",
        "ETHUSDT": "ETH/USDT",
        "SOLUSDT": "SOL/USDT",
        "BNBUSDT": "BNB/USDT",
        "XRPUSDT": "XRP/USDT",
        "ADAUSDT": "ADA/USDT",
        "DOGEUSDT": "DOGE/USDT",
        "MATICUSDT": "MATIC/USDT",
        "DOTUSDT": "DOT/USDT",
        "LINKUSDT": "LINK/USDT",
        "AVAXUSDT": "AVAX/USDT",
        "LTCUSDT": "LTC/USDT",
        "ATOMUSDT": "ATOM/USDT",
        "UNIUSDT": "UNI/USDT",
        "XLMUSDT": "XLM/USDT",
        "ETCUSDT": "ETC/USDT",
        "FILUSDT": "FIL/USDT",
        "APTUSDT": "APT/USDT",
        "ARBUSDT": "ARB/USDT",
        "OPUSDT": "OP/USDT",
    }

    async def fetch(self) -> List[TickerData]:
        try:
            # Fetch 24hr ticker for all symbols
            data = await self._get_json(
                "https://api.binance.com/api/v3/ticker/24hr"
            )

            results: List[TickerData] = []
            now = time.time()

            for item in data:
                sym = item.get("symbol", "")
                if sym not in self.SYMBOL_MAP:
                    continue
                try:
                    price = float(item.get("lastPrice", 0))
                except (TypeError, ValueError):
                    continue

                try:
                    volume = float(item.get("quoteVolume", 0))
                except (TypeError, ValueError):
                    volume = None

                results.append(
                    TickerData(
                        exchange=self.name,
                        symbol=self.SYMBOL_MAP[sym],
                        price=price,
                        volume_24h=volume,
                        timestamp=now,
                    )
                )
            return results
        except Exception as e:
            self._log.warning("Fetch failed: %s", e)
            return []


# ---------------------------------------------------------------------------
# Bybit
# ---------------------------------------------------------------------------

class BybitCollector(BaseCollector):
    """Collector for Bybit USDT perpetuals."""

    name = "bybit"

    SYMBOLS = [
        "BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT",
        "ADAUSDT", "DOGEUSDT", "DOTUSDT", "LINKUSDT", "AVAXUSDT",
        "LTCUSDT", "ATOMUSDT", "UNIUSDT", "XLMUSDT", "ETCUSDT",
        "FILUSDT", "APTUSDT", "ARBUSDT", "OPUSDT", "MATICUSDT",
    ]

    async def fetch(self) -> List[TickerData]:
        try:
            # Fetch tickers for USDT perpetual category
            params = {"category": "linear", "limit": 50}
            data = await self._get_json(
                "https://api.bybit.com/v5/market/tickers",
                headers={"X-BAPI-SEND-KEY": ""},
            )

            results: List[TickerData] = []
            now = time.time()
            items = data.get("result", {}).get("list", [])

            for item in items:
                sym = item.get("symbol", "")
                if sym not in self.SYMBOLS:
                    continue
                try:
                    price = float(item.get("lastPrice", 0))
                except (TypeError, ValueError):
                    continue

                try:
                    volume = float(item.get("turnover24h", 0))
                except (TypeError, ValueError):
                    volume = None

                try:
                    open_interest = float(item.get("openInterest", 0))
                except (TypeError, ValueError):
                    open_interest = None

                try:
                    funding_rate = float(item.get("fundingRate", 0))
                except (TypeError, ValueError):
                    funding_rate = None

                results.append(
                    TickerData(
                        exchange=self.name,
                        symbol=sym,
                        price=price,
                        volume_24h=volume,
                        funding_rate=funding_rate,
                        open_interest=open_interest,
                        timestamp=now,
                    )
                )
            return results
        except Exception as e:
            self._log.warning("Fetch failed: %s", e)
            return []


# ---------------------------------------------------------------------------
# OKX
# ---------------------------------------------------------------------------

class OKXCollector(BaseCollector):
    """Collector for OKX USDT-margined perpetuals."""

    name = "okx"

    INST_TYPES = ["SWAP"]

    async def fetch(self) -> List[TickerData]:
        try:
            # Get all tickers for SWAP instruments
            all_tickers: List[Dict] = []
            for inst_type in self.INST_TYPES:
                data = await self._get_json(
                    f"https://www.okx.com/api/v5/market/tickers?instType={inst_type}&uly=USDT"
                )
                ticks = data.get("data", [])
                all_tickers.extend(ticks)

            results: List[TickerData] = []
            now = time.time()

            for item in all_tickers:
                inst_id = item.get("instId", "")
                # Normalize: BTC-USDT-SWAP -> BTC/USDT
                if "-USDT-SWAP" not in inst_id:
                    continue
                sym = inst_id.replace("-USDT-SWAP", "") + "/USDT"

                try:
                    price = float(item.get("last", 0))
                except (TypeError, ValueError):
                    continue

                try:
                    volume = float(item.get("vol24h", 0))
                except (TypeError, ValueError):
                    volume = None

                try:
                    open_interest = float(item.get("openInterest", 0))
                except (TypeError, ValueError):
                    open_interest = None

                try:
                    funding_rate = float(item.get("fundingRate", 0))
                except (TypeError, ValueError):
                    funding_rate = None

                results.append(
                    TickerData(
                        exchange=self.name,
                        symbol=sym,
                        price=price,
                        volume_24h=volume,
                        funding_rate=funding_rate,
                        open_interest=open_interest,
                        timestamp=now,
                    )
                )
            return results
        except Exception as e:
            self._log.warning("Fetch failed: %s", e)
            return []


# ---------------------------------------------------------------------------
# CoinGecko
# ---------------------------------------------------------------------------

class CoinGeckoCollector(BaseCollector):
    """Collector for CoinGecko spot prices (used as reference)."""

    name = "coingecko"

    COIN_IDS = [
        "bitcoin", "ethereum", "solana", "binancecoin", "ripple",
        "cardano", "dogecoin", "polygon", "polkadot", "chainlink",
        "avalanche-2", "litecoin", "cosmos", "uniswap", "stellar",
        "ethereum-classic", "filecoin", "aptos", "arbitrum", "optimism",
    ]

    async def fetch(self) -> List[TickerData]:
        try:
            ids = ",".join(self.COIN_IDS)
            data = await self._get_json(
                f"https://api.coingecko.com/api/v3/simple/price"
                f"?ids={ids}&vs_currencies=usdt"
                f"&include_24hr_vol=true&include_market_cap=true",
            )

            results: List[TickerData] = []
            now = time.time()

            for coin_id, info in data.items():
                price_info = info.get("usdt", {})
                if isinstance(price_info, dict):
                    price = price_info.get("usd", 0)
                    volume = price_info.get("usd_24h_vol", 0)
                else:
                    price = price_info
                    volume = info.get("usd_24h_vol", 0)

                sym = coin_id.replace("-", " ").title().replace(" ", "") + "/USDT"
                # Map known coins
                symbol_map = {
                    "Bitcoin": "BTC/USDT",
                    "Ethereum": "ETH/USDT",
                    "Solana": "SOL/USDT",
                    "Binancecoin": "BNB/USDT",
                    "Ripple": "XRP/USDT",
                    "Cardano": "ADA/USDT",
                    "Dogecoin": "DOGE/USDT",
                    "Polygon": "MATIC/USDT",
                    "Polkadot": "DOT/USDT",
                    "Chainlink": "LINK/USDT",
                    "Avalanche2": "AVAX/USDT",
                    "Litecoin": "LTC/USDT",
                    "Cosmos": "ATOM/USDT",
                    "Uniswap": "UNI/USDT",
                    "Stellar": "XLM/USDT",
                    "EthereumClassic": "ETC/USDT",
                    "Filecoin": "FIL/USDT",
                    "Aptos": "APT/USDT",
                    "Arbitrum": "ARB/USDT",
                    "Optimism": "OP/USDT",
                }
                sym = symbol_map.get(sym, sym)

                try:
                    price = float(price)
                except (TypeError, ValueError):
                    price = 0.0

                try:
                    volume = float(volume) if volume else None
                except (TypeError, ValueError):
                    volume = None

                results.append(
                    TickerData(
                        exchange=self.name,
                        symbol=sym,
                        price=price,
                        volume_24h=volume,
                        timestamp=now,
                    )
                )
            return results
        except Exception as e:
            self._log.warning("Fetch failed: %s", e)
            return []


# ---------------------------------------------------------------------------
# Data Gatherer — orchestrates all collectors
# ---------------------------------------------------------------------------

class DataGatherer:
    """
    Main gatherer that runs all collectors on a configurable interval
    and writes results into a DataStore.
    """

    def __init__(
        self,
        db_path: str = "market_data.db",
        interval: float = 15.0,
    ):
        self.db = DataStore(db_path)
        self.interval = interval
        self._running = False
        self._session: Optional[aiohttp.ClientSession] = None
        self._collectors: List[BaseCollector] = []
        self._log = logger

    def _build_collectors(self, session: aiohttp.ClientSession) -> List[BaseCollector]:
        return [
            HyperliquidCollector(session),
            BinanceCollector(session),
            BybitCollector(session),
            OKXCollector(session),
            CoinGeckoCollector(session),
        ]

    def _ticker_to_record(self, t: TickerData) -> Dict[str, Any]:
        return {
            "timestamp": t.timestamp,
            "exchange": t.exchange,
            "symbol": t.symbol,
            "price": t.price,
            "volume_24h": t.volume_24h,
            "funding_rate": t.funding_rate,
            "open_interest": t.open_interest,
            "premium_index": t.premium_index,
        }

    async def _fetch_once(self) -> None:
        """Fetch from all collectors and store results."""
        if not self._session:
            return

        all_tickers: List[TickerData] = []
        for collector in self._collectors:
            try:
                tickers = await collector.fetch()
                all_tickers.extend(tickers)
                self._log.debug(
                    "%s: collected %d tickers", collector.name, len(tickers)
                )
            except Exception as e:
                self._log.error("%s collector error: %s", collector.name, e)

        # Deduplicate by exchange+symbol, keeping last occurrence
        seen: Dict[tuple, TickerData] = {}
        for t in all_tickers:
            seen[(t.exchange, t.symbol)] = t

        market_records = [self._ticker_to_record(t) for t in seen.values()]
        inserted = self.db.insert_market_data(market_records)
        self._log.info("Inserted %d market data records", inserted)

        # Also store snapshots where we have bid/ask
        snapshot_records = [
            {
                "timestamp": t.timestamp,
                "exchange": t.exchange,
                "symbol": t.symbol,
                "bid": t.bid,
                "ask": t.ask,
                "mid": t.mid,
                "last": t.price,
                "volume": t.volume_24h,
            }
            for t in seen.values()
            if t.bid is not None and t.ask is not None
        ]
        if snapshot_records:
            snap_inserted = self.db.insert_price_snapshots(snapshot_records)
            self._log.debug("Inserted %d snapshot records", snap_inserted)

        # Update metadata with last run time
        self.db.set_metadata("last_run", datetime.utcnow().isoformat())

    async def _run_loop(self) -> None:
        self._session = aiohttp.ClientSession()
        self._collectors = self._build_collectors(self._session)

        while self._running:
            try:
                await self._fetch_once()
            except Exception as e:
                self._log.error("Fetch loop error: %s", e)

            await asyncio.sleep(self.interval)

        await self._session.close()

    def start(self) -> None:
        """Start the gatherer (blocking)."""
        self._running = True
        self._log.info(
            "Starting data gatherer (interval=%.1fs)", self.interval
        )
        try:
            asyncio.run(self._run_loop())
        except KeyboardInterrupt:
            self._log.info("Interrupted, shutting down.")
        finally:
            self._running = False
            self.db.close()
            self._log.info("Data gatherer stopped.")

    def stop(self) -> None:
        self._running = False


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Continuous market data gatherer")
    parser.add_argument(
        "--db",
        default="market_data.db",
        help="Path to SQLite database (default: market_data.db)",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=15.0,
        help="Fetch interval in seconds (default: 15)",
    )
    args = parser.parse_args()

    gatherer = DataGatherer(db_path=args.db, interval=args.interval)

    # Graceful shutdown on SIGINT/SIGTERM
    def handle_signal(signum, frame):
        gatherer.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    gatherer.start()


if __name__ == "__main__":
    main()