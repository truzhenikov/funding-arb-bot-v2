import httpx
import logging
from .base import BaseScanner, FundingRate

logger = logging.getLogger(__name__)

BASE_URL = "https://fapi.asterdex.com"


class AsterScanner(BaseScanner):
    """
    Aster DEX — perpetual futures (API стиль Binance Futures).
    Публичные эндпоинты, без авторизации для чтения.
    """

    exchange_name = "Aster"

    async def _get_funding_intervals(self) -> dict[str, int]:
        """Возвращает {symbol: funding_interval_hours} из /fapi/v1/fundingInfo."""
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(f"{BASE_URL}/fapi/v1/fundingInfo")
                data = resp.json()
            return {
                item["symbol"]: int(item.get("fundingIntervalHours") or 8)
                for item in data
            }
        except Exception as e:
            logger.debug(f"Aster: не удалось получить интервалы фандинга: {e}")
            return {}

    async def _get_volumes(self) -> dict[str, float]:
        """Возвращает {symbol: daily_volume_usd} из /fapi/v1/ticker/24hr."""
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(f"{BASE_URL}/fapi/v1/ticker/24hr")
                data = resp.json()
            return {
                item["symbol"]: float(item.get("quoteVolume") or 0)
                for item in data
            }
        except Exception as e:
            logger.debug(f"Aster: не удалось получить объёмы: {e}")
            return {}

    async def get_funding_rates(self) -> list[FundingRate]:
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(f"{BASE_URL}/fapi/v1/premiumIndex")
                data = resp.json()
        except Exception as e:
            logger.error(f"Aster: ошибка запроса: {e}")
            return []

        if not isinstance(data, list):
            logger.error(f"Aster: неожиданный формат: {type(data)}")
            return []

        # Параллельно получаем интервалы и объёмы
        import asyncio
        intervals_task = self._get_funding_intervals()
        volumes_task = self._get_volumes()
        intervals, volumes = await asyncio.gather(intervals_task, volumes_task)

        rates = []
        for item in data:
            try:
                raw_symbol = item.get("symbol", "")
                if not raw_symbol:
                    continue

                # BTCUSDT → BTC
                symbol = raw_symbol.replace("USDT", "").replace("USDC", "").replace("BUSD", "")
                if not symbol or len(symbol) < 2:
                    continue

                # lastFundingRate — ставка за интервал (формат как Binance)
                funding_rate = float(item.get("lastFundingRate") or 0)
                if funding_rate == 0:
                    continue

                mark_price = float(item.get("markPrice") or 0)

                # Определяем интервал для этого символа
                interval_hours = intervals.get(raw_symbol, 8)

                # Переводим в часовую ставку и APR
                hourly_rate = funding_rate / interval_hours
                apr = hourly_rate * 24 * 365 * 100

                volume_usd = volumes.get(raw_symbol, 0)

                rates.append(FundingRate(
                    exchange="Aster",
                    symbol=symbol,
                    rate=hourly_rate,
                    interval_hours=1,
                    apr=apr,
                    volume_usd=volume_usd,
                    mark_price=mark_price,
                ))
            except Exception as e:
                logger.debug(f"Aster: ошибка парсинга {item.get('symbol', '?')}: {e}")

        logger.info(f"Aster: получено {len(rates)} рынков")
        return rates
