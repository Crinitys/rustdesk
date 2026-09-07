#!/usr/bin/env python3
"""Build the Android (arm64) APK of this fork locally, on a Windows host.

CI builds on Ubuntu; this reproduces it here so a Dart-only change does not need
a 45-minute round trip. Every tool it runs comes from the build partition, which
resolves `use_tool.json` beside this file against its own `tools.json`; nothing
is installed on the machine and nothing is taken from the system PATH.

Phases, in order:

  1. tools     check the partition's tools, add what only this build needs
  2. env       assemble the build environment and smoke-test it
  3. prebuild  native dependencies, generated bindings, librustdesk.so
  4. build     the Flutter app, packaged and signed
  5. cleanup   drop intermediates, restore the files the build patched

Usage:

    python build-local/build.py                     # all phases
    python build-local/build.py --list              # show phases
    python build-local/build.py --only build        # rerun one phase
    python build-local/build.py --start-at prebuild # resume from a phase
    python build-local/build.py --force             # ignore "already done"
    python build-local/build.py --install-apk       # adb install at the end
    python build-local/build.py --keep-patches      # skip the restore

RUSTDESK_TOOLS_ROOT points at the build partition when it is not E:/.
"""

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path

# --------------------------------------------------------------------------- config

REPO = Path(__file__).resolve().parent.parent
BUILD_LOCAL = Path(__file__).resolve().parent
FLUTTER_DIR = REPO / "flutter"

# The build partition. Its build_env.py turns a tool name and version into a
# directory, so this script never hard-codes where a toolchain lives, and two
# repositories can pin different versions of the same tool.
TOOLS_ROOT = Path(os.environ.get("RUSTDESK_TOOLS_ROOT", "E:/"))
sys.path.insert(0, str(TOOLS_ROOT))
try:
    from build_env import CACHE_ROOT, read_use_tool, resolve
    from build_pre import NotReady, prepare
except ImportError as exc:
    raise SystemExit(f"ERROR: no build partition at {TOOLS_ROOT} ({exc})\n"
                     "  Point RUSTDESK_TOOLS_ROOT at the directory holding build_env.py.")

USE_TOOL = BUILD_LOCAL / "use_tool.json"
WANTED = read_use_tool(USE_TOOL)


def tool(name):
    return resolve(name, WANTED.get(name))


# Everything this build stages, patches or produces. All of it is ignored by
# git, and deleting this directory costs only the time to rebuild.
DOWNLOADS = BUILD_LOCAL / "cache/downloads"
GRADLE_HOME = BUILD_LOCAL / "cache/gradle"
STRAWBERRY_DIR = BUILD_LOCAL / "cache/strawberry"
PERLLIB = BUILD_LOCAL / "perllib"
OUT_DIR = BUILD_LOCAL / "out"
HWCODEC_DIR = BUILD_LOCAL / "sources/hwcodec"
LIBSODIUM_SYS_DIR = BUILD_LOCAL / "sources/libsodium-sys"
MANIFEST = BUILD_LOCAL / "patched-files.json"

GIT_DIR = tool("git").dir
JDK_DIR = tool("jdk").dir
ANDROID_SDK = tool("android-sdk").dir
LLVM_DIR = tool("llvm").dir
MINGW_DIR = tool("mingw").dir
FLUTTER_SDK = tool("flutter").dir
VCPKG_ROOT = tool("vcpkg").dir
CARGO_HOME = tool("cargo").dir
RUSTUP_HOME = tool("rustup").dir
SODIUM_DIR = tool("sodium").dir
NDK = tool("ndk").dir

FLUTTER_VERSION = tool("flutter").version
NDK_VERSION = tool("ndk").version

ABI = "arm64-v8a"
RUST_TARGET = "aarch64-linux-android"
VCPKG_TRIPLET = "arm64-android"

# The host side is GNU, not MSVC, so a bare machine needs no Visual Studio: the
# MinGW toolchain comes from the partition, while MSVC cannot legally be
# redistributed. It also supplies the cmake/ninja/nasm fallback for vcpkg.
HOST_RUST_TARGET = "x86_64-pc-windows-gnu"
HOST_VCPKG_TRIPLET = "x64-mingw-static"

