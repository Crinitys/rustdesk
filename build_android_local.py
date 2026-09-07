#!/usr/bin/env python3
"""Build the Android (arm64) APK of this fork locally, on a Windows host.

CI builds on Ubuntu; this reproduces it here so a Dart-only change does not need
a 45-minute round trip. It assumes nothing but a freshly installed Windows: the
`install` phase puts every missing tool on the machine, and every phase is
idempotent, so re-running is cheap.

Phases, in order:

  1. install   put the required programs and libraries on the machine
  2. verify    check what is installed: versions, paths, presence
  3. env       assemble the build environment and smoke-test it
  4. prebuild  native dependencies, generated bindings, librustdesk.so
  5. build     the Flutter app, packaged and signed
  6. cleanup   drop intermediates, restore the files the build patched

Usage:

    python build_android_local.py                     # all phases
    python build_android_local.py --list              # show phases
    python build_android_local.py --only build        # rerun one phase
    python build_android_local.py --start-at prebuild # resume from a phase
    python build_android_local.py --force             # ignore "already done"
    python build_android_local.py --install-apk       # adb install at the end
    python build_android_local.py --keep-patches      # skip the restore
"""

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import urllib.request
import zipfile
from pathlib import Path

# --------------------------------------------------------------------------- config

REPO = Path(__file__).resolve().parent
FLUTTER_DIR = REPO / "flutter"

# Everything this build downloads, compiles or stages lives under one directory
# outside the repo, so none of it leaks into the rest of the machine and it can
# be deleted in one go. Override with RUSTDESK_BUILDENV.
BUILDENV = Path(os.environ.get("RUSTDESK_BUILDENV", REPO.parent / "rustdesk-buildenv"))
TOOLCHAINS = BUILDENV / "toolchains"
DOWNLOADS = BUILDENV / "caches/downloads"
PERLLIB = BUILDENV / "perllib"
OUT_DIR = BUILDENV / "out"

ABI = "arm64-v8a"
RUST_TARGET = "aarch64-linux-android"
VCPKG_TRIPLET = "arm64-android"
HOST_VCPKG_TRIPLET = "x64-windows-static"

# Pinned to what the Android CI uses. Newer Flutter (3.44) drops the v1 plugin
# embedding that the pinned file_picker / flutter_plugin_android_lifecycle still
# rely on, and its migrator rewrites tracked gradle files behind your back.
FLUTTER_VERSION = "3.24.5"
FLUTTER_ZIP_URL = ("https://storage.googleapis.com/flutter_infra_release/releases/stable/"
                   f"windows/flutter_windows_{FLUTTER_VERSION}-stable.zip")
FLUTTER_SDK = TOOLCHAINS / "flutter"

VCPKG_ROOT = Path(os.environ.get("VCPKG_ROOT", TOOLCHAINS / "vcpkg"))
VCPKG_COMMIT = "9e593bb18ea69cc5095e012465dcd675a822ed0d"

FRB_VERSION = "1.80.1"  # must match `flutter_rust_bridge` in flutter/pubspec.yaml
HWCODEC_URL = "https://github.com/rustdesk-org/hwcodec"
HWCODEC_REV = "778df1f99597722473b29443bac22ae6c23946fe"
HWCODEC_DIR = BUILDENV / "sources/hwcodec"

# Default to rustup's own toolchain. The in-repo stage1 compiler is built against
# an OLLVM fork and the librustdesk.so it produces crashes the app on launch
# (confirmed by swapping only that .so into an otherwise identical APK).
# Set RUSTDESK_RUST_TOOLCHAIN=<name> to opt into a linked custom toolchain.
RUST_TOOLCHAIN = os.environ.get("RUSTDESK_RUST_TOOLCHAIN") or None

GIT_USR_BIN = Path(r"C:\Program Files\Git\usr\bin")
# LLVM is here only so ffigen/bindgen have a libclang.dll; the Android SDK ships
# libclang_android.dll, which they cannot load. Kept inside the build env rather
# than installed system-wide, where its clang would shadow other projects'.
LLVM_VERSION = "22.1.8"
LLVM_DIR = TOOLCHAINS / "llvm"
LLVM_URL = (f"https://github.com/llvm/llvm-project/releases/download/llvmorg-{LLVM_VERSION}"
            f"/clang+llvm-{LLVM_VERSION}-x86_64-pc-windows-msvc.tar.xz")

