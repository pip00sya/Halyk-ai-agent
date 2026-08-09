# Halyk Covenant Agent

Автономный evidence-first агент для Halyk AI Challenge. Он читает архив документов и
реестр транзакций, выбирает действующие версии документов, строит формализованный план
расчёта, вычисляет ковенанты через `Decimal`, проверяет транзакции-улики контрфактом и
создаёт валидный `submission.json`.

Публичный контрольный прогон: **36/36 ячеек, локальный score 1.000000**.

## Локальная дизайнерская панель

В проекте есть Halyk AI Command Center — продуктовый dashboard в визуальном языке Halyk для запуска агента,
валидации `submission.json`, просмотра всех решений и их `decision_trace`, а также чтения
документации проекта. Ключи API не передаются в браузер: сервер только сообщает, настроена
ли нужная переменная окружения.

```powershell
.\run_dashboard.ps1
```

После запуска откроется `http://127.0.0.1:8765`. Альтернативный запуск:

```powershell
python dashboard_server.py
```

Панель работает на стандартной библиотеке Python и не добавляет web-зависимостей. Public
score внутри интерфейса явно помечен как калибровочный: результат `36/36` не подменяет
проверку LLM-пайплайна на приватном наборе.

## Что реализовано

- индексирование всех PDF с SHA-256 и постраничным извлечением текста;
- классификация договоров, KYC, аудита, AUP и казначейских записок;
- приоритет действующих/финальных документов над черновиками и редакциями 2024 года;
- поиск страниц с недостаточным текстовым слоем;
- vision-OCR таких страниц в приватном LLM-режиме;
- строгий Structured Output: planner/reviewer обязаны вернуть JSON по динамической схеме ковенантов;
- независимый второй LLM-проход, проверяющий первый план по исходным доказательствам;
- параллельное планирование независимых сценариев (по умолчанию 3 worker) с retry API;
- расширенный ledger-контекст: строки сценария, счёта заёмщика и явно упомянутые cross-reference транзакции;
- кэширование обычного и vision-OCR текста;
- безопасный DSL арифметики: `+`, `-`, `*`, `/`, `abs`, `min`, `max`;
- безопасные документированные `status_override` для waiver/exception/applicability без искажения `actual`;
- точные вычисления `Decimal` и округление только перед записью результата;
- контрфактическая проверка `evidence_txn_id`;
- fail-closed проверка планов: выдуманный PDF, неизвестный `txn_id`, неподдерживаемая формула или неотносящаяся улика останавливают запуск;
- строгая проверка ключей, типов и транзакций по `submission_template.json`;
- локальная копия публичной формулы scoring;
- `decision_trace.json` с фактами, формулой и происхождением каждого результата;
- отдельный режим для приватного датасета без использования публичных ответов.
- preflight до вызова модели и независимый QA-отчёт после расчёта с SHA-256 итогового файла;
- телеметрия LLM-вызовов: latency, input/output/cached tokens и число ошибок без сохранения ключа.

## Почему это не LLM-wrapper

Модель здесь не пишет финальный JSON напрямую. Она преобразует неструктурированные документы
в ограниченный семантический план. Затем отдельный детерминированный слой проверяет ссылки на
файлы и транзакции, вычисляет значения через `Decimal`, применяет только формализованные
исключения, доказывает улику контрфактом и валидирует контракт submission. Любой план с
галлюцинацией останавливается до записи ответа. Именно этот разделяемый audit trail можно показать
жюри и воспроизвести повторным запуском.

Сценарий 90-секундной защиты и боевой таймлайн приватного окна находятся в
[`WINNING_STRATEGY.md`](WINNING_STRATEGY.md).

## Быстрый публичный запуск

Из корня проекта:

```powershell
python -m pip install -r requirements.txt
python run_agent.py run `
  --data agentic-bank-public `
  --output submission.json `
  --team "НАЗВАНИЕ_КОМАНДЫ" `
  --contact-email "EMAIL"
```

Или готовым PowerShell-скриптом:

```powershell
.\run_public.ps1 -Team "НАЗВАНИЕ_КОМАНДЫ" -ContactEmail "EMAIL"
```

Проверка:

```powershell
python run_agent.py validate `
  --data agentic-bank-public `
  --submission submission.json

python run_agent.py score `
  --submission submission.json `
  --ground-truth agentic-bank-public\ground_truth.json `
  --details
```

Ожидаемый итог публичного прогона:

```text
score      = 1.0
points     = 36.0
max_points = 36.0
```

## Приватный запуск

Приватный набор не совпадёт с SHA-256 публичного набора, поэтому публичный пакет фактов
автоматически отключается. Агент запускает собственный LLM-планировщик и vision-OCR.

1. Установить Python 3.11+ и зависимости. Агент сам ищет `pdftoppm`, в том числе в локальном runtime Codex.
2. Задать ключ только в переменной окружения:

```powershell
$env:OPENAI_API_KEY = "..."
```

3. Запустить:

```powershell
.\run_private.ps1 `
  -DataDirectory ".\agentic-bank-hidden" `
  -Team "НАЗВАНИЕ_КОМАНДЫ" `
  -ContactEmail "EMAIL" `
  -Model "gpt-5.6" `
  -ReasoningEffort "high"
```

