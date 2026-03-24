import httpx
import logging
from .base import BaseScanner, FundingRate

logger = logging.getLogger(__name__)


class AsterScanner(BaseScanner):
    """
    Aster (ранее Astra) — perpetual futures DEX.
    Публичный API для funding rates.
    """

    exchange_name = "Aster"

    async def get_funding_rates(self) -> list[FundingRate]:
        # TODO: реализовать после изучения API
        # Примерный план:
        # 1. Получить список рынков
        # 2. Получить текущие funding rates
        # 3. Преобразовать в FundingRate
        logger.warning("Aster сканер ещё не реализован")
        return []
