import httpx
from .base import BaseScanner, FundingRate

HYPERLIQUID_API = "https://api.hyperliquid.xyz/info"


class HyperliquidScanner(BaseScanner):
    """Hyperliquid — публичный API, без авторизации."""

    exchange_name = "Hyperliquid"

    async def get_funding_rates(self) -> list[FundingRate]:
        payload = {"type": "metaAndAssetCtxs"}

        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.post(HYPERLIQUID_API, json=payload)
            response.raise_for_status()
            data = response.json()

        assets = data[0]["universe"]
        contexts = data[1]

        results = []
        for asset, ctx in zip(assets, contexts):
            symbol = asset["name"]
            funding_str = ctx.get("funding")

            if funding_str is None:
                continue

            rate = float(funding_str)
            interval_hours = 1
            apr = rate * 24 * 365 * 100

            mark_price = float(ctx.get("markPx") or 0)
            oi_contracts = float(ctx.get("openInterest") or 0)
            open_interest_usd = oi_contracts * mark_price

            results.append(FundingRate(
                exchange="Hyperliquid",
                symbol=symbol,
                rate=rate,
                interval_hours=interval_hours,
                apr=apr,
                open_interest_usd=open_interest_usd,
                mark_price=mark_price,
            ))

        return results
