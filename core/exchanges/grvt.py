"""
GRVT (Gravity) executor — использует grvt-pysdk (async).
Аутентификация: API key + EIP-712 подпись ордеров через ETH private key.
SDK: pip install grvt-pysdk
"""
import logging
from decimal import Decimal

from .base import BaseExchangeExecutor

logger = logging.getLogger(__name__)


class GRVTExecutor(BaseExchangeExecutor):
    """Клиент для торговли на GRVT через grvt-pysdk."""

    name = "GRVT"
    fee_rate = 0.0003  # ~0.03% taker (maker получает ребейт -0.01%)

    def __init__(self, api_key: str, private_key: str, trading_account_id: str = ""):
        self._api_key = api_key
        self._private_key = private_key
        self._trading_account_id = trading_account_id
        self._api = None
        self._markets_loaded = False

    async def _get_api(self):
        """Ленивая инициализация SDK клиента."""
        if self._api is None:
            try:
                from pysdk.grvt_ccxt_pro import GrvtCcxtPro
                from pysdk.grvt_ccxt_env import GrvtEnv

                params = {
                    "api_key": self._api_key,
                    "private_key": self._private_key,
                    "trading_account_id": self._trading_account_id,
                }
                self._api = GrvtCcxtPro(GrvtEnv.PROD, logger, parameters=params)
            except ImportError:
                raise RuntimeError("grvt-pysdk не установлен: pip install grvt-pysdk")
        if not self._markets_loaded:
            await self._api.load_markets()
            self._markets_loaded = True
        return self._api

    def _to_instrument(self, symbol: str) -> str:
        """BTC → BTC_USDT_Perp"""
        return f"{symbol.upper()}_USDT_Perp"

    async def get_mark_price(self, symbol: str) -> float:
        api = await self._get_api()
        instrument = self._to_instrument(symbol)
        ticker = await api.fetch_mini_ticker(instrument)
        price = float(ticker.get("mark_price") or ticker.get("last") or 0)
        if price == 0:
            raise ValueError(f"Не удалось получить цену {symbol} на GRVT")
        return price

    async def market_open(self, symbol: str, is_long: bool, size_usd: float) -> dict:
        api = await self._get_api()
        instrument = self._to_instrument(symbol)

        price = await self.get_mark_price(symbol)
        size = size_usd / price

        side = "buy" if is_long else "sell"

        logger.info(f"GRVT: {'лонг' if is_long else 'шорт'} {symbol}, ${size_usd}, size={size:.6f}")

        order = await api.create_order(
            symbol=instrument,
            order_type="market",
            side=side,
            amount=Decimal(str(round(size, 8))),
        )

        # Парсим результат
        filled_size = float(order.get("filled") or order.get("amount") or size)
        filled_price = float(order.get("average") or order.get("price") or price)

        logger.info(f"GRVT: ордер исполнен {symbol}, size={filled_size}, price={filled_price}")
        return {
            "order_id": order.get("id"),
            "size": filled_size,
            "size_usd": size_usd,
            "price": filled_price,
        }

    async def market_close(self, symbol: str, size: float = 0, was_long: bool = True) -> dict:
        api = await self._get_api()
        instrument = self._to_instrument(symbol)

        price = await self.get_mark_price(symbol)

        # Закрываем: если был лонг → sell, если шорт → buy
        side = "sell" if was_long else "buy"
        close_size = size if size > 0 else abs((await self._get_position_size(symbol)) or 0)

        if close_size == 0:
            logger.info(f"GRVT: позиция {symbol} уже закрыта")
            return {"symbol": symbol, "price": price, "fee": 0}

        logger.info(f"GRVT: закрытие {symbol}, size={close_size}")

        order = await api.create_order(
            symbol=instrument,
            order_type="market",
            side=side,
            amount=Decimal(str(round(close_size, 8))),
            params={"reduce_only": True},
        )

        exit_price = float(order.get("average") or order.get("price") or price)
        logger.info(f"GRVT: позиция {symbol} закрыта, price={exit_price}")
        return {"symbol": symbol, "price": exit_price, "fee": 0}

    async def _get_position_size(self, symbol: str) -> float | None:
        """Возвращает размер открытой позиции (+ лонг, - шорт)."""
        positions = await self.get_positions()
        if positions is None:
            return None
        for pos in positions:
            if pos["symbol"] == symbol.upper():
                return pos["quantity"]
        return 0

    async def get_positions(self) -> list[dict] | None:
        try:
            api = await self._get_api()
            raw = await api.fetch_positions()

            positions = []
            for pos in raw:
                symbol_raw = pos.get("symbol") or pos.get("instrument") or ""
                # BTC_USDT_Perp → BTC
                symbol = symbol_raw.split("_")[0].upper() if "_" in symbol_raw else symbol_raw
                qty = float(pos.get("contracts") or pos.get("amount") or 0)
                side = pos.get("side", "")
                if side == "short":
                    qty = -abs(qty)
                elif side == "long":
                    qty = abs(qty)
                if qty != 0:
                    positions.append({"symbol": symbol, "quantity": qty})
            return positions
        except Exception as e:
            logger.warning(f"GRVT get_positions ошибка: {e}")
            return None

    async def get_balance(self) -> float | None:
        try:
            api = await self._get_api()
            balance = await api.fetch_balance()
            # Ищем USDT баланс
            if isinstance(balance, dict):
                usdt = balance.get("USDT", {})
                if isinstance(usdt, dict):
                    return float(usdt.get("free") or usdt.get("available") or 0)
                total = balance.get("total", {})
                if isinstance(total, dict):
                    return float(total.get("USDT") or 0)
            return None
        except Exception as e:
            logger.warning(f"GRVT get_balance ошибка: {e}")
            return None

    async def close(self):
        if self._api:
            try:
                await self._api.close()
            except Exception:
                pass
            self._api = None
            self._markets_loaded = False