# The ports the Android build actually consumes. Installing them explicitly, in
# classic mode, avoids the manifest's `host: true` entries, which would build a
# second copy of everything for the host.
VCPKG_PORTS = ["aom", "cpu-features", "libjpeg-turbo", "opus", "libvpx", "libyuv", "ffmpeg"]

FRB_VERSION = "1.80.1"  # must match `flutter_rust_bridge` in flutter/pubspec.yaml
HWCODEC_URL = "https://github.com/rustdesk-org/hwcodec"
HWCODEC_REV = "778df1f99597722473b29443bac22ae6c23946fe"
LIBSODIUM_SYS_VERSION = "0.2.7"

# Default to rustup's own toolchain. The custom stage1 compiler on the partition
# is built against an OLLVM fork and the librustdesk.so it produces crashes the
# app on launch (confirmed by swapping only that .so into an otherwise identical
# APK). Set RUSTDESK_RUST_TOOLCHAIN=<name> to opt into a linked custom toolchain.
RUST_TOOLCHAIN = os.environ.get("RUSTDESK_RUST_TOOLCHAIN") or None

BUILD_TOOLS_VERSION = "37.0.0"
ANDROID_PLATFORM = "android-36"  # matches compileSdkVersion in flutter/android/app/build.gradle

GIT_USR_BIN = GIT_DIR / "usr/bin"

# Strawberry Perl is never executed; it is only a source of the pure-perl modules
# Git's cut-down perl lacks, and only until perllib is staged. The portable zip
# avoids an installer that would put its gcc/make/ld on the system PATH.
STRAWBERRY_VERSION = "5.40.2.1"
STRAWBERRY_URL = (f"https://strawberryperl.com/download/{STRAWBERRY_VERSION}"
                  f"/strawberry-perl-{STRAWBERRY_VERSION}-64bit-portable.zip")

# A symlink, so the signing key itself stays outside the repository.
KEYSTORE = Path(os.environ.get("RUSTDESK_KEYSTORE",
                               BUILD_LOCAL / "keys/rustdesk-personal.keystore"))
KEY_ALIAS = os.environ.get("RUSTDESK_KEY_ALIAS", "rustdesk-personal")
KEY_PASS = os.environ.get("RUSTDESK_KEY_PASS", "rustdesk123")

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

NDK_LLVM = NDK / "toolchains/llvm/prebuilt/windows-x86_64"
NDK_BIN = NDK_LLVM / "bin"
NDK_MAKE_BIN = NDK / "prebuilt/windows-x86_64/bin"
NDK_SYSROOT_LIB = NDK_LLVM / "sysroot/usr/lib" / RUST_TARGET

# --------------------------------------------------------------------------- helpers


def log(msg):
    print(f"\n=== {msg}", flush=True)


def info(msg):
    print(f"    {msg}", flush=True)


def fail(msg):
    raise SystemExit(f"ERROR: {msg}")


def check_relocation():
    """Drop the caches that bake in absolute paths when the partition has moved.

    Gradle's transform cache and Flutter's local.properties both record where the
    toolchains were, and a stale entry fails in ways that do not name the cause.
    Everything else here is relocatable, and cargo simply rebuilds.
    """
    stamp = BUILD_LOCAL / ".tools-root"
    current = str(TOOLS_ROOT.resolve())
    previous = stamp.read_text(encoding="utf-8").strip() if stamp.exists() else None
    if previous == current:
        return
    if previous is not None:
        log(f"tools moved from {previous}")
        shutil.rmtree(GRADLE_HOME, ignore_errors=True)
        (FLUTTER_DIR / "android/local.properties").unlink(missing_ok=True)
        info("cleared the Gradle cache and Flutter's local.properties")
    BUILD_LOCAL.mkdir(parents=True, exist_ok=True)
    stamp.write_text(current, encoding="utf-8")


def msys_path(path):
    """Git's perl is an msys program: it splits PERL5LIB on ':' and resolves
    '/d/...' through the msys drive mounts, so a 'D:\\...' value would be cut in
    half at the colon."""
    path = Path(path).resolve()
    return "/" + path.drive[0].lower() + path.as_posix()[2:]


