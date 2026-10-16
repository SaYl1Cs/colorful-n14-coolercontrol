# Building the patched CoolerControl components

This is a source-only publication for the pinned N14 integration. Generated
binaries, dependency checkouts, build caches, test logs and local authorization
files are not published in Git. The included checksum manifest records the
locally verified development build; rebuilding with different tools/assets can
produce different hashes.

## Requirements

Build **as an ordinary user**, not root. Install a suitable Rust/Cargo toolchain,
Node/npm, protobuf compiler and the native build dependencies documented by the
pinned CoolerControl release. The development build used Rust/Cargo 1.99.0,
protobuf compiler 36.1 and two Cargo jobs. Allow several GB of build space.

Runtime CPU/RAM overhead is separate from these build-time requirements. Rust and
Node are not background services installed by this integration.

The Python/C offline suite needs Python 3.11+ and a C compiler. Install Lua 5.4
for the Lua tests (also required by installer preflight). PyGObject/GTK 4 and a
graphical display enable the optional widget tests; unavailable optional tests
are explicitly skipped. Runtime installation additionally requires the existing
integration, systemd, mounted debugfs, the pinned firmware, compatible native
libraries, and private local authorization described in [README.md](README.md).
A clean source checkout intentionally has no producer executables or success
receipts and must not pass the installer's release verification until built.

## Pinned sources

| Component | Repository | Commit |
| --- | --- | --- |
| CoolerControl 5.0.1 | https://gitlab.com/coolercontrol/coolercontrol | `d9201247d6f61a5ddf8e2af8faa3aba10641e340` |
| custom-device 0.2.0 | https://gitlab.com/coolercontrol/cc-plugin-custom-device | `4e7268179a521f625ef00ab2c7f3de34d2e81c74` |

Do not apply the patches to arbitrary upstream main/master revisions. The
patches include new files, schema changes and tests; both components are required.

## Rebuild procedure

From this project's directory, using Bash and **new checkout directories**:

```bash
set -euo pipefail
ROOT=$(pwd)
mkdir -p build-verification

git clone https://gitlab.com/coolercontrol/coolercontrol.git build-verification/daemon-source
git -C build-verification/daemon-source checkout --detach d9201247d6f61a5ddf8e2af8faa3aba10641e340
git -C build-verification/daemon-source apply --check "$ROOT/patches/coolercontrold-producer-provenance.patch"
git -C build-verification/daemon-source apply "$ROOT/patches/coolercontrold-producer-provenance.patch"

git clone https://gitlab.com/coolercontrol/cc-plugin-custom-device.git build-verification/plugin-source
git -C build-verification/plugin-source checkout --detach 4e7268179a521f625ef00ab2c7f3de34d2e81c74
git -C build-verification/plugin-source apply --check "$ROOT/patches/custom-device-producer-provenance.patch"
git -C build-verification/plugin-source apply "$ROOT/patches/custom-device-producer-provenance.patch"

# The daemon embeds the complete UI, not a placeholder index.html.
(
  cd build-verification/daemon-source/coolercontrol-ui
  if [ -f package-lock.json ]; then
    npm ci --ignore-scripts --no-audit --no-fund
  else
    npm install --ignore-scripts --no-audit --no-fund
  fi
  npm run build-daemon-ui
)

# Record an exit receipt only after the corresponding command succeeds.
# Remove previous receipts first so a failed rerun cannot reuse stale success.
rm -f build-verification/daemon-tests.exit build-verification/daemon-release.exit \
      build-verification/plugin-tests.exit build-verification/plugin-release.exit

CARGO_TARGET_DIR="$ROOT/build-verification/daemon-target" \
  cargo test --manifest-path build-verification/daemon-source/coolercontrold/Cargo.toml \
  -p coolercontrold --bin coolercontrold --locked -j2 \
  > build-verification/daemon-tests.log 2>&1
printf '0\n' > build-verification/daemon-tests.exit

CARGO_TARGET_DIR="$ROOT/build-verification/daemon-target" \
  cargo build --manifest-path build-verification/daemon-source/coolercontrold/Cargo.toml \
  -p coolercontrold --bin coolercontrold --release --locked -j2 \
  > build-verification/daemon-release.log 2>&1
printf '0\n' > build-verification/daemon-release.exit

CARGO_TARGET_DIR="$ROOT/build-verification/plugin-target" \
  cargo test --manifest-path build-verification/plugin-source/Cargo.toml \
  --bin custom-device --locked -j2 > build-verification/plugin-tests.log 2>&1
printf '0\n' > build-verification/plugin-tests.exit

CARGO_TARGET_DIR="$ROOT/build-verification/plugin-target" \
  cargo build --manifest-path build-verification/plugin-source/Cargo.toml \
  --bin custom-device --release --locked -j2 > build-verification/plugin-release.log 2>&1
printf '0\n' > build-verification/plugin-release.exit

PYTHONPATH=.:tests python3 -B -m unittest discover -s tests -v
```

Inspect the actual logs and resolve failures; do not fabricate successful test
receipts. These commands document the source/build flow, not an assertion that
fresh dependency downloads reproduce identical binaries on every distribution.

After reviewing successful tests and the trusted patched source, generate a
checksum manifest for **your** build. Do not use this step to bless downloaded
unverified executables. The literal `nbfc-n14-control/` prefix is the installer's
manifest format, independent of your checkout directory name:

```bash
python3 - <<'PY'
from pathlib import Path
import hashlib
paths = (
    'build-verification/daemon-target/release/coolercontrold',
    'build-verification/plugin-target/release/custom-device',
    'patches/coolercontrold-producer-provenance.patch',
    'patches/custom-device-producer-provenance.patch',
)
rows = []
for name in paths:
    with Path(name).open('rb') as stream:
        digest = hashlib.file_digest(stream, 'sha256').hexdigest()
    rows.append(f'{digest}  nbfc-n14-control/{name}\n')
Path('patches/BUILD-SHA256SUMS').write_text(''.join(rows))
PY
```

Checksums establish artifact consistency, not independent software trust. The
installer also checks ELF compatibility, dynamic dependencies, firmware identity,
active service prerequisites, the private token, the actual profile bindings,
and post-start control health.

## Deployment

Review the upgrade prerequisites in [README.md](README.md). This installer is
not a bootstrap for a machine without the existing guarded N14 integration.
Run preflight before installation; do not install only one side of the protocol:

```bash
sudo bash install.sh --preflight
sudo bash install.sh
```

The installer prints the complete backup path and exact rollback command before
stopping services. It leaves distribution binaries, global EC write permissions,
NVIDIA configuration, LACT profiles and unrelated plugins unchanged.

Firmware restrictions and known limits are documented in
[DEPLOYMENT-VERIFICATION.md](DEPLOYMENT-VERIFICATION.md),
[QUIET-LIFECYCLE.md](QUIET-LIFECYCLE.md), and
[patches/PRODUCER-CONTRACT.md](patches/PRODUCER-CONTRACT.md).
