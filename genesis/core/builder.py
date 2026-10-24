"""APK build engine: isolated workspace per build, protected template, Gradle wrapper, APK verification."""

from __future__ import annotations

import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from ..config import Settings
from .schemas import AppManifest
from .validation import SOURCE_ROOT, ValidatedFile, ValidationError

APK_REL = Path("app/build/outputs/apk/debug/app-debug.apk")
# Never copied from the template into a workspace.
TEMPLATE_EXCLUDES = {".gradle", "build", "local.properties", "genesis.properties", ".idea", ".kotlin"}
SAMPLE_SOURCE = "GenesisApp.kt"
MAX_LOG_BYTES = 5_000_000
GRADLE_MAX_FILE_BLOCKS = 8 * 1024 * 1024  # 512-byte blocks = 4 GiB


@dataclass
class GradleResult:
    exit_code: int | None
    output: str
    timed_out: bool
    seconds: float
    cancelled: bool = False

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out and not self.cancelled


@dataclass
class ApkInfo:
    path: Path
    size: int
    package: str | None
    signature_verified: bool
    checks: list[str]


class BuildError(RuntimeError):
    pass


def android_string_escape(text: str) -> str:
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    text = text.replace("\\", "\\\\").replace("'", "\\'").replace('"', '\\"')
    if text[:1] in ("@", "?"):
        text = "\\" + text
    return text


