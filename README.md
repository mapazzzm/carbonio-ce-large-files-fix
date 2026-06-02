# carbonio-ce-large-files-fix

[<sup>ru</sup> Русский](#русский) | [<sup>en</sup> English](#english)

---

## Русский

Набор патчей, превращающий **Carbonio Community Edition** в почтовый сервер, который умеет работать с файлами **произвольного** размера в Carbonio Files (upload, download, public-link, smart-link в письмах).

Протестировано на `carbonio-files-ce 1.1.2` + `carbonio-storages-ce 1.0.16` (Carbonio CE 26.x на Ubuntu Noble). Проверено на файлах больше 7 ГБ.

### Что чинит

| Баг в стоковой Carbonio CE | Симптом | Решение в репо |
|---|---|---|
| `BlobService.verifyBlobExists` ложно возвращает `false` для блобов > ~700 МБ | UI показывает `500 Internal Server Error` после 100% загрузки | **2-байтный JAR-патч** (`ifne` → `ifge`) пропускает ложный throw |
| Sidecar `envoy_files` имеет дефолтный `stream_idle_timeout=5min` в HCM | Медленные загрузки (слабый канал, паузы) получают `408` от envoy | **nginx-обход**: `/services/files/upload` идёт напрямую на Java `127.78.0.2:10000` |
| Жёсткий 2 ГиБ-лимит (2³¹) на тело запроса у некоторых reverse-прокси | Загрузка зависает на ~2 ГиБ независимо от размера файла | **Chunked-upload sidecar + JS-патч**: браузер режет файлы >200 МБ на куски по 100 МБ; sidecar собирает и шлёт в Java |
| `PublicBlobController` прокси через OkHttp с дефолтным read timeout; storages закрывает idle TCP в середине стрима → `EOFException` | Скачивание public-link файлов > ~3 ГБ падает с `500` | **Sidecar endpoint `public-download`**: ищет токен в `carbonio-files-db` через peer-auth, отдаёт blob через `sendfile()` (zero-copy, Range) |
| Java heap `-Xmx4096m` слишком мал для стриминга больших блобов | OOM при UI-скачивании | **Heap bump** до 12 ГБ |
| Smart-link mail attach использует `fetch()` (не XHR), не идёт через chunked | Большие вложения через "Загрузить как смарт-ссылку" упирается в 2 ГБ лимит reverse-прокси | **`window.__cuFetch`** перехватывает fetch в `mails-edit-view`, направляет в chunked |
| Архитектурный шум: каждое Carbonio-приложение монтирует свой `PostHogProvider` | Console забит `[PostHog.js] posthog was already loaded elsewhere` | **Surgical patch** убирает именно эту строку warn в shell-ui |

Каждое исправление независимо — можно ставить выборочно (например, chunked upload не нужен если у вас нет reverse-прокси с лимитом размера тела запроса и канал быстрый).

### Состав

```
patches/blobservice_patch.py   – 2-байтный JAR-патч, идемпотентный, с бэкапом
patches/postgres_role.sql      – Создаёт роль 'zextras' с read-only на link/node/revision
patches/java_heap_bump.sh      – -Xmx4096m → -Xmx12288m в /usr/bin/carbonio-files
sidecar/carbonio-chunked-upload.py        – Python aiohttp sidecar: chunked upload + public-link download
sidecar/carbonio-chunked-upload.service   – systemd unit (User=zextras)
js/chunked-upload-prefix.js    – IIFE prepend к carbonio-files-ui 659.*.chunk.js и mails-edit-view.*.chunk.js
nginx/location-blocks.conf     – 3 location-блока (через proxy_pass с URI, без rewrite) добавляются в оба шаблона nginx
install.sh                     – ставит всё с бэкапами (идемпотентный, ru/en по $LANG)
uninstall.sh                   – восстанавливает из бэкапов
```

### Фиксированные параметры (без user-config — чтобы установка была простой)

| Параметр | Значение | Почему |
|---|---|---|
| Размер чанка | **100 МБ** | Sweet spot для 50-1000 Мбит/с; ниже типичного 2 ГиБ-лимита reverse-прокси; единичный retry не дорого |
| Параллелизм | **5 крупных, до 16 мелких** | Carbonio LIMIT поднят 1→16. Файлы <50 МБ идут свободно (Chrome HTTP/1.1 sock cap ~6 регулирует естественно). Файлы 50-200 МБ и chunked гейтятся в prefix до 5, чтобы не делить bandwidth/память на много потоков |
| Порог chunked | **200 МБ** | Файлы меньше идут стоковым single-request путём (без оверхеда sidecar) |
| Throttle прогресса | **250 мс** | Без него React делает 50+ ре-рендеров/сек и "Загрузки" зависают |
| Java heap | **12 ГБ** | Скачивание 7+ ГБ через Java-стрим комфортно (4 ГБ был на грани); 12 ГБ оставляет запас на 32 ГБ ОЗУ |

### Поддерживается / не поддерживается

- **Поддерживается**: Carbonio CE 26.x, Ubuntu Noble (22.04+). Single-node Carbonio. PostgreSQL с `local all all peer` в `pg_hba.conf` (дефолт Carbonio).
- **Не тестировалось**: multi-node Carbonio (sidecar должен располагаться на узле carbonio-files и иметь доступ к диску storages).

### Установка

```bash
curl -sL https://github.com/mapazzzm/carbonio-ce-large-files-fix/archive/main.tar.gz | tar xz && sudo carbonio-ce-large-files-fix-main/install.sh
```

Для обновления — повторно выполнить ту же команду (install.sh идемпотентный: повторно применяет патчи только если они слетели, бэкапы сохраняются в `/root/backups/carbonio-ce-large-files-fix/<timestamp>/`).

install.sh автоматически выбирает локаль (ru/en) из `$LANG`. Что делает:
1. Проверяет версии (`carbonio-files-ce`, `carbonio-storages-ce`, `carbonio-files-ui`).
2. Бэкапит всё что меняет в `/root/backups/carbonio-ce-large-files-fix/<timestamp>/`.
3. Применяет JAR-патч.
4. Настраивает роль PostgreSQL `zextras`.
5. Устанавливает и запускает sidecar-сервис.
6. Вставляет nginx location-блоки в оба шаблона, regen и reload.
7. Prepend-ит JS IIFE к `659.*.chunk.js` и `mails-edit-view.*.chunk.js` (хэши автоопределяются).
8. Поднимает Java heap.

Общее время выполнения ~30 секунд. Carbonio перезапускается один раз (carbonio-files). Пользователям нужен hard refresh (Ctrl+F5).

### Удаление

```bash
curl -sL https://raw.githubusercontent.com/mapazzzm/carbonio-ce-large-files-fix/main/uninstall.sh | sudo bash
```

Восстанавливает JAR, `/usr/bin/carbonio-files`, оба nginx-шаблона и JS-чанки из `/root/backups/`. Удаляет sidecar-сервис. Роль PostgreSQL и granti оставлены (вреда нет, при желании удаляются вручную).

### Операционные детали

- **Бэкапы**: каждое выполнение install.sh создаёт новый каталог `/root/backups/carbonio-ce-large-files-fix/<timestamp>/`. Ротация: хранятся 10 последних, более старые удаляются автоматически.
- **Stale-session reaper в sidecar**: фоновый таск раз в 10 минут чистит записи о сессиях в памяти и orphan-каталоги в `/opt/zextras/chunked-upload-tmp/`, чьи сессии неактивны более 24 часов. Защита от утечки памяти при сбоях клиента.
- **Defense-in-depth в public-link download**: путь к blob-файлу `resolve()`-ится и проверяется на принадлежность `BLOBS_ROOT` — даже если в БД попадёт некорректный `node_id`, выход за пределы хранилища даст 403.

### Как chunked upload выглядит в браузере

1. Пользователь перетаскивает файл > 200 МБ в Files.
2. JS IIFE перехватывает `XMLHttpRequest`, который carbonio-files-ui шлёт на `POST /services/files/upload`.
3. Файл нарезается на куски 100 МБ через `File.slice()`.
4. До 5 кусков параллельно летят как `POST /services/files/upload-chunked?session=<uuid>&part=N&total=M` с оригинальными заголовками `Filename`, `ParentId`, `Cookie`.
5. nginx направляет их в sidecar (`127.0.0.1:5795`).
6. Sidecar пишет каждый part в `/opt/zextras/chunked-upload-tmp/<session>/<N>.partial`, потом atomic rename в `<N>.bin`. **Atomic rename критичен** — без него финализация видит наполовину записанные файлы и шлёт обрезанный blob.
7. Когда все parts на месте, sidecar берёт asyncio lock (один таск побеждает, опоздавшие получают закэшированный ответ), стримит части по порядку через `aiofiles` и POST-ит склеенное тело в Java carbonio-files на `127.78.0.2:10000/upload`.
8. Java обрабатывает это как обычный upload (создаёт node, пишет blob в storages, отдаёт JSON).
9. Sidecar форвардит ответ Java через nginx в HTTP-ответ последнего chunk.
10. JS IIFE подсовывает этот JSON в *оригинальный* XHR через fake `load` event, чтобы остальной код Carbonio UI работал без модификаций.

**Плавающий progress-overlay** (sticky bottom-right) показывает прогресс сразу — не надо ждать lazy-загрузки "Загрузки". Overlay подбирает стиль с открытого Carbonio-диалога (модальные окна), чтобы выглядеть как родной элемент UI. Скрыт пока сессия в статусе queued (например, ждёт подтверждения smart-link). `pointer-events: none` — overlay никогда не перехватывает клики по снэкбару Carbonio в том же углу.

### Как работает public-link download

1. Пользователь открывает `https://<host>/services/files/public/link/download/<TOKEN>`.
2. nginx направляет в sidecar `/public-download/<TOKEN>`.
3. Sidecar запрашивает `link → node → revision` через asyncpg + peer-auth (без пароля — peer к роли `zextras`).
4. Проверяет `expire_at`; для ссылок с `access_code` возвращает 401 (пользователь получает Java-промпт как раньше).
5. Стримит blob из `/opt/zextras/carbonio-storages/blobs/<2chars>/<node>-<version>` через `aiohttp.web.FileResponse`. Sendfile + поддержка Range.

Java не задействована — нет OOM, нет EOFException.

### Smart-link mail attach

Когда пользователь прикрепляет большой файл к письму и выбирает "Загрузить как смарт-ссылку", `mails-edit-view.chunk.js` шлёт upload через `fetch()`, а не XHR. Наш `window.__cuFetch` — адаптер fetch → XHR, поэтому такие загрузки тоже идут через chunked sidecar и видны в overlay.

После завершения загрузки делается тройная точечная инвалидация Apollo-кэша:
- `cache.evict({id: 'ROOT_QUERY', fieldName: 'getNode', args: {node_id: parentId}})` — запись результата запроса
- `cache.evict({id: 'Folder:<parentId>'})` — нормализованная сущность папки целиком
- `cache.modify` с DELETE по `storeFieldName` — fallback на случай нестандартного формата

Apollo автоматически рефетчит при следующем render — файл появляется на Главной Files без F5. Остальной кэш (соседние папки, дерево навигации, `getPath`) не трогается.

### Известное поведение Carbonio CE (не патчим)

**Batch attach → batch smart-link**: если пользователь прикрепляет к письму несколько файлов, и **суммарный** размер всех вложений превышает `zimbraMtaMaxMessageSize`, Carbonio предлагает преобразовать **всю пачку** в смарт-ссылки. Маленькие файлы (даже 500 КБ) из batch'а тоже улетают в Files и вставляются как ссылки. Это упрощённая логика mails-edit-view ("всё или ничего" по batch), а не нашего патча. Если хотите чтобы маленькие файлы остались inline-вложениями — добавляйте их отдельно от больших.

### Лицензия

AGPL-3.0.

---

## English

A bundle of patches that makes **Carbonio Community Edition** handle files of arbitrary size in Carbonio Files (upload, download, public-link, smart-link in mail).

Tested against `carbonio-files-ce 1.1.2` + `carbonio-storages-ce 1.0.16` (Carbonio CE 26.x on Ubuntu Noble). Verified on files larger than 7 GB.

### What it fixes

| Bug in stock Carbonio CE | Symptom | This repo |
|---|---|---|
| `BlobService.verifyBlobExists` falsely returns `false` for blobs > ~700 MB | UI shows `500 Internal Server Error` after upload reaches 100% | **2-byte JAR patch** (`ifne` → `ifge`) skips the false-positive throw |
| `envoy_files` sidecar HCM has default `stream_idle_timeout=5min` | Slow uploads (low bandwidth, long pauses) get `408` from envoy | **nginx bypass**: route `/services/files/upload` direct to Java on `127.78.0.2:10000` |
| Hard 2 GiB (`2³¹`) request-body limit in some reverse proxies | Upload stalls at ~2 GiB regardless of file size | **Chunked upload sidecar + JS patch**: browser splits >200 MB into 100 MB chunks; sidecar stitches them and POSTs to Java |
| `PublicBlobController` proxies storages via OkHttp with default read timeout; storages closes idle TCP mid-stream → `EOFException` | Public-link download of files > ~3 GB fails with `500` | **Sidecar `public-download` endpoint**: looks up token in `carbonio-files-db` via peer-auth, sends blob with `sendfile()` (zero-copy, Range support) |
| Java heap `-Xmx4096m` undersized for streaming large blobs | OOM during UI download | **Heap bump** to 12 GB |
| Smart-link mail attach uses `fetch()` (not XHR), bypasses chunked | Large attachments via "Convert to smart link" still hit the reverse-proxy 2 GB limit | **`window.__cuFetch`** intercepts the fetch in `mails-edit-view`, routes through chunked |
| Architectural noise: each Carbonio app mounts its own `PostHogProvider` | Console spammed with `[PostHog.js] posthog was already loaded elsewhere` | **Surgical patch** removes just that warn line in shell-ui |

Each fix is independent and can be skipped (e.g. you don't need chunked upload if your deployment has no reverse proxy with a request-body size limit and your link is fast enough to keep envoy from idling out).

### What's in here

```
patches/blobservice_patch.py   – 2-byte JAR patch, idempotent, backs JAR up
patches/postgres_role.sql      – Creates the 'zextras' DB role with read-only on link/node/revision
patches/java_heap_bump.sh      – -Xmx4096m → -Xmx12288m in /usr/bin/carbonio-files
sidecar/carbonio-chunked-upload.py        – Python aiohttp sidecar; handles chunked upload + public-link download
sidecar/carbonio-chunked-upload.service   – systemd unit (User=zextras)
js/chunked-upload-prefix.js    – IIFE prepended to carbonio-files-ui 659.*.chunk.js and mails-edit-view.*.chunk.js
nginx/location-blocks.conf     – 3 nginx location blocks (proxy_pass with URI, no rewrite) added to both templates
install.sh                     – installs everything with backups (idempotent, ru/en by $LANG)
uninstall.sh                   – restores from backups
```

### Defaults (deliberately not configurable to keep installer simple)

| Knob | Value | Why |
|---|---|---|
| Chunk size | **100 MB** | Sweet spot 50-1000 Mbit/s; below the typical 2 GiB reverse-proxy body limit; small enough that one retry isn't expensive |
| Parallelism | **5 large, up to 16 small** | Carbonio LIMIT bumped 1→16. Files <50 MB run free (browser HTTP/1.1 socket cap ~6 throttles naturally). 50-200 MB and chunked uploads are gated in the prefix to 5 so bandwidth/memory isn't split across too many concurrent transfers |
| Threshold | **200 MB** | Files below this go through the stock single-request path (no overhead from sidecar) |
| Progress throttle | **250 ms** | Without it React re-renders 50+ times/sec and the Uploads view stalls |
| Java heap | **12 GB** | 7+ GB downloads via Java stream comfortably (4 GB was tight); 12 GB still leaves room on a 32 GB box |

### Supported / not supported

- **Supported**: Carbonio CE 26.x, Ubuntu Noble (22.04+). Single-node Carbonio. PostgreSQL with `local all all peer` in `pg_hba.conf` (Carbonio default).
- **Not tested**: multi-node Carbonio (sidecar would need to live on the carbonio-files node with access to the storages disk).

### Install

```bash
curl -sL https://github.com/mapazzzm/carbonio-ce-large-files-fix/archive/main.tar.gz | tar xz && sudo carbonio-ce-large-files-fix-main/install.sh
```

install.sh auto-picks the locale (ru/en) from `$LANG`. The installer:
1. Verifies versions (`carbonio-files-ce`, `carbonio-storages-ce`, `carbonio-files-ui`).
2. Backs up everything it touches under `/root/backups/carbonio-ce-large-files-fix/<timestamp>/`.
3. Applies the JAR patch.
4. Sets up the PostgreSQL `zextras` role.
5. Installs and starts the sidecar service.
6. Inserts the nginx location blocks into both templates, regens config, reloads nginx.
7. Prepends the JS IIFE to `659.*.chunk.js` and `mails-edit-view.*.chunk.js` (hashes auto-detected).
8. Bumps the Java heap.

Total runtime ~30 seconds. Carbonio is restarted once (carbonio-files). Browser users may need a hard refresh (Ctrl+F5).

### Uninstall

```bash
curl -sL https://raw.githubusercontent.com/mapazzzm/carbonio-ce-large-files-fix/main/uninstall.sh | sudo bash
```

Restores the JAR, `/usr/bin/carbonio-files`, both nginx templates and the JS chunks from `/root/backups/`. Removes the sidecar service. PostgreSQL role and grants left intact (no harm, easy to drop manually).

### Operational notes

- **Backups**: each install.sh run creates `/root/backups/carbonio-ce-large-files-fix/<timestamp>/`. The 10 most recent are kept; older ones are pruned automatically.
- **Stale-session reaper in sidecar**: a background task every 10 minutes drops in-memory session bookkeeping and orphan dirs under `/opt/zextras/chunked-upload-tmp/` whose last activity is older than 24 hours. Protects against memory leaks if a client crashes mid-upload.
- **Defense-in-depth in public-link download**: the blob path is `resolve()`-d and verified to stay inside `BLOBS_ROOT` — even if a malformed `node_id` somehow lands in the DB, escaping the storages root returns 403.

### How does the chunked upload look in the browser

1. User drags a file > 200 MB into Files.
2. JS IIFE intercepts the `XMLHttpRequest` Carbonio Files-ui sends to `POST /services/files/upload`.
3. File is sliced into 100 MB chunks via `File.slice()`.
4. Up to 5 chunks fly in parallel as `POST /services/files/upload-chunked?session=<uuid>&part=N&total=M`, each with the original `Filename`, `ParentId`, `Cookie` headers forwarded.
5. nginx routes them to the sidecar (`127.0.0.1:5795`).
6. Sidecar writes each part to `/opt/zextras/chunked-upload-tmp/<session>/<N>.partial`, then atomically renames to `<N>.bin`. **The atomic rename is essential** — without it, the finalisation logic sees half-written files and ships a truncated blob.
7. When all parts are present, sidecar takes an asyncio lock (one task wins, late callers get the cached response), streams the parts in order with `aiofiles`, and POSTs the merged body to Java carbonio-files on `127.78.0.2:10000/upload`.
8. Java treats it like any other upload (creates node, writes blob to storages, returns JSON).
9. Sidecar forwards Java's reply back through nginx to the last chunk's HTTP response.
10. The JS IIFE feeds that JSON to the *original* XHR via fake `load` event so the rest of Carbonio's UI code keeps working unmodified.

A **floating progress overlay** (sticky bottom-right) shows progress immediately — no need to wait for the lazy-loaded Uploads view. The overlay samples styles from a live Carbonio dialog (modal/dropdown) so it blends in as a native component. It's hidden while sessions are queued (e.g. waiting for the "Convert to smart link?" confirmation). `pointer-events: none` so it never intercepts clicks on Carbonio's own snackbar appearing in the same corner.

### How public-link download works

1. User opens `https://<host>/services/files/public/link/download/<TOKEN>`.
2. nginx routes it to sidecar `/public-download/<TOKEN>`.
3. Sidecar queries `link → node → revision` via asyncpg + peer auth (no password — peer to the `zextras` role).
4. Validates `expire_at`; for `access_code`-protected links returns 401 (the user gets a Java prompt as before).
5. Streams the blob from `/opt/zextras/carbonio-storages/blobs/<2chars>/<node>-<version>` using `aiohttp.web.FileResponse`. Sendfile + Range support included.

No Java touch — no OOM, no EOFException.

### Smart-link mail attach

When the user drops a large file into a mail compose and picks "Convert to smart link", `mails-edit-view.chunk.js` uploads via `fetch()` rather than XHR. Our `window.__cuFetch` is a fetch→XHR adapter, so those uploads also route through the chunked sidecar and show in the overlay.

After upload completes, we do a triple targeted Apollo cache invalidation:
- `cache.evict({id: 'ROOT_QUERY', fieldName: 'getNode', args: {node_id: parentId}})` — the query result entry
- `cache.evict({id: 'Folder:<parentId>'})` — the normalised entity itself
- `cache.modify` DELETE by `storeFieldName` — fallback for non-standard formats

Apollo automatically refetches on the next render — the new file shows up on Files Home without F5. Sibling cache entries (other folders, navigation tree, `getPath`) are left intact.

### Known Carbonio CE behaviour (not patched)

**Batch attach → batch smart-link**: if the user attaches multiple files to a mail, and the **combined** size of all attachments exceeds `zimbraMtaMaxMessageSize`, Carbonio offers to convert the **whole batch** to smart-links. Small files (even 500 KB) in the batch are also uploaded to Files and inserted as links. This is mails-edit-view's simplified "all-or-nothing" batch logic, not our patch. If you want small files to remain inline attachments, add them separately from large ones.

### License

AGPL-3.0.
