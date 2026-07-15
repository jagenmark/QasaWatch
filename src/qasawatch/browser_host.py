"""Lifecycle manager for an externally supervised, persistent Chrome process."""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import tempfile
import time
import uuid
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
    if os.name == "nt":
        for env in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
            if root := os.getenv(env):
                candidates.append(Path(root) / "Google/Chrome/Application/chrome.exe")
    else:
        for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser"):
            if found := shutil.which(name):
                candidates.append(Path(found))
    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()
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

    def start_or_adopt(self, *, timeout: float = 15.0) -> BrowserDescriptor:
        self.state_dir.mkdir(parents=True, exist_ok=True); self.profile_dir.mkdir(exist_ok=True)
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
        chrome = (self.executable or find_chrome()).resolve()
        port = self.port or _free_loopback_port()
        token = uuid.uuid4().hex
        args = [str(chrome), f"--remote-debugging-port={port}", "--remote-debugging-address=127.0.0.1",
                f"--user-data-dir={self.profile_dir.resolve()}", f"--qasawatch-owner={token}", "--no-first-run", "--no-default-browser-check"]
        flags = 0
        if os.name == "nt":
            flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        process = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL, close_fds=True, creationflags=flags)
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
                    raise BrowserHostError(f"Chrome exited during startup ({returncode})")
                # Chrome on Windows can exit its launcher with code 0 before
                # the long-lived browser process and CDP listener are visible.
            time.sleep(0.1)
        raise BrowserHostError("Chrome DevTools endpoint did not become ready")

    def recover(self, *, timeout: float = 15.0) -> BrowserDescriptor:
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
        """Validate PID, executable, profile and unguessable launch token."""
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
    def _command_matches(descriptor: BrowserDescriptor, command: str) -> bool:
        folded = command.casefold()
        return (Path(descriptor.executable).name.casefold() in folded
                and f"--qasawatch-owner={descriptor.owner_token}".casefold() in folded
                and f"--user-data-dir={descriptor.profile_dir}".casefold() in folded)

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
            os.kill(descriptor.pid, 15)


def _free_loopback_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0)); return int(sock.getsockname()[1])


def _process_command_line(pid: int) -> str | None:
    if os.name != "nt":
        try: return Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace")
        except OSError: return None
    script = f"(Get-CimInstance Win32_Process -Filter 'ProcessId={pid}').CommandLine"
    try:
        result = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                                capture_output=True, text=True, timeout=3, check=False)
        return result.stdout.strip() or None
    except (OSError, subprocess.TimeoutExpired):
        return None


def _iter_process_command_lines() -> list[tuple[int, str]]:
    if os.name != "nt":
        values: list[tuple[int, str]] = []
        for directory in Path("/proc").glob("[0-9]*"):
            try:
                command = (directory / "cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace")
                if command:
                    values.append((int(directory.name), command))
            except (OSError, ValueError):
                continue
        return values
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
