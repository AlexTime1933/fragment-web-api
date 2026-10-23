# Используемые библиотеки и источники

Главный принцип: **seed-фраза попадает только в код этого проекта и в `cryptography` (PyCA)**.
Ни одна сторонняя TON-библиотека не участвует в подписи и отправке транзакций.
Ячейки, BoC, адреса, мнемоника и кошельки W5/V4R2 написаны с нуля (`fragment_api/ton/`)
и проверены бит в бит против эталона (см. ниже).

## Runtime-зависимости (ставятся вместе с библиотекой)

Точные версии и SHA-256 всех файлов — в `requirements.lock`
(`pip install --require-hashes -r requirements.lock`). Все версии опубликованы более 2 недель назад.

| Пакет | Версия | Для чего | Источник / мейнтейнер |
|---|---|---|---|
| **httpx** | 0.28.1 | Async HTTP-клиент: запросы к fragment.com и tonapi.io | github.com/encode/httpx — Encode (авторы Starlette, Uvicorn, DRF) |
| httpcore | 1.0.9 | транспорт httpx | github.com/encode/httpcore — Encode |
| h11 | 0.16.0 | HTTP/1.1 протокол для httpcore | github.com/python-hyper/h11 — python-hyper |
| anyio | 4.15.1 | async-слой httpx | github.com/agronholm/anyio — Alex Grönholm |
| idna | 3.20 | международные домены (зависимость httpx) | github.com/kjd/idna |
| certifi | 2026.7.22 | корневые сертификаты (Mozilla CA bundle) для HTTPS | github.com/certifi/python-certifi |
| typing_extensions | 4.16.0 | бэкпорты типов (зависимость anyio) | github.com/python/typing_extensions — команда CPython |
| **cryptography** | 50.0.2 | Ed25519: ключ из seed и подпись транзакций / ton_proof | github.com/pyca/cryptography — Python Cryptographic Authority (OpenSSL backend) |
| cffi | 2.1.1 | C-биндинги для cryptography | github.com/python-cffi/cffi |
| pycparser | 3.0 | зависимость cffi | github.com/eliben/pycparser |

Стандартная библиотека Python: `hashlib` (SHA-256/512, PBKDF2 для seed), `hmac`, `base64`,
`json`, `re`, `html`, `asyncio`, `decimal`, `secrets`.

## Только для разработки

| Пакет | Версия | Для чего | Источник |
|---|---|---|---|
| pytest | 9.1.1 | тесты | github.com/pytest-dev/pytest |
| setuptools | 80.9.0 | сборка пакета (`pip install .`) | github.com/pypa/setuptools |

## Данные, встроенные в код

| Что | Откуда | Как проверено |
|---|---|---|
| Код контракта W5 (V5R1) | `ton-org/ton` (официальный TS SDK), `src/wallets/v5r1/WalletContractV5R1.ts` | хэш кода `20834b7b…b72f` сверяется при импорте; совпадает с хэшем реальных W5 в сети |
| Код контракта V4R2 | `ton-org/ton`, `src/wallets/v4/WalletContractV4.ts` | хэш `feb5ff68…d5c0` сверяется при импорте |
| Словарь BIP39 (2048 слов) | `bitcoin/bips/bip-0039/english.txt` | SHA-256 `2f5eed53…dbda` сверяется при каждой загрузке |
| Алгоритм мнемоники TON | `ton-org/ton-crypto` `src/mnemonic/mnemonic.ts` | совпадение ключей с tonutils |
| BIP39 seed + SLIP-0010, путь `m/44'/607'/0'` (12-словные фразы) | спецификации BIP-0039 и SLIP-0010 | опубликованные тест-векторы (Trezor BIP39, SLIP-0010 vector 1); ключ из реального 12-словного seed даёт ровно подключённый на Fragment W5-кошелёк |
| Протокол Fragment | JS сайта `fragment.com/js/auction.js` | живые запросы (цены, поиск) |
| API tonapi | `tonkeeper/opentonapi/api/openapi.yml` | живые read-only запросы |

## Эталон для проверки (НЕ зависимость, в проект не входит)

`tonutils` использовалась только как эталон в отдельном изолированном venv, на случайных
одноразовых seed. Из неё сгенерирован `tests/fixtures/tonutils_vectors.json`.

Аудит цепочки (исходники wheel-файлов прочитаны и проверены grep'ом на `eval/exec`,
сетевые вызовы, чтение переменных окружения, отправку seed):

| Пакет | Версия | Автор | Итог |
|---|---|---|---|
| tonutils | 2.3.1 | nessshon (github.com/nessshon/tonutils) | чисто: сеть только к публичным RPC (toncenter, tonapi, ton.org config, lite-серверы); seed используется только локально для подписи |
| ton-core | 0.6.0 | nessshon (форк pytoniq-core) | чисто; `urlopen` только для `ton.org/global-config.json` |
| pytoniq-core | 0.2.1 | yungwine (github.com/yungwine/pytoniq) | чисто |
| ↳ x25519 | 0.0.2 | 1ocalhost | ⚠️ малоизвестный пакет, единственный релиз — **одна из причин не тянуть эту цепочку в runtime** |
| ↳ pycryptodomex, bitarray, requests, PyNaCl, setuptools | — | известные проекты | ок |

Результаты сверки с эталоном и сетью:
- публичные ключи из seed — совпадают (4/4 вектора);
- адреса W5 и V4R2 — совпадают с tonutils (4/4) и с **реальными кошельками в блокчейне** (6 W5 + 2 V4R2, ключ получен из контракта `get_public_key`);
- подписанные тела переводов W5 и V4R2 — **совпадают бит в бит** с tonutils (включая подпись);
- `ton_proof` — принят официальным верификатором Tonkeeper (`tonapi /v2/wallet/auth/proof`, библиотека tongo), подделанная подпись отклоняется;
- эмуляция tonapi первой транзакции (деплой W5 + перевод с комментарием) — `success`.

## Сознательно НЕ используются

- готовые «fragment API» библиотеки — часть работает через закрытый сервер посредника,
  часть замечена в краже средств; код не копировался;
- `tonsdk` — не поддерживается;
- `python-dotenv` — библиотека вообще не читает `.env`, файлы и переменные окружения: значения передаёт приложение;
- BeautifulSoup/lxml — HTML Fragment разбирается регулярками по стабильным CSS-классам.
