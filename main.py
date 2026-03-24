"""
Funding Arbitrage Bot — дельта-нейтральный арбитраж фандинга.
Универсальный: работает с любыми комбинациями бирж.
"""
import asyncio
import json
import logging
import time

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, MessageHandler, ContextTypes, filters
from telegram.constants import ParseMode

from config import (
    SCAN_INTERVAL_SECONDS, TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, POSITION_SIZE_USD,
    AUTHOR_CHANNEL, AUTHOR_CHANNEL_NAME, DONATION_WALLET_EVM, DONATION_WALLET_SOL,
    EXCHANGES, LIQ_WARN_PCT, LIQ_AUTO_CLOSE_PCT, PRICE_WARN_PCT, PRICE_AUTO_CLOSE_PCT,
    NEG_APR_HARD_CLOSE, NEG_APR_WAIT_HOURS,
)
from scanners.hyperliquid import HyperliquidScanner
from scanners.backpack import BackpackScanner
from scanners.lighter import LighterScanner
from scanners.grvt import GRVTScanner
from scanners.aster import AsterScanner
from core.analyzer import find_pair_opportunities, calc_net_apr_for_pair
from core.executor import open_pair, close_pair, scale_in_pair, get_executor
from bot.telegram import send_pair_signal, send_message, send_message_get_id
from db.database import (
    init_db, save_funding_snapshot,
    get_open_pairs, get_positions_by_pair, get_closed_pairs, count_closed_pairs,
    save_setting, load_setting,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# ─── Антиспам ───────────────────────────────────────────────────────────────────
_sent_signals: dict[str, tuple[float, float]] = {}
SIGNAL_COOLDOWN_HOURS = 4
APR_GROWTH_THRESHOLD = 0.5

_verify_alerts_sent: dict[str, float] = {}
VERIFY_ALERT_COOLDOWN_SECONDS = 300

_liq_alerts_sent: dict[str, float] = {}
LIQ_ALERT_COOLDOWN_SECONDS = 1800

_negative_funding_since: dict[str, float] = {}

# ─── Настройки (загружаются из БД при старте) ─────────────────────────────────
# Размеры позиций: "global" (один размер для всех) или "per_exchange" (раздельный)
_position_size_mode: str = "global"  # "global" | "per_exchange"
_global_position_size: float = POSITION_SIZE_USD
_exchange_sizes: dict[str, float] = {}  # {"Backpack": 100, "Lighter": 200, ...}

# Какие биржи включены для сигналов
_enabled_exchanges: set[str] = set(EXCHANGES.values())  # все по умолчанию

# Защита от двойного нажатия
_opening_pairs: set = set()

# Ожидаем ввод
_waiting_for_size: str | None = None  # None или "global" / exchange_name
_waiting_for_scale_in: tuple | None = None

# ─── Кнопки ──────────────────────────────────────────────────────────────────
BTN_POSITIONS = "📊 Мои позиции"
BTN_SCAN = "🔍 Сканировать сейчас"
BTN_HISTORY = "📋 История"
BTN_SETTINGS = "⚙️ Настройки"
BTN_SUPPORT = "💙 Поддержать автора"

# ─── Сканеры ─────────────────────────────────────────────────────────────────
ALL_SCANNERS = [
    BackpackScanner(),
    LighterScanner(),
    HyperliquidScanner(),
    GRVTScanner(),
    AsterScanner(),
]


def persistent_keyboard():
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton(BTN_POSITIONS), KeyboardButton(BTN_SCAN)],
            [KeyboardButton(BTN_HISTORY), KeyboardButton(BTN_SETTINGS)],
            [KeyboardButton(BTN_SUPPORT)],
        ],
        resize_keyboard=True,
    )


def get_position_size(exchange_name: str) -> float:
    """Возвращает размер позиции для биржи с учётом режима."""
    if _position_size_mode == "per_exchange":
        return _exchange_sizes.get(exchange_name, _global_position_size)
    return _global_position_size


# ─── Загрузка/сохранение настроек ────────────────────────────────────────────

async def _load_settings():
    """Загружает настройки из БД при старте."""
    global _position_size_mode, _global_position_size, _exchange_sizes, _enabled_exchanges

    mode = await load_setting("position_size_mode", "global")
    _position_size_mode = mode

    size = await load_setting("global_position_size", str(POSITION_SIZE_USD))
    _global_position_size = float(size)

    sizes_json = await load_setting("exchange_sizes", "{}")
    try:
        _exchange_sizes = json.loads(sizes_json)
    except json.JSONDecodeError:
        _exchange_sizes = {}

    enabled = await load_setting("enabled_exchanges", "")
    if enabled:
        _enabled_exchanges = set(enabled.split(","))
    else:
        _enabled_exchanges = set(EXCHANGES.values())