def perl_ok(module):
    env = dict(os.environ, PERL5LIB=msys_path(PERLLIB))
    return subprocess.run([str(GIT_USR_BIN / "perl.exe"), f"-M{module}", "-e", "1"],
                          capture_output=True, env=env).returncode == 0


def find_llvm():
    """A stock LLVM on purpose. The OLLVM fork on the partition can also drive
    ffigen/bindgen, but keeping the whole toolchain stock removes a variable from
    an already fragile cross-compile."""
    return LLVM_DIR if (LLVM_DIR / "bin/libclang.dll").exists() else None


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
    # Drop other msys bin dirs too: a system Git's usr\bin sits on PATH ahead of
    # ours, and its perl would win over the one the partition supplies.
    def wanted(entry):
        low = entry.lower().replace("/", "\\")
        if "strawberry" in low:
            return False
        return not (low.endswith(r"\git\usr\bin") and str(GIT_DIR).lower() not in low)

    entries = [p for p in env["PATH"].split(os.pathsep) if p and wanted(p)]
    env["PATH"] = os.pathsep.join(
        [str(FLUTTER_SDK / "bin"), str(CARGO_HOME / "bin"), str(JDK_DIR / "bin"),
         str(GIT_DIR / "cmd"), str(ANDROID_SDK / "platform-tools")] + entries
        # Appended, so a system cmake/ninja/nasm still wins if there is one; on a
        # bare machine these are the only copies. gcc for the host build lives
        # here too.
        + [str(MINGW_DIR / "bin"), str(NDK_MAKE_BIN), str(GIT_USR_BIN)])
    env["RUSTUP_HOME"] = str(RUSTUP_HOME)
    env["CARGO_HOME"] = str(CARGO_HOME)
    env["JAVA_HOME"] = str(JDK_DIR)
    env["ANDROID_SDK_ROOT"] = str(ANDROID_SDK)
    env["ANDROID_HOME"] = str(ANDROID_SDK)

    if RUST_TOOLCHAIN:
        env["RUSTUP_TOOLCHAIN"] = RUST_TOOLCHAIN

    # Modules OpenSSL's Configure needs, staged rather than installed into Git.
    env["PERL5LIB"] = msys_path(PERLLIB)
    # OpenSSL's Makefile re-invokes perl, and on the way the msys2 runtime would
    # rewrite that POSIX path back to 'D:/...', which perl then splits at the
    # colon into two bogus @INC entries. Exclude the variable from conversion.
    env["MSYS2_ENV_CONV_EXCL"] = "PERL5LIB"
    # Gradle's cache belongs to this repository -- it is keyed by the projects
    # built through it -- while the pub cache is keyed by package and version and
    # is shared with every other repository on the partition.
    env["GRADLE_USER_HOME"] = str(GRADLE_HOME)
    env["PUB_CACHE"] = str(CACHE_ROOT / "pub")

    # libsodium-sys builds from source via autotools on Linux CI, which cannot run
    # here, so point it at prebuilt libs. Do not set SODIUM_STATIC (deprecated,
    # the crate panics) or SODIUM_SHARED (would force dynamic linking).
    # Only the target's directory is set: with a GNU host, both builds would ask
    # for the same libsodium.a, and the patched libsodium-sys lets the host fall
    # back to the copy bundled in that crate.
    env[f'{RUST_TARGET.upper().replace("-", "_")}_SODIUM_LIB_DIR'] = str(SODIUM_DIR)
    env["VCPKG_DEFAULT_HOST_TRIPLET"] = HOST_VCPKG_TRIPLET

    llvm = find_llvm()
    if llvm:
        env["LIBCLANG_PATH"] = str(llvm / "bin")
        # Given the NDK sysroot, bindgen's libclang stops finding its own resource
        # headers, so stddef.h and friends go missing. Hand it the sysroot and
        # that include directory explicitly.
        #
        # Not via CPATH: that reaches every compiler, and the MinGW gcc running
        # the host builds chokes on clang's headers. Two variables are needed --
        # bindgen 0.59 (hwcodec) reads only the plain one, while newer versions
        # prefer the hyphenated per-target name, which outranks the underscored
        # one cargo-ndk sets.
        sysroot = NDK_LLVM / "sysroot"
        builtin = sorted((llvm / "lib/clang").glob("*/include"))
        clang_args = (f"--sysroot={sysroot.as_posix()}"
                      f" -I{(sysroot / 'usr/include' / RUST_TARGET).as_posix()}"
                      + (f" -I{builtin[-1].as_posix()}" if builtin else ""))
        env["BINDGEN_EXTRA_CLANG_ARGS"] = clang_args
        env[f"BINDGEN_EXTRA_CLANG_ARGS_{RUST_TARGET}"] = clang_args

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


