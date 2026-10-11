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
- [`coolercontrol_adapter.py`](coolercontrol_adapter.py) — защищённый Custom Device adapter CoolerControl;
- [`coolercontrol-config.json`](coolercontrol-config.json) — конфигурация логического канала двух физических вентиляторов;
- [`sensor_cache.py`](sensor_cache.py) — постоянный кэш температур через NVML без запуска `nvidia-smi` при каждом опросе;
- [`colorful-n14-nbfc-prepare.service`](colorful-n14-nbfc-prepare.service) — проверка платформы и подготовка разрешения ACPI;
- [`colorful-n14-sensors.service`](colorful-n14-sensors.service) — сервис кэша CPU/GPU температур;
- [`build_profile.py`](build_profile.py), [`curves.json`](curves.json) и [`configs/Colorful N14 24 - Temperature Curves - GPU Command Readback.json`](configs/Colorful%20N14%2024%20-%20Temperature%20Curves%20-%20GPU%20Command%20Readback.json) — профиль и его воспроизводимая сборка для NBFC;
- [`provenance`](provenance) — исходные файлы из NBFC ZIP и описание производных изменений.

## Установка

Сначала установите CoolerControl и модуль `acpi_call` средствами своего дистрибутива. Для обычного Arch или совместимого репозитория:

```bash
sudo pacman -S --needed coolercontrol acpi_call
```

Для DKMS-ядра или LTS-ядра вместо `acpi_call` может потребоваться соответствующий пакет `acpi_call-dkms` или `acpi_call-lts`.

Затем выполните:

```bash
git clone <URL-ЭТОГО-РЕПОЗИТОРИЯ>
cd nbfc-n14-control
sudo ./install.sh
```

Установщик:

1. проверяет точную модель, BIOS, DSDT и профиль до изменения системы;
2. отказывается работать при запущенном `nbfc_service`;
3. устанавливает ACPI-проверку, sensor-cache и adapter;
4. устанавливает конфигурацию CoolerControl;
5. создаёт резервную копию изменяемых системных файлов в `/var/backups/colorful-n14-control/`;
6. включает необходимые systemd units и перезапускает CoolerControl.

Старое имя [`install-coolercontrol-adapter.sh`](install-coolercontrol-adapter.sh) оставлено как совместимый wrapper и вызывает основной установщик.

## Настройка CoolerControl

После установки в интерфейсе CoolerControl:

1. создайте Graph Profile для `CPU Temperature`;
2. создайте Graph Profile для `GPU Temperature`;
3. создайте Mix Profile с операцией `Maximum`;
4. назначьте его на канал `COLORFUL N14 24 — Both Fans`.

Канал является логическим: одна команда применяется к регистрам обоих вентиляторов.

- UI `0%` преобразуется в raw `1`;
- raw `0` намеренно никогда не записывается;
- UI `100%` преобразуется в raw `70`;
- RPM GPU не имитируется: доступен только command readback, физическое вращение GPU-вентилятора не подтверждено.

## Почему больше нет постоянных запусков `nvidia-smi`

CoolerControl продолжает обновлять график раз в секунду, но [`sensor_cache.py`](sensor_cache.py) остаётся одним запущенным процессом и обращается непосредственно к `libnvidia-ml.so.1` через NVML. Он обновляет root-owned файлы в `/run/colorful-n14-control/`.

Команды [`coolercontrol_adapter.py`](coolercontrol_adapter.py) читают только свежий кэш. Если кэш отсутствует, повреждён или старше пяти секунд, команда завершается с ошибкой вместо использования устаревшей температуры. При остановке sensor service временные файлы удаляются.

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
systemctl --no-pager status coolercontrold.service
journalctl -u colorful-n14-nbfc-prepare.service -u colorful-n14-sensors.service -u coolercontrold.service -b
```

Если подготовка сообщает о несовпадении DSDT, не используйте `--force` и не подставляйте другой hash. Сначала нужно отдельно исследовать новую прошивку.

## NBFC-режим и разработка

NBFC-файлы сохранены для воспроизводимости исходного решения, но основной установщик их не активирует. Профиль строится офлайн:

```bash
python3 build_profile.py
python3 -m unittest discover -s tests -p 'test_*.py' -v
```

Тесты имитируют ACPI/EC и не заменяют осторожную проверку на реальном ноутбуке.

## Происхождение

Исходный ZIP с NBFC-профилем сохранён в [`provenance/README.original.md`](provenance/README.original.md), а производный профиль и изменения транспорта описаны в [`provenance/manifest.json`](provenance/manifest.json) и [`provenance/derivative.patch`](provenance/derivative.patch).