# Strawberry Perl is never executed; it is only a source of the pure-perl modules
# Git's cut-down perl lacks. The portable zip avoids an installer that would put
# its gcc/make/ld on the system PATH, where other projects would pick them up.
STRAWBERRY_VERSION = "5.40.2.1"
STRAWBERRY_DIR = TOOLCHAINS / "strawberry"
STRAWBERRY_URL = (f"https://strawberryperl.com/download/{STRAWBERRY_VERSION}"
                  f"/strawberry-perl-{STRAWBERRY_VERSION}-64bit-portable.zip")

KEYSTORE = Path(os.environ.get("RUSTDESK_KEYSTORE",
                               r"C:\Users\Thurion\.android-keys\rustdesk-personal.keystore"))
KEY_ALIAS = os.environ.get("RUSTDESK_KEY_ALIAS", "rustdesk-personal")
KEY_PASS = os.environ.get("RUSTDESK_KEY_PASS", "rustdesk123")

SODIUM_DIR = BUILDENV / "libs/sodium"
OUT_APK = OUT_DIR / f"rustdesk-{ABI}-signed.apk"
ALIGNED_APK = OUT_DIR / f"rustdesk-{ABI}-aligned.apk"

# Perl modules OpenSSL's Configure needs that Git's cut-down perl omits. They are
# pure perl, so they can be borrowed from Strawberry; XS modules cannot.
PERL_MODULES = {
    "ExtUtils::MakeMaker": "ExtUtils",
    "Pod::Usage": "Pod",
    "Text::Wrap": "Text",
    "Getopt::Long": "Getopt",
    "Locale::Maketext::Simple": "Locale",
}

# --------------------------------------------------------------------------- helpers


def log(msg):
    print(f"\n=== {msg}", flush=True)


def info(msg):
    print(f"    {msg}", flush=True)


def fail(msg):
    raise SystemExit(f"ERROR: {msg}")


def msys_path(path):
    """Git's perl is an msys program: it splits PERL5LIB on ':' and resolves
    '/d/...' through the msys drive mounts, so a 'D:\...' value would be cut in
    half at the colon."""
    path = Path(path).resolve()
    return "/" + path.drive[0].lower() + path.as_posix()[2:]


def perl_ok(module):
    env = dict(os.environ, PERL5LIB=msys_path(PERLLIB))
    return subprocess.run([str(GIT_USR_BIN / "perl.exe"), f"-M{module}", "-e", "1"],
                          capture_output=True, env=env).returncode == 0


def find_ndk():
    if os.environ.get("ANDROID_NDK_HOME"):
        return Path(os.environ["ANDROID_NDK_HOME"])
    root = Path(os.environ["LOCALAPPDATA"]) / "Android/Sdk/ndk"
    versions = sorted(p for p in root.glob("*") if p.is_dir())
    if not versions:
        fail(f"no NDK under {root}; install one from Android Studio's SDK Manager")
    return versions[-1]


def find_llvm():
    """A stock LLVM on purpose. The OLLVM forks next to this repo can also drive
    ffigen/bindgen, but keeping the whole toolchain stock removes a variable from
    an already fragile cross-compile. Override with LLVM_PATH."""
    candidates = [Path(os.environ["LLVM_PATH"])] if os.environ.get("LLVM_PATH") else []
    candidates += [LLVM_DIR, Path(r"C:\Program Files\LLVM")]
    for c in candidates:
        if (c / "bin/libclang.dll").exists():
            return c
    return None


NDK = find_ndk()
NDK_LLVM = NDK / "toolchains/llvm/prebuilt/windows-x86_64"
NDK_BIN = NDK_LLVM / "bin"
NDK_MAKE_BIN = NDK / "prebuilt/windows-x86_64/bin"
NDK_SYSROOT_LIB = NDK_LLVM / "sysroot/usr/lib" / RUST_TARGET


