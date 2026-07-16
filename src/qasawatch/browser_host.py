"""Lifecycle manager for an externally supervised, persistent Chrome process."""

from __future__ import annotations

import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import uuid
from collections.abc import Sequence
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import IO
from urllib.request import urlopen


class BrowserHostError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class BrowserDescriptor:
    pid: int
    port: int
    owner_token: str
    executable: str
    profile_dir: str
    started_at: float


def find_chrome() -> Path:
    candidates: list[Path] = []
    configured = os.getenv("QASAWATCH_CHROME_EXECUTABLE")
    if configured:
        candidate = Path(configured).expanduser()
        if candidate.is_file():
            return candidate.absolute()
        raise BrowserHostError(
            f"QASAWATCH_CHROME_EXECUTABLE does not point to a file: {candidate}"
        )
    if sys.platform == "darwin":
        candidates.extend(
            [
                Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
                Path.home()
                / "Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            ]
        )
    elif os.name == "nt":
        for env in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
            if root := os.getenv(env):
                candidates.append(Path(root) / "Google/Chrome/Application/chrome.exe")
    else:
        for name in (
            "google-chrome",
            "google-chrome-stable",
            "google-chrome-beta",
            "google-chrome-unstable",
            "chromium",
            "chromium-browser",
        ):
            if found := shutil.which(name):
                candidates.append(Path(found))
        candidates.extend(
            [
                Path("/opt/google/chrome/chrome"),
                Path("/usr/lib/chromium/chromium"),
                Path("/usr/lib/chromium-browser/chromium-browser"),
            ]
        )
    for candidate in candidates:
        if candidate.is_file():
            # Keep launcher symlinks intact. Ubuntu's /snap/bin/chromium points
            # at the generic /usr/bin/snap multiplexer; resolving it would drop
            # the required "chromium" application name and Snap exits with 64.
            return candidate.absolute()
    raise BrowserHostError("Google Chrome executable was not found")


class ProfileLock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.file: IO[bytes] | None = None

    def acquire(self) -> None:
        if self.file is not None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.file = self.path.open("a+b")
        try:
            if os.name == "nt":
                import msvcrt
                self.file.seek(0); msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self.file.close(); self.file = None
            raise BrowserHostError(f"Chrome profile is already supervised: {self.path.parent}") from exc

    def release(self) -> None:
        if not self.file: return
        try:
            if os.name == "nt":
                import msvcrt
                self.file.seek(0); msvcrt.locking(self.file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(), fcntl.LOCK_UN)
        finally:
            self.file.close(); self.file = None


class ChromeHost:
    """Starts or adopts only Chrome instances carrying our owner token.

    Releasing this object deliberately does not terminate Chrome: manual browser
    access remains available when the application exits.
    """

    def __init__(self, state_dir: Path, *, executable: Path | None = None, port: int | None = None) -> None:
        self.state_dir = Path(state_dir)
        self.profile_dir = self.state_dir / "chrome-profile"
        self.descriptor_path = self.state_dir / "chrome.json"
        self.executable = executable
        self.port = port
        self._lock = ProfileLock(self.state_dir / "profile.supervisor.lock")

    def start_or_adopt(self, *, timeout: float = 30.0) -> BrowserDescriptor:
        self.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.profile_dir.mkdir(mode=0o700, exist_ok=True)
        if os.name != "nt":
            self.state_dir.chmod(0o700)
            self.profile_dir.chmod(0o700)
        self._lock.acquire()
        descriptor = self.read_descriptor()
        owned = self._refresh_owned_descriptor(descriptor) if descriptor else None
        if owned:
            if owned != descriptor:
                self._write_descriptor(owned)
            if self._cdp_healthy(owned.port):
                return owned
            self._terminate_owned(owned)
            deadline = time.monotonic() + min(timeout, 5.0)
            while self._refresh_owned_descriptor(owned) and time.monotonic() < deadline:
                time.sleep(0.05)
        if (
            sys.platform.startswith("linux")
            and not os.getenv("DISPLAY")
            and not os.getenv("WAYLAND_DISPLAY")
        ):
            raise BrowserHostError(
                "Chrome needs a graphical display. Start QasaWatch from a Linux "
                "desktop session, or set DISPLAY for an X11/Xvfb session."
            )
        chrome = (self.executable or find_chrome()).expanduser().absolute()
        port = self.port or _free_loopback_port()
        token = uuid.uuid4().hex
        args = [str(chrome), f"--remote-debugging-port={port}", "--remote-debugging-address=127.0.0.1",
                f"--user-data-dir={self.profile_dir.resolve()}", f"--qasawatch-owner={token}", "--no-first-run", "--no-default-browser-check"]
        flags = 0
        if os.name == "nt":
            flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        stderr_fd, stderr_name = tempfile.mkstemp(
            prefix="chrome-startup-", suffix=".log", dir=self.state_dir
        )
        stderr_path = Path(stderr_name)
        try:
            with os.fdopen(stderr_fd, "wb") as stderr:
                try:
                    process = subprocess.Popen(
                        args,
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.DEVNULL,
                        stderr=stderr,
                        close_fds=True,
                        creationflags=flags,
                    )
                except OSError as exc:
                    raise BrowserHostError(
                        f"Chrome could not be started from {chrome} "
                        f"({type(exc).__name__})"
                    ) from exc
            descriptor = BrowserDescriptor(process.pid, port, token, str(chrome), str(self.profile_dir.resolve()), time.time())
            self._write_descriptor(descriptor)
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                returncode = process.poll()
                if self._cdp_healthy(port):
                    handed_off = self._refresh_owned_descriptor(descriptor)
                    if handed_off is not None and (
                        returncode is None or handed_off.pid != process.pid
                    ):
                        if handed_off != descriptor:
                            self._write_descriptor(handed_off)
                        return handed_off
                if returncode is not None:
                    handed_off = self._refresh_owned_descriptor(descriptor)
                    if handed_off is not None:
                        descriptor = handed_off
                        self._write_descriptor(descriptor)
                    elif returncode != 0:
                        raise BrowserHostError(
                            _chrome_startup_error(returncode, stderr_path)
                        )
                    # Chrome on Windows can exit its launcher with code 0 before
                    # the long-lived browser process and CDP listener are visible.
                time.sleep(0.1)
            raise BrowserHostError(_chrome_startup_error(None, stderr_path))
        finally:
            stderr_path.unlink(missing_ok=True)

    def recover(self, *, timeout: float = 30.0) -> BrowserDescriptor:
        old = self.read_descriptor()
        owned = self._refresh_owned_descriptor(old) if old else None
        if owned:
            self._terminate_owned(owned)
            deadline = time.monotonic() + min(timeout, 5.0)
            while self._refresh_owned_descriptor(owned) and time.monotonic() < deadline:
                time.sleep(0.05)
        return self.start_or_adopt(timeout=timeout)

    def read_descriptor(self) -> BrowserDescriptor | None:
        try:
            return BrowserDescriptor(**json.loads(self.descriptor_path.read_text("utf-8")))
        except (OSError, ValueError, TypeError):
            return None

    def owns_process(self, descriptor: BrowserDescriptor) -> bool:
        """Validate PID against the exact private launch markers."""
        command = _process_command_line(descriptor.pid)
        return bool(command and self._command_matches(descriptor, command))

    def descriptor_healthy(self, descriptor: BrowserDescriptor | None = None) -> bool:
        current = descriptor or self.read_descriptor()
        if current is None:
            return False
        owned = self._refresh_owned_descriptor(current)
        return bool(owned and self._cdp_healthy(owned.port))

    def _refresh_owned_descriptor(
        self, descriptor: BrowserDescriptor
    ) -> BrowserDescriptor | None:
        """Resolve Chrome launcher-to-browser PID handoff without losing ownership."""

        if self.owns_process(descriptor):
            return descriptor
        for pid, command in _iter_process_command_lines():
            if self._command_matches(descriptor, command):
                return replace(descriptor, pid=pid)
        return None

    @staticmethod
    def _command_matches(
        descriptor: BrowserDescriptor, command: str | Sequence[str]
    ) -> bool:
        # Linux launchers commonly hand off from a wrapper such as
        # `google-chrome` to `/opt/google/chrome/chrome`. The executable name is
        # therefore not a stable ownership marker. These values are supplied
        # only to QasaWatch's dedicated browser and remain unchanged across the
        # handoff.
        markers = (
            (f"--qasawatch-owner={descriptor.owner_token}", False),
            (f"--user-data-dir={descriptor.profile_dir}", os.name == "nt"),
            (f"--remote-debugging-port={descriptor.port}", False),
        )
        return all(
            _contains_exact_argument(command, marker, ignore_case=ignore_case)
            for marker, ignore_case in markers
        )

    def close(self) -> None:
        self._lock.release()

    def _write_descriptor(self, descriptor: BrowserDescriptor) -> None:
        fd, temporary = tempfile.mkstemp(prefix="chrome-", suffix=".tmp", dir=self.state_dir)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(asdict(descriptor), handle); handle.flush(); os.fsync(handle.fileno())
            os.replace(temporary, self.descriptor_path)
        finally:
            if os.path.exists(temporary): os.unlink(temporary)

    @staticmethod
    def _cdp_healthy(port: int) -> bool:
        try:
            with urlopen(f"http://127.0.0.1:{port}/json/version", timeout=0.5) as response:
                return response.status == 200 and "webSocketDebuggerUrl" in json.load(response)
        except Exception:
            return False

    @staticmethod
    def _terminate_owned(descriptor: BrowserDescriptor) -> None:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(descriptor.pid), "/T", "/F"], capture_output=True, check=False)
        else:
            try:
                os.kill(descriptor.pid, 15)
            except ProcessLookupError:
                # Chrome may exit after ownership was checked.
                return
            except PermissionError as exc:
                raise BrowserHostError(
                    f"QasaWatch cannot stop its Chrome process (PID {descriptor.pid})"
                ) from exc


