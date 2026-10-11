# Управление вентиляторами COLORFUL N14 24 через CoolerControl

Экспериментальное решение для **COLORFUL N14 24** с:

- BIOS `2.07`;
- EC firmware `2.0`;
- контроллером `55:71`;
- ACPI-методом `\\_SB.AMW0.WMBA`;
- индексным транспортом `0x302`.

Решение намеренно завершается с ошибкой на другой модели, BIOS или DSDT. Это не универсальный профиль для всех ноутбуков Colorful.

## Что входит в репозиторий

- [`install.sh`](install.sh) — основной установщик для Arch-based систем;
- [`coolercontrol_adapter.py`](coolercontrol_adapter.py) — защищённая логика ACPI и проверки платформы;
- [`adapter_server.py`](adapter_server.py) — один постоянный процесс обработки команд вместо запуска Python на каждый опрос;
- [`src/adapter_client.c`](src/adapter_client.c) — маленький C-клиент Unix-сокета, запускаемый CoolerControl;
- [`coolercontrol-config.json`](coolercontrol-config.json) — конфигурация логического канала двух физических вентиляторов;
- [`sensor_cache.py`](sensor_cache.py) — низкозатратный CPU-кэш и suspend-aware GPU-кэш через NVML;
- [`thermal_state.py`](thermal_state.py) — атомарный формат snapshot и проверка его свежести;
- [`colorful-n14-nbfc-prepare.service`](colorful-n14-nbfc-prepare.service) — проверка платформы и подготовка разрешения ACPI;
- [`colorful-n14-sensors.service`](colorful-n14-sensors.service) — сервис кэша CPU/GPU температур;
- [`colorful-n14-adapter.service`](colorful-n14-adapter.service) — постоянный backend с thermal watchdog;
- [`build_profile.py`](build_profile.py), [`curves.json`](curves.json) и [`configs/Colorful N14 24 - Temperature Curves - GPU Command Readback.json`](configs/Colorful%20N14%2024%20-%20Temperature%20Curves%20-%20GPU%20Command%20Readback.json) — профиль и его воспроизводимая сборка для NBFC;
- [`provenance`](provenance) — исходные файлы из NBFC ZIP и описание производных изменений.

## Установка

Сначала установите CoolerControl, модуль `acpi_call` и компилятор из `base-devel` средствами своего дистрибутива. Для обычного Arch или совместимого репозитория:

```bash
sudo pacman -S --needed coolercontrol acpi_call base-devel
```

Для DKMS-ядра или LTS-ядра вместо `acpi_call` может потребоваться соответствующий пакет `acpi_call-dkms` или `acpi_call-lts`. `base-devel` нужен только на этапе установки для сборки маленького C-клиента; после установки он не участвует в опросе.

Затем выполните:

```bash
git clone https://github.com/SaYl1Cs/colorful-n14-coolercontrol.git
cd colorful-n14-coolercontrol
sudo ./install.sh
```

Установщик:

1. проверяет точную модель, BIOS, DSDT и профиль до изменения системы;
2. отказывается работать при запущенном `nbfc_service`;
3. собирает маленький C-клиент и устанавливает постоянные adapter/sensor services;
4. сохраняет посторонние устройства Custom Device и заменяет только свою запись;
5. создаёт резервную копию изменяемых системных файлов в `/var/backups/colorful-n14-control/`;
6. включает необходимые systemd units и перезапускает CoolerControl.

Старое имя [`install-coolercontrol-adapter.sh`](install-coolercontrol-adapter.sh) оставлено как совместимый wrapper и вызывает основной установщик.

## Настройка CoolerControl

После установки в интерфейсе CoolerControl:

1. создайте Graph Profile для `CPU Temperature`;
2. создайте Graph Profile для `GPU cooling input (CPU fallback while GPU sleeps)`;
3. создайте Mix Profile с операцией `Maximum`;
4. назначьте его на канал `COLORFUL N14 24 — Both Fans`.

Канал является логическим: одна команда применяется к регистрам обоих вентиляторов.

- UI `0%` преобразуется в raw `1`;
- raw `0` намеренно никогда не записывается;
- UI `100%` преобразуется в raw `70`;
- RPM GPU не имитируется: доступен только command readback, физическое вращение GPU-вентилятора не подтверждено.