def build_env():
    """The environment every build command runs under.

    PATH entries are native Windows paths: cargo spawns Win32 processes, which
    cannot resolve msys-style '/c/...' entries.
    """
    env = dict(os.environ)
    env["VCPKG_ROOT"] = str(VCPKG_ROOT)
    env["ANDROID_NDK_HOME"] = str(NDK)
    env["ANDROID_NDK_ROOT"] = str(NDK)

    # OpenSSL's Configure demands a perl that emits Unix-like paths and rejects
    # Strawberry, so drop Strawberry and let Git's perl win. Git's usr\bin is
    # appended, never prepended: its msys DLLs shadow system ones and make
    # msbuild die with 0xc0000142 (DLL init failed).
    entries = [p for p in env["PATH"].split(os.pathsep)
               if p and "strawberry" not in p.lower()]
    env["PATH"] = os.pathsep.join(
        [str(FLUTTER_SDK / "bin")] + entries + [str(NDK_MAKE_BIN), str(GIT_USR_BIN)])

    if RUST_TOOLCHAIN:
        env["RUSTUP_TOOLCHAIN"] = RUST_TOOLCHAIN

    # Modules OpenSSL's Configure needs, staged rather than installed into Git.
    env["PERL5LIB"] = msys_path(PERLLIB)
    # Keep this build's Gradle and pub caches out of the shared per-user ones,
    # so the pinned Flutter cannot disturb other projects on this machine.
    env["GRADLE_USER_HOME"] = str(BUILDENV / "caches/gradle")
    env["PUB_CACHE"] = str(BUILDENV / "caches/pub")

    # libsodium-sys builds from source via autotools on Linux CI, which cannot run
    # here, so point it at prebuilt libs. Do not set SODIUM_STATIC (deprecated,
    # the crate panics) or SODIUM_SHARED (would force dynamic linking).
    env["SODIUM_LIB_DIR"] = str(SODIUM_DIR)

    # bindgen loads a system libclang whose resource dir does not match the NDK
    # sysroot, so compiler-provided headers like stddef.h go missing. CPATH is
    # read by clang itself and therefore reaches libclang;
    # BINDGEN_EXTRA_CLANG_ARGS would not work, because cargo-ndk sets the
    # target-specific BINDGEN_EXTRA_CLANG_ARGS_<triple> and bindgen then ignores
    # the generic one.
    builtin = sorted((NDK_LLVM / "lib/clang").glob("*/include"))
    if builtin:
        env["CPATH"] = os.pathsep.join(
            [str(builtin[-1])] + ([env["CPATH"]] if env.get("CPATH") else []))

    llvm = find_llvm()
    if llvm:
        env["LIBCLANG_PATH"] = str(llvm / "bin")

    # The vcpkg C libraries are built by NDK clang, which emits LSE
    # outline-atomics calls (__aarch64_ldadd*). Those live in compiler-rt, which
    # rustc's link line does not pull in on its own.
    builtins = sorted((NDK_LLVM / "lib/clang").glob(
        "*/lib/linux/libclang_rt.builtins-aarch64-android.a"))
    if builtins:
        env["RUSTFLAGS"] = f'{env.get("RUSTFLAGS", "")} -Clink-arg={builtins[-1]}'.strip()

    return env


def run(cmd, cwd=REPO, env=None, check=True):
    env = env or build_env()
    printable = " ".join(str(c) for c in cmd)
    print(f"$ {printable}", flush=True)
    cmd = [str(c) for c in cmd]
    # CreateProcess cannot launch .bat/.cmd (flutter, apksigner) directly.
    resolved = shutil.which(cmd[0], path=env["PATH"]) or cmd[0]
    prefix = ["cmd", "/c", resolved] if resolved.lower().endswith((".bat", ".cmd")) else [resolved]
    r = subprocess.run(prefix + cmd[1:], cwd=str(cwd), env=env)
    if check and r.returncode != 0:
        fail(f"command failed (exit {r.returncode}): {printable}")
    return r.returncode


def capture(cmd, cwd=REPO, env=None):
    env = env or build_env()
    cmd = [str(c) for c in cmd]
    resolved = shutil.which(cmd[0], path=env["PATH"]) or cmd[0]
    prefix = ["cmd", "/c", resolved] if resolved.lower().endswith((".bat", ".cmd")) else [resolved]
    r = subprocess.run(prefix + cmd[1:], cwd=str(cwd), env=env,
                       capture_output=True, text=True, errors="replace")
    return r.returncode, (r.stdout or "") + (r.stderr or "")


def which(name):
    return shutil.which(name, path=build_env()["PATH"])


def patch_file(path, transform, note):
    """Edit a tracked file and remember that we did, so `cleanup` can put it back.

    The file is recorded even when the patch is already applied, so a resumed or
    repeated run still leaves something for cleanup to restore. Restoring goes
    through git rather than a saved copy, which keeps line endings and
    .gitattributes handling correct.
    """
    rel = path.relative_to(REPO).as_posix()
    original = path.read_text(encoding="utf-8")
    patched = transform(original)
    if patched != original:
        path.write_text(patched, encoding="utf-8")
        info(f"patched {rel} ({note})")
    _record(rel, _sha(path))
    return patched != original


