"""
Genera release/ con setup_scheduler_task.exe y scheduler.exe
Uso: python build_release.py
"""

import os
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
RELEASE = os.path.join(ROOT, "release")
BUILD = os.path.join(ROOT, "build")


def run_pyinstaller(args: list[str]) -> None:
    cmd = [sys.executable, "-m", "PyInstaller", *args]
    print(">", " ".join(cmd))
    subprocess.run(cmd, check=True, cwd=ROOT)


def main() -> None:
    subprocess.run(
        [sys.executable, "-m", "pip", "install", "pyinstaller"],
        check=True,
        cwd=ROOT,
    )
    subprocess.run(
        [sys.executable, "-m", "pip", "install", "-r", "requirements.txt"],
        check=True,
        cwd=ROOT,
    )

    env_backup = None
    env_release = os.path.join(RELEASE, ".env")
    if os.path.isfile(env_release):
        env_backup = os.path.join(ROOT, ".env.release.bak")
        shutil.copy2(env_release, env_backup)

    if os.path.isdir(RELEASE):
        shutil.rmtree(RELEASE)
    os.makedirs(RELEASE, exist_ok=True)

    common = ["--noconfirm", "--clean", "--distpath", RELEASE, "--workpath", BUILD]

    run_pyinstaller(
        [
            *common,
            "--onefile",
            "--name",
            "setup_scheduler_task",
            "--hidden-import",
            "app_paths",
            "--hidden-import",
            "dotenv",
            "setup_scheduler_task.py",
        ]
    )

    run_pyinstaller(
        [
            *common,
            "--onefile",
            "--name",
            "scheduler",
            "--hidden-import",
            "app_paths",
            "--hidden-import",
            "login",
            "--hidden-import",
            "notificaciones",
            "--hidden-import",
            "scrapping_general",
            "--hidden-import",
            "playwright",
            "--hidden-import",
            "psycopg",
            "--collect-submodules",
            "playwright",
            "scheduler.py",
        ]
    )

    shutil.copy(
        os.path.join(ROOT, ".env.example"),
        os.path.join(RELEASE, ".env.example"),
    )
    readme = os.path.join(ROOT, "README_RELEASE.md")
    if os.path.isfile(readme):
        shutil.copy(readme, os.path.join(RELEASE, "README.md"))

    if env_backup and os.path.isfile(env_backup):
        shutil.copy2(env_backup, env_release)
        os.remove(env_backup)
        print("Restaurado tu .env en release/")
    elif os.path.isfile(os.path.join(ROOT, ".env")):
        shutil.copy2(os.path.join(ROOT, ".env"), env_release)
        print("Copiado .env del proyecto a release/")

    if os.path.isdir(BUILD):
        shutil.rmtree(BUILD, ignore_errors=True)

    print("\nListo. Carpeta release/:")
    for name in sorted(os.listdir(RELEASE)):
        path = os.path.join(RELEASE, name)
        if os.path.isfile(path):
            print(f"  - {name} ({os.path.getsize(path) / (1024 * 1024):.1f} MB)")


if __name__ == "__main__":
    main()