Скрипт сначала выполняет `preflight` без расхода токенов, а затем запускает до трёх независимых
сценарных planner одновременно. Число worker можно уменьшить при строгом rate limit:

```powershell
.\run_private.ps1 `
  -DataDirectory "C:\path\to\private-dataset" `
  -Team "НАЗВАНИЕ_КОМАНДЫ" `
  -ContactEmail "EMAIL" `
  -Workers 2
```

Эквивалентная команда:

```powershell
python run_agent.py run `
  --data "C:\path\to\private-dataset" `
  --output submission.json `
  --team "НАЗВАНИЕ_КОМАНДЫ" `
  --contact-email "EMAIL" `
  --mode llm `
  --model gpt-5.6 `
  --reasoning-effort high
```

По умолчанию приватный режим делает второй проверочный проход. Флаг `--single-pass` экономит
время и токены, но для финальной попытки не рекомендуется.

Ключ API не выводится и не сохраняется. Для `pdftoppm` можно явно задать путь:

```powershell
$env:PDFTOPPM = "C:\path\to\pdftoppm.exe"
```

API-запросы отправляются с `store: false`. Актуальные доступные модели следует сверять с
[официальным каталогом OpenAI](https://developers.openai.com/api/docs/models).

## Что отправлять на платформу

По форме задания оцениваемые артефакты — это два независимых пункта:

1. ссылка на GitHub с кодом агента, README и тестами;
2. созданный агентом `submission.json`, загруженный в одну из трёх попыток.

Command Center полезен как демонстрация на защите и как локальный операторский интерфейс, но
не заменяет JSON. Если до дедлайна мало времени, приоритет всегда такой: валидный private
`submission.json` → GitHub → demo URL. В GitHub нельзя добавлять `agentic-bank-hidden/`, ключи,
кэши OCR и артефакты приватного запуска — они исключены через `.gitignore`.

## Режимы

| Режим | Поведение |
|---|---|
| `auto` | Публичный fact pack используется только при точном совпадении SHA-256; иначе запускается LLM. |
| `public` | Требует точного совпадения публичного набора и останавливается при несовпадении. |
| `llm` | Всегда выполняет семантическое планирование и vision-OCR. |

`ground_truth.json` используется исключительно командой `score`. Рабочая команда `run` его
не открывает.

## Результаты и артефакты

После запуска появляются:

```text
submission.json
artifacts/
  preflight_report.json
  run_report.json
  quality_report.json
  document_manifest.json
  semantic_plans.json
  plan_review.json          # private LLM-режим
  decision_trace.json
  document_text_cache/
  vision_ocr_cache/       # только после LLM/OCR
```

- `submission.json` — файл для загрузки на сайт;
- `run_report.json` — статистика запуска и предупреждения о незаполненных метаданных;
- `preflight_report.json` — hard gates датасета, API/OCR runtime и fingerprint isolation до запуска;
- `quality_report.json` — схема, источники, Decimal, evidence counterfactual, boundary risk и SHA-256;
- `document_manifest.json` — тип, SHA-256, версия и качество текста каждого PDF;
- `semantic_plans.json` — формулы, факты, документированные исключения и источники до вычисления;
- `plan_review.json` — confirmed/corrected/fallback статус независимого второго LLM-прохода;
- `decision_trace.json` — полный технический аудит 36 решений;
- кэши ускоряют повторный запуск и не являются ответами.

## Почему evidence выбирается отдельно

Агент сначала получает verdict на полной точности. Затем по очереди исключает только
допустимых кандидатов `evidence_candidates` и повторяет расчёт. Транзакция указывается как
улика, только если контрфактическое исключение меняет verdict. Поэтому крупнейшая операция
не становится evidence автоматически.

## Защита от главных ошибок

- Публичный fact pack привязан к двум SHA-256 и не может случайно примениться к приватным данным.
- В выражениях запрещены импорт, доступ к файлам и произвольные Python-вызовы.
- Пустая сумма разрешена только при наличии факта из авторитетного документа.
- Черновики и недействующие редакции понижаются в authority ranking.
- Перед записью проверяются все сценарии, все ковенанты и точные имена полей.
- Evidence обязан существовать в реестре.
- Итоговый JSON создаёт агент; вручную формировать приватные ответы не требуется.
- `readiness_score` — внутренний операционный QA-индикатор, а не официальный балл соревнования.

## Тесты

```powershell
python -m unittest discover -s tests -v
python -m compileall -q halyk_agent tests run_agent.py score_public.py validate_submission.py dashboard_server.py
```

## Структура проекта

```text
halyk_agent/
  cli.py           команды run / validate / score
  documents.py     PDF-индекс и authority ranking
  llm.py           Responses API, vision-OCR и semantic planner
  engine.py        Decimal-расчёты и counterfactual evidence
  expressions.py   безопасный арифметический DSL
  validation.py    проверка submission.json
  scoring.py       публичный локальный scoring
  config/
    public_fact_pack.json
tests/
run_agent.py
run_public.ps1
run_private.ps1
```

## Важное правило соревнования

Codex и другие готовые агенты допустимы для разработки решения, но приватные ответы должен
создавать собственный агент. Поэтому в боевом прогоне нельзя вручную переписывать verdict,
actual или evidence после запуска. Допустим технический контроль процесса, повторный запуск
после исправления кода и проверка валидатора.