def restore_patched_files():
    tracked = _manifest()
    for rel in BUILD_BYPRODUCTS:
        tracked.setdefault(rel, _sha(REPO / rel))
    if not tracked:
        return
    for rel, digest in sorted(tracked.items()):
        path = REPO / rel
        if not path.exists():
            continue
        code, _ = capture(["git", "diff", "--quiet", "--", rel], env=dict(os.environ))
        if code == 0:
            continue  # already matches HEAD
        if _sha(path) != digest:
            info(f"skip restore of {rel}: changed since the build touched it")
            continue
        run(["git", "checkout", "--", rel], env=dict(os.environ))
        info(f"restored {rel}")
    MANIFEST.unlink(missing_ok=True)


# --------------------------------------------------------------------------- 1. install


def phase_install(args):
    """Put everything the build needs on the machine."""
    _install_llvm()
    _install_perl_modules()
    _install_vcpkg()
    _install_flutter_sdk(args.force)

    if not which("cargo"):
        fail("cargo not found; install Rust from https://rustup.rs")
    if not which("cargo-ndk"):
        run(["cargo", "install", "cargo-ndk", "--locked"])
    if not which("flutter_rust_bridge_codegen"):
        run(["cargo", "install", "flutter_rust_bridge_codegen",
             "--version", FRB_VERSION, "--features", "uuid", "--locked"])
    if RUST_TOOLCHAIN:
        info(f"using custom rust toolchain {RUST_TOOLCHAIN}; skipping `rustup target add`")
    else:
        run(["rustup", "target", "add", RUST_TARGET], env=dict(os.environ))
        if capture(["rustfmt", "--version"])[0] != 0:
            run(["rustup", "component", "add", "rustfmt"], env=dict(os.environ))


def _download(url, dest):
    if dest.exists():
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    info(f"downloading {url}")
    try:
        urllib.request.urlretrieve(url, dest)
    except Exception as exc:
        fail(f"download failed: {url}\n  {exc}")
    return dest


def _install_llvm():
    if find_llvm():
        return
    import tarfile
    archive = _download(LLVM_URL, DOWNLOADS / Path(LLVM_URL).name)
    info(f"extracting LLVM into {LLVM_DIR}")
    with tarfile.open(archive) as tf:
        tf.extractall(TOOLCHAINS)
    extracted = next(TOOLCHAINS.glob("clang+llvm-*"), None)
    if extracted:
        extracted.rename(LLVM_DIR)
    if not find_llvm():
        fail(f"libclang.dll still missing after extracting {archive}")


def _strawberry_lib():
    for candidate in (STRAWBERRY_DIR / "perl/lib", Path(r"C:\Strawberry\perl\lib")):
        if candidate.is_dir():
            return candidate
    archive = _download(STRAWBERRY_URL, DOWNLOADS / Path(STRAWBERRY_URL).name)
    info(f"extracting Strawberry Perl into {STRAWBERRY_DIR}")
    with zipfile.ZipFile(archive) as z:
        z.extractall(STRAWBERRY_DIR)
    lib = STRAWBERRY_DIR / "perl/lib"
    if not lib.is_dir():
        fail(f"{lib} not found after extracting {archive}")
    return lib


def _install_perl_modules():
    """Stage the modules under PERLLIB and reach them through PERL5LIB, rather
    than writing into the Git for Windows installation."""
    perl = GIT_USR_BIN / "perl.exe"
    if not perl.exists():
        fail(f"{perl} not found; install Git for Windows")
    missing = [(m, d) for m, d in PERL_MODULES.items() if not perl_ok(m)]
    if not missing:
        return
    strawberry_lib = _strawberry_lib()
    for module, subdir in missing:
        src = strawberry_lib / subdir
        if not src.is_dir():
            fail(f"{module} missing from Git's perl and not found at {src}")
        dst = PERLLIB / subdir
        dst.mkdir(parents=True, exist_ok=True)
        shutil.copytree(src, dst, dirs_exist_ok=True)
        info(f"staged {subdir} in {PERLLIB} for {module}")
        if not perl_ok(module):
            fail(f"{module} is still not loadable by {perl}")


def _install_vcpkg():
    if (VCPKG_ROOT / "vcpkg.exe").exists():
        return
    if not (VCPKG_ROOT / ".git").exists():
        run(["git", "clone", "https://github.com/microsoft/vcpkg.git", str(VCPKG_ROOT)])
    run(["git", "checkout", VCPKG_COMMIT], cwd=VCPKG_ROOT)
    run([str(VCPKG_ROOT / "bootstrap-vcpkg.bat"), "-disableMetrics"], cwd=VCPKG_ROOT)