def _free_loopback_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0)); return int(sock.getsockname()[1])


def _chrome_startup_error(returncode: int | None, stderr_path: Path) -> str:
    try:
        diagnostic = stderr_path.read_text(
            encoding="utf-8", errors="replace"
        )[-131_072:]
    except OSError:
        diagnostic = ""
    lowered = diagnostic.lower()
    if "no usable sandbox" in lowered:
        return (
            "Chrome's Linux sandbox is unavailable. Install Google Chrome or "
            "Chromium through the operating system so its Ubuntu AppArmor "
            "profile is installed, or ask the administrator to permit Chrome "
            "user namespaces. Do not start QasaWatch with sudo."
        )
    if "missing x server" in lowered or "cannot open display" in lowered:
        return (
            "Chrome could not reach the graphical display. Start QasaWatch "
            "from a Linux desktop session, or configure DISPLAY for X11/Xvfb."
        )
    if "error while loading shared libraries" in lowered:
        return (
            "Chrome is missing required Linux shared libraries. Install Chrome "
            "or Chromium through the operating system and restart QasaWatch."
        )
    if returncode is None:
        return (
            "Chrome DevTools endpoint did not become ready. Verify the Chrome "
            "installation and graphical display."
        )
    return (
        f"Chrome exited during startup ({returncode}). Verify the Chrome "
        "installation and graphical display."
    )