async def _save_settings():
    """Сохраняет текущие настройки в БД."""
    await save_setting("position_size_mode", _position_size_mode)
    await save_setting("global_position_size", str(_global_position_size))
    await save_setting("exchange_sizes", json.dumps(_exchange_sizes))
    await save_setting("enabled_exchanges", ",".join(_enabled_exchanges))


# ─── Сканирование ────────────────────────────────────────────────────────────

async def fetch_all_rates() -> dict:
    """Запускает все сканеры параллельно."""
    results = await asyncio.gather(
        *[s.get_funding_rates() for s in ALL_SCANNERS],
        return_exceptions=True,
    )
    exchange_rates = {}
    for scanner, result in zip(ALL_SCANNERS, results):
        name = scanner.__class__.__name__.replace("Scanner", "")
        if isinstance(result, Exception):
            logger.error(f"Сканер {name}: {result}")
        elif result:
            exchange_rates[result[0].exchange] = result
    return exchange_rates


def should_send_signal(key: str, current_apr: float) -> bool:
    if key not in _sent_signals:
        return True
    prev_apr, prev_time = _sent_signals[key]
    hours_passed = (time.time() - prev_time) / 3600
    if hours_passed >= SIGNAL_COOLDOWN_HOURS:
        return True
    if current_apr > 0 and prev_apr > 0 and current_apr > prev_apr * (1 + APR_GROWTH_THRESHOLD):
        return True
    return False


# ─── Верификация позиций ─────────────────────────────────────────────────────

async def _verify_positions(exchange_rates: dict):
    """Сверяет позиции в БД с реальными на биржах."""
    pairs = await get_open_pairs()
    if not pairs:
        return

    # Собираем реальные позиции с каждой биржи
    real_positions: dict[str, dict] = {}  # {exchange: {symbol: quantity}}

    for pair in pairs:
        for leg in pair["legs"]:
            exch = leg["exchange"]
            if exch in real_positions:
                continue
            try:
                executor = get_executor(exch)
                positions = await executor.get_positions()
                await executor.close()
                if positions is not None:
                    real_positions[exch] = {p["symbol"]: p["quantity"] for p in positions}
            except Exception as e:
                logger.warning(f"Верификация: {exch} недоступен: {e}")

    alerts = []
    for pair in pairs:
        for leg in pair["legs"]:
            exch = leg["exchange"]
            symbol = leg["symbol"]
            if exch not in real_positions:
                continue

            real_qty = real_positions[exch].get(symbol, 0)
            expected_long = (leg["direction"] == "LONG")

            if real_qty == 0:
                alerts.append(f"*{symbol}* {exch}: позиция исчезла (в БД: {leg['direction']})")
            elif (real_qty > 0) != expected_long:
                real_dir = "LONG" if real_qty > 0 else "SHORT"
                alerts.append(f"*{symbol}* {exch}: направление не совпадает (БД: {leg['direction']}, биржа: {real_dir})")

    if not alerts:
        return

    alert_key = "|".join(sorted(alerts))
    last_sent = _verify_alerts_sent.get(alert_key, 0)
    if time.time() - last_sent < VERIFY_ALERT_COOLDOWN_SECONDS:
        return

    _verify_alerts_sent[alert_key] = time.time()
    await send_message(
        "🚨 *РАСХОЖДЕНИЕ ПОЗИЦИЙ!*\n\n" +
        "\n".join(f"⚠️ {a}" for a in alerts) +
        "\n\n_Проверь позиции на биржах и в боте (📊 Мои позиции)._"
    )


# ─── Мониторинг открытых пар ─────────────────────────────────────────────────

