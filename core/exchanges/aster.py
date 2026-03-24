import logging
from .base import BaseExchangeExecutor

logger = logging.getLogger(__name__)


class AsterExecutor(BaseExchangeExecutor):
    """Aster — perpetual futures DEX. Заглушка."""

    name = "Aster"
    fee_rate = 0.0003  # примерно

    def __init__(self, api_key: str, api_secret: str):
        self._api_key = api_key
        self._api_secret = api_secret

    async def market_open(self, symbol: str, is_long: bool, size_usd: float) -> dict:
        raise NotImplementedError("Aster executor ещё не реализован")

    async def market_close(self, symbol: str, size: float = 0, was_long: bool = True) -> dict:
        raise NotImplementedError("Aster executor ещё не реализован")

    async def get_positions(self) -> list[dict] | None:
        logger.warning("Aster get_positions ещё не реализован")
        return None
