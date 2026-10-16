"""Read-only sampler prerequisites; preserve the global EC write policy.

O_RDONLY is the sampler's access mode, NOT a requirement that ec_sys globally
forbid writes. Never unload/load modules here, change write_support, rewrite
modprobe/boot options, or request a reboot. Identity is checked by prepare.
"""
from pathlib import Path
import argparse
import os


class PrerequisiteError(RuntimeError):
    pass


def check(root=Path('/')):
    root = Path(root)
    if not (root / 'sys/module/ec_sys').is_dir():
        raise PrerequisiteError('ec_sys must already be available; no live module changes attempted')
    try:
        mounts = (root / 'proc/self/mountinfo').read_text().splitlines()
        mounted = False
        for line in mounts:
            fields = line.split()
            if '-' in fields:
                separator = fields.index('-')
                if (len(fields) > separator + 2 and separator >= 6
                        and fields[4] == '/sys/kernel/debug'
                        and fields[separator + 1] == 'debugfs'):
                    mounted = True
        if not mounted:
            raise PrerequisiteError('debugfs must be mounted at /sys/kernel/debug')
        if (root / 'run/nbfc_service.pid').exists():
            raise PrerequisiteError('NBFC PID file exists; resolve it before deployment')
        for process in (root / 'proc').iterdir():
            if process.name.isdecimal():
                try:
                    if (process / 'comm').read_text().strip() == 'nbfc_service':
                        raise PrerequisiteError('nbfc_service is running')
                except (FileNotFoundError, ProcessLookupError):
                    pass
        # Verify access without reading or writing even one EC byte.
        fd = os.open(root / 'sys/kernel/debug/ec/ec0/io',
                     os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
        os.close(fd)
    except OSError as exc:
        raise PrerequisiteError('cannot verify mounted debugfs, NBFC absence, or read-only EC access') from exc


ASSETS = {
    '/etc/modules-load.d/colorful-n14-control.conf': 'acpi_call\nec_sys\n',
    '/etc/systemd/system/colorful-n14-ec-readonly.service': '''[Unit]
Description=Verify N14 read-only sampler access (global EC write policy unchanged)
Requires=sys-kernel-debug.mount
After=sys-kernel-debug.mount systemd-modules-load.service
Before=colorful-n14-sensors.service colorful-n14-adapter.service coolercontrold.service
Conflicts=nbfc_service.service

[Service]
Type=oneshot
ExecStart=/usr/bin/python3 -B /usr/local/share/colorful-n14-control/install_prerequisites.py
RemainAfterExit=no
''',
    '/etc/systemd/system/colorful-n14-sensors.service.d/70-ec-readonly.conf': '''[Unit]
Requires=colorful-n14-ec-readonly.service
After=colorful-n14-ec-readonly.service

[Service]
ExecStartPre=/usr/bin/python3 -B /usr/local/share/colorful-n14-control/install_prerequisites.py
''',
}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    try:
        check()
    except PrerequisiteError as exc:
        parser.exit(1, f'STOP: preflight prerequisite refusal: {exc}\n')