def _install_flutter_sdk(force):
    if (FLUTTER_SDK / "bin/flutter.bat").exists() and not force:
        info(f"Flutter SDK already at {FLUTTER_SDK}")
    else:
        zip_path = DOWNLOADS / "flutter.zip"
        DOWNLOADS.mkdir(parents=True, exist_ok=True)
        FLUTTER_SDK.parent.mkdir(parents=True, exist_ok=True)
        if not zip_path.exists():
            info(f"downloading Flutter {FLUTTER_VERSION} (~1 GB)")
            urllib.request.urlretrieve(FLUTTER_ZIP_URL, zip_path)
        info("extracting Flutter SDK")
        with zipfile.ZipFile(zip_path) as z:
            z.extractall(FLUTTER_SDK.parent)
    # The CI applies this to the SDK itself for 3.24.5; do the same.
    patch = REPO / ".github/patches/flutter_3.24.4_dropdown_menu_enableFilter.diff"
    if subprocess.run(["git", "apply", "--reverse", "--check", str(patch)],
                      cwd=str(FLUTTER_SDK), capture_output=True).returncode != 0:
        run(["git", "apply", str(patch)], cwd=FLUTTER_SDK)
        info("applied the dropdown_menu patch to the Flutter SDK")


# --------------------------------------------------------------------------- 2. verify


def phase_verify(args):
    """Check what is installed. Reports everything before failing, so one run
    tells you the whole list of what is missing."""
    problems = []

    for name in ("cmake", "ninja", "nasm", "git", "java", "cargo", "rustup", "cargo-ndk"):
        path = which(name)
        print(f"    {name:<24} {path or 'MISSING'}")
        if not path:
            problems.append(f"{name} not on PATH")

    for label, path in (("NDK", NDK), ("NDK clang", NDK_BIN), ("NDK make", NDK_MAKE_BIN),
                        ("NDK sysroot libs", NDK_SYSROOT_LIB), ("vcpkg", VCPKG_ROOT / "vcpkg.exe"),
                        ("Flutter SDK", FLUTTER_SDK / "bin/flutter.bat"), ("keystore", KEYSTORE)):
        ok = path.exists()
        print(f"    {label:<24} {path} {'' if ok else '  <-- MISSING'}")
        if not ok:
            problems.append(f"{label} missing at {path}")

    llvm = find_llvm()
    print(f"    {'libclang':<24} {llvm / 'bin/libclang.dll' if llvm else 'MISSING'}")
    if not llvm:
        problems.append("libclang.dll not found (winget install --id LLVM.LLVM -e)")

    for label, tool in (("zipalign", "zipalign.exe"), ("apksigner", "apksigner.bat")):
        found = _build_tool(tool, required=False)
        print(f"    {label:<24} {found or 'MISSING'}")
        if not found:
            problems.append(f"{label} missing from the Android SDK build-tools")

    code, out = capture(["flutter", "--version"])
    version = next((ln for ln in out.splitlines() if ln.startswith("Flutter ")), out.strip()[:80])
    print(f"    {'flutter version':<24} {version}")
    if code != 0 or FLUTTER_VERSION not in version:
        problems.append(f"flutter is not {FLUTTER_VERSION}: {version}")

    code, out = capture(["rustc", "--version"])
    print(f"    {'rustc':<24} {out.strip()[:80]}")
    if code != 0:
        problems.append("rustc not runnable")
    elif RUST_TOOLCHAIN is None:
        code, out = capture(["rustc", "--print", "target-list"])
        if RUST_TARGET not in out:
            problems.append(f"{RUST_TARGET} target not installed (rustup target add {RUST_TARGET})")

    perl = GIT_USR_BIN / "perl.exe"
    missing_modules = [m for m in PERL_MODULES if not perl_ok(m)]
    print(f"    {'git perl modules':<24} {'OK' if not missing_modules else ', '.join(missing_modules)}")
    if missing_modules:
        problems.append(f"Git perl is missing {', '.join(missing_modules)}")

    if problems:
        fail("verification failed:\n  - " + "\n  - ".join(problems))
    info("all checks passed")


def _build_tool(name, required=True):
    root = Path(os.environ["LOCALAPPDATA"]) / "Android/Sdk/build-tools"
    versions = sorted(p for p in root.glob("*") if (p / name).exists())
    if not versions:
        if required:
            fail(f"{name} not found under {root}")
        return None
    return versions[-1] / name


# --------------------------------------------------------------------------- 3. env