# Files the build tools rewrite on their own (cargo because of the hwcodec
# [patch], `flutter pub get` because the pinned Flutter resolves older packages).
# They are build byproducts here, not edits worth keeping.
BUILD_BYPRODUCTS = ["Cargo.lock", "flutter/pubspec.lock"]


def _manifest():
    return json.loads(MANIFEST.read_text(encoding="utf-8")) if MANIFEST.exists() else {}


def _record(rel, digest):
    BUILD_LOCAL.mkdir(parents=True, exist_ok=True)
    data = _manifest()
    data[rel] = digest
    MANIFEST.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else ""


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


# --------------------------------------------------------------------------- 1. tools


def phase_tools(args):
    """Check the partition's tools, then add what only this build needs."""
    try:
        prepare(USE_TOOL, verbose=True)
    except NotReady as exc:
        fail(f"{exc}\n  Install or repair them on the partition at {TOOLS_ROOT}.")
    _stage_perl_modules()
    _install_rust_extras()
    _patch_flutter_sdk()
    _check_build_only_tools()


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


def _install_rust_extras():
    """The partition supplies rustup and cargo; this build needs a GNU host
    toolchain, the Android target and two cargo subcommands on top."""
    # A GNU host, so build scripts link with MinGW's gcc instead of MSVC's link.exe.
    if capture(["rustc", "-vV"])[1].find(HOST_RUST_TARGET) < 0:
        run(["rustup", "toolchain", "install", f"stable-{HOST_RUST_TARGET}", "--profile", "minimal"])
        run(["rustup", "default", f"stable-{HOST_RUST_TARGET}"])
        run(["rustup", "target", "add", RUST_TARGET])
    # bindgen shells out to rustfmt to format what it generates.
    if capture(["rustfmt", "--version"])[0] != 0:
        run(["rustup", "component", "add", "rustfmt"])
    for name, args in (("cargo-ndk", ["cargo-ndk"]),
                       ("flutter_rust_bridge_codegen",
                        ["flutter_rust_bridge_codegen", "--version", FRB_VERSION,
                         "--features", "uuid"])):
        if not (CARGO_HOME / f"bin/{name}.exe").exists():
            run(["cargo", "install", *args, "--locked"])


def _strawberry_lib():
    if (STRAWBERRY_DIR / "perl/lib").is_dir():
        return STRAWBERRY_DIR / "perl/lib"
    archive = _download(STRAWBERRY_URL, DOWNLOADS / Path(STRAWBERRY_URL).name)
    info(f"extracting Strawberry Perl into {STRAWBERRY_DIR}")
    with zipfile.ZipFile(archive) as z:
        z.extractall(STRAWBERRY_DIR)
    lib = STRAWBERRY_DIR / "perl/lib"
    if not lib.is_dir():
        fail(f"{lib} not found after extracting {archive}")
    return lib


def _stage_perl_modules():
    """Stage the modules under PERLLIB and reach them through PERL5LIB, rather
    than writing into the Git installation the partition owns."""
    perl = GIT_USR_BIN / "perl.exe"
    if not perl.exists():
        fail(f"{perl} not found; the partition's git tool is incomplete")
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


def _patch_flutter_sdk():
    """The CI applies this to the SDK itself for 3.24.5; do the same. It belongs
    to that Flutter version, which is why it is applied to the SDK rather than
    carried here."""
    patch = REPO / ".github/patches/flutter_3.24.4_dropdown_menu_enableFilter.diff"
    if subprocess.run(["git", "apply", "--reverse", "--check", str(patch)],
                      cwd=str(FLUTTER_SDK), capture_output=True).returncode != 0:
        run(["git", "apply", str(patch)], cwd=FLUTTER_SDK)
        info("applied the dropdown_menu patch to the Flutter SDK")


