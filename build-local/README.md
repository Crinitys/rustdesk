# build-local

The local Windows build of the Android APK, and everything it stages while it
runs. CI builds this fork on Ubuntu; `build.py` reproduces that here so a
Dart-only change does not need a 45-minute round trip.

```
python build-local/build.py            # all phases
python build-local/build.py --list     # what the phases are
python build-local/build.py --only build
```

## Where the tools come from

None of them live here, and none are installed on the machine. `build.py` asks
the build partition (`E:/` by default, `RUSTDESK_TOOLS_ROOT` to override) to
resolve each tool named in `use_tool.json`:

```
E:/tools.json      what tools exist, at which versions, in which directory
E:/build_env.py    resolve(name, version) -> that directory
E:/build_pre.py    prepare(use_tool.json) -> checks and readies just those tools
```

`use_tool.json` names tools and versions, never paths. Pinning a different NDK
or Flutter is a one-line edit there, provided the partition has that version.

The build needs a few things on top of stock tools -- a GNU host Rust toolchain,
the Android target, `cargo-ndk`, `flutter_rust_bridge_codegen`, the perl modules
OpenSSL's Configure wants, and the Flutter 3.24.5 dropdown patch. The `tools`
phase adds those; each step is idempotent.

## What is kept here

Only `build.py`, `use_tool.json`, this file and `.gitignore` are tracked.
Everything else is build state, ignored by git and safe to delete:

| path | what it is |
| --- | --- |
| `cache/gradle` | `GRADLE_USER_HOME`; keyed by this repository's projects, so it stays here rather than on the partition |
| `cache/downloads`, `cache/strawberry` | one-off downloads the build needs |
| `perllib` | pure-perl modules staged for Git's cut-down perl |
| `sources/hwcodec`, `sources/libsodium-sys` | patched checkouts cargo is pointed at |
| `triplets` | vcpkg triplet overlay generated for the Windows NDK |
| `out` | the aligned and signed APK |
| `keys` | a symlink to the signing keystore, which itself stays outside the repository |
| `patched-files.json`, `.tools-root` | what `cleanup` restores, and which partition the caches were built against |

The pub cache is not here: it is keyed by package and version, so it is shared
at `E:/Cache/pub` with every other repository.

## Signing

`RUSTDESK_KEYSTORE`, `RUSTDESK_KEY_ALIAS` and `RUSTDESK_KEY_PASS` override the
defaults. The keystore is reached through a symlink so the key itself never sits
inside the checkout.
