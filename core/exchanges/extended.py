import logging
from decimal import Decimal

import httpx

from .base import BaseExchangeExecutor

logger = logging.getLogger(__name__)

EXTENDED_API_BASE = "https://api.starknet.extended.exchange/api/v1"


class ExtendedExecutor(BaseExchangeExecutor):
    """
    Клиент для торговли на Extended Exchange (StarkNet perp DEX).
    Требует: pip install x10-python-trading-starknet
    """

    name = "Extended"
    fee_rate = 0.00025  # ~0.025% taker

    def __init__(self, api_key: str, public_key: str, private_key: str, vault_id: int):
        self._api_key = api_key
        self._public_key = public_key
        self._private_key = private_key
        self._vault_id = vault_id
        self._trading_client = None
        self._stark_account = None
        self._endpoint_config = None

    def _init_client(self):
        if self._trading_client is not None:
            return
        try:
            from x10.perpetual.trading_client.trading_client import StarkPerpetualAccount, PerpetualTradingClient
            from x10.config import MAINNET_CONFIG
        except ImportError:
            raise RuntimeError("x10-python-trading-starknet не установлен: pip install x10-python-trading-starknet")
        self._endpoint_config = MAINNET_CONFIG
        self._stark_account = StarkPerpetualAccount(
            api_key=self._api_key,
            public_key=self._public_key,
            private_key=self._private_key,
            vault=self._vault_id,
        )
        self._trading_client = PerpetualTradingClient(MAINNET_CONFIG, self._stark_account)

    @staticmethod
    def _market_name(symbol: str) -> str:
        s = symbol.upper()
        if "-" not in s:
            return f"{s}-USD"
        return s

    async def _get_market(self, symbol: str):
        self._init_client()
        market_name = self._market_name(symbol)
        markets = await self._trading_client.markets_info.get_markets_dict()
        market = markets.get(market_name)
        if not market:
            raise ValueError(f"Extended: рынок {market_name} не найден")
        return market

    async def get_mark_price(self, symbol: str) -> float:
        market_name = self._market_name(symbol)
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(f"{EXTENDED_API_BASE}/info/markets")
            items = resp.json()
        items = items if isinstance(items, list) else items.get("data", [])
        for item in items:
            sym = item.get("market") or item.get("symbol") or item.get("name") or ""
            if sym.upper() == market_name.upper():
                stats = item.get("marketStats") or {}
                price = (
                    item.get("mark_price")
                    or item.get("markPrice")
                    or stats.get("markPrice")
                    or stats.get("mark_price")
                )
                if price:
                    return float(price)
        raise ValueError(f"Extended: mark price для {symbol} не найдена")

    async def market_open(self, symbol: str, is_long: bool, size_usd: float) -> dict:
        from x10.perpetual.order_object import create_order_object
        from x10.perpetual.orders import OrderSide, TimeInForce

        self._init_client()
        market = await self._get_market(symbol)
        mark_price = await self.get_mark_price(symbol)

        mark = Decimal(str(mark_price))
        slippage = Decimal("0.02")
        price = mark * (1 + slippage) if is_long else mark * (1 - slippage)
        price = market.trading_config.round_price(price)

        qty = Decimal(str(size_usd)) / mark
        qty = max(qty, market.trading_config.min_order_size)
        step = market.trading_config.min_order_size_change
        if step and step > 0:
            qty = Decimal(int(qty / step)) * step

        side = OrderSide.BUY if is_long else OrderSide.SELL

        order = create_order_object(
            account=self._stark_account,
            starknet_domain=self._endpoint_config.starknet_domain,
            market=market,
            side=side,
            amount_of_synthetic=qty,
            price=price,
            time_in_force=TimeInForce.IOC,
            reduce_only=False,
            post_only=False,
        )
        result = await self._trading_client.orders.place_order(order=order)
        order_id = result.data.id if hasattr(result, "data") else result.id

        logger.info(f"Extended: открыт {'лонг' if is_long else 'шорт'} {symbol}, qty={qty}, mark={mark_price}")
        return {
            "order_id": order_id,
            "size": float(qty),
            "size_usd": size_usd,
            "price": mark_price,
            "fee": round(size_usd * self.fee_rate, 6),
        }

    async def market_close(self, symbol: str, size: float = 0, was_long: bool = True) -> dict:
        from x10.perpetual.order_object import create_order_object
        from x10.perpetual.orders import OrderSide, TimeInForce

        self._init_client()
        market = await self._get_market(symbol)
        mark_price = await self.get_mark_price(symbol)

        positions = await self.get_positions()
        pos = next((p for p in positions if p["symbol"] == symbol.upper()), None) if positions else None
        if not pos:
            logger.info(f"Extended: позиция {symbol} не найдена — считаем уже закрытой")
            return {"symbol": symbol, "price": mark_price, "fee": 0}

        real_size = abs(pos["quantity"])
        if real_size == 0:
            return {"symbol": symbol, "price": mark_price, "fee": 0}

        qty = size if size > 0 else real_size
        qty = min(qty, real_size)

        mark = Decimal(str(mark_price))
        slippage = Decimal("0.02")
        close_side = OrderSide.SELL if was_long else OrderSide.BUY
        price = mark * (1 - slippage) if was_long else mark * (1 + slippage)
        price = market.trading_config.round_price(price)

        qty_dec = Decimal(str(qty))
        step = market.trading_config.min_order_size_change
        if step and step > 0:
            qty_dec = Decimal(int(qty_dec / step)) * step

        order = create_order_object(
            account=self._stark_account,
            starknet_domain=self._endpoint_config.starknet_domain,
            market=market,
            side=close_side,
            amount_of_synthetic=qty_dec,
            price=price,
            time_in_force=TimeInForce.IOC,
            reduce_only=True,
            post_only=False,
        )
        await self._trading_client.orders.place_order(order=order)
        logger.info(f"Extended: позиция {symbol} закрыта, qty={qty_dec}")
        return {
            "symbol": symbol,
            "price": mark_price,
            "fee": round(float(qty_dec) * mark_price * self.fee_rate, 6),
        }

    async def get_positions(self) -> list[dict] | None:
        try:
            self._init_client()
            resp = await self._trading_client.account.get_positions()
            result = []
            for pos in resp.data:
                symbol = pos.market.split("-")[0].upper()
                qty = float(pos.size)
                side_str = str(pos.side).upper()
                if "SHORT" in side_str:
                    qty = -abs(qty)
                result.append({
                    "symbol": symbol,
                    "quantity": qty,
                    "mark_price": float(pos.mark_price),
                    "liquidation_price": float(pos.liquidation_price) if pos.liquidation_price else 0,
                })
            return result
        except Exception as e:
            logger.warning(f"Extended get_positions ошибка: {e}")
            return None

    async def get_balance(self) -> float | None:
        try:
            self._init_client()
            account = await self._trading_client.account.get_balance()
            if hasattr(account, "data"):
                data = account.data
                for key in ("equity", "balance", "available_for_trade", "available_for_withdrawal"):
                    val = getattr(data, key, None)
                    if val is not None:
                        return float(val)
            return None
        except Exception as e:
            logger.warning(f"Extended get_balance ошибка: {e}")
            return None

    async def get_liquidation_info(self, symbol: str) -> dict | None:
        positions = await self.get_positions()
        if not positions:
            return None
        pos = next((p for p in positions if p["symbol"] == symbol.upper()), None)
        if not pos:
            return None
        liq = float(pos.get("liquidation_price") or 0)
        mark = float(pos.get("mark_price") or 0)
        if liq > 0 and mark > 0:
            return {
                "liquidation_price": liq,
                "mark_price": mark,
                "leverage": "?",
            }
        return None
