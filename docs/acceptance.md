# Приёмка и проверки

[Быстрый запуск](../README.md#быстрый-запуск-через-ansible) · [Результаты испытаний](../VALIDATION.md) · [Эксплуатация](operations.md)

Все команды выполняются из корня репозитория. `llmctl` — дополнительная обёртка над теми же Ansible playbooks: она подготавливает окружение и автоматизирует последовательность проверок. Прямой запуск Ansible описан в README.

## Автоматизированная приёмка через llmctl

Скачайте репозиторий и откройте его каталог на контроллере. Подготовьте **чистую Ubuntu 24.04 VM с GPU** и добавьте SSH-ключ. В примерах замените `SERVER_IP`, имя пользователя `ubuntu` и путь к ключу на свои значения; если провайдер выдал пользователя `root`, используйте его во всех SSH-командах и в `--user`.

Сверьте отпечаток SSH host key по доверенному каналу и проверьте подключение:

```bash
ssh -i ~/.ssh/id_ed25519 ubuntu@SERVER_IP true
```

Полная приёмка включает установку, повторное применение и **тестовую перезагрузку сервера**:

```bash
./llmctl accept --host SERVER_IP --user ubuntu \
  --key ~/.ssh/id_ed25519 --reboot-test
```

`llmctl` создаёт локальное Python-окружение, устанавливает закреплённые версии инструментов и Ansible collections, создаёт приватный `inventory/generated.yml` и последовательно выполняет:

1. Ожидание SSH/Python и завершения cloud-init, проверки RAM, свободного диска и GPU.
2. Установку драйвера, необходимый reboot, Docker/Toolkit и контейнер Ollama.
3. Загрузку модели, реальный inference, проверку GPU и запись manifest установленной инфраструктуры.
4. Повторное применение с требованием **`changed=0` на каждом хосте**.
5. Дополнительную тестовую перезагрузку и проверку автоматического прогрева, API и GPU **без переустановки**.
6. Финальную проверку и сохранение JSON-отчётов в `.local/reports/<run-id>/`.

`--reboot-test` означает дополнительную перезагрузку выбранного тестового сервера. Без него `accept` проверяет развёртывание и идемпотентность, но пропускает этот тест; необходимый для драйвера reboot по-прежнему определяется `llm_allow_reboot`. Если `sudo` требует пароль, добавьте `--ask-become-pass` к каждой команде `deploy`, `verify` или `accept`. Для нестандартного SSH-порта используйте `--port` в `llmctl` и `-p` в команде `ssh`. Пароли и содержимое приватных ключей не сохраняются в inventory.

Обычное развёртывание и последующие проверки:

```bash
./llmctl deploy --host SERVER_IP --user ubuntu --key ~/.ssh/id_ed25519
./llmctl verify --inventory inventory/generated.yml
./llmctl accept --inventory inventory/generated.yml --reboot-test
./llmctl check
```

Команды выше показаны как отдельные сценарии: после успешного `accept --reboot-test` повторять `deploy` и `accept` не требуется. `verify` проверяет уже установленную модель, а `check` выполняет проверки на контроллере.

Для настроек модели используйте `inventory/group_vars/gpu_servers.yml` или отдельный файл `settings.local.yml`, исключённый из Git. Передавайте его через `--vars-file settings.local.yml` при каждом `deploy`, `verify` и `accept`: сгенерированный inventory хранит параметры SSH, но не запоминает дополнительные переменные. Файл может быть зашифрован Ansible Vault, если настроен способ получения пароля Vault, например `ANSIBLE_VAULT_PASSWORD_FILE`. Секреты нельзя помещать в открытый inventory.

При запуске с `--host` файл `inventory/generated.yml` перезаписывается для указанного сервера. Для нескольких серверов подготовьте отдельный inventory и используйте `--inventory`. `llmctl` блокирует параллельные операции из одного каталога проекта; одновременно управлять тем же сервером из нескольких каталогов или контроллеров не следует.

Скрипт не подтверждает неизвестные SSH host keys автоматически и не устанавливает Python на удалённый сервер.

## Критерии приёмки на GPU

Команда `accept --reboot-test` автоматизирует развёртывание, проверку идемпотентности и восстановление после перезагрузки. Для повторной приёмки с сохранённым inventory:

```bash
./llmctl accept --inventory inventory/generated.yml --reboot-test
```

Ожидаемые результаты:

- Нет ошибок Ansible и недоступных хостов; проверки GPU и генерации успешны.
- При повторном применении — `changed=0`: модель не скачивается повторно, контейнер не пересоздаётся. Проверочный запрос при этом выполняется заново.
- После тестового reboot API и прогрев модели восстанавливаются без повторного развёртывания.
- В каталоге отчёта создан `result.json` со статусом `passed`.

Для проверки без установки и перезагрузки:

```bash
./llmctl verify --inventory inventory/generated.yml
```

При ручном создании inventory замените путь на `inventory/hosts.yml`. Если использовали `--vars-file` или `--ask-become-pass`, передайте их и в эти команды. Дополнительно выполните API-запрос через SSH-туннель; прямой доступ к API по внешнему IP сервера должен быть закрыт.

Дополнительные серверные команды:

```bash
sudo systemctl is-enabled docker
sudo docker compose --project-directory /opt/gpu-llm ps
sudo docker compose --project-directory /opt/gpu-llm exec -T ollama nvidia-smi
sudo docker compose --project-directory /opt/gpu-llm exec -T ollama ollama ps
sudo /usr/local/bin/gpu-llm-check --config /opt/gpu-llm/probe.json
```

`size_vram > 0` доказывает размещение части модели на GPU, но не полное размещение и не скорость генерации. Долю CPU/GPU смотрите в `ollama ps`; производительность измеряйте отдельно при целевом размере контекста и конкурентной нагрузке. Пустой `ollama ps` после истечения `keep_alive` — нормальная выгрузка модели; следующий запрос загрузит её снова. При `llm_warm_on_boot: true` служба `gpu-llm-warmup.service` прогревает модель после загрузки ОС и проверяет использование VRAM.

## Отчёты и границы приёмки

Локальные отчёты `llmctl` содержат статистику Ansible и результаты проверки генерации на GPU. Они записываются с правами `0600`; `result.json` со статусом `passed` создаётся только после успеха всех выбранных стадий. Требуется свежий отчёт хотя бы об одном обработанном хосте, без ошибок и недоступных хостов, с успешной GPU-проверкой каждого обработанного хоста. Результат относится к хостам, выбранным inventory и `--limit`. Аргументы задач не включаются в callback-отчёт; вывод самого Ansible остаётся в терминале.

SHA исходного кода автоматически не записывается. Перед повторной приёмкой сохраните вывод `git rev-parse HEAD` и `git status --short` рядом с локальным отчётом; изменения рабочего дерева отметьте отдельно. Это позволит связать результаты с проверенной версией. Фактические версии установленных компонентов сохраняются на сервере в `/var/lib/gpu-llm/deployment.json`.

## Проверки без GPU

Самый короткий запуск — `./llmctl check`: инструменты проверки устанавливаются автоматически. При уже подготовленном окружении доступен `./llmctl check --use-current-env`.

Эти проверки не устанавливают драйверы, не создают контейнеры и не обращаются к серверу из inventory. Они проверяют код и конфигурацию; подтвердить работу CUDA и генерацию на GPU можно только на GPU-сервере. Для последнего шага нужен Docker CLI с Compose, без работающего Docker daemon.

Эквивалентные команды для ручного запуска:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-dev.txt
ansible-galaxy collection install -r requirements.yml

ansible-playbook -i inventory/hosts.example.yml site.yml --syntax-check
ansible-playbook -i inventory/hosts.example.yml verify.yml --syntax-check
ansible-playbook -i inventory/hosts.example.yml reboot-test.yml --syntax-check
ansible-lint site.yml verify.yml reboot-test.yml tests/render.yml
python -m unittest discover -s tests -v

ansible-playbook -i localhost, tests/render.yml
docker compose -f .cache/compose.yml config --quiet
ansible-playbook -i localhost, tests/render.yml -e '{"llm_gpu_device_ids":["0"]}'
docker compose -f .cache/compose.yml config --quiet
```

`site.yml --check` ограничен предварительной проверкой платформы и переменных и затем завершает обработку хоста. Он не симулирует установку пакетов, reboot, CUDA или inference. GitHub Actions выполняет эти проверки и проверяет синтаксис systemd units на Linux.
