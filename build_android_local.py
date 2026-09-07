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
# A rustup installation of this build's own, so the shared ~/.rustup and
# ~/.cargo -- which other projects here rely on, including a custom OLLVM
# toolchain -- are never touched. cargo-ndk and flutter_rust_bridge_codegen live
# in it too, since nothing else uses them.
RUSTUP_HOME = TOOLCHAINS / "rustup"
CARGO_HOME = TOOLCHAINS / "cargo"
RUSTUP_INIT_URL = "https://static.rust-lang.org/rustup/dist/x86_64-pc-windows-msvc/rustup-init.exe"

ABI = "arm64-v8a"
NDK_VERSION = "28.2.13676358"  # r28c, the version the CI builds with
RUST_TARGET = "aarch64-linux-android"
VCPKG_TRIPLET = "arm64-android"

# The host side is GNU, not MSVC, so a bare machine needs no Visual Studio: the
# MinGW toolchain ships in the build env, while MSVC cannot legally be
# redistributed. It also supplies the cmake/ninja/nasm fallback for vcpkg.
HOST_RUST_TARGET = "x86_64-pc-windows-gnu"
HOST_VCPKG_TRIPLET = "x64-mingw-static"
MINGW_DIR = TOOLCHAINS / "mingw"

# The ports the Android build actually consumes. Installing them explicitly, in
# classic mode, avoids the manifest's `host: true` entries, which would build a
# second copy of everything for the host.
VCPKG_PORTS = ["aom", "cpu-features", "libjpeg-turbo", "opus", "libvpx", "libyuv", "ffmpeg"]

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
LIBSODIUM_SYS_VERSION = "0.2.7"
LIBSODIUM_SYS_DIR = BUILDENV / "sources/libsodium-sys"

# Default to rustup's own toolchain. The in-repo stage1 compiler is built against
# an OLLVM fork and the librustdesk.so it produces crashes the app on launch
# (confirmed by swapping only that .so into an otherwise identical APK).
# Set RUSTDESK_RUST_TOOLCHAIN=<name> to opt into a linked custom toolchain.
RUST_TOOLCHAIN = os.environ.get("RUSTDESK_RUST_TOOLCHAIN") or None

# Portable copies of tools other projects on this machine also use, so this
# directory can be zipped and unpacked on a bare PC. They are copies, never
# moves: the system installations stay where the other projects expect them.
GIT_DIR = TOOLCHAINS / "git"
GIT_URL_API = "https://api.github.com/repos/git-for-windows/git/releases/latest"
GIT_ASSET = "PortableGit-*-64-bit.7z.exe"

JDK_DIR = TOOLCHAINS / "jdk"
JDK_URL_API = "https://api.github.com/repos/adoptium/temurin17-binaries/releases/latest"
JDK_ASSET = "OpenJDK17U-jdk_x64_windows_hotspot_*.zip"

ANDROID_SDK = TOOLCHAINS / "android-sdk"
BUILD_TOOLS_VERSION = "37.0.0"
ANDROID_PLATFORM = "android-36"  # matches compileSdkVersion in flutter/android/app/build.gradle

GIT_USR_BIN = GIT_DIR / "usr/bin"
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

# A symlink, so the signing key itself stays outside and never travels in a zip
# of this directory.
KEYSTORE = Path(os.environ.get("RUSTDESK_KEYSTORE",
                               BUILDENV / "keys/rustdesk-personal.keystore"))
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