async def _monitor_open_pairs(exchange_rates: dict):
    """Универсальный мониторинг всех открытых пар: фандинг + ликвидация."""
    pairs = await get_open_pairs()
    if not pairs:
        return

    # Строим карту ставок
    rates_map = {}
    for exch_name, rates in exchange_rates.items():
        for r in rates:
            rates_map[f"{r.exchange}:{r.symbol}"] = r

    for pair in pairs:
        legs = pair["legs"]
        if len(legs) < 2:
            continue

        pair_id = pair["pair_id"]
        symbol = legs[0]["symbol"]

        # Считаем нетто APR
        net_apr = calc_net_apr_for_pair(legs, rates_map)

        # ── Проверка APR ─────────────────────────────────────────────────────
        if net_apr < NEG_APR_HARD_CLOSE:
            logger.warning(f"Автозакрытие {symbol}: нетто APR={net_apr:.1f}%")
            _negative_funding_since.pop(pair_id, None)
            apr_details = " | ".join(
                f"{l['exchange']} `{rates_map.get(f'{l[\"exchange\"]}:{symbol}', type('', (), {'apr': 0})).apr:+.1f}%`"
                for l in legs
            )
            await _auto_close_pair(
                pair_id, symbol, legs,
                reason=f"нетто APR упал до `{net_apr:.1f}%` (порог {NEG_APR_HARD_CLOSE}%)\n{apr_details}",
            )
            continue

        elif net_apr < 0:
            if pair_id not in _negative_funding_since:
                _negative_funding_since[pair_id] = time.time()
                alert_key = f"alert:{pair_id}:negative"
                if should_send_signal(alert_key, net_apr):
                    _sent_signals[alert_key] = (net_apr, time.time())
                    keyboard = InlineKeyboardMarkup([[
                        InlineKeyboardButton("❌ Закрыть пару", callback_data=f"close_pair:{pair_id}:{symbol}"),
                    ]])
                    apr_details = " | ".join(
                        f"{l['exchange']} `{rates_map.get(f'{l[\"exchange\"]}:{symbol}', type('', (), {'apr': 0})).apr:+.1f}%`"
                        for l in legs
                    )
                    await send_message(
                        f"⚠️ *Фандинг ушёл в минус — {symbol}*\n\n"
                        f"{apr_details}\n"
                        f"Нетто: `{net_apr:+.1f}%` APR\n\n"
                        f"Жду `{int(NEG_APR_WAIT_HOURS)}ч` — если не восстановится, закрою автоматически.",
                        reply_markup=keyboard,
                    )
            else:
                hours_waited = (time.time() - _negative_funding_since[pair_id]) / 3600
                if hours_waited >= NEG_APR_WAIT_HOURS:
                    logger.warning(f"Автозакрытие {symbol}: фандинг в минусе {hours_waited:.1f}ч")
                    _negative_funding_since.pop(pair_id, None)
                    await _auto_close_pair(
                        pair_id, symbol, legs,
                        reason=f"нетто APR `{net_apr:+.1f}%` не восстановился за `{hours_waited:.0f}ч`",
                    )
                    continue
        else:
            _negative_funding_since.pop(pair_id, None)

        # ── Проверка ликвидации и отклонения цены ────────────────────────────
        for leg in legs:
            exch_name = leg["exchange"]
            try:
                executor = get_executor(exch_name)

                # Ликвидационная цена (если биржа поддерживает)
                liq_info = await executor.get_liquidation_info(symbol)
                if liq_info:
                    liq_price = liq_info["liquidation_price"]
                    mark_price = liq_info["mark_price"]
                    leverage = liq_info.get("leverage", "?")
                    distance_pct = abs(mark_price - liq_price) / mark_price * 100

                    if distance_pct < LIQ_AUTO_CLOSE_PCT:
                        await _auto_close_pair(
                            pair_id, symbol, legs,
                            reason=f"до ликвидации {exch_name} осталось `{distance_pct:.1f}%` (порог {LIQ_AUTO_CLOSE_PCT}%)\n"
                                   f"Цена: `${mark_price:.4f}` → Ликвидация: `${liq_price:.4f}` (плечо {leverage}x)",
                        )
                        break

                    elif distance_pct < LIQ_WARN_PCT:
                        alert_key = f"liq:{pair_id}:{exch_name}:warn"
                        if time.time() - _liq_alerts_sent.get(alert_key, 0) >= LIQ_ALERT_COOLDOWN_SECONDS:
                            _liq_alerts_sent[alert_key] = time.time()
                            keyboard = InlineKeyboardMarkup([[
                                InlineKeyboardButton("❌ Закрыть пару", callback_data=f"close_pair:{pair_id}:{symbol}"),
                            ]])
                            await send_message(
                                f"⚠️ *РИСК ЛИКВИДАЦИИ — {symbol}*\n\n"
                                f"{exch_name} ({leg['direction']}): до ликвидации `{distance_pct:.1f}%`\n"
                                f"  Цена: `${mark_price:.4f}` → Ликвидация: `${liq_price:.4f}` (плечо {leverage}x)\n\n"
                                f"⚠️ Закрою автоматически при `{LIQ_AUTO_CLOSE_PCT}%`",
                                reply_markup=keyboard,
                            )

                # Отклонение цены от входа (для бирж без ликвидационной цены)
                elif leg.get("entry_price"):
                    entry = leg["entry_price"]
                    rate = rates_map.get(f"{exch_name}:{symbol}")
                    cur_price = rate.mark_price if rate and rate.mark_price else 0
                    if cur_price and entry:
                        if leg["direction"] == "LONG":
                            loss_pct = (entry - cur_price) / entry * 100
                        else:
                            loss_pct = (cur_price - entry) / entry * 100

                        direction_str = "упала" if leg["direction"] == "LONG" else "выросла"

                        if loss_pct >= PRICE_AUTO_CLOSE_PCT:
                            await _auto_close_pair(
                                pair_id, symbol, legs,
                                reason=f"{exch_name} ({leg['direction']}): цена {direction_str} на `{loss_pct:.1f}%` от входа\n"
                                       f"Вход: `${entry:.4f}` → Сейчас: `${cur_price:.4f}`",
                            )
                            break

                        elif loss_pct > PRICE_WARN_PCT:
                            alert_key = f"liq:{pair_id}:{exch_name}:price"
                            if time.time() - _liq_alerts_sent.get(alert_key, 0) >= LIQ_ALERT_COOLDOWN_SECONDS:
                                _liq_alerts_sent[alert_key] = time.time()
                                keyboard = InlineKeyboardMarkup([[
                                    InlineKeyboardButton("❌ Закрыть пару", callback_data=f"close_pair:{pair_id}:{symbol}"),
                                ]])
                                await send_message(
                                    f"⚠️ *РИСК — {symbol}*\n\n"
                                    f"{exch_name} ({leg['direction']}): цена {direction_str} на `{loss_pct:.1f}%`\n"
                                    f"  Вход: `${entry:.4f}` → Сейчас: `${cur_price:.4f}`\n\n"
                                    f"⚠️ Закрою при `{PRICE_AUTO_CLOSE_PCT}%`",
                                    reply_markup=keyboard,
                                )

                await executor.close()
            except Exception as e:
                logger.debug(f"Мониторинг {exch_name} {symbol}: {e}")


