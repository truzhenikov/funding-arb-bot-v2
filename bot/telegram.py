import logging
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from config import TELEGRAM_TOKEN, TELEGRAM_CHAT_ID

logger = logging.getLogger(__name__)


def get_bot() -> Bot:
    return Bot(token=TELEGRAM_TOKEN)


async def send_pair_signal(opp: dict, size_usd: float = 0) -> None:
    """Отправляет сигнал о найденной паре с кнопками."""
    bot = get_bot()

    exch_a = opp["exchange_a"]
    exch_b = opp["exchange_b"]
    symbol = opp["symbol"]
    dir_a = opp["dir_a"]
    dir_b = opp["dir_b"]

    label_a = "шорт ↓" if dir_a == "SHORT" else "лонг ↑"
    label_b = "шорт ↓" if dir_b == "SHORT" else "лонг ↑"

    text = (
        f"🔀 *{symbol}* — {exch_a} × {exch_b}\n\n"
        f"  {exch_a} ({label_a}): `{opp['apr_a']:+.1f}%`\n"
        f"  {exch_b} ({label_b}): `{opp['apr_b']:+.1f}%`\n"
        f"  📈 Нетто: `~{opp['net_apr']:.1f}% APR`"
    )

    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton(
            "✅ Открыть пару",
            callback_data=f"open_pair:{exch_a}:{exch_b}:{symbol}:{dir_a}:{dir_b}"
        ),
        InlineKeyboardButton("❌ Пропустить", callback_data="skip"),
    ]])

    await bot.send_message(
        chat_id=TELEGRAM_CHAT_ID,
        text=text,
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=keyboard,
    )
    logger.info(f"Сигнал: {symbol} {exch_a}×{exch_b}, APR={opp['net_apr']:.1f}%")


async def send_message(text: str, reply_markup=None) -> None:
    bot = get_bot()
    await bot.send_message(
        chat_id=TELEGRAM_CHAT_ID,
        text=text,
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=reply_markup,
    )


async def send_message_get_id(text: str, reply_markup=None) -> int | None:
    bot = get_bot()
    msg = await bot.send_message(
        chat_id=TELEGRAM_CHAT_ID,
        text=text,
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=reply_markup,
    )
    return msg.message_id


async def pin_message(message_id: int) -> None:
    try:
        bot = get_bot()
        await bot.pin_chat_message(
            chat_id=TELEGRAM_CHAT_ID,
            message_id=message_id,
            disable_notification=True,
        )
    except Exception as e:
        logger.warning(f"Не удалось запинить сообщение: {e}")


async def unpin_message(message_id: int) -> None:
    try:
        bot = get_bot()
        await bot.unpin_chat_message(
            chat_id=TELEGRAM_CHAT_ID,
            message_id=message_id,
        )
    except Exception as e:
        logger.warning(f"Не удалось открепить сообщение: {e}")