def _contains_exact_argument(
    command: str | Sequence[str], argument: str, *, ignore_case: bool = False
) -> bool:
    """Match one command-line argument, allowing platform-added quoting."""
    if not isinstance(command, str):
        if len(command) == 1 and any(character.isspace() for character in command[0]):
            # Snap Chromium can expose its rewritten browser command as one
            # NUL-delimited /proc entry instead of conventional argv entries.
            return _contains_exact_argument(
                command[0], argument, ignore_case=ignore_case
            )
        if ignore_case:
            expected = argument.casefold()
            return any(value.casefold() == expected for value in command)
        return argument in command
    pattern = rf'(?:(?<=^)|(?<=\s))"?{re.escape(argument)}"?(?=\s|$)'
    flags = re.IGNORECASE if ignore_case else 0
    return re.search(pattern, command, flags=flags) is not None


def _process_command_line(pid: int) -> str | tuple[str, ...] | None:
    if os.name != "nt":
        if sys.platform.startswith("linux"):
            try:
                raw = Path(f"/proc/{pid}/cmdline").read_bytes()
                return tuple(
                    value.decode(errors="replace")
                    for value in raw.split(b"\0")
                    if value
                )
            except OSError:
                return None
        try:
            result = subprocess.run(
                ["ps", "-p", str(pid), "-o", "command="],
                capture_output=True,
                text=True,
                timeout=3,
                check=False,
            )
            return result.stdout.strip() or None
        except (OSError, subprocess.TimeoutExpired):
            return None
    script = f"(Get-CimInstance Win32_Process -Filter 'ProcessId={pid}').CommandLine"
    try:
        result = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                                capture_output=True, text=True, timeout=3, check=False)
        return result.stdout.strip() or None
    except (OSError, subprocess.TimeoutExpired):
        return None


def _iter_process_command_lines() -> list[tuple[int, str | tuple[str, ...]]]:
    if os.name != "nt":
        if sys.platform.startswith("linux"):
            values: list[tuple[int, str | tuple[str, ...]]] = []
            for directory in Path("/proc").glob("[0-9]*"):
                try:
                    raw = (directory / "cmdline").read_bytes()
                    command = tuple(
                        value.decode(errors="replace")
                        for value in raw.split(b"\0")
                        if value
                    )
                    if command:
                        values.append((int(directory.name), command))
                except (OSError, ValueError):
                    continue
            return values
        try:
            result = subprocess.run(
                ["ps", "-axo", "pid=,command="],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            values = []
            for line in result.stdout.splitlines():
                fields = line.strip().split(None, 1)
                if len(fields) == 2:
                    try:
                        values.append((int(fields[0]), fields[1]))
                    except ValueError:
                        continue
            return values
        except (OSError, subprocess.TimeoutExpired):
            return []
    script = (
        "Get-CimInstance Win32_Process -Filter \"Name='chrome.exe'\" | "
        "Select-Object ProcessId,CommandLine | ConvertTo-Json -Compress"
    )
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if not result.stdout.strip():
            return []
        payload = json.loads(result.stdout)
        rows = payload if isinstance(payload, list) else [payload]
        return [
            (int(row["ProcessId"]), str(row["CommandLine"]))
            for row in rows
            if row.get("ProcessId") and row.get("CommandLine")
        ]
    except (OSError, ValueError, TypeError, subprocess.TimeoutExpired):
        return []
