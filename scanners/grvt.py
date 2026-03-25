import httpx
import logging
from .base import BaseScanner, FundingRate

logger = logging.getLogger(__name__)

MARKET_DATA_URL = "https://market-data.grvt.io"


class GRVTScanner(BaseScanner):
    """
    GRVT (Gravity) — ZK-powered derivatives exchange.
    Публичный API, без авторизации для чтения.
    Все запросы — POST с JSON body.
    """

    exchange_name = "GRVT"

    async def _get_perp_instruments(self) -> list[str]:
        """Возвращает список всех перп инструментов (BTC_USDT_Perp и т.д.)."""
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.post(
                    f"{MARKET_DATA_URL}/full/v1/all_instruments",
                    json={},
                )
                resp.raise_for_status()
                data = resp.json()

            instruments = []
            for item in data.get("result", data.get("instruments", [])):
                # instrument_name: "BTC_USDT_Perp"
                name = item.get("instrument") or item.get("instrument_name") or ""
                kind = item.get("instrument_type") or item.get("kind") or ""
                if "PERP" in kind.upper() or name.endswith("_Perp"):
                    instruments.append(name)
            return instruments
        except Exception as e:
            logger.error(f"GRVT: ошибка получения инструментов: {e}")
            return []

    async def _get_ticker(self, instrument: str) -> dict | None:
        """Получает тикер для одного инструмента (включает funding_rate_curr)."""
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(
                    f"{MARKET_DATA_URL}/full/v1/ticker",
                    json={"instrument": instrument},
                )
                if resp.status_code != 200:
                    return None
                data = resp.json()
                return data.get("result", data)
        except Exception:
            return None

    async def get_funding_rates(self) -> list[FundingRate]:
        instruments = await self._get_perp_instruments()
        if not instruments:
            logger.warning("GRVT: нет перп инструментов")
            return []

        # Запрашиваем тикеры параллельно (пачками по 20 чтобы не перегружать)
        import asyncio
        rates = []
        batch_size = 20

        for i in range(0, len(instruments), batch_size):
            batch = instruments[i:i + batch_size]
            results = await asyncio.gather(
                *[self._get_ticker(inst) for inst in batch],
                return_exceptions=True,
            )

            for instrument, result in zip(batch, results):
                if isinstance(result, Exception) or result is None:
                    continue

                try:
                    # instrument: "BTC_USDT_Perp" → symbol: "BTC"
                    symbol = instrument.split("_")[0].upper()

                    # funding_rate_curr — текущая ставка (в долях)
                    funding_rate = float(result.get("funding_rate_curr") or result.get("funding_rate") or 0)
                    if funding_rate == 0:
                        continue

                    mark_price = float(result.get("mark_price") or 0)
                    open_interest = float(result.get("open_interest") or 0)
                    # OI в контрактах → USD
                    oi_usd = open_interest * mark_price if mark_price else 0

                    # Объём за 24ч
                    volume_usd = float(result.get("quote_volume") or result.get("volume_24h_quote") or 0)

                    # GRVT фандинг может быть 1h/4h/8h, по умолчанию 8h
                    # funding_rate_curr — ставка за текущий интервал
                    # Для APR: предполагаем часовую ставку
                    # Если ставка за 8ч — делим на 8
                    funding_interval = int(result.get("funding_interval_hours") or 8)
                    hourly_rate = funding_rate / funding_interval
                    apr = hourly_rate * 24 * 365 * 100

                    rates.append(FundingRate(
                        exchange="GRVT",
                        symbol=symbol,
                        rate=hourly_rate,
                        interval_hours=1,
                        apr=apr,
                        open_interest_usd=oi_usd,
                        volume_usd=volume_usd,
                        mark_price=mark_price,
                    ))
                except Exception as e:
                    logger.debug(f"GRVT: ошибка парсинга {instrument}: {e}")

        logger.info(f"GRVT: получено {len(rates)} рынков")
        return rates
