import httpx
import logging
from .base import BaseScanner, FundingRate

logger = logging.getLogger(__name__)


class GRVTScanner(BaseScanner):
    """
    GRVT (Gravity) — ZK-powered derivatives exchange.
    Публичный API для funding rates.
    Документация: https://docs.grvt.io/
    """

    exchange_name = "GRVT"
    BASE_URL = "https://edge.grvt.io/market/v1"

    async def get_funding_rates(self) -> list[FundingRate]:
        # TODO: реализовать после изучения API
        # Примерный план:
        # 1. GET /instruments — получить список перп рынков
        # 2. GET /funding-rates — получить текущие ставки
        # 3. Преобразовать в FundingRate
        logger.warning("GRVT сканер ещё не реализован")
        return []