def phase_env(args):
    """Assemble the build environment and smoke-test that it actually works.

    Every value here exists to work around a specific host/target confusion; the
    checks below are the ones that failed first when a value was wrong.
    """
    env = build_env()
    for key in ("VCPKG_ROOT", "ANDROID_NDK_HOME", "SODIUM_LIB_DIR", "CPATH",
                "LIBCLANG_PATH", "RUSTFLAGS", "RUSTUP_TOOLCHAIN"):
        if env.get(key):
            print(f"    {key:<18} {env[key]}")
    print(f"    {'PATH (head)':<18} {os.pathsep.join(env['PATH'].split(os.pathsep)[:3])} ...")

    checks = [
        ("perl", ["perl", "-e", "print 'ok'"], "ok"),
        ("perl modules", ["perl", "-MExtUtils::MakeMaker", "-MPod::Usage", "-e", "print 'ok'"], "ok"),
        ("make", ["make", "--version"], "Make"),
        ("flutter", ["flutter", "--version"], FLUTTER_VERSION),
        ("cargo", ["cargo", "--version"], "cargo"),
        ("cargo-ndk", ["cargo", "ndk", "--version"], "cargo-ndk"),
        ("ndk clang", [str(NDK_BIN / "clang.exe"), "--version"], "clang"),
    ]
    problems = []
    for label, cmd, expect in checks:
        code, out = capture(cmd, env=env)
        first = out.strip().splitlines()[0][:70] if out.strip() else ""
        ok = code == 0 and expect in out
        print(f"    {label:<18} {'OK  ' if ok else 'FAIL'} {first}")
        if not ok:
            problems.append(f"{label}: {first or f'exit {code}'}")

    # Whichever perl wins on PATH must be the msys one; a native Windows perl
    # makes OpenSSL's Configure bail out with "doesn't produce Unix like paths".
    perl_path = shutil.which("perl", path=env["PATH"]) or ""
    print(f"    {'perl resolves to':<18} {perl_path}")
    if "strawberry" in perl_path.lower():
        problems.append("perl resolves to Strawberry; OpenSSL's Configure will reject it")

    if problems:
        fail("environment check failed:\n  - " + "\n  - ".join(problems))
    info("environment is usable")


# --------------------------------------------------------------------------- 4. prebuild


def phase_prebuild(args):
    """Everything the Flutter build consumes: native libs, bindings, the .so."""
    _vcpkg_dependencies(args.force)
    _stage_sodium()
    _patch_hwcodec()
    _generate_bridge(args.force)
    _build_rust_lib(args.force)
    _stage_jni_libs()


def _overlay_triplet():
    """aom's cmake defaults CMAKE_ASM_COMPILER to a bare `as`, which the Windows
    NDK does not ship. Override it in a local overlay so the repo's own triplet,
    used by the Linux CI, stays untouched."""
    overlay = BUILDENV / "triplets"
    overlay.mkdir(parents=True, exist_ok=True)
    base = (REPO / "res/vcpkg-triplets" / f"{VCPKG_TRIPLET}.cmake").read_text()
    base = base.replace(
        "set(VCPKG_CMAKE_CONFIGURE_OPTIONS -DANDROID_ABI=arm64-v8a)",
        "set(VCPKG_CMAKE_CONFIGURE_OPTIONS -DANDROID_ABI=arm64-v8a\n"
        f"    -DCMAKE_ASM_COMPILER={(NDK_BIN / 'clang.exe').as_posix()}\n"
        "    -DCMAKE_ASM_COMPILER_TARGET=aarch64-none-linux-android21)")
    (overlay / f"{VCPKG_TRIPLET}.cmake").write_text(base)
    return overlay


def _vcpkg_dependencies(force):
    exe = VCPKG_ROOT / "vcpkg.exe"
    lib_dir = VCPKG_ROOT / "installed" / VCPKG_TRIPLET / "lib"
    overlay = _overlay_triplet()
    install_root = (VCPKG_ROOT / "installed").as_posix()

    if (lib_dir / "libopus.a").exists() and not force:
        info("vcpkg manifest dependencies already installed")
    else:
        run([exe, "install", "--triplet", VCPKG_TRIPLET,
             f"--x-install-root={install_root}", f"--overlay-triplets={overlay.as_posix()}"])

    # libsodium is not in the repo manifest (Linux builds it from source), so
    # install it in classic mode, from VCPKG_ROOT so no manifest is picked up.
    if not (lib_dir / "libsodium.a").exists():
        run([exe, "install", f"libsodium:{VCPKG_TRIPLET}",
             f"--x-install-root={install_root}", f"--overlay-triplets={overlay.as_posix()}"],
            cwd=VCPKG_ROOT)
    host_lib = VCPKG_ROOT / "installed" / HOST_VCPKG_TRIPLET / "lib" / "libsodium.lib"
    if not host_lib.exists():
        run([exe, "install", f"libsodium:{HOST_VCPKG_TRIPLET}",
             f"--x-install-root={install_root}"], cwd=VCPKG_ROOT)


