import httpx
import logging
from .base import BaseScanner, FundingRate

logger = logging.getLogger(__name__)


def _strip_symbol(raw: str) -> str:
    """BTC_USDC_PERP → BTC"""
    return raw.split("_")[0]


class BackpackScanner(BaseScanner):
    """Backpack Exchange — публичный API, без авторизации."""

    exchange_name = "Backpack"
    URL = "https://api.backpack.exchange/api/v1/markPrices"

    async def _get_volumes(self) -> dict:
        """Возвращает {symbol: daily_volume_usd} из /api/v1/tickers."""
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get("https://api.backpack.exchange/api/v1/tickers")
                tickers = resp.json()
            return {
                t["symbol"].split("_")[0].upper(): float(t.get("quoteVolume") or 0)
                for t in tickers if "_PERP" in t.get("symbol", "")
            }
        except Exception as e:
            logger.debug(f"Backpack: не удалось получить объёмы: {e}")
            return {}

    async def get_funding_rates(self) -> list[FundingRate]:
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(self.URL, params={"marketType": "PERP"})
                data = resp.json()
        except Exception as e:
            logger.error(f"Backpack: ошибка запроса: {e}")
            return []

        if not isinstance(data, list):
            logger.error(f"Backpack: неожиданный формат ответа: {type(data)}")
            return []

        volumes = await self._get_volumes()

        rates = []
        for item in data:
            symbol_raw = item.get("symbol", "")
            if "PERP" not in symbol_raw:
                continue

            try:
                hourly_rate = float(item.get("fundingRate", 0) or 0)
                apr = hourly_rate * 24 * 365 * 100
                mark_price = float(item.get("markPrice", 0) or 0)
                sym = _strip_symbol(symbol_raw)
                rates.append(FundingRate(
                    exchange="Backpack",
                    symbol=sym,
                    rate=hourly_rate,
                    interval_hours=1,
                    apr=apr,
                    volume_usd=volumes.get(sym, 0),
                    mark_price=mark_price,
                ))
            except Exception as e:
                logger.debug(f"Backpack: ошибка парсинга {symbol_raw}: {e}")

        logger.info(f"Backpack: получено {len(rates)} рынков")
        return rates
