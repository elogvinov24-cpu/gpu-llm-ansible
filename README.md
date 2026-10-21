# GPU-сервер с LLM API: Ansible + NVIDIA + Docker + Ollama

Playbook устанавливает драйвер NVIDIA, Docker Engine и NVIDIA Container Toolkit, запускает Ollama, загружает модель и проверяет генерацию через API. По умолчанию: `qwen2.5:7b-instruct-q4_K_M`, Ollama 0.40.7 с закреплённым SHA-256 образа, постоянное хранилище моделей, автозапуск и периодические проверки состояния.

Ollama выбран для одного сервера и демонстрации API: он объединяет загрузку модели и HTTP-сервис в одном контейнере ([официальная инструкция](https://docs.ollama.com/docker)). При большом числе одновременных запросов стоит сравнить его с [vLLM](https://docs.vllm.ai/en/latest/serving/openai_compatible_server/) на целевой модели и нагрузке; переход не требуется для выполнения этого задания.

Версия **0.40.7** закреплена как проверенная: с ней выполнены испытания на Ubuntu 24.04.5 и NVIDIA L4, описанные в [VALIDATION.md](VALIDATION.md). SHA-256 фиксирует конкретный образ для повторного развёртывания. Обновление версии и digest выполняется после проверки на тестовом сервере; веса модели фиксируются отдельно через `llm_model_expected_digest`.

```text
site.yml                   Основной playbook развёртывания
roles/llm/                 Установка, контейнер и эксплуатационные проверки
roles/llm/defaults/main.yml Полный список настроек
inventory/hosts.example.yml Пример inventory без реальных адресов
verify.yml                 Проверка без переустановки
reboot-test.yml             Проверка восстановления после reboot
VALIDATION.md              Результаты испытаний и их ограничения
docs/                      Приёмка, эксплуатация и развитие архитектуры
```

## Требования

- **Предусмотрена поддержка Ubuntu Server 22.04/24.04 amd64 с systemd; фактически проверено на Ubuntu 24.04.5 с NVIDIA L4.** Нужна NVIDIA GPU на выделенном сервере или в VM с полноценным PCI passthrough.
- Ориентир для модели по умолчанию: 8 ГБ VRAM, 16 ГБ RAM и 30 ГБ свободного диска. Потребление зависит от контекста и нагрузки; требования к конкретной GPU и особенности дисков — в [руководстве по эксплуатации](docs/operations.md#совместимость-и-ресурсы).
- Контроллер: Linux/macOS, SSH-клиент, Python **3.11–3.13**, `venv` и `pip`. Сервер: Python 3, SSH-ключ и пользователь с `sudo` либо `root`.
- Во время установки контроллеру нужен доступ к PyPI/Ansible Galaxy, серверу — к Ubuntu, Docker, NVIDIA и реестру моделей Ollama. Используйте чистый сервер: конфликтующие контейнерные пакеты автоматически не удаляются.

## Быстрый запуск через Ansible

Все команды выполняются из корня скачанного репозитория на контроллере. Во всех примерах используется **`ubuntu`**: замените его, `SERVER_IP` и путь к SSH-ключу на свои значения одинаково в inventory и SSH-командах.

Установите зависимости и создайте inventory:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
ansible-galaxy collection install -r requirements.yml
cp inventory/hosts.example.yml inventory/hosts.yml
```

В `inventory/hosts.yml` задайте `ansible_host` и `ansible_user`; раскомментируйте `ansible_ssh_private_key_file` и укажите свой ключ. Сверьте отпечаток SSH host key по доверенному каналу при первом подключении. Затем проверьте доступ и выполните playbook:

```bash
ssh -i ~/.ssh/id_ed25519 ubuntu@SERVER_IP true
ansible -i inventory/hosts.yml gpu_servers -m ansible.builtin.ping
ansible-playbook -i inventory/hosts.yml site.yml --limit gpu01
```

Если `sudo` требует пароль, добавьте `--ask-become-pass` к `ansible-playbook`. Для нестандартного SSH-порта задайте `ansible_port` в inventory и `-p` в SSH-командах. В новом терминале снова активируйте `.venv`.

**Первый запуск может перезагрузить сервер и перезапустить Docker.** Выполняйте его в окно обслуживания. При включённом Secure Boot и необходимости установки драйвера сначала подготовьте подписанные модули/MOK: [подробности](docs/operations.md#secure-boot-и-ручная-перезагрузка).

Успех: в Ansible recap нет ошибок и недоступных хостов, проверка модели возвращает `status: ok`. Несколько серверов обрабатываются последовательно. Необязательная обёртка `llmctl` автоматизирует подготовку окружения, повторный прогон и reboot-test — см. [приёмку](docs/acceptance.md).

## API через SSH-туннель

API публикуется только на **`127.0.0.1:11434`** GPU-сервера и недоступен напрямую по внешнему IP. Локальный API Ollama [не требует авторизации](https://docs.ollama.com/api/authentication); удалённый доступ в этой конфигурации защищён SSH. Локальные пользователи сервера должны быть доверенными.

На рабочей машине откройте туннель и оставьте команду работающей:

```bash
ssh -i ~/.ssh/id_ed25519 -N -o ExitOnForwardFailure=yes \
  -L 127.0.0.1:11435:127.0.0.1:11434 ubuntu@SERVER_IP
```

В другом терминале той же машины:

```bash
curl --fail-with-body --max-time 300 \
  http://127.0.0.1:11435/api/generate \
  -H 'Content-Type: application/json' \
  -d '{
    "model": "qwen2.5:7b-instruct-q4_K_M",
    "prompt": "Объясни в трёх предложениях, зачем нужен Ansible.",
    "stream": false,
    "options": {"temperature": 0.2, "num_predict": 128, "num_ctx": 2048}
  }'
```

Ожидается HTTP 200 и JSON с непустым `response`, `done: true`, положительным `eval_count`. Текст ответа не фиксирован; `stream: false` возвращает один JSON вместо потока NDJSON ([поля API](https://docs.ollama.com/api/generate)). При смене модели или порта скорректируйте JSON и туннель; при запросе на самом сервере используйте порт `11434`. [Подключение бэкенда с другой машины или из контейнера](docs/operations.md#подключение-других-сервисов).

## Основные настройки

Переопределения задаются в [inventory/group_vars/gpu_servers.yml](inventory/group_vars/gpu_servers.yml). Полный список — в [defaults](roles/llm/defaults/main.yml), пояснения — в [эксплуатации](docs/operations.md#полный-обзор-настроек).

| Переменная | По умолчанию | Назначение |
|---|---|---|
| `llm_image` | Ollama 0.40.7 + SHA-256 | Образ контейнера |
| `llm_model` | `qwen2.5:7b-instruct-q4_K_M` | Модель с явным тегом |
| `llm_model_expected_digest` | пусто | Необязательная проверка точного digest весов |
| `llm_nvidia_driver_package` | пусто | Сохранить исправный драйвер ≥570; иначе выбор Ubuntu |
| `llm_allow_reboot` | `true` | Разрешить необходимую перезагрузку |
| `llm_gpu_device_ids` | `[]` | Все GPU или заданные UUID/индексы |
| `llm_context_length` | `2048` | Контекст по умолчанию и для smoke test |
| `llm_num_parallel` | `1` | Параллельные запросы к модели |
| `llm_port` | `11434` | Порт API на loopback хоста |
| `llm_data_dir` | `/var/lib/ollama` | Постоянное хранилище моделей |

Локальные переопределения можно хранить в исключённом из Git `settings.local.yml`. Передавайте `-e @settings.local.yml` при каждом запуске `site.yml`, `verify.yml` и `reboot-test.yml`: эти значения не сохраняются в inventory автоматически.

## Проверка результата

Повторная проверка без переустановки и перезагрузки:

```bash
ansible-playbook -i inventory/hosts.yml verify.yml --limit gpu01
```

Проверяются `nvidia-smi` на хосте и в контейнере, реальная генерация и положительный `size_vram` у выбранной модели. Это подтверждает размещение **хотя бы части модели на GPU**, а не полный GPU offload или производительность. Долю CPU/GPU можно посмотреть через `ollama ps`.

Повторный запуск `site.yml` должен дать `changed=0`. Автозапуск обеспечивают Docker restart policy и служба прогрева; health timer сохраняет состояние и метрики примерно раз в минуту. Проверка состояния сама не перезапускает зависший контейнер. Независимые алерты на ошибку, устаревший результат и отсутствие метрик описаны в [эксплуатации](docs/operations.md#автозапуск-и-мониторинг).

Без GPU выполните `./llmctl check` — понадобятся Docker CLI и Compose, но не Docker daemon. Это статические проверки и тесты с подставными ответами; они не подтверждают работу реальной GPU. Полная процедура, включая отдельную тестовую перезагрузку, — в [docs/acceptance.md](docs/acceptance.md); реальные результаты и версии — в [VALIDATION.md](VALIDATION.md).

## Ограничения и развитие

Один GPU-сервер остаётся единой точкой отказа. Полный прогон на пересозданной чистой VM с Ubuntu 24.04 и L4 выполнен; Ubuntu 22.04, Secure Boot, несколько GPU и длительная нагрузка не проверены. `--check` проверяет платформу и переменные, но не симулирует установку драйвера или inference.

Дальнейшие улучшения: несколько GPU-узлов с балансировкой по готовности модели; DCGM/node_exporter → VictoriaMetrics/Grafana; VictoriaLogs; защищённый gateway или VPN с firewall; Infisical для секретов. **Это предложения, данный playbook их не разворачивает.** [Обоснование и детали](docs/architecture.md).

Перед эксплуатацией под рабочей нагрузкой: [нагрузочные испытания, доступ нескольких клиентов и проверка других конфигураций](docs/architecture.md#подготовка-к-эксплуатации).

Обновления, откат, Secure Boot и диагностика вынесены в [docs/operations.md](docs/operations.md).