def _stage_sodium():
    """libsodium-sys's build script runs on the host, so its cfg!(target_env) sees
    msvc and it emits the link name "libsodium" for the host build scripts AND
    for the Android target. One directory satisfies both: link.exe resolves that
    name to libsodium.lib, the Android lld to liblibsodium.a."""
    SODIUM_DIR.mkdir(parents=True, exist_ok=True)
    shutil.copy2(VCPKG_ROOT / "installed" / VCPKG_TRIPLET / "lib/libsodium.a",
                 SODIUM_DIR / "liblibsodium.a")
    shutil.copy2(VCPKG_ROOT / "installed" / HOST_VCPKG_TRIPLET / "lib/libsodium.lib",
                 SODIUM_DIR / "libsodium.lib")
    info(f"sodium libraries staged in {SODIUM_DIR}")


def _patch_hwcodec():
    """hwcodec gates its Windows-only sources on `#[cfg(windows)]`, which in a
    build script describes the HOST. Cross-compiling to Android from Windows
    therefore drags in win.cpp and d3d11. Patch a local checkout to test the
    target instead, and point cargo at it."""
    if not HWCODEC_DIR.exists():
        BUILDENV.mkdir(parents=True, exist_ok=True)
        run(["git", "clone", HWCODEC_URL, str(HWCODEC_DIR)], cwd=HWCODEC_DIR.parent)
        run(["git", "checkout", HWCODEC_REV], cwd=HWCODEC_DIR)
        run(["git", "submodule", "update", "--init", "--recursive"], cwd=HWCODEC_DIR)

    build_rs = HWCODEC_DIR / "build.rs"
    text = build_rs.read_text(encoding="utf-8")
    # Only the two blocks in fn main(); the later ones sit under
    # cfg(all(windows, feature = "vram")) code that must keep its attributes.
    for old, new in (
        ('    #[cfg(windows)]\n    {\n        ["d3d11", "dxgi"]',
         '    if target_os == "windows"\n    {\n        ["d3d11", "dxgi"]'),
        ('    #[cfg(windows)]\n    {\n        let win_path',
         '    if target_os == "windows"\n    {\n        let win_path'),
    ):
        if old in text:
            text = text.replace(old, new, 1)
    if text != build_rs.read_text(encoding="utf-8"):
        build_rs.write_text(text, encoding="utf-8")
        info("patched hwcodec build.rs to test the target, not the host")

    patch_file(REPO / ".cargo/config.toml",
               lambda t: t if "rustdesk-org/hwcodec" in t else
               t.rstrip("\n") + f'\n\n[patch."{HWCODEC_URL}"]\n'
               f'hwcodec = {{ path = "{HWCODEC_DIR.as_posix()}" }}\n',
               "point hwcodec at the patched checkout")


def _generate_bridge(force):
    dart = FLUTTER_DIR / "lib/generated_bridge.dart"
    rust = REPO / "src/bridge_generated.rs"
    if dart.exists() and rust.exists() and not force:
        info("flutter_rust_bridge output already present")
        return
    llvm = find_llvm()
    if not llvm:
        fail("libclang.dll not found; run the install phase")
    run(["flutter_rust_bridge_codegen", "--llvm-path", str(llvm),
         "--rust-input", "./src/flutter_ffi.rs",
         "--dart-output", "./flutter/lib/generated_bridge.dart",
         "--c-output", "./flutter/macos/Runner/bridge_generated.h"])


def _build_rust_lib(force):
    so = REPO / "target" / RUST_TARGET / "release/liblibrustdesk.so"
    if so.exists() and not force:
        info(f"{so.name} already built")
        return
    run(["cargo", "ndk", "--platform", "21", "--target", RUST_TARGET,
         "build", "--release", "--features", "flutter,hwcodec"])
    if not so.exists():
        fail(f"{so} was not produced")


def _stage_jni_libs():
    dst = FLUTTER_DIR / "android/app/src/main/jniLibs" / ABI
    dst.mkdir(parents=True, exist_ok=True)
    shutil.copy2(REPO / "target" / RUST_TARGET / "release/liblibrustdesk.so",
                 dst / "librustdesk.so")
    shutil.copy2(NDK_SYSROOT_LIB / "libc++_shared.so", dst / "libc++_shared.so")
    for f in sorted(dst.iterdir()):
        info(f"{f.name}  {f.stat().st_size:,} bytes")


