from dataclasses import dataclass


@dataclass
class FundingRate:
    exchange: str           # название биржи
    symbol: str             # монета, например "BTC"
    rate: float             # ставка за период (в долях, не в %)
    interval_hours: int     # как часто выплачивается (обычно 1 или 8 часов)
    apr: float              # годовая доходность в %
    open_interest_usd: float = 0.0  # открытый интерес в долларах
    volume_usd: float = 0.0         # дневной объём торгов в долларах
    mark_price: float = 0.0         # текущая рыночная цена


class BaseScanner:
    """Базовый класс. Каждый сканер биржи наследуется от него."""

    exchange_name: str = ""

    async def get_funding_rates(self) -> list[FundingRate]:
        raise NotImplementedError