class AndroidBuilder:
    def __init__(self, settings: Settings):
        self.s = settings

    # ---------- workspace ----------
    def prepare_workspace(self, workspace: Path, manifest: AppManifest) -> None:
        if workspace.exists():
            raise BuildError(f"workspace already exists: {workspace}")
        self._ensure_inside(workspace, self.s.builds_dir)

        def ignore(dirpath: str, names: list[str]) -> set[str]:
            skip = {n for n in names if n in TEMPLATE_EXCLUDES}
            if Path(dirpath).resolve() == (self.s.template_dir / SOURCE_ROOT).resolve():
                skip.add(SAMPLE_SOURCE)
            return skip

        shutil.copytree(self.s.template_dir, workspace, ignore=ignore, symlinks=False)
        gradlew = workspace / "gradlew"
        if gradlew.exists():
            try:
                gradlew.chmod(0o755)
            except OSError:
                pass
        if not self.s.android_home:
            raise BuildError("Android SDK not found (set ANDROID_HOME)")
        (workspace / "local.properties").write_text(f"sdk.dir={self._prop_escape(str(self.s.android_home))}\n")
        (workspace / "genesis.properties").write_text(f"applicationId={manifest.package_id}\nversionName=1.0\n")
        argb = "0xFF" + manifest.primary_color.lstrip("#").upper()
        (workspace / SOURCE_ROOT / "ui/theme/ThemeConfig.kt").write_text(
            "// GENESIS PROTECTED FILE — rewritten by the server from validated manifest values.\n"
            "package com.genesis.app.ui.theme\n\n"
            f"internal const val GENESIS_PRIMARY_COLOR: Long = {argb}\n"
            f'internal const val GENESIS_THEME_MODE: String = "{manifest.theme_mode.value}"\n'
        )
        res = workspace / "app/src/main/res/values"
        (res / "strings.xml").write_text(
            '<?xml version="1.0" encoding="utf-8"?>\n<resources>\n'
            f'    <string name="app_name">{android_string_escape(manifest.app_name)}</string>\n</resources>\n'
        )
        (res / "colors.xml").write_text(
            '<?xml version="1.0" encoding="utf-8"?>\n<resources>\n'
            f'    <color name="genesis_icon_background">#FF{manifest.primary_color.lstrip("#").upper()}</color>\n</resources>\n'
        )

    def write_sources(self, workspace: Path, files: list[ValidatedFile]) -> None:
        src_root = (workspace / SOURCE_ROOT).resolve()
        for f in files:
            target = (src_root / f.rel_path).resolve()
            # Defense in depth: validation already normalized the path.
            if src_root not in target.parents:
                raise ValidationError(f"refusing to write outside the source root: {f.rel_path}")
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.is_symlink():
                raise ValidationError(f"refusing to write through a symlink: {f.rel_path}")
            target.write_text(f.content, encoding="utf-8")

    def read_sources(self, workspace: Path) -> dict[str, str]:
        src_root = workspace / SOURCE_ROOT
        out = {}
        for p in sorted(src_root.rglob("*.kt")):
            rel = p.relative_to(src_root).as_posix()
            if rel != "MainActivity.kt" and not rel.startswith(("ui/theme/", "ui/kit/")):
                out[rel] = p.read_text(encoding="utf-8")
        return out

    # ---------- gradle ----------
    def gradle_env(self) -> dict[str, str]:
        """Minimal environment: secrets such as GEMINI_API_KEY are never passed to Gradle."""
        if not self.s.java_home:
            raise BuildError("JDK 17+ not found (set JAVA_HOME)")
        if sys.platform == "win32":
            sys_root = os.environ.get("SystemRoot", r"C:\Windows")
            path_parts = [
                str(self.s.java_home / "bin"),
                os.path.join(sys_root, "system32"),
                sys_root,
                os.path.join(sys_root, "System32", "Wbem"),
            ]
            env = {
                "PATH": ";".join(path_parts),
                "USERPROFILE": str(Path.home()),
                "HOME": str(Path.home()),
                "JAVA_HOME": str(self.s.java_home),
                "ANDROID_HOME": str(self.s.android_home or ""),
                "ANDROID_SDK_ROOT": str(self.s.android_home or ""),
                "GRADLE_USER_HOME": str(self.s.gradle_user_home),
                "SystemRoot": sys_root,
                "SystemDrive": os.environ.get("SystemDrive", "C:"),
                "COMSPEC": os.environ.get("COMSPEC", os.path.join(sys_root, "system32", "cmd.exe")),
                "PATHEXT": os.environ.get("PATHEXT", ".COM;.EXE;.BAT;.CMD"),
                "TEMP": os.environ.get("TEMP", os.environ.get("TMP", str(Path.home() / "AppData/Local/Temp"))),
                "TMP": os.environ.get("TMP", os.environ.get("TEMP", str(Path.home() / "AppData/Local/Temp"))),
                "CI": "true",
            }
        else:
            env = {
                "PATH": f"{self.s.java_home / 'bin'}:/usr/bin:/bin:/usr/sbin:/sbin",
                "HOME": str(Path.home()),
                "JAVA_HOME": str(self.s.java_home),
                "ANDROID_HOME": str(self.s.android_home or ""),
                "ANDROID_SDK_ROOT": str(self.s.android_home or ""),
                "GRADLE_USER_HOME": str(self.s.gradle_user_home),
                "LANG": "en_US.UTF-8",
                "TMPDIR": os.environ.get("TMPDIR", "/tmp"),
                "CI": "true",
            }
        return env

    def run_gradle(self, workspace: Path, log_path: Path, on_line: Callable[[str], None] | None = None,
                   task: str = "assembleDebug", cancel: threading.Event | None = None) -> GradleResult:
        if task not in {"assembleDebug"}:
            raise BuildError(f"task not allowed: {task}")
        self.s.gradle_user_home.mkdir(parents=True, exist_ok=True)
        is_win = sys.platform == "win32"
        wrapper = workspace / ("gradlew.bat" if is_win else "gradlew")
        gradle = [str(wrapper), task, "--console=plain", "--warning-mode=none", "-Dorg.gradle.welcome=never"]
        if is_win:
            # On Windows, cmd.exe /c executes the batch wrapper. ulimit is not supported on Windows;
            # resource safety is enforced via watchdog timeout, log size bounds, and process-tree termination.
            cmd = ["cmd.exe", "/c", *gradle]
            popen_kwargs = {"creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200)}
        else:
            # Resource limits for the whole Gradle process tree: no core dumps, no file larger than 4 GiB.
            # (Applied with ulimit instead of preexec_fn, which is unsafe in this multi-threaded server.)
            cmd = ["/bin/sh", "-c", f'ulimit -c 0; ulimit -f {GRADLE_MAX_FILE_BLOCKS}; exec "$0" "$@"', *gradle]
            popen_kwargs = {"start_new_session": True}
        start = time.monotonic()
        lines: list[str] = []
        written = 0
        with open(log_path, "a", encoding="utf-8") as log:
            log.write(f"$ {'./gradlew.bat' if is_win else './gradlew'} {task}\n")
            proc = subprocess.Popen(
                cmd, cwd=workspace, env=self.gradle_env(), stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1, **popen_kwargs,
            )
            timed_out = threading.Event()
            cancelled = threading.Event()
            finished = threading.Event()

            def kill() -> None:
                if is_win:
                    try:
                        subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                                       capture_output=True, timeout=10)
                    except Exception:
                        proc.kill()
                else:
                    try:
                        os.killpg(proc.pid, signal.SIGTERM)
                        time.sleep(5)
                        os.killpg(proc.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass

            def watchdog() -> None:
                # Ends the whole Gradle process group on timeout or when the user cancels the build.
                deadline = start + self.s.build_timeout
                while not finished.wait(0.5):
                    if cancel is not None and cancel.is_set():
                        cancelled.set()
                        return kill()
                    if time.monotonic() >= deadline:
                        timed_out.set()
                        return kill()

            timer = threading.Thread(target=watchdog, daemon=True, name="gradle-watchdog")
            timer.start()
            try:
                assert proc.stdout is not None
                for line in proc.stdout:
                    lines.append(line)
                    if written < MAX_LOG_BYTES:
                        log.write(line)
                        written += len(line)
                    if on_line:
                        on_line(line.rstrip("\n"))
                proc.wait()
            finally:
                finished.set()
            if timed_out.is_set():
                log.write(f"\n!! Build timed out after {self.s.build_timeout}s and was terminated.\n")
            if cancelled.is_set():
                log.write("\n!! Build cancelled by the user; Gradle was terminated.\n")
        return GradleResult(proc.returncode, "".join(lines), timed_out.is_set(), time.monotonic() - start,
                            cancelled.is_set())

    # ---------- verification ----------
    def verify_apk(self, workspace: Path, expected_package: str) -> ApkInfo:
        apk = workspace / APK_REL
        checks: list[str] = []
        if not apk.is_file():
            raise BuildError(f"expected APK not found at {APK_REL}")
        size = apk.stat().st_size
        if size < 10_000:
            raise BuildError(f"APK is suspiciously small ({size} bytes)")
        checks.append(f"APK exists ({size:,} bytes)")
        try:
            with zipfile.ZipFile(apk) as z:
                names = set(z.namelist())
                bad = z.testzip()
        except zipfile.BadZipFile as e:
            raise BuildError(f"APK is not a valid zip archive: {e}") from e
        if bad:
            raise BuildError(f"APK has a corrupt entry: {bad}")
        for required in ("AndroidManifest.xml", "classes.dex", "resources.arsc"):
            if required not in names:
                raise BuildError(f"APK is missing {required}")
        checks.append("archive intact; contains AndroidManifest.xml, classes.dex, resources.arsc")

        package = None
        bt = self.s.build_tools_dir()
        env = self.gradle_env()
        is_win = sys.platform == "win32"
        aapt2_bin = None
        if bt:
            for cand in (bt / "aapt2.exe", bt / "aapt2"):
                if cand.is_file():
                    aapt2_bin = cand
                    break
        if aapt2_bin:
            out = subprocess.run([str(aapt2_bin), "dump", "badging", str(apk)], capture_output=True, text=True, timeout=60, env=env)
            m = re.search(r"package: name='([^']+)'", out.stdout)
            package = m.group(1) if m else None
            if package != expected_package:
                raise BuildError(f"APK package is {package!r}, expected {expected_package!r}")
            checks.append(f"aapt2: package name {package}")
            # AndroidX core adds one signature-level internal permission; anything else is unexpected.
            allowed = {f"{expected_package}.DYNAMIC_RECEIVER_NOT_EXPORTED_PERMISSION"}
            perms = set(re.findall(r"uses-permission: name='([^']+)'", out.stdout))
            if perms - allowed:
                raise BuildError(f"APK requests unexpected permissions: {sorted(perms - allowed)}")
            checks.append("aapt2: no user-facing permissions requested")
        signed = False
        apksigner_bin = None
        if bt:
            for cand in (bt / "apksigner.bat", bt / "apksigner.exe", bt / "apksigner"):
                if cand.is_file():
                    apksigner_bin = cand
                    break
        if apksigner_bin:
            cmd = ["cmd.exe", "/c", str(apksigner_bin), "verify", str(apk)] if apksigner_bin.name.endswith((".bat", ".cmd")) else [str(apksigner_bin), "verify", str(apk)]
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=120, env=env)
            signed = out.returncode == 0
            if not signed:
                raise BuildError(f"apksigner verify failed: {(out.stdout + out.stderr).strip()[:500]}")
            checks.append("apksigner: signature verified (debug key)")
        return ApkInfo(apk, size, package, signed, checks)

    # ---------- cleanup ----------
    def cleanup_workspace(self, workspace: Path) -> None:
        """Remove bulky intermediates but keep sources and logs for inspection."""
        for sub in ("app/build", "build", ".gradle", ".kotlin"):
            target = workspace / sub
            if target.exists():
                self._ensure_inside(target, self.s.builds_dir)
                shutil.rmtree(target, ignore_errors=True)

    @staticmethod
    def _ensure_inside(path: Path, parent: Path) -> None:
        if parent.resolve() not in path.resolve().parents:
            raise BuildError(f"{path} is outside {parent}")

    @staticmethod
    def _prop_escape(value: str) -> str:
        return value.replace("\\", "\\\\").replace(":", "\\:").replace("=", "\\=").replace(" ", "\\ ")
