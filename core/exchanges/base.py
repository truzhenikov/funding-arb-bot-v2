from abc import ABC, abstractmethod


class BaseExchangeExecutor(ABC):
    """
    Единый интерфейс для всех бирж.
    Каждая биржа реализует эти методы — универсальный executor
    может работать с любой комбинацией бирж через этот интерфейс.
    """

    name: str = ""          # "Backpack", "Lighter", "Hyperliquid", ...
    fee_rate: float = 0.0   # Примерная комиссия за сделку (0.0004 = 0.04%)

    @abstractmethod
    async def market_open(self, symbol: str, is_long: bool, size_usd: float) -> dict:
        """
        Открывает рыночный ордер.
        Возвращает: {"size": float, "price": float, "size_usd": float, ...}
        """

    @abstractmethod
    async def market_close(self, symbol: str, size: float, was_long: bool) -> dict:
        """
        Закрывает позицию.
        size: размер в базовом токене.
        was_long: True если позиция была лонг.
        Возвращает: {"price": float, "fee": float, ...}
        """

    @abstractmethod
    async def get_positions(self) -> list[dict] | None:
        """
        Возвращает открытые позиции.
        Каждый элемент: {"symbol": str, "quantity": float} (+ = лонг, - = шорт).
        Возвращает None при ошибке (не удалось получить данные).
        """

    async def get_balance(self) -> float | None:
        """Возвращает свободный баланс в USD. None если не поддерживается."""
        return None

    async def get_mark_price(self, symbol: str) -> float:
        """Получает текущую mark price для символа."""
        raise NotImplementedError(f"{self.name} не реализовал get_mark_price")

    async def get_liquidation_info(self, symbol: str) -> dict | None:
        """
        Возвращает информацию о ликвидации для открытой позиции.
        {"liquidation_price": float, "mark_price": float, "leverage": str}
        Возвращает None если биржа не поддерживает или нет позиции.
        """
        return None

    async def close(self):
        """Закрывает соединения (если нужно). По умолчанию ничего не делает."""
        pass