# --------------------------------------------------------------------------- 5. build


def phase_build(args):
    """Build, package and sign the app."""
    _apply_build_tweaks()
    run(["flutter", "pub", "get"], cwd=FLUTTER_DIR)
    run(["flutter", "build", "apk", "--release",
         "--target-platform", "android-arm64", "--split-per-abi"], cwd=FLUTTER_DIR)

    built = FLUTTER_DIR / "build/app/outputs/flutter-apk" / f"app-{ABI}-release.apk"
    if not built.exists():
        fail(f"{built} was not produced")
    info(f"built {built.name}  {built.stat().st_size:,} bytes")

    _sign(built)
    if args.install_apk:
        _adb_install()


def _apply_build_tweaks():
    # 1 GB is not enough for the jetifier transform of the Flutter engine jar.
    patch_file(FLUTTER_DIR / "android/gradle.properties",
               lambda t: t.replace("org.gradle.jvmargs=-Xmx1024M",
                                   "org.gradle.jvmargs=-Xmx6g -XX:MaxMetaspaceSize=1g"),
               "raise the Gradle heap")
    # The release signing config needs upstream's secrets; this build signs with
    # our own key afterwards instead.
    patch_file(FLUTTER_DIR / "android/app/build.gradle",
               lambda t: t.replace("signingConfig signingConfigs.release",
                                   "signingConfig signingConfigs.debug"),
               "build unsigned, sign afterwards")


def _sign(built):
    if not KEYSTORE.exists():
        fail(f"keystore not found: {KEYSTORE}")
    BUILDENV.mkdir(parents=True, exist_ok=True)
    run([_build_tool("zipalign.exe"), "-p", "-f", "4", built, ALIGNED_APK])
    run([_build_tool("apksigner.bat"), "sign",
         "--ks", KEYSTORE, "--ks-key-alias", KEY_ALIAS,
         "--ks-pass", f"pass:{KEY_PASS}", "--key-pass", f"pass:{KEY_PASS}",
         "--out", OUT_APK, ALIGNED_APK])
    info(f"signed {OUT_APK}")


def _adb_install():
    code, out = capture(["adb", "devices"])
    if "\tdevice" not in out:
        fail("no adb device attached")
    run(["adb", "install", "-r", OUT_APK])


# --------------------------------------------------------------------------- 6. cleanup


def phase_cleanup(args):
    """Drop intermediates and hand the repo back the way it was found."""
    if ALIGNED_APK.exists():
        ALIGNED_APK.unlink()
        info(f"removed {ALIGNED_APK.name}")
    if args.keep_patches:
        info("--keep-patches: leaving the patched files in place")
    else:
        restore_patched_files()
    if OUT_APK.exists():
        info(f"APK: {OUT_APK}  {OUT_APK.stat().st_size:,} bytes")
    else:
        info("no signed APK present; run the build phase")


# --------------------------------------------------------------------------- driver

PHASES = [
    ("install", phase_install),
    ("verify", phase_verify),
    ("env", phase_env),
    ("prebuild", phase_prebuild),
    ("build", phase_build),
    ("cleanup", phase_cleanup),
]


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--list", action="store_true", help="list the phases and exit")
    ap.add_argument("--only", nargs="+", metavar="PHASE", help="run just these phases")
    ap.add_argument("--start-at", metavar="PHASE", help="run from this phase onwards")
    ap.add_argument("--force", action="store_true", help='ignore "already done" skips')
    ap.add_argument("--install-apk", action="store_true", help="adb install the signed APK")
    ap.add_argument("--keep-patches", action="store_true",
                    help="do not restore the files the build patched")
    args = ap.parse_args()

    names = [n for n, _ in PHASES]
    if args.list:
        for name, fn in PHASES:
            print(f"{name:<10} {(fn.__doc__ or '').strip().splitlines()[0]}")
        return

    selected = PHASES
    if args.only:
        unknown = set(args.only) - set(names)
        if unknown:
            fail(f"unknown phase(s): {', '.join(sorted(unknown))}")
        selected = [p for p in PHASES if p[0] in args.only]
    elif args.start_at:
        if args.start_at not in names:
            fail(f"unknown phase: {args.start_at}")
        selected = PHASES[names.index(args.start_at):]

    for index, (name, fn) in enumerate(selected, 1):
        log(f"phase {index}/{len(selected)}: {name}")
        fn(args)
    log(f"done -> {OUT_APK}")


if __name__ == "__main__":
    main()