def _check_build_only_tools():
    """The pieces the partition's own checks know nothing about."""
    problems = []
    for label, path in (("cargo-ndk", CARGO_HOME / "bin/cargo-ndk.exe"),
                        ("frb codegen", CARGO_HOME / "bin/flutter_rust_bridge_codegen.exe"),
                        ("NDK clang", NDK_BIN / "clang.exe"),
                        ("NDK sysroot libs", NDK_SYSROOT_LIB),
                        ("libclang", LLVM_DIR / "bin/libclang.dll"),
                        ("android platform", ANDROID_SDK / "platforms" / ANDROID_PLATFORM / "android.jar"),
                        ("zipalign", ANDROID_SDK / "build-tools" / BUILD_TOOLS_VERSION / "zipalign.exe"),
                        ("apksigner", ANDROID_SDK / "build-tools" / BUILD_TOOLS_VERSION / "apksigner.bat"),
                        ("keystore", KEYSTORE)):
        ok = path.exists()
        print(f"    {label:<20} {path} {'' if ok else '  <-- MISSING'}")
        if not ok:
            problems.append(f"{label} missing at {path}")
    if problems:
        fail("build tools missing:\n  - " + "\n  - ".join(problems))


def _build_tool(name, required=True):
    path = ANDROID_SDK / "build-tools" / BUILD_TOOLS_VERSION / name
    if path.exists():
        return path
    if required:
        fail(f"{name} not found at {path}; run the tools phase")
    return None


# --------------------------------------------------------------------------- 2. env