async def _auto_close_pair(pair_id: str, symbol: str, legs: list, reason: str):
    """Автоматически закрывает пару и уведомляет."""
    try:
        if not legs:
            legs = await get_positions_by_pair(pair_id)
        await close_pair(pair_id=pair_id, symbol=symbol, legs=legs)
        exch_names = " × ".join(l["exchange"] for l in legs)
        await send_message(
            f"🤖 *АВТОЗАКРЫТИЕ — {symbol}* ({exch_names})\n\n"
            f"Причина: {reason}\n\n"
            f"✅ Пара закрыта автоматически."
        )
    except Exception as e:
        logger.error(f"Автозакрытие {pair_id} ({symbol}) провалилось: {e}")
        await send_message(
            f"🚨 *АВТОЗАКРЫТИЕ ПРОВАЛИЛОСЬ — {symbol}!*\n\n"
            f"Причина: {reason}\n\n"
            f"❌ Ошибка: `{e}`\n\n"
            f"⚠️ *Закрой пару вручную немедленно!*"
        )


# ─── Поиск новых возможностей ────────────────────────────────────────────────

async def _scan_opportunities(exchange_rates: dict):
    """Ищет пары и отправляет сигналы."""
    opps = find_pair_opportunities(exchange_rates, _enabled_exchanges)
    if not opps:
        return

    logger.info(f"Найдено {len(opps)} возможностей")

    for opp in opps[:5]:  # Топ 5
        signal_key = f"{opp['exchange_a']}:{opp['exchange_b']}:{opp['symbol']}:{opp['dir_a']}:{opp['dir_b']}"
        if not should_send_signal(signal_key, opp["net_apr"]):
            continue

        await send_pair_signal(opp)
        _sent_signals[signal_key] = (opp["net_apr"], time.time())


# ─── Основной цикл сканирования ─────────────────────────────────────────────

async def scan_and_notify():
    """Сканируем все биржи, мониторим позиции, ищем возможности."""
    logger.info("Запуск сканирования...")

    exchange_rates = await fetch_all_rates()
    if not exchange_rates:
        logger.error("Нет данных ни от одной биржи")
        return

    await save_funding_snapshot(exchange_rates)
    await _verify_positions(exchange_rates)
    await _monitor_open_pairs(exchange_rates)
    await _scan_opportunities(exchange_rates)


# ─── Telegram handlers ──────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"👋 *Funding Arbitrage Bot*\n\n"
        f"Дельта-нейтральный арбитраж фандинга.\n"
        f"Биржи: {', '.join(sorted(_enabled_exchanges))}\n\n"
        f"Бот сканирует рынок каждые {SCAN_INTERVAL_SECONDS} сек и присылает сигналы.\n"
        f"Используй кнопки внизу для управления.",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=persistent_keyboard(),
    )