## Низкая нагрузка и suspend GPU

CoolerControl продолжает опрашивать канал раз в секунду, но команда теперь является скомпилированным [`adapter_client.c`](src/adapter_client.c:1). Она только обращается к постоянному [`adapter_server.py`](adapter_server.py:1) через Unix-сокет и не создаёт Python-процесс на каждый вызов.

[`sensor_cache.py`](sensor_cache.py:1) по умолчанию работает в строгом режиме `N14_GPU_POLL=never`:

- CPU hwmon-пути находятся один раз и переоткрываются только после ошибки;
- GPU не инициализирует NVML и не вызывает NVIDIA driver;
- `runtime_status=suspended` означает **ноль обращений к NVIDIA driver**;
- состояния `suspending`, `resuming`, `unknown` и отсутствие sysfs runtime-PM также не опрашиваются.

Когда GPU спит, реальную температуру получить невозможно без её пробуждения. Поэтому канал получает явно обозначенный `GPU cooling input`: используется CPU temperature, а поле `gpu_temp` остаётся `null` и не выдаётся как настоящая температура GPU. Это безопасный fallback для Mix Profile, но не измерение температуры видеокарты.

Опциональный режим `Environment=N14_GPU_POLL=active-only` можно включить только если допустимо краткое измерение уже активной GPU. В этом режиме GPU проверяется не чаще одного раза в 5 секунд, NVML загружается лениво и закрывается сразу после измерения. При `runtime_status=suspended` NVML всё равно не вызывается; но строгий `never` является единственным режимом с гарантией нулевых обращений к драйверу.

Кэш публикуется атомарно в `/run/colorful-n14-control/temperatures.json` и использует `CLOCK_BOOTTIME`, поэтому обычное пробуждение после suspend не превращает старое значение в свежее. Отсутствующий, повреждённый или устаревший snapshot вызывает thermal watchdog: он запрашивает raw `70`, а не продолжает низкую команду.

Путь управления вентиляторами остаётся ACPI-путём с проверками DMI, DSDT, WMI, EC ID и временного permission ticket. Эти проверки нельзя заменять кэшированными значениями.

## Защитные ограничения

Профиль не является исправлением прошивки и не гарантирует безопасность эксперимента. В текущем варианте:

- управление разрешено только для одного точно проверенного DSDT;
- управление выполняется только от root;
- одновременно с NBFC управление запрещается;
- при частичной записи выполняется best-effort запрос обоих вентиляторов на raw `70`;
- при штатном завершении NBFC-профиля запрашивается raw `70`, а не неизвестный режим `Normal`;
- RPM GPU считается неизвестным;
- raw `0` и минимальный непроверенный режим не используются.

Не запускайте CoolerControl adapter и NBFC одновременно. Перед ручным использованием NBFC сначала остановите CoolerControl и связанные systemd units.

## Диагностика

```bash
systemctl --no-pager status colorful-n14-nbfc-prepare.service
systemctl --no-pager status colorful-n14-sensors.service
systemctl --no-pager status colorful-n14-adapter.service
systemctl --no-pager status coolercontrold.service
journalctl -u colorful-n14-nbfc-prepare.service -u colorful-n14-sensors.service -u colorful-n14-adapter.service -u coolercontrold.service -b
```

Если подготовка сообщает о несовпадении DSDT, не используйте `--force` и не подставляйте другой hash. Сначала нужно отдельно исследовать новую прошивку.

## NBFC-режим и разработка

NBFC-файлы сохранены для воспроизводимости исходного решения, но основной установщик их не активирует. Профиль строится офлайн:

```bash
python3 build_profile.py
python3 -m unittest discover -s tests -p 'test_*.py' -v
cc -std=c11 -O2 -Wall -Wextra -Werror src/adapter_client.c -o /tmp/colorful-n14-adapter-client
```

Тесты имитируют ACPI/EC и не заменяют осторожную проверку на реальном ноутбуке.

## Происхождение

Исходный ZIP с NBFC-профилем сохранён в [`provenance/README.original.md`](provenance/README.original.md), а производный профиль и изменения транспорта описаны в [`provenance/manifest.json`](provenance/manifest.json) и [`provenance/derivative.patch`](provenance/derivative.patch).