def phase_env(args):
    """Assemble the build environment and smoke-test that it actually works.

    Every value here exists to work around a specific host/target confusion; the
    checks below are the ones that failed first when a value was wrong.
    """
    env = build_env()
    for key in ("VCPKG_ROOT", "ANDROID_NDK_HOME", "GRADLE_USER_HOME", "PUB_CACHE",
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


# --------------------------------------------------------------------------- 3. prebuild


def phase_prebuild(args):
    """Everything the Flutter build consumes: native libs, bindings, the .so."""
    _vcpkg_dependencies(args.force)
    _stage_sodium()
    _patch_libsodium_sys()
    _patch_hwcodec()
    _generate_bridge(args.force)
    _build_rust_lib(args.force)
    _stage_jni_libs()


def _overlay_triplet():
    """aom's cmake defaults CMAKE_ASM_COMPILER to a bare `as`, which the Windows
    NDK does not ship. Override it in a local overlay so the repo's own triplet,
    used by the Linux CI, stays untouched."""
    overlay = BUILD_LOCAL / "triplets"
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

    # Classic mode, run from VCPKG_ROOT so the repo manifest is not picked up:
    # its `host: true` entries would build a second copy of every library for
    # the host, which nothing here consumes.
    missing = [p for p in VCPKG_PORTS + ["libsodium"]
               if not (lib_dir / f"lib{'jpeg' if p == 'libjpeg-turbo' else p.removeprefix('lib')}.a").exists()]
    if not missing and not force:
        info("vcpkg dependencies already installed")
        return
    run([exe, "install", *[f"{p}:{VCPKG_TRIPLET}" for p in VCPKG_PORTS + ["libsodium"]],
         f"--x-install-root={install_root}", f"--overlay-triplets={overlay.as_posix()}"],
        cwd=VCPKG_ROOT)


def _stage_sodium():
    """Only the Android library is staged. libsodium-sys is patched to read a
    target-specific directory, so the host build keeps using the prebuilt copy
    bundled inside that crate."""
    SODIUM_DIR.mkdir(parents=True, exist_ok=True)
    shutil.copy2(VCPKG_ROOT / "installed" / VCPKG_TRIPLET / "lib/libsodium.a",
                 SODIUM_DIR / "libsodium.a")
    info(f"sodium library staged in {SODIUM_DIR}")


def _patch_libsodium_sys():
    """libsodium-sys reads a single SODIUM_LIB_DIR, but its build script runs on
    the host: the host and the Android builds would be pointed at the same
    libsodium.a. Patch a local copy to look at <TARGET>_SODIUM_LIB_DIR first."""
    if not (LIBSODIUM_SYS_DIR / "build.rs").exists():
        src = next((CARGO_HOME / "registry/src").glob(f"*/libsodium-sys-{LIBSODIUM_SYS_VERSION}"), None)
        if src is None:
            fail(f"libsodium-sys {LIBSODIUM_SYS_VERSION} not in the cargo registry yet; "
                 "run the prebuild phase once so cargo fetches it")
        shutil.copytree(src, LIBSODIUM_SYS_DIR)
        for p in LIBSODIUM_SYS_DIR.rglob("*"):
            p.chmod(0o644 if p.is_file() else 0o755)

    build_rs = LIBSODIUM_SYS_DIR / "build.rs"
    text = build_rs.read_text(encoding="utf-8")
    if "target_sodium_lib_dir" not in text:
        text = text.replace(
            '    let lib_dir_isset = env::var("SODIUM_LIB_DIR").is_ok();',
            "    let lib_dir_isset = target_sodium_lib_dir().is_some();", 1)
        text = text.replace(
            'fn find_libsodium_env() {\n    let lib_dir = env::var("SODIUM_LIB_DIR").unwrap(); '
            '// cannot fail\n',
            'fn target_sodium_lib_dir() -> Option<String> {\n'
            '    let target = env::var("TARGET").unwrap_or_default()'
            '.to_uppercase().replace(\'-\', "_");\n'
            '    let per_target = format!("{}_SODIUM_LIB_DIR", target);\n'
            '    println!("cargo:rerun-if-env-changed={}", per_target);\n'
            '    env::var(&per_target).ok().or_else(|| env::var("SODIUM_LIB_DIR").ok())\n'
            '}\n\n'
            'fn find_libsodium_env() {\n'
            '    let lib_dir = target_sodium_lib_dir().expect("SODIUM_LIB_DIR is set");\n', 1)
        build_rs.write_text(text, encoding="utf-8")
        info("patched libsodium-sys build.rs for a per-target library directory")

    patch_file(REPO / ".cargo/config.toml",
               lambda t: t if "libsodium-sys" in t else
               t.rstrip("\n") + "\n\n[patch.crates-io]\n"
               f'libsodium-sys = {{ path = "{LIBSODIUM_SYS_DIR.as_posix()}" }}\n',
               "point libsodium-sys at the patched checkout")


def _patch_hwcodec():
    """hwcodec gates its Windows-only sources on `#[cfg(windows)]`, which in a
    build script describes the HOST. Cross-compiling to Android from Windows
    therefore drags in win.cpp and d3d11. Patch a local checkout to test the
    target instead, and point cargo at it."""
    if not HWCODEC_DIR.exists():
        HWCODEC_DIR.parent.mkdir(parents=True, exist_ok=True)
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
        fail(f"libclang.dll not found under {LLVM_DIR}")
    run(["flutter_rust_bridge_codegen", "--llvm-path", str(llvm),
         "--rust-input", "./src/flutter_ffi.rs",
         "--dart-output", "./flutter/lib/generated_bridge.dart",
         "--c-output", "./flutter/macos/Runner/bridge_generated.h"])


def _build_rust_lib(force):
    so = REPO / "target" / RUST_TARGET / "release/liblibrustdesk.so"
    if so.exists() and not force:
        info(f"{so.name} already built")
        return
    # --link-builtins: the vcpkg C libraries are compiled by NDK clang, which
    # emits LSE outline-atomics calls (__aarch64_ldadd*) that live in compiler-rt
    # and that rustc's link line would otherwise leave undefined.
    run(["cargo", "ndk", "--platform", "21", "--target", RUST_TARGET, "--link-builtins",
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


# --------------------------------------------------------------------------- 4. build


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
    OUT_DIR.mkdir(parents=True, exist_ok=True)
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


# --------------------------------------------------------------------------- 5. cleanup


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
    ("tools", phase_tools),
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

    check_relocation()

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