async def show_positions(update: Update):
    """Показывает открытые позиции."""
    position_groups = await get_open_pairs()

    if not position_groups:
        await update.message.reply_text("📭 Открытых позиций нет")
        return

    # Собираем текущие ставки
    exchange_rates = await fetch_all_rates()
    rates_map = {}
    for exch_name, rates in exchange_rates.items():
        for r in rates:
            rates_map[f"{r.exchange}:{r.symbol}"] = r

    for group in position_groups:
        legs = group["legs"]
        pair_id = group["pair_id"]
        symbol = legs[0]["symbol"]
        opened_ago = (time.time() - legs[0]["opened_at"]) / 3600

        if pair_id and len(legs) >= 2:
            # Двуногая пара
            total_usd = sum(l["position_size_usd"] for l in legs)
            net_apr = calc_net_apr_for_pair(legs, rates_map)
            apr_status = "🟢" if net_apr >= 50 else "🟡" if net_apr >= 0 else "🔴"

            lines = []
            total_earned = 0
            has_earnings = False
            for leg in legs:
                dir_label = "шорт ↓" if leg["direction"] == "SHORT" else "лонг ↑"
                rate = rates_map.get(f"{leg['exchange']}:{symbol}")
                apr_str = f"`{rate.apr:+.1f}%`" if rate else "_?_"
                lines.append(f"  {leg['exchange']} ({dir_label}): `${leg['entry_price']:.4f}` APR: {apr_str}")

                if rate:
                    sign = 1 if leg["direction"] == "SHORT" else -1
                    earned = sign * rate.rate * opened_ago * leg["position_size_usd"]
                    total_earned += earned
                    has_earnings = True

            earned_str = f"`${total_earned:.4f}` (~оценка)" if has_earnings else "_нет данных_"

            exch_names = " × ".join(l["exchange"] for l in legs)
            text = (
                f"🔀 *{symbol}* — {exch_names} {apr_status}\n\n"
                + "\n".join(lines) + "\n"
                f"💵 Размер: `${total_usd:.0f}` (по `${legs[0]['position_size_usd']:.0f}` на ногу)\n"
                f"⏱ Открыта: `{opened_ago:.1f}ч назад`\n"
                f"  └ Нетто APR: `{net_apr:+.1f}%`\n"
                f"💰 Заработано: {earned_str}"
            )
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton("➕ Добавить", callback_data=f"scale_in:{pair_id}:{symbol}"),
                InlineKeyboardButton("❌ Закрыть пару", callback_data=f"close_pair:{pair_id}:{symbol}"),
            ]])
        else:
            # Одиночная позиция
            pos = legs[0]
            label = "шорт" if pos.get("direction") == "SHORT" else "лонг"
            rate = rates_map.get(f"{pos['exchange']}:{pos['symbol']}")
            current_apr = abs(rate.apr) if rate else 0
            earned = abs(rate.rate if rate else 0) * opened_ago * pos["position_size_usd"]
            status = "🟢" if current_apr >= 100 else "🟡" if current_apr >= 30 else "🔴"

            text = (
                f"📌 *{pos['symbol']}* — {pos['exchange']} {status}\n\n"
                f"📋 Тип: `{label}`\n"
                f"💵 Размер: `${pos['position_size_usd']}`\n"
                f"📈 Цена входа: `${pos['entry_price']:.4f}`\n"
                f"⏱ Открыта: `{opened_ago:.1f}ч назад`\n"
                f"📊 APR: `{current_apr:.1f}%`\n"
                f"💰 Заработано (~): `${earned:.4f}`"
            )
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton("❌ Закрыть", callback_data=f"close:{pos['id']}:{pos['symbol']}")
            ]])

        await update.message.reply_text(
            text, parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard
        )


