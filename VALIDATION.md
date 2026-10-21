# Проверка решения

Тестовая конфигурация: Ubuntu 24.04.5 LTS, NVIDIA L4, 8 vCPU и 32 ГБ RAM. Драйвер, Docker, NVIDIA Container Toolkit, контейнер и службы настроены через Ansible.

## Происхождение результатов

Полная приёмка выполнена 22.10.2026 на пересозданной чистой VM: стадии deploy, idempotency, reboot и verify завершились в 01:35–01:37 МСК. На сервере до запуска отсутствовали Docker и `nvidia-smi`, было 47 ГБ свободного диска. Весь стек установлен через Ansible без ручной установки пакетов.

Опубликованные исходники развёртывания побайтово совпадают с испытанными. Сводный SHA-256 файлов без Markdown: `511163f8a62c79bf5f8c08be37ab32dc066b049c77fbceb71e23b1948c446595`. Он вычислен по отслеживаемым путям в лексикографическом порядке: для каждого файла последовательно добавлены UTF-8 путь, нулевой байт, содержимое и нулевой байт. Состав и индивидуальные хеши сохранены вместе с локальными отчётами.

Дополнительно выполнены контрольный `verify` на GPU и API-запрос через SSH-туннель. Статические проверки выполнялись на macOS с Python 3.13.0, ansible-core 2.19.14, ansible-lint 26.10.1 и Compose CLI 2.24.5-desktop.1. [GitHub Actions](https://github.com/elogvinov24-cpu/gpu-llm-ansible/actions) запускает проверки на Ubuntu 24.04 с Python 3.12; каждый запуск связан с проверяемым коммитом.

## Проверенная конфигурация

| Компонент | Версия |
|---|---|
| Ядро Ubuntu | 6.8.0-147-generic |
| NVIDIA driver | 595.91.07 |
| Docker Engine | 29.9.2 |
| Docker Compose на сервере | 5.6.1 |
| NVIDIA Container Toolkit | 1.20.1-1 |
| Ollama | 0.40.7, image закреплён по SHA-256 |
| Модель | qwen2.5:7b-instruct-q4_K_M |
| Контекст / параллелизм | 2048 / 1 |

Образ Ollama: `sha256:2e6cc92ffa43b4b1a2e70e315407e00ac97ec125afc71277d73c7efe01c4f31b`.

Digest модели из GPU-проверки: `845dbda0ea48ed749caafd9e6037047aa19acfcfd82e704d7ca97d631a0b697e`.

## Результаты приёмки

Команда для повторения приёмки; замените пользователя и ключ на свои:

```bash
./llmctl accept --host SERVER_IP --user ubuntu --key ~/.ssh/id_ed25519 --reboot-test
```

| Проверка | Результат |
|---|---|
| Установка драйвера и необходимая перезагрузка | PASS |
| Docker, NVIDIA runtime и GPU внутри контейнера | PASS |
| Загрузка модели и реальная генерация через API | PASS |
| Размещение выбранной модели на GPU | PASS: `gpu_resident_bytes=4635859352` после генерации; подтверждает размещение хотя бы части модели на GPU |
| Повторное применение playbook | PASS: `changed=0`, failed=0, unreachable=0 |
| Дополнительный reboot без повторного deploy | PASS: контейнер, автоматический warmup и API восстановились |
| Отдельный `verify.yml` после reboot | PASS |
| systemd unit syntax и выполнение health timer | PASS |
| Запрос с рабочей машины через SSH-туннель | PASS: HTTP 200, непустой ответ, `done=true` |
| Привязка API к loopback | PASS: проверены Docker bindings и listener `127.0.0.1:11434` |

Статистика Ansible: deploy — `ok=76, changed=27`; idempotency — `ok=63, changed=0`; reboot — `ok=10, changed=1`; verify — `ok=6, changed=0`. На всех стадиях `failed=0, unreachable=0`. Выполнены две перезагрузки: после установки драйвера и отдельная для проверки автозапуска.

После завершения приёмки API-запрос с контроллера через SSH-туннель вернул HTTP 200, `done=true`, непустой ответ и 89 сгенерированных токенов. Запрос напрямую к публичному адресу на порту API получил `Connection refused`; это наблюдение с одного контроллера, а не проверка правил внешнего firewall.

В финальном `verify` получены `status: ok` и `generated_tokens: 10`. Положительный `size_vram` не доказывает полный GPU offload или производительность. Команда `llmctl accept` сохраняет JSON-отчёты в `.local/reports/`; процедура и критерии описаны в [docs/acceptance.md](docs/acceptance.md).

## Локальные проверки

- Syntax-check всех трёх playbooks: PASS.
- ansible-lint: 0 failures, 0 warnings.
- 35 Python-тестов: PASS. Проверяют отказ при CPU fallback, несовпадении модели/digest, пустом или незавершённом ответе; исключают ложный успех при отсутствии хостов, устаревшем отчёте и изменениях на повторном прогоне. Также проверяют нормальный простой модели, сохранение намеренной остановки контейнера, права inventory/отчётов, обработку cloud-init и конфигурацию Compose/systemd. Это проверки логики и шаблонов с подставными ответами, а не драйвера или CUDA.
- Рендер Compose для всех GPU и выбранного `device_ids`, `docker compose config --quiet`: PASS.
- `llmctl setup` из нового каталога без virtualenv/collections: PASS; повторный setup не переустанавливал зависимости.
- `llmctl check` и проверка реального сервера из автоматически подготовленного окружения: PASS.

GitHub Actions выполняет проверки без доступа к GPU-серверу, включая проверку синтаксиса systemd units на Linux; состав шагов — в [workflow](.github/workflows/validate.yml). Проверка подготовки окружения через `llmctl setup` и GPU-испытания выполнялись отдельно от CI.

## Границы проверки

Полный прогон повторён на пересозданной чистой VM с L4 и Ubuntu 24.04 для исходников с указанной контрольной суммой. Не проверялись Ubuntu 22.04, включённый Secure Boot/MOK, несколько GPU, MIG/NVSwitch, другие модели, длительная нагрузка, обновление ядра/драйвера и аварийное восстановление.

API доступен локально и через SSH-туннель. HTTPS gateway, API-ключи, VPN/firewall, DCGM, VictoriaMetrics/Grafana, VictoriaLogs, Infisical и HA описаны в [предложениях по архитектуре](docs/architecture.md) и данным playbook не разворачиваются.