def check_relocation():
    """Drop the caches that bake in absolute paths when this directory has moved.

    Gradle's transform cache and Flutter's local.properties both record where the
    build env was, and a stale entry fails in ways that do not name the cause.
    Everything else here is relocatable, and cargo simply rebuilds.
    """
    stamp = BUILDENV / ".buildenv-path"
    current = str(BUILDENV.resolve())
    previous = stamp.read_text(encoding="utf-8").strip() if stamp.exists() else None
    if previous == current:
        return
    if previous is not None:
        log(f"build env moved from {previous}")
        shutil.rmtree(BUILDENV / "caches/gradle", ignore_errors=True)
        (FLUTTER_DIR / "android/local.properties").unlink(missing_ok=True)
        info("cleared the Gradle cache and Flutter's local.properties")
    BUILDENV.mkdir(parents=True, exist_ok=True)
    stamp.write_text(current, encoding="utf-8")


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
    """The build env holds its own NDK, pinned to the r28c the CI uses. Other
    projects on this machine keep their own versions under the Android SDK, and
    picking "the newest installed one" would silently follow those."""
    if os.environ.get("ANDROID_NDK_HOME"):
        return Path(os.environ["ANDROID_NDK_HOME"])
    return TOOLCHAINS / "ndk"


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
    # Drop other msys bin dirs too: a system Git's usr\bin sits on PATH ahead of
    # ours, and its perl would win over the portable one this build ships.
    def wanted(entry):
        low = entry.lower().replace("/", "\\")
        if "strawberry" in low:
            return False
        return not (low.endswith(r"\git\usr\bin") and str(BUILDENV).lower() not in low)

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
    # Keep this build's Gradle and pub caches out of the shared per-user ones,
    # so the pinned Flutter cannot disturb other projects on this machine.
    env["GRADLE_USER_HOME"] = str(BUILDENV / "caches/gradle")
    env["PUB_CACHE"] = str(BUILDENV / "caches/pub")

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


MANIFEST = BUILDENV / "patched-files.json"

# Files the build tools rewrite on their own (cargo because of the hwcodec
# [patch], `flutter pub get` because the pinned Flutter resolves older packages).
# They are build byproducts here, not edits worth keeping.
BUILD_BYPRODUCTS = ["Cargo.lock", "flutter/pubspec.lock"]


def _manifest():
    return json.loads(MANIFEST.read_text(encoding="utf-8")) if MANIFEST.exists() else {}


def _record(rel, digest):
    BUILDENV.mkdir(parents=True, exist_ok=True)
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


# --------------------------------------------------------------------------- 1. install


def phase_install(args):
    """Put everything the build needs on the machine."""
    _install_git()
    _install_jdk()
    _install_llvm()
    _install_perl_modules()
    _install_rust()
    _install_ndk()
    _install_android_sdk()
    _install_vcpkg()
    _install_flutter_sdk(args.force)


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


def _latest_asset(api_url, pattern):
    import fnmatch
    with urllib.request.urlopen(api_url) as r:
        release = json.load(r)
    for asset in release["assets"]:
        if fnmatch.fnmatch(asset["name"], pattern):
            return asset["browser_download_url"]
    fail(f"no asset matching {pattern} in {api_url}")


def _install_git():
    """Git supplies two things: git itself, and the msys perl that OpenSSL's
    Configure insists on (it rejects a native Windows perl)."""
    if (GIT_DIR / "cmd/git.exe").exists():
        return
    archive = _download(_latest_asset(GIT_URL_API, GIT_ASSET), DOWNLOADS / "PortableGit.7z.exe")
    info(f"extracting PortableGit into {GIT_DIR}")
    run([archive, f"-o{GIT_DIR}", "-y"], env=dict(os.environ))
    if not (GIT_DIR / "usr/bin/perl.exe").exists():
        fail(f"perl.exe missing after extracting {archive}")


def _install_jdk():
    if (JDK_DIR / "bin/java.exe").exists():
        return
    archive = _download(_latest_asset(JDK_URL_API, JDK_ASSET), DOWNLOADS / "jdk17.zip")
    info(f"extracting the JDK into {JDK_DIR}")
    with zipfile.ZipFile(archive) as z:
        z.extractall(TOOLCHAINS)
    extracted = next(TOOLCHAINS.glob("jdk-17*"), None)
    if extracted:
        extracted.rename(JDK_DIR)
    if not (JDK_DIR / "bin/java.exe").exists():
        fail(f"java.exe missing after extracting {archive}")