async def show_settings(update: Update):
    """Показывает меню настроек."""
    rows = []

    # Переключатель бирж
    rows.append([InlineKeyboardButton("── Биржи ──", callback_data="noop")])
    exchange_btns = []
    for exch_id, exch_name in EXCHANGES.items():
        is_on = exch_name in _enabled_exchanges
        exchange_btns.append(InlineKeyboardButton(
            f"{'✅' if is_on else '❌'} {exch_name}",
            callback_data=f"toggle_exchange:{exch_name}"
        ))
        if len(exchange_btns) == 3:
            rows.append(exchange_btns)
            exchange_btns = []
    if exchange_btns:
        rows.append(exchange_btns)

    # Режим размера позиций
    rows.append([InlineKeyboardButton("── Размер позиций ──", callback_data="noop")])
    is_global = _position_size_mode == "global"
    rows.append([
        InlineKeyboardButton(
            f"{'🔘' if is_global else '⚪'} Общий (${_global_position_size:.0f})",
            callback_data="size_mode:global"
        ),
        InlineKeyboardButton(
            f"{'🔘' if not is_global else '⚪'} Раздельный",
            callback_data="size_mode:per_exchange"
        ),
    ])

    if is_global:
        # Кнопки быстрого выбора размера
        rows.append([
            InlineKeyboardButton("$15", callback_data="setsize:global:15"),
            InlineKeyboardButton("$50", callback_data="setsize:global:50"),
            InlineKeyboardButton("$100", callback_data="setsize:global:100"),
            InlineKeyboardButton("$250", callback_data="setsize:global:250"),
            InlineKeyboardButton("$500", callback_data="setsize:global:500"),
        ])
        rows.append([InlineKeyboardButton("✏️ Ввести вручную", callback_data="setsize:global:manual")])
    else:
        # Размеры для каждой биржи
        for exch_id, exch_name in EXCHANGES.items():
            if exch_name not in _enabled_exchanges:
                continue
            size = _exchange_sizes.get(exch_name, _global_position_size)
            rows.append([InlineKeyboardButton(f"── {exch_name} (${size:.0f}) ──", callback_data="noop")])
            rows.append([
                InlineKeyboardButton("$15", callback_data=f"setsize:{exch_name}:15"),
                InlineKeyboardButton("$50", callback_data=f"setsize:{exch_name}:50"),
                InlineKeyboardButton("$100", callback_data=f"setsize:{exch_name}:100"),
                InlineKeyboardButton("$250", callback_data=f"setsize:{exch_name}:250"),
                InlineKeyboardButton("$500", callback_data=f"setsize:{exch_name}:500"),
            ])
            rows.append([InlineKeyboardButton(f"✏️ Ввести ({exch_name})", callback_data=f"setsize:{exch_name}:manual")])

    # Описание текущих настроек
    if is_global:
        desc = f"Режим: *общий* — `${_global_position_size:.0f}` на каждую ногу"
    else:
        parts = [f"{n}: `${_exchange_sizes.get(n, _global_position_size):.0f}`" for n in sorted(_enabled_exchanges)]
        desc = "Режим: *раздельный*\n" + "\n".join(parts)

    enabled_list = ", ".join(sorted(_enabled_exchanges)) or "ни одна"

    await update.message.reply_text(
        f"⚙️ *Настройки*\n\n"
        f"Включены: {enabled_list}\n"
        f"{desc}",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup(rows),
    )


HISTORY_PAGE_SIZE = 5


async def _build_history_page(page: int) -> tuple:
    total_count = await count_closed_pairs()
    if total_count == 0:
        return "📭 Закрытых позиций пока нет", None

    total_pages = (total_count + HISTORY_PAGE_SIZE - 1) // HISTORY_PAGE_SIZE
    page = max(0, min(page, total_pages - 1))
    items = await get_closed_pairs(limit=HISTORY_PAGE_SIZE, offset=page * HISTORY_PAGE_SIZE)

    lines = [f"📋 *История* (стр. {page + 1}/{total_pages})\n"]
    for item in items:
        legs = item["legs"]
        if not legs:
            continue
        symbol = legs[0]["symbol"]
        exch_names = " × ".join(l["exchange"] for l in legs)
        closed_at = item.get("closed_at", 0)
        ago_h = (time.time() - closed_at) / 3600 if closed_at else 0

        total_pnl = sum(l.get("pnl_price_usd") or 0 for l in legs)
        total_fees = sum(l.get("fees_usd") or 0 for l in legs)
        total_usd = sum(l.get("position_size_usd", 0) for l in legs)

        pnl_emoji = "🟢" if total_pnl >= 0 else "🔴"
        lines.append(
            f"{pnl_emoji} *{symbol}* — {exch_names}\n"
            f"  💵 ${total_usd:.0f} | P&L: `${total_pnl:.4f}` | Комиссии: `${total_fees:.4f}`\n"
            f"  ⏱ {ago_h:.0f}ч назад"
        )

    nav_btns = []
    if page > 0:
        nav_btns.append(InlineKeyboardButton("⬅️", callback_data=f"history_page:{page - 1}"))
    if page < total_pages - 1:
        nav_btns.append(InlineKeyboardButton("➡️", callback_data=f"history_page:{page + 1}"))

    keyboard = InlineKeyboardMarkup([nav_btns]) if nav_btns else None
    return "\n".join(lines), keyboard


async def show_history(update: Update):
    text, keyboard = await _build_history_page(0)
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard)


