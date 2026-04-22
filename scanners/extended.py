import asyncio
import logging

import httpx

from .base import BaseScanner, FundingRate

logger = logging.getLogger(__name__)


def _strip_symbol(raw: str) -> str:
    """BTC-USD -> BTC."""
    return raw.split("-")[0]


class ExtendedScanner(BaseScanner):
    """Extended Exchange (Starknet) — публичный API, без авторизации."""

    exchange_name = "Extended"
    BASE_URL = "https://api.starknet.extended.exchange/api/v1"

    async def _get_book_top(self, market_name: str, timeout: float = 2.5) -> tuple[float, float]:
        """Возвращает (best_bid, best_ask) из orderbook."""
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.get(
                    f"{self.BASE_URL}/info/orderbook",
                    params={"market": market_name},
                )
                data = resp.json()
            payload = data if isinstance(data, dict) else {}
            bids = payload.get("bids") or payload.get("buy") or []
            asks = payload.get("asks") or payload.get("sell") or []

            def _price(row):
                if isinstance(row, dict):
                    return float(row.get("price") or row.get("p") or 0)
                if isinstance(row, (list, tuple)) and row:
                    return float(row[0] or 0)
                return 0.0

            bid = _price(bids[0]) if bids else 0.0
            ask = _price(asks[0]) if asks else 0.0
            return bid, ask
        except Exception as e:
            logger.debug(f"Extended: не удалось получить стакан {market_name}: {e}")
            return 0.0, 0.0

    async def enrich_book_top(self, symbol: str, timeout: float = 2.5) -> dict | None:
        """Точечно добирает bid/ask для конкретного символа с жёстким таймаутом."""
        try:
            market_name = f"{symbol.upper()}-USD"
            bid, ask = await asyncio.wait_for(self._get_book_top(market_name, timeout=timeout), timeout=timeout + 0.3)
            return {"symbol": symbol.upper(), "bid_price": bid, "ask_price": ask}
        except Exception as e:
            logger.debug(f"Extended: enrich_book_top ошибка для {symbol}: {e}")
            return None

    async def get_funding_rates(self) -> list[FundingRate]:
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(f"{self.BASE_URL}/info/markets")
                markets_data = resp.json()
        except Exception as e:
            logger.error(f"Extended: ошибка получения рынков: {e}")
            return []

        items = markets_data if isinstance(markets_data, list) else markets_data.get("data", markets_data.get("markets", []))
        rates = []

        for item in items:
            symbol_raw = item.get("name") or item.get("market") or item.get("symbol") or ""
            if not symbol_raw:
                continue

            try:
                stats = item.get("marketStats") or {}
                funding_rate = float(stats.get("fundingRate") or 0)
                # fundingRate на Extended — отрицательный означает шорты получают funding.
                # Инвертируем, чтобы в боте positive APR означал доход для SHORT.
                funding_rate = -funding_rate
                apr = funding_rate * 24 * 365 * 100

                oi = float(stats.get("openInterest") or 0)
                mark_price = float(stats.get("markPrice") or 0)
                oi_usd = oi * mark_price if mark_price else oi
                volume_usd = float(stats.get("dailyVolume") or 0)
                bid_price = float(
                    item.get("bestBid")
                    or item.get("bid")
                    or stats.get("bestBid")
                    or stats.get("bid")
                    or stats.get("bidPrice")
                    or 0
                )
                ask_price = float(
                    item.get("bestAsk")
                    or item.get("ask")
                    or stats.get("bestAsk")
                    or stats.get("ask")
                    or stats.get("askPrice")
                    or 0
                )
                if bid_price <= 0 or ask_price <= 0:
                    bid_price, ask_price = await self._get_book_top(symbol_raw)

                rates.append(FundingRate(
                    exchange="Extended",
                    symbol=_strip_symbol(symbol_raw),
                    rate=funding_rate,
                    interval_hours=1,
                    apr=apr,
                    open_interest_usd=oi_usd,
                    volume_usd=volume_usd,
                    mark_price=mark_price,
                    bid_price=bid_price,
                    ask_price=ask_price,
                ))
            except Exception as e:
                logger.debug(f"Extended: ошибка парсинга {symbol_raw}: {e}")

        logger.info(f"Extended: получено {len(rates)} рынков")
        return rates
