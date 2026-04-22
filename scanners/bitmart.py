import asyncio
import logging

import httpx

from .base import BaseScanner, FundingRate

logger = logging.getLogger(__name__)


class BitMartScanner(BaseScanner):
    """BitMart Futures — публичный API, без авторизации."""

    exchange_name = "BitMart"
    BASE_URL = "https://api-cloud-v2.bitmart.com"

    async def _get_book_top(self, symbol: str, timeout: float = 2.5) -> tuple[float, float]:
        """Возвращает (best_bid, best_ask) из depth."""
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.get(
                    f"{self.BASE_URL}/contract/public/depth",
                    params={"symbol": f"{symbol.upper()}USDT", "limit": 5},
                )
                data = resp.json()
            payload = (data or {}).get("data") or {}

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
            logger.debug(f"BitMart: не удалось получить стакан {symbol}: {e}")
            return 0.0, 0.0

    async def enrich_book_top(self, symbol: str, timeout: float = 2.5) -> dict | None:
        """Точечно добирает bid/ask для конкретного символа с жёстким таймаутом."""
        try:
            bid, ask = await asyncio.wait_for(self._get_book_top(symbol, timeout=timeout), timeout=timeout + 0.3)
            return {"symbol": symbol.upper(), "bid_price": bid, "ask_price": ask}
        except Exception as e:
            logger.debug(f"BitMart: enrich_book_top ошибка для {symbol}: {e}")
            return None

    async def get_funding_rates(self) -> list[FundingRate]:
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(f"{self.BASE_URL}/contract/public/details")
                payload = resp.json()
        except Exception as e:
            logger.error(f"BitMart: ошибка запроса: {e}")
            return []

        symbols = ((payload or {}).get("data") or {}).get("symbols") or []
        rates: list[FundingRate] = []

        parsed = []
        for item in symbols:
            try:
                symbol = (item.get("base_currency") or "").upper()
                quote = (item.get("quote_currency") or "").upper()
                status = str(item.get("status") or "").lower()
                if not symbol or quote != "USDT" or status == "delisted":
                    continue
                parsed.append(item)
            except Exception:
                continue

        for item in parsed:
            try:
                symbol = (item.get("base_currency") or "").upper()
                funding_rate = float(item.get("funding_rate") or 0)
                interval_hours = int(item.get("funding_interval_hours") or 8)
                mark_price = float(item.get("last_price") or item.get("index_price") or 0)
                apr = funding_rate * (24 / interval_hours) * 365 * 100
                bid_price = float(
                    item.get("best_bid_price")
                    or item.get("bid_price")
                    or item.get("bid")
                    or 0
                )
                ask_price = float(
                    item.get("best_ask_price")
                    or item.get("ask_price")
                    or item.get("ask")
                    or 0
                )
                # Массово depth не дёргаем, чтобы не подвешивать цикл.
                # Для выбранных пар добираем стакан точечно позже.

                rates.append(FundingRate(
                    exchange="BitMart",
                    symbol=symbol,
                    rate=funding_rate,
                    interval_hours=interval_hours,
                    apr=apr,
                    open_interest_usd=float(item.get("open_interest_value") or 0),
                    volume_usd=float(item.get("turnover_24h") or 0),
                    mark_price=mark_price,
                    bid_price=bid_price,
                    ask_price=ask_price,
                ))
            except Exception as e:
                logger.debug(f"BitMart: ошибка парсинга {item.get('symbol')}: {e}")

        logger.info(f"BitMart: получено {len(rates)} рынков")
        return rates
