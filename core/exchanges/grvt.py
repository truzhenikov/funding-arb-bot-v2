import logging
from .base import BaseExchangeExecutor

logger = logging.getLogger(__name__)


class GRVTExecutor(BaseExchangeExecutor):
    """GRVT (Gravity) — ZK-powered derivatives exchange. Заглушка."""

    name = "GRVT"
    fee_rate = 0.0003  # примерно

    def __init__(self, api_key: str, private_key: str):
        self._api_key = api_key
        self._private_key = private_key

    async def market_open(self, symbol: str, is_long: bool, size_usd: float) -> dict:
        raise NotImplementedError("GRVT executor ещё не реализован")

    async def market_close(self, symbol: str, size: float = 0, was_long: bool = True) -> dict:
        raise NotImplementedError("GRVT executor ещё не реализован")

    async def get_positions(self) -> list[dict] | None:
        logger.warning("GRVT get_positions ещё не реализован")
        return None
