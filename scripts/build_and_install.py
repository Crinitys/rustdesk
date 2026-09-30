"""Build the Android APK locally, then install it on an adb device the user picks.

    python scripts/build_and_install.py               # build, then install
    python scripts/build_and_install.py --no-build    # install the last build
    python scripts/build_and_install.py --start-at build

Any other argument is passed to build-local/build.py. The install uses
`adb install -r`, so the app's data stays; that needs the APK signed with the
same key as the installed app, which build.py does by default.
"""

import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
BUILD_PY = REPO / "build-local/build.py"

sys.path.insert(0, str(BUILD_PY.parent))
import build  # noqa: E402  resolves the partition's tools and the APK path

ADB = str(build.ANDROID_SDK / "platform-tools/adb.exe")


def devices():
    out = subprocess.run([ADB, "devices", "-l"], capture_output=True, text=True, check=True).stdout
    found = []
    for line in out.splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 2 and parts[1] == "device":
            model = next((p[6:] for p in parts if p.startswith("model:")), "?")
            found.append((parts[0], model))
    return found


def pick_device():
    while True:
        found = devices()
        if not found:
            input("No adb device attached. Connect one, then press Enter...")
            continue
        for i, (serial, model) in enumerate(found, 1):
            print(f"  {i}. {serial}  ({model})")
        answer = input("Install on which device? [number, r=refresh, q=quit]: ").strip().lower()
        if answer == "q":
            sys.exit("install skipped")
        if answer.isdigit() and 1 <= int(answer) <= len(found):
            return found[int(answer) - 1][0]


def main():
    args = sys.argv[1:]
    if "--no-build" in args:
        args.remove("--no-build")
    else:
        subprocess.run([sys.executable, str(BUILD_PY), *args], check=True)

    if not build.OUT_APK.exists():
        sys.exit(f"APK not found: {build.OUT_APK}")
    serial = pick_device()
    subprocess.run([ADB, "-s", serial, "install", "-r", str(build.OUT_APK)], check=True)
    print(f"installed {build.OUT_APK.name} on {serial}")


if __name__ == "__main__":
    try:
        main()
    except subprocess.CalledProcessError as e:
        sys.exit(e.returncode)