async def scan_manual(update: Update):
    """Ручное сканирование."""
    await update.message.reply_text("🔍 Сканирую все биржи...")
    exchange_rates = await fetch_all_rates()
    if not exchange_rates:
        await update.message.reply_text("❌ Нет данных ни от одной биржи")
        return

    opps = find_pair_opportunities(exchange_rates, _enabled_exchanges, min_pair_apr=20)

    if not opps:
        await update.message.reply_text("📭 Нет подходящих пар сейчас")
        return

    lines = ["🔍 *Топ возможности:*\n"]
    for opp in opps[:10]:
        dir_a = "↓" if opp["dir_a"] == "SHORT" else "↑"
        dir_b = "↓" if opp["dir_b"] == "SHORT" else "↑"
        lines.append(
            f"*{opp['symbol']}* — {opp['exchange_a']}{dir_a} × {opp['exchange_b']}{dir_b}: "
            f"`{opp['net_apr']:.1f}%` APR"
        )

    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)


# ─── Обработка кнопок ────────────────────────────────────────────────────────

async def handle_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global _waiting_for_size, _waiting_for_scale_in

    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "skip" or data == "noop":
        return

    # ── Открытие пары ────────────────────────────────────────────────────────
    if data.startswith("open_pair:"):
        parts = data.split(":")
        if len(parts) < 6:
            return
        _, exch_a, exch_b, symbol, dir_a, dir_b = parts

        lock_key = f"{exch_a}:{exch_b}:{symbol}"
        if lock_key in _opening_pairs:
            await query.edit_message_text("⏳ Уже открываю эту пару...")
            return
        _opening_pairs.add(lock_key)

        # Размер = минимум из двух бирж (если раздельный режим)
        size = min(get_position_size(exch_a), get_position_size(exch_b))

        await query.edit_message_text(
            f"⏳ Открываю пару {symbol}: {exch_a} {dir_a} + {exch_b} {dir_b} (${size})..."
        )

        try:
            result = await open_pair(exch_a, exch_b, symbol, dir_a, dir_b, size, entry_apr=0)
            leg_a = result["leg_a"]
            leg_b = result["leg_b"]
            await query.edit_message_text(
                f"✅ *Пара открыта: {symbol}*\n\n"
                f"  {exch_a}: `${leg_a['price']:.4f}` ({dir_a})\n"
                f"  {exch_b}: `${leg_b['price']:.4f}` ({dir_b})\n"
                f"💵 Размер: `${size}` на каждую ногу",
                parse_mode=ParseMode.MARKDOWN,
            )
        except Exception as e:
            await query.edit_message_text(f"❌ Ошибка открытия пары:\n`{e}`", parse_mode=ParseMode.MARKDOWN)
        finally:
            _opening_pairs.discard(lock_key)

    # ── Закрытие пары ────────────────────────────────────────────────────────
    elif data.startswith("close_pair:"):
        parts = data.split(":")
        pair_id = parts[1]
        symbol = parts[2] if len(parts) > 2 else "?"

        await query.edit_message_text(f"⏳ Закрываю пару {symbol}...")
        try:
            legs = await get_positions_by_pair(pair_id)
            await close_pair(pair_id, symbol, legs)
            await query.edit_message_text(f"✅ Пара {symbol} закрыта.", parse_mode=ParseMode.MARKDOWN)
        except Exception as e:
            await query.edit_message_text(f"❌ Ошибка закрытия:\n`{e}`", parse_mode=ParseMode.MARKDOWN)

    # ── Scale in ─────────────────────────────────────────────────────────────
    elif data.startswith("scale_in:"):
        parts = data.split(":")
        pair_id = parts[1]
        symbol = parts[2] if len(parts) > 2 else "?"
        _waiting_for_scale_in = (pair_id, symbol)
        await query.edit_message_text(
            f"✏️ Введи сумму для добавления к паре {symbol} (в USD):"
        )

    # ── Переключение бирж ────────────────────────────────────────────────────
    elif data.startswith("toggle_exchange:"):
        exch_name = data.split(":")[1]
        if exch_name in _enabled_exchanges:
            _enabled_exchanges.discard(exch_name)
        else:
            _enabled_exchanges.add(exch_name)
        await _save_settings()
        status = "✅ включена" if exch_name in _enabled_exchanges else "❌ выключена"
        await query.edit_message_text(f"{exch_name}: {status}\n\nНажми ⚙️ Настройки чтобы обновить меню.")

    # ── Режим размера позиций ────────────────────────────────────────────────
    elif data.startswith("size_mode:"):
        global _position_size_mode
        _position_size_mode = data.split(":")[1]
        await _save_settings()
        mode_str = "общий" if _position_size_mode == "global" else "раздельный"
        await query.edit_message_text(f"Режим размера: *{mode_str}*\n\nНажми ⚙️ Настройки чтобы обновить.", parse_mode=ParseMode.MARKDOWN)

    # ── Установка размера ────────────────────────────────────────────────────
    elif data.startswith("setsize:"):
        parts = data.split(":")
        target = parts[1]  # "global" или exchange_name
        value = parts[2]

        if value == "manual":
            _waiting_for_size = target
            await query.edit_message_text(
                f"✏️ Введи размер позиции в USD для {'всех бирж' if target == 'global' else target}:"
            )
        else:
            size = float(value)
            if target == "global":
                _global_position_size = size
            else:
                _exchange_sizes[target] = size
            await _save_settings()
            await query.edit_message_text(
                f"✅ Размер {'общий' if target == 'global' else target}: `${size:.0f}`",
                parse_mode=ParseMode.MARKDOWN,
            )

    # ── Пагинация истории ────────────────────────────────────────────────────
    elif data.startswith("history_page:"):
        page = int(data.split(":")[1])
        text, keyboard = await _build_history_page(page)
        await query.edit_message_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard)


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка текстовых сообщений."""
    global _waiting_for_size, _waiting_for_scale_in

    text = update.message.text.strip()

    # Кнопки клавиатуры
    if text == BTN_POSITIONS:
        await show_positions(update)
    elif text == BTN_SCAN:
        await scan_manual(update)
    elif text == BTN_HISTORY:
        await show_history(update)
    elif text == BTN_SETTINGS:
        await show_settings(update)
    elif text == BTN_SUPPORT:
        await update.message.reply_text(
            f"💙 *Поддержать автора*\n\n"
            f"Канал: [{AUTHOR_CHANNEL_NAME}]({AUTHOR_CHANNEL})\n\n"
            f"EVM: `{DONATION_WALLET_EVM}`\n"
            f"SOL: `{DONATION_WALLET_SOL}`",
            parse_mode=ParseMode.MARKDOWN,
        )
    # Ввод размера позиции
    elif _waiting_for_size is not None:
        target = _waiting_for_size
        _waiting_for_size = None
        try:
            size = float(text.replace("$", "").replace(",", "."))
            if size < 5:
                await update.message.reply_text("❌ Минимум $5")
                return
            if target == "global":
                global _global_position_size
                _global_position_size = size
            else:
                _exchange_sizes[target] = size
            await _save_settings()
            await update.message.reply_text(
                f"✅ Размер {'общий' if target == 'global' else target}: `${size:.0f}`",
                parse_mode=ParseMode.MARKDOWN,
            )
        except ValueError:
            await update.message.reply_text("❌ Введи число, например: `100`", parse_mode=ParseMode.MARKDOWN)
    # Ввод суммы для scale_in
    elif _waiting_for_scale_in is not None:
        pair_id, symbol = _waiting_for_scale_in
        _waiting_for_scale_in = None
        try:
            add_usd = float(text.replace("$", "").replace(",", "."))
            if add_usd < 5:
                await update.message.reply_text("❌ Минимум $5")
                return
            await update.message.reply_text(f"⏳ Добавляю ${add_usd} к паре {symbol}...")
            legs = await get_positions_by_pair(pair_id)
            result = await scale_in_pair(pair_id, symbol, legs, add_usd)
            await update.message.reply_text(
                f"✅ Добавлено `${add_usd}` к паре {symbol}",
                parse_mode=ParseMode.MARKDOWN,
            )
        except Exception as e:
            await update.message.reply_text(f"❌ Ошибка scale in:\n`{e}`", parse_mode=ParseMode.MARKDOWN)


# ─── Запуск ──────────────────────────────────────────────────────────────────

async def post_init(app):
    """Выполняется после запуска бота."""
    await init_db()
    await _load_settings()

    scheduler = AsyncIOScheduler()
    scheduler.add_job(scan_and_notify, "interval", seconds=SCAN_INTERVAL_SECONDS)
    scheduler.start()
    logger.info(f"Планировщик запущен: каждые {SCAN_INTERVAL_SECONDS} сек")


def main():
    if not TELEGRAM_TOKEN:
        raise RuntimeError("TELEGRAM_TOKEN не задан в .env")
    if not TELEGRAM_CHAT_ID:
        raise RuntimeError("TELEGRAM_CHAT_ID не задан в .env")

    app = Application.builder().token(TELEGRAM_TOKEN).post_init(post_init).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CallbackQueryHandler(handle_button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    logger.info("Бот запускается...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
