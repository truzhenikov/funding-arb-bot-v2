import httpx
import logging
from .base import BaseScanner, FundingRate

logger = logging.getLogger(__name__)


class LighterScanner(BaseScanner):
    """Lighter (ZK order book DEX) — публичный API, без авторизации."""

    exchange_name = "Lighter"
    URL = "https://mainnet.zklighter.elliot.ai/api/v1/funding-rates"

    async def _get_volumes(self) -> dict:
        """Возвращает {symbol: daily_volume_usd} из exchange_stats."""
        try:
            import lighter
            api_client = lighter.ApiClient(lighter.Configuration(host="https://mainnet.zklighter.elliot.ai"))
            order_api = lighter.OrderApi(api_client)
            stats = await order_api.exchange_stats()
            result = {ob.symbol.upper(): float(ob.daily_quote_token_volume) for ob in stats.order_book_stats}
            await api_client.close()
            return result
        except Exception as e:
            logger.debug(f"Lighter: не удалось получить объёмы: {e}")
            return {}

    async def get_funding_rates(self) -> list[FundingRate]:
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(self.URL)
                data = resp.json()
        except Exception as e:
            logger.error(f"Lighter: ошибка запроса: {e}")
            return []

        all_items = data.get("funding_rates", [])
        items = [i for i in all_items if i.get("exchange", "").lower() == "lighter"]

        volumes = await self._get_volumes()

        rates = []
        for item in items:
            symbol_raw = (
                item.get("symbol") or
                item.get("market") or
                item.get("ticker") or ""
            ).upper().replace("-USD", "").replace("-USDT", "").replace("-USDC", "")

            if not symbol_raw:
                continue

            try:
                rate_8h = float(
                    item.get("rate") or
                    item.get("funding_rate") or
                    item.get("fundingRate") or 0
                )
                hourly_rate = rate_8h / 8
                apr = hourly_rate * 24 * 365 * 100

                rates.append(FundingRate(
                    exchange="Lighter",
                    symbol=symbol_raw,
                    rate=hourly_rate,
                    interval_hours=1,
                    apr=apr,
                    volume_usd=volumes.get(symbol_raw, 0),
                ))
            except Exception as e:
                logger.debug(f"Lighter: ошибка парсинга {symbol_raw}: {e}")

        logger.info(f"Lighter: получено {len(rates)} рынков")
        return rates
