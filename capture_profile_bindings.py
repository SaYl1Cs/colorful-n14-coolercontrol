#!/usr/bin/env python3
"""Read-only binding discovery. Prints IDs only; never writes files or profiles.

Defaults are the previously observed local CPU/GPU/Max profile UIDs. Source and
channel IDs are resolved from the local CustomDevice catalog, not display labels.
Parent deployment stores the resulting identity-only object root:root 0600.
"""
import argparse
import json
import os
import resource
import sys

from authorize_coolercontrol import TOKEN_FILE
from profile_selector import LocalTransport
import control_policy

CPU = '240b496f-027a-4e71-a978-47703a14c9ee'
GPU = 'f8997ee4-df30-4f2b-8b44-7eb08e5d0398'
MIX = '5732965b-667c-4f05-84f4-d0ead7cc0c43'


def capture(api, profile_ids=None):
    profiles = control_policy.catalog(api.request('GET', '/profiles'), 'profiles')
    functions = control_policy.catalog(api.request('GET', '/functions'), 'functions')
    devices = control_policy.catalog(api.request('GET', '/devices'), 'devices')
    candidates = []
    for uid, device in devices.items():
        info = device.get('info', {})
        if (info.get('driver_info', {}).get('name') == 'CustomDevice'
                and {'cpu_temp', 'gpu_temp'} <= set(info.get('temps', {}))
                and 'both_fans' in info.get('channels', {})):
            candidates.append(uid)
    control_policy.require(len(candidates) == 1, 'ambiguous or missing local EC device')
    device = candidates[0]
    binding = {'profiles': profile_ids or dict(cpu=CPU, gpu=GPU, mix=MIX),
               'sources': {role: dict(device_uid=device, temp_name=role + '_temp') for role in ('cpu', 'gpu')},
               'target': dict(device_uid=device, channel_name='both_fans')}
    # Invalid/foreign sources are NOT rewritten to match the new binding.
    control_policy.live_policy(binding, profiles, functions, devices)
    return binding


def main():
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cpu-profile', default=CPU)
    parser.add_argument('--gpu-profile', default=GPU)
    parser.add_argument('--mix-profile', default=MIX)
    args = parser.parse_args()
    if os.geteuid() != 0:
        raise ValueError('root required to read enrolled credential')
    value = capture(LocalTransport(TOKEN_FILE), dict(cpu=args.cpu_profile, gpu=args.gpu_profile, mix=args.mix_profile))
    print(json.dumps(value, indent=2))


if __name__ == '__main__':
    try:
        main()
    except Exception:
        print('Binding discovery failed; no profiles, credentials or files changed', file=sys.stderr)
        raise SystemExit(1)
