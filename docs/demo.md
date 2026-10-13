# Публичное демо

Показ платформы «одно ядро — два магазина». Витрины — статические страницы
`apps/web/public/demo/shop/` (P9-00a), виджет на них — обычный `widget.js` с ключом тенанта
из `?key=`. После деплоя (P9-01) витрины открываются по боевому адресу:
`https://<домен>/demo/shop/lumiere.html?key=…` — ключи перевыпускать не нужно, origin веб-чата
разрешён для встраивания всегда (ADR-0022, дополнение P9-00b).

Ниже — запасной вариант до деплоя: временная ссылка через quick tunnel cloudflared
(P9-00b). Зависит от сети: нужен исходящий порт 7844 до Cloudflare (см. «Если туннель не
поднимается»).

## Подготовка (один раз)

1. `cloudflared` — внешний бинарь, не зависимость проекта:
   ```bash
   curl -L -o /tmp/cloudflared.deb \
     https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64.deb
   sudo dpkg -i /tmp/cloudflared.deb   # в своём терминале: sudo спрашивает пароль
   ```
   Без sudo: `dpkg-deb -x /tmp/cloudflared.deb /tmp/cf && cp /tmp/cf/usr/bin/cloudflared ~/.local/bin/`.
2. Данные демо-тенантов: `make up && make pilot`. Ключи виджета — `SEED_WIDGET_KEY`
   (`demo-beauty=wk_…,demo-garden=wk_…`) в `.env`; по ним скрипт собирает ссылки.
3. Пользователь админки, чтобы показать диалоги:
   `make admin-user email=demo@example.com args="--tenant demo-beauty=viewer --tenant demo-garden=viewer"`.
4. Лимит расходов у провайдера LLM: ссылка публичная, каждый ход — платный запрос.

## Запуск

```bash
make up
make demo-public          # dev-серверы на 3000/8000 должны быть остановлены
```

Скрипт (`scripts/demo-public.sh`) поднимает два quick tunnel (web и API со случайными
адресами `https://*.trycloudflare.com`), API с `CORS_ORIGINS` = адрес веб-туннеля и
`--proxy-headers` (лимиты считаются по IP посетителя, ADR-0030), worker и production-сборку
web с `NEXT_PUBLIC_API_URL` = адрес API-туннеля, затем печатает ссылки:

```
Lumière:       https://….trycloudflare.com/demo/shop/lumiere.html?key=wk_…
Зелёный угол:  https://….trycloudflare.com/demo/shop/garden.html?key=wk_…
Админка:       https://….trycloudflare.com/admin/login
```

Ключи в ссылках можно задать явно: `DEMO_BEAUTY_KEY=… DEMO_GARDEN_KEY=… make demo-public`.
Логи — `.demo/`. Ctrl+C останавливает всё; упавший процесс останавливает остальные.

Встраивание без правки ключей: витрины лежат на origin веб-чата, а `GET /v1/public/widget`
добавляет origin своего чата (`cors_origins`) к `allowed_origins` ключа — дополнение ADR-0022.

## Сценарии показа

**Lumière** (косметика и парфюмерия):
- «Сухая кожа, шелушится зимой — что посоветуете?» — уточняющие вопросы, затем карусель кремов.
- «Подарок маме до 5000 ₽» — подбор подарка, сравнение вариантов.
- «Свежий аромат для офиса, без сладости» — подбор по нотам и поводу.
- «Хочу записаться на консультацию» — форма заявки с подтверждением.

**Зелёный угол** (садовый центр):
- «Что посадить в тени под деревьями?» — растения с фильтрами по освещению и зоне.
- «Туя буреет весной — что делать?» — ответ по базе знаний и товары ухода.
- «Нужен дизайнер на участок» — форма выезда (телефон, дата, выбор из списка).

Одно ядро: те же диалоги на двух витринах отличаются только конфигурацией тенанта
(сценарии, каталог, база знаний, формы, брендинг). После показа — диалоги в админке:
«Диалоги» тенанта, просмотр хода по шагам.

## Ограничения

- Ссылки живут, пока работает скрипт, и меняются при каждом запуске; у quick tunnel нет
  гарантий доступности — для постоянного адреса нужен деплой (P9-01).
- Лимиты частоты включены (ADR-0030): 20 ходов в минуту и 30 диалогов в час с одного IP.
- Админка доступна по той же ссылке — вход по паролю; не выдавать роли `editor` без нужды.
- Proxy страницы `/widget` ходит в API через API-туннель (`NEXT_PUBLIC_API_URL` одна на
  сервер и браузер) — iframe открывается на ~1 с медленнее, чем локально.

## Если туннель не поднимается

`туннель … не поднялся` и в `.demo/tunnel-*.log` — `failed to request quick Tunnel`
или нет строк `Registered tunnel connection`: cloudflared не достучался до Cloudflare.
Ему нужны `https://api.trycloudflare.com` и порт 7844 (UDP для QUIC, TCP для HTTP/2)
edge-серверов; HTTP-прокси (`HTTPS_PROXY`) для самого туннеля он не использует. Проверка:

```bash
timeout 5 bash -c 'echo > /dev/tcp/region1.v2.argotunnel.com/7844' && echo ok
```

- В логе `failed to request quick Tunnel: … Client.Timeout exceeded`: `api.trycloudflare.com`
  отвечает дольше таймаута cloudflared (бывает 10–20+ с). Скрипт повторяет запрос до 5 раз;
  если не помогло — запустить позже.
- Сеть режет QUIC, TCP открыт: `TUNNEL_TRANSPORT_PROTOCOL=http2 make demo-public`.
- Закрыт и TCP 7844 (сеть с обязательным прокси, WSL за прокси Windows): включить в
  VPN-клиенте Windows режим TUN/системного маршрута — при `networkingMode=mirrored` трафик
  WSL пойдёт через него.
- TCP-проверка проходит, но в логе `TLS handshake with edge error: … i/o timeout`: TUN
  принимает соединение, а дальше 7844 не пропускают — маршрутизация VPN отправляет адреса
  Cloudflare напрямую (правила «RU/частные напрямую»: в прокси `198.41.192.0/19` и
  `domain:argotunnel.com`) или сервер VPN режет нестандартные порты.
- Запасной вариант — ngrok (аккаунт и authtoken; на бесплатном плане страница-предупреждение
  перед сайтом мешает iframe и запросам к API) — скриптом не поддерживается.