def _install_android_sdk():
    missing = [p for p in (ANDROID_SDK / "build-tools" / BUILD_TOOLS_VERSION / "zipalign.exe",
                           ANDROID_SDK / "platforms" / ANDROID_PLATFORM / "android.jar",
                           ANDROID_SDK / "platform-tools/adb.exe") if not p.exists()]
    if not missing:
        return
    fail("Android SDK components missing:\n  " + "\n  ".join(str(p) for p in missing)
         + "\n  Install them with Android Studio's SDK Manager "
           f"(Build-Tools {BUILD_TOOLS_VERSION}, Platform {ANDROID_PLATFORM}, Platform-Tools),\n"
           f"  then copy those three directories under {ANDROID_SDK}.")


def _install_rust():
    """Bootstrap a rustup of this build's own. `--no-modify-path` matters: the
    shared installation must keep owning the user's PATH."""
    if not (CARGO_HOME / "bin/rustc.exe").exists():
        init = _download(RUSTUP_INIT_URL, DOWNLOADS / "rustup-init.exe")
        run([init, "-y", "--no-modify-path", "--profile", "minimal",
             "--default-toolchain", f"stable-{HOST_RUST_TARGET}", "--target", RUST_TARGET])
    # A GNU host, so build scripts link with MinGW's gcc instead of MSVC's link.exe.
    if capture(["rustc", "-vV"])[1].find(HOST_RUST_TARGET) < 0:
        run(["rustup", "toolchain", "install", f"stable-{HOST_RUST_TARGET}", "--profile", "minimal"])
        run(["rustup", "default", f"stable-{HOST_RUST_TARGET}"])
        run(["rustup", "target", "add", RUST_TARGET])
    # bindgen shells out to rustfmt to format what it generates.
    if capture(["rustfmt", "--version"])[0] != 0:
        run(["rustup", "component", "add", "rustfmt"])
    for tool, args in (("cargo-ndk", ["cargo-ndk"]),
                       ("flutter_rust_bridge_codegen",
                        ["flutter_rust_bridge_codegen", "--version", FRB_VERSION,
                         "--features", "uuid"])):
        if not (CARGO_HOME / f"bin/{tool}.exe").exists():
            run(["cargo", "install", *args, "--locked"])


def _install_ndk():
    if (NDK / "source.properties").exists():
        return
    fail(f"no NDK at {NDK}.\n"
         f"  Install NDK {NDK_VERSION} with Android Studio's SDK Manager, then move it here:\n"
         f"  move \"%LOCALAPPDATA%\\Android\\Sdk\\ndk\\{NDK_VERSION}\" \"{NDK}\"")


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

    for name in ("git", "java", "cargo", "rustup"):
        path = which(name)
        print(f"    {name:<24} {path or 'MISSING'}")
        if not path:
            problems.append(f"{name} not on PATH")

    # vcpkg vendors its own cmake, ninja and nasm under downloads/tools, so these
    # only matter if it has to build a port and prefers a system copy.
    for name in ("cmake", "ninja", "nasm"):
        print(f"    {name + ' (optional)':<24} {which(name) or 'not on PATH; vcpkg will use its own'}")

    for label, path in (("rustc", CARGO_HOME / "bin/rustc.exe"),
                        ("cargo-ndk", CARGO_HOME / "bin/cargo-ndk.exe"),
                        ("frb codegen", CARGO_HOME / "bin/flutter_rust_bridge_codegen.exe"),
                        ("NDK", NDK), ("NDK clang", NDK_BIN), ("NDK make", NDK_MAKE_BIN),
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
    path = ANDROID_SDK / "build-tools" / BUILD_TOOLS_VERSION / name
    if path.exists():
        return path
    if required:
        fail(f"{name} not found at {path}; run the install phase")
    return None


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
    _patch_libsodium_sys()
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
        src = next((TOOLCHAINS / "cargo/registry/src").glob(f"*/libsodium-sys-{LIBSODIUM_SYS_VERSION}"), None)
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
