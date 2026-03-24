# Funding Arbitrage Bot

## Цель проекта
Бот для дельта-нейтрального арбитража funding rate на крипто перп биржах.
Стратегия: лонг на одной бирже + шорт на другой = зарабатываем на разнице фандинга.

## Владелец
Павел. Не программист, учится на практике.
- Объяснять просто, без лишней терминологии
- Предлагать один лучший вариант
- Перед рискованными изменениями — предупреждать и спрашивать

## Биржи
- **Backpack** — сканер + executor (Ed25519 аутентификация)
- **Lighter** — сканер + executor (lighter-sdk)
- **Hyperliquid** — сканер + executor (hyperliquid-python-sdk)
- **GRVT** — заглушки (TODO)
- **Aster** — заглушки (TODO)

## Ключевая архитектура
Универсальная система — любая биржа может быть скомбинирована с любой другой.

### Базовые интерфейсы
- `scanners/base.py` — `BaseScanner` + `FundingRate` dataclass
- `core/exchanges/base.py` — `BaseExchangeExecutor` (market_open, market_close, get_positions, get_balance, get_liquidation_info)

### Универсальные функции
- `core/executor.py` — `open_pair()`, `close_pair()`, `scale_in_pair()` — работают с любыми биржами через `get_executor(name)`
- `core/analyzer.py` — `find_pair_opportunities()` — перебирает ВСЕ комбинации бирж

### Настройки размера позиций
- Режим "global" — один размер для всех бирж
- Режим "per_exchange" — отдельный размер для каждой биржи
- Переключатель в Telegram настройках

## Структура проекта
```
funding-arb-bot/
├── main.py              # Telegram бот, планировщик, хэндлеры
├── config.py            # Конфигурация из .env
├── scanners/
│   ├── base.py          # BaseScanner, FundingRate
│   ├── backpack.py      # Backpack публичный API
│   ├── lighter.py       # Lighter публичный API
│   ├── hyperliquid.py   # Hyperliquid публичный API
│   ├── grvt.py          # GRVT (TODO)
│   └── aster.py         # Aster (TODO)
├── core/
│   ├── analyzer.py      # Универсальный поиск пар
│   ├── executor.py      # Универсальное открытие/закрытие пар
│   └── exchanges/
│       ├── base.py      # BaseExchangeExecutor
│       ├── backpack.py  # Backpack executor
│       ├── lighter.py   # Lighter executor
│       ├── hyperliquid.py # Hyperliquid executor
│       ├── grvt.py      # GRVT (TODO)
│       └── aster.py     # Aster (TODO)
├── bot/
│   └── telegram.py      # Функции отправки сообщений
├── db/
│   └── database.py      # SQLite (positions, funding_history, settings)
├── .env.example
├── .gitignore
└── requirements.txt
```

## Запуск
```bash
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env     # заполнить ключи
python main.py
```

## Безопасность
- Приватные ключи ТОЛЬКО в `.env`
- `.env` в `.gitignore`
- Никогда не логировать приватные ключи
- Перед изменениями в executor.py — предупреждать

## Добавление новой биржи
1. Создать `scanners/new_exchange.py` (наследник BaseScanner)
2. Создать `core/exchanges/new_exchange.py` (наследник BaseExchangeExecutor)
3. Добавить в `config.py`: переменные окружения + запись в EXCHANGES
4. Добавить в `core/executor.py`: ветку в `get_executor()`
5. Добавить в `main.py`: импорт сканера в ALL_SCANNERS
