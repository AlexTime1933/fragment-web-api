# fragment-web-to-api

**[English](#english)** · **[Русский](#русский)**

---

<a id="english"></a>

## English

A Python library that buys **Telegram Stars** and **Telegram Premium** on [fragment.com](https://fragment.com)
for any Telegram user and pays for them from **your own TON wallet**: TON, or USDT on the TON network.
Fragment has no public API. This library talks to the same web interface the site uses and signs the
payment locally. Use it to build Telegram bots, websites and shops.

> **Unofficial project.** It is not affiliated with Telegram or Fragment. Fragment may change its
> website at any time, and this library may then need an update. **The library moves real money from
> your wallet.** Read the safety section, start with small amounts and use `dry_run=True` first.

### Features

- Prices for Stars and Premium, recipient lookup, purchases for any `@username` or for yourself.
- Payment in **TON** or **USDT (TON network)** from the same wallet.
- Local signing: TON cells, BoC, addresses and **W5 (V5R1) / V4R2** wallets are written from scratch.
  They are verified bit-for-bit against a reference implementation and against real wallets on-chain
  (see [LIBRARIES.md](LIBRARIES.md), in Russian).
- **Safety checks before signing**: allowed destinations, amount against the quoted price, hard caps,
  balance, network, sender. A suspicious transaction is refused and **nothing is signed**.
- **Pure API, stores nothing.** Your application passes cookies, seed and API key as values. The
  library does not read `.env`, files or environment variables and does not write to disk.
- Only two direct dependencies: `httpx` (Encode) and `cryptography` (PyCA). All versions are pinned
  with SHA-256 hashes in `requirements.lock`.
- Async API (`Fragment`) and a blocking wrapper (`FragmentSync`).
- Seed phrase: 24-word TON or 12/24-word BIP39, detected automatically.

### What you need

1. **Python 3.10+**.
2. **A TON wallet** (for example Tonkeeper, W5) and its **seed phrase**.
3. **Fragment cookies.** Log in to fragment.com with Telegram and connect the same wallet. Then open
   DevTools (F12) → Application → Cookies → `https://fragment.com` and copy `stel_ssid`, `stel_token`,
   `stel_ton_token` and `stel_dt`.
   - Before the first purchase Fragment asks you to **link the wallet to your Telegram account**.
     This is permanent, so do it yourself in the browser. The library detects the request
     (`WalletLinkRequiredError`) and never does it on its own.
   - Fragment may also ask for KYC (`VerificationRequiredError`). It can only be passed in the browser.
4. **A tonapi.io API key** from [tonconsole.com](https://tonconsole.com). It is used to read the
   balance and to send and track transactions.
5. Funds on the wallet: TON or USDT for the purchase, plus a little TON for network fees
   (about 0.05 TON per USDT payment; the unused part comes back).

### Installation

The package is not on PyPI. Clone the repository and install the pinned dependencies:

```bash
git clone <this repository URL> fragment-web-api
cd fragment-web-api
python -m venv .venv
# Windows: .venv\Scripts\python   Linux/macOS: .venv/bin/python
.venv/bin/python -m pip install --require-hashes -r requirements.lock
.venv/bin/python -m pip install --no-deps .
```

The pip package is called `fragment-web-to-api`; in code you import `fragment_api`.

### Quick start

```python
import asyncio
from fragment_api import Fragment

async def main():
    async with Fragment.create(
        cookies={"stel_ssid": "...", "stel_token": "...", "stel_ton_token": "...", "stel_dt": "-180"},
        mnemonic="word1 word2 ... word24",   # without a seed only prices and lookups work
        tonapi_key="AE...",
        wallet_version="v5r1",               # W5 (current Tonkeeper) or "v4r2"
    ) as f:
        print(await f.get_stars_price(250))       # StarsPrice(quantity=250, price_ton=..., price_usd=...)
        print(await f.get_premium_prices())       # [PremiumPrice(months=3, ...), ...]
        print(await f.find_stars_recipient("@username"))

        # rehearsal: signed and emulated by tonapi, NOT sent
        dry = await f.buy_stars("@alice", 100, dry_run=True)
        print(dry.extra["emulation"])

        r1 = await f.buy_stars("@alice", 100)                               # pay in TON
        r2 = await f.gift_premium("@bob", 12, payment_method="usdt_ton")    # pay in USDT (TON network)
        r3 = await f.buy_stars(None, 50)                                    # for yourself
        print(r1.message_hash, await f.get_balance(), await f.get_usdt_balance())

        # Fragment may rotate cookies; saving them is up to your application
        new_cookies = f.cookies

asyncio.run(main())
```

Blocking code (scripts, Django, …) takes the same arguments:

```python
from fragment_api import FragmentSync

with FragmentSync.create(cookies, mnemonic=seed, tonapi_key=key) as f:
    print(f.get_stars_price(100))
    f.buy_stars("@alice", 100)
```

Low-level access: `f.client` (`FragmentClient`), `f.wallet` (`WalletV5R1`), `f.tonapi` (`TonApiClient`).

### How a purchase works

1. `searchStarsRecipient` / `searchPremiumGiftRecipient`: `@username` → Fragment's recipient id.
2. Check that the wallet linked to the Fragment session is the wallet of your seed.
3. `initBuyStarsRequest` / `initGiftPremiumRequest`: Fragment creates the order and quotes the price.
4. `getBuyStarsLink` / `getGiftPremiumLink`: Fragment returns a TON Connect transaction.
5. **Local safety checks** (`SafetyPolicy`), see below.
6. Signing with the seed key, sending through tonapi.
7. Waiting for the wallet transaction on-chain (by normalized message hash, TEP-467).
8. `confirm_method`: the signed BoC is reported back to Fragment, as the website does.

Purchases from one wallet are executed one after another automatically (seqno).

### Safety checks (`SafetyPolicy`)

Before anything is signed:

- **TON payment:** the destination must be one of Fragment's known payment wallets
  (`FRAGMENT_PAYMENT_ADDRESSES`); the amount must not exceed the quoted price (+2 % / 0.05 TON) or
  `max_amount_ton` (100); at most 4 messages, no `stateInit`; correct network; not expired; Fragment
  must expect the payment from your wallet; the balance must cover the amount and fees.
- **USDT payment** (`payment_method="usdt_ton"`, a TEP-74 jetton transfer): exactly one message, sent
  to **your own** USDT jetton wallet (its address is derived on-chain from the USDT master contract,
  not taken from Fragment); USDT goes to an allowed Fragment address; the unused gas returns to your
  wallet; no custom payload; at most `max_jetton_gas_ton` (0.1 TON) for gas; the USDT amount must not
  exceed Fragment's USD price (+2 % / 0.05) or `max_amount_usdt` (100); enough USDT and TON on the wallet.

All limits are fields of `SafetyPolicy` and can be passed to `Fragment.create(safety=...)`.

### Errors

| Exception | When |
|---|---|
| `AuthenticationError` | cookies expired |
| `WalletNotConnectedError` | no wallet linked to the session (`link_wallet()`) |
| `WalletLinkRequiredError` | Fragment asks to link the wallet to the Telegram account (do it in the browser) |
| `VerificationRequiredError` | Fragment requires KYC |
| `RecipientNotFoundError` | no such user / already has Premium / a bot |
| `InvalidAmountError` | Stars outside 50…10 000 000, months not 3/6/12 |
| `TransactionSafetyError` | Fragment's transaction failed a safety check, **nothing was signed** |
| `InsufficientBalanceError` | not enough TON or USDT |
| `TransactionFailedError` | the wallet transaction failed on-chain |
| `TransactionTimeoutError` | sent, but not confirmed yet. **Do not retry blindly**; check `check_payment(e.message_hash)` first |

All of them inherit from `FragmentAPIError`.

### Tests

```bash
.venv/bin/python -m pip install pytest==9.1.1
.venv/bin/python -m pytest tests
```

The tests run offline against fake fragment.com and tonapi. They check cells, addresses, keys and
signed transfers against reference vectors, the full purchase flow and every safety check.

### Project layout

```
fragment_api/
  service.py        Fragment: high-level API, SafetyPolicy
  sync.py           FragmentSync
  exceptions.py
  fragment/         fragment.com client: client.py, parser.py, models.py
  ton/              cell.py, boc.py, address.py, mnemonic.py, keys.py, messages.py,
                    wallets.py (W5/V4R2), tonconnect.py, tonapi.py, jetton.py (USDT, TEP-74)
tests/              offline tests and fixtures
requirements.lock   exact dependency versions with SHA-256 hashes
LIBRARIES.md        dependencies, sources and verification notes (Russian)
```

### Example application

**telegram-shop-on-fragment-web-api** (a separate repository) is a ready-made Telegram shop
bot built on this library. It has orders, payment providers and an admin panel.

### License

[The Unlicense](LICENSE): the code is released into the **public domain**. Anyone may copy, modify,
use, sell and distribute it for any purpose, without any conditions, not even attribution.

The code was written with the help of an AI model (Claude, Anthropic) under human direction.
It comes **as is, without any warranty**. You are responsible for checking it before you trust it
with real money.

---

<a id="русский"></a>

## Русский

Python-библиотека для покупки **Telegram Stars** и **Telegram Premium** на [fragment.com](https://fragment.com)
любому пользователю Telegram с оплатой со **своего TON-кошелька**: в TON или в USDT (сеть TON).
У Fragment нет публичного API. Библиотека работает с тем же веб-интерфейсом, что и сайт, и подписывает
платёж локально. Подходит для Telegram-ботов, сайтов и магазинов.

> **Неофициальный проект.** Не связан с Telegram и Fragment. Fragment может в любой момент изменить
> сайт, и тогда библиотеку придётся обновить. **Библиотека тратит настоящие деньги с вашего кошелька.**
> Прочитайте раздел о безопасности, начинайте с маленьких сумм и сначала используйте `dry_run=True`.

### Возможности

- Цены на Stars и Premium, поиск получателя, покупка любому `@username` или себе.
- Оплата в **TON** или **USDT (сеть TON)** с одного и того же кошелька.
- Локальная подпись: ячейки TON, BoC, адреса и кошельки **W5 (V5R1) / V4R2** написаны с нуля. Они
  проверены бит в бит против эталонной реализации и реальных кошельков в блокчейне
  (см. [LIBRARIES.md](LIBRARIES.md)).
- **Проверки безопасности до подписи**: разрешённые адреса, сумма против цены, жёсткие лимиты, баланс,
  сеть, отправитель. Подозрительная транзакция отклоняется, и **ничего не подписывается**.
- **Чистый API, ничего не хранит.** Cookie, seed и ключ API передаёт ваше приложение как значения.
  Библиотека не читает `.env`, файлы и переменные окружения и ничего не пишет на диск.
- Всего две прямые зависимости: `httpx` (Encode) и `cryptography` (PyCA). Все версии закреплены
  с хэшами SHA-256 в `requirements.lock`.
- Асинхронный API (`Fragment`) и синхронная обёртка (`FragmentSync`).
- Seed: 24 слова в формате TON или 12/24 слова в формате BIP39, определяется автоматически.

### Что понадобится

1. **Python 3.10+**.
2. **TON-кошелёк** (например Tonkeeper, W5) и его **seed-фраза**.
3. **Cookie Fragment.** Войдите на fragment.com через Telegram и подключите тот же кошелёк. Затем
   откройте инструменты разработчика (F12) → Application → Cookies → `https://fragment.com` и скопируйте
   `stel_ssid`, `stel_token`, `stel_ton_token` и `stel_dt`.
   - Перед первой покупкой Fragment попросит **привязать кошелёк к Telegram-аккаунту**. Это
     необратимо, поэтому сделайте это сами в браузере. Библиотека распознаёт этот запрос
     (`WalletLinkRequiredError`) и никогда не выполняет его сама.
   - Fragment может потребовать KYC (`VerificationRequiredError`). Пройти его можно только в браузере.
4. **API-ключ tonapi.io** на [tonconsole.com](https://tonconsole.com). Он нужен, чтобы читать баланс,
   отправлять транзакции и следить за ними.
5. Средства на кошельке: TON или USDT на покупку и немного TON на комиссию сети
   (около 0.05 TON на USDT-платёж, неизрасходованное возвращается).

### Установка

Пакета нет на PyPI. Склонируйте репозиторий и установите закреплённые зависимости:

```bash
git clone <адрес этого репозитория> fragment-web-api
cd fragment-web-api
python -m venv .venv
# Windows: .venv\Scripts\python   Linux/macOS: .venv/bin/python
.venv/bin/python -m pip install --require-hashes -r requirements.lock
.venv/bin/python -m pip install --no-deps .
```

Пакет pip называется `fragment-web-to-api`, в коде импортируется `fragment_api`.

### Быстрый старт

```python
import asyncio
from fragment_api import Fragment

async def main():
    async with Fragment.create(
        cookies={"stel_ssid": "...", "stel_token": "...", "stel_ton_token": "...", "stel_dt": "-180"},
        mnemonic="слово1 слово2 ... слово24",  # без seed работают только цены и поиск
        tonapi_key="AE...",
        wallet_version="v5r1",                 # W5 (современный Tonkeeper) или "v4r2"
    ) as f:
        print(await f.get_stars_price(250))       # StarsPrice(quantity=250, price_ton=..., price_usd=...)
        print(await f.get_premium_prices())       # [PremiumPrice(months=3, ...), ...]
        print(await f.find_stars_recipient("@username"))

        # пробный прогон: подпись и эмуляция в tonapi, БЕЗ отправки
        dry = await f.buy_stars("@alice", 100, dry_run=True)
        print(dry.extra["emulation"])

        r1 = await f.buy_stars("@alice", 100)                               # оплата в TON
        r2 = await f.gift_premium("@bob", 12, payment_method="usdt_ton")    # оплата в USDT (сеть TON)
        r3 = await f.buy_stars(None, 50)                                    # себе
        print(r1.message_hash, await f.get_balance(), await f.get_usdt_balance())

        # Fragment может обновить cookie; сохранить их — забота приложения
        new_cookies = f.cookies

asyncio.run(main())
```

Синхронно (скрипты, Django и т. п.), аргументы те же:

```python
from fragment_api import FragmentSync

with FragmentSync.create(cookies, mnemonic=seed, tonapi_key=key) as f:
    print(f.get_stars_price(100))
    f.buy_stars("@alice", 100)
```

Низкоуровневый доступ: `f.client` (`FragmentClient`), `f.wallet` (`WalletV5R1`), `f.tonapi` (`TonApiClient`).

### Как проходит покупка

1. `searchStarsRecipient` / `searchPremiumGiftRecipient`: `@username` → id получателя на Fragment.
2. Проверка, что к сессии Fragment привязан именно кошелёк из вашего seed.
3. `initBuyStarsRequest` / `initGiftPremiumRequest`: Fragment создаёт заказ и называет цену.
4. `getBuyStarsLink` / `getGiftPremiumLink`: Fragment отдаёт транзакцию TON Connect.
5. **Локальные проверки безопасности** (`SafetyPolicy`), см. ниже.
6. Подпись ключом из seed, отправка через tonapi.
7. Ожидание транзакции кошелька в блокчейне (по нормализованному хэшу сообщения, TEP-467).
8. `confirm_method`: подписанный BoC сообщается Fragment, как это делает сайт.

Покупки с одного кошелька автоматически выполняются по очереди (seqno).

### Проверки безопасности (`SafetyPolicy`)

До подписи:

- **Оплата в TON:** адрес получателя — только из известных платёжных кошельков Fragment
  (`FRAGMENT_PAYMENT_ADDRESSES`); сумма не больше цены (+2 % / 0.05 TON) и `max_amount_ton` (100);
  не больше 4 сообщений, без `stateInit`; правильная сеть; срок не истёк; Fragment ждёт оплату именно
  с вашего кошелька; баланса хватает на сумму и комиссию.
- **Оплата в USDT** (`payment_method="usdt_ton"`, jetton-перевод TEP-74): ровно одно сообщение на
  **ваш собственный** USDT-кошелёк (его адрес вычисляется в блокчейне из контракта USDT, а не берётся
  у Fragment); USDT уходит на разрешённый адрес Fragment; неизрасходованный газ возвращается на ваш
  кошелёк; без custom payload; на газ не больше `max_jetton_gas_ton` (0.1 TON); сумма USDT не больше
  цены Fragment в $ (+2 % / 0.05) и `max_amount_usdt` (100); хватает USDT и TON.

Все лимиты — поля `SafetyPolicy`, их можно передать в `Fragment.create(safety=...)`.

### Ошибки

| Исключение | Когда |
|---|---|
| `AuthenticationError` | cookie истекли |
| `WalletNotConnectedError` | к сессии не привязан кошелёк (`link_wallet()`) |
| `WalletLinkRequiredError` | Fragment просит привязать кошелёк к Telegram-аккаунту (сделайте в браузере) |
| `VerificationRequiredError` | Fragment требует KYC |
| `RecipientNotFoundError` | нет такого пользователя / уже есть Premium / это бот |
| `InvalidAmountError` | Stars вне 50…10 000 000, месяцы не 3/6/12 |
| `TransactionSafetyError` | транзакция Fragment не прошла проверку, **ничего не подписано** |
| `InsufficientBalanceError` | мало TON или USDT |
| `TransactionFailedError` | транзакция кошелька упала в сети |
| `TransactionTimeoutError` | отправлено, но ещё не подтверждено. **Не повторяйте вслепую**, сначала `check_payment(e.message_hash)` |

Все наследуются от `FragmentAPIError`.

### Тесты

```bash
.venv/bin/python -m pip install pytest==9.1.1
.venv/bin/python -m pytest tests
```

Тесты работают офлайн, на поддельных fragment.com и tonapi. Они сверяют ячейки, адреса, ключи и
подписанные переводы с эталонными векторами, проверяют весь цикл покупки и каждую проверку безопасности.

### Структура

```
fragment_api/
  service.py        Fragment: высокоуровневый API, SafetyPolicy
  sync.py           FragmentSync
  exceptions.py
  fragment/         клиент fragment.com: client.py, parser.py, models.py
  ton/              cell.py, boc.py, address.py, mnemonic.py, keys.py, messages.py,
                    wallets.py (W5/V4R2), tonconnect.py, tonapi.py, jetton.py (USDT, TEP-74)
tests/              офлайн-тесты и фикстуры
requirements.lock   точные версии зависимостей с хэшами SHA-256
LIBRARIES.md        зависимости, источники и как всё проверено
```

### Пример приложения

**telegram-shop-on-fragment-web-api** (отдельный репозиторий) — готовый Telegram-бот-магазин
на этой библиотеке: заказы, платёжные системы, админка.

### Лицензия

[The Unlicense](LICENSE): код передан в **общественное достояние**. Любой может копировать, изменять,
использовать, продавать и распространять его для любых целей без каких-либо условий, даже без
указания автора.

Код написан с помощью нейросети (Claude, Anthropic) под руководством человека. Он предоставляется
**как есть, без каких-либо гарантий**. Перед тем как доверять ему настоящие деньги, проверьте его сами.
