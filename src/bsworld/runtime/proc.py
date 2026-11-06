# Copyright 2026 Xiaomi Corporation
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
"""ProcessManager: spawn each child into its own session group so cleanup can
take down the whole process tree, log into a per-process file, and kill in
reverse spawn order on shutdown.
"""

from __future__ import annotations

import os
import signal
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from types import FrameType

from ..deps import BSWorldSetupError
from ..log import log, warn

SignalHandler = signal.Handlers | Callable[[int, FrameType | None], None]


@dataclass
class Proc:
    name: str
    popen: subprocess.Popen
    log_path: Path
    log_fh: object  # file object kept alive while the child runs
    owns_log_fh: bool = True

    @property
    def pid(self) -> int:
        return self.popen.pid

    def alive(self) -> bool:
        return self.popen.poll() is None


@dataclass
class ProcessManager:
    procs: list[Proc] = field(default_factory=list)
    _signals_installed: bool = False
    _signal_handler: SignalHandler | None = field(default=None, init=False, repr=False)
    _previous_signal_handlers: dict[signal.Signals, SignalHandler] = field(
        default_factory=dict, init=False, repr=False
    )

    def spawn(
        self,
        name: str,
        cmd: list,
        *,
        cwd: Path,
        log_dir: Path,
        stdin: int | None = None,
        stdout: int | None = None,
        stderr: int | None = None,
        log_fh: object | None = None,
        env: dict[str, str] | None = None,
        pass_fds: Sequence[int] = (),
    ) -> Proc:
        """Spawn a background process.

        stdout/stderr can be pre-opened fds, such as a FIFO write end;
        if omitted, stdout+stderr are redirected into log_fh directly.
        pass_fds keeps further descriptors open in the child, at the same
        numbers.
        """
        if not Path(cmd[0]).exists():
            raise BSWorldSetupError(f"missing executable: {cmd[0]}")
        if not cwd.exists() or not cwd.is_dir():
            raise BSWorldSetupError(f"spawn {name}: cwd not found: {cwd}")

        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / f"{name}.log"

        owns_log_fh = False
        if log_fh is None:
            log_fh = open(log_path, "w", buffering=1)
            owns_log_fh = True
            if stdout is None:
                stdout = log_fh
            if stderr is None:
                stderr = subprocess.STDOUT

        argv = [str(c) for c in cmd]
        log(f"spawn {name}: {' '.join(argv)} (cwd={cwd})")
        log(f"    log -> {log_path}")

        try:
            popen = subprocess.Popen(
                argv,
                cwd=str(cwd),
                stdin=stdin if stdin is not None else subprocess.DEVNULL,
                stdout=stdout,
                stderr=stderr,
                start_new_session=True,
                close_fds=True,
                pass_fds=pass_fds,
                env=env,
            )
        except Exception:
            if owns_log_fh:
                try:
                    log_fh.close()
                except Exception:
                    pass
            raise
        log(f"    pid={popen.pid}")

        proc = Proc(
            name=name,
            popen=popen,
            log_path=log_path,
            log_fh=log_fh,
            owns_log_fh=owns_log_fh,
        )
        self.procs.append(proc)
        return proc

    def stop(self, proc: Proc) -> None:
        """Stop one owned process while keeping other PHY participants alive."""
        if proc not in self.procs:
            return
        self._report_exit(proc)
        if proc.alive():
            self._kill_session(proc.pid, signal.SIGKILL)
        self._wait_and_close(proc)
        self.procs.remove(proc)

    def _report_exit(self, proc: Proc) -> None:
        rc = proc.popen.poll()
        if rc is not None and rc != 0:
            warn(f"{proc.name}: exited unexpectedly (rc={rc})")
            self._dump_tail(proc, lines=20)

    @staticmethod
    def _wait_and_close(proc: Proc) -> None:
        try:
            proc.popen.wait(timeout=5)
        except subprocess.TimeoutExpired:
            warn(f"{proc.name}: process did not exit after SIGKILL")
        if proc.owns_log_fh:
            try:
                proc.log_fh.close()
            except Exception as exc:
                warn(f"{proc.name}: failed to close log file: {exc}")

    def _dump_tail(self, proc: Proc, *, lines: int) -> None:
        try:
            with open(proc.log_path, "r", errors="replace") as fh:
                tail = fh.readlines()[-lines:]
        except FileNotFoundError:
            return
        if not tail:
            return
        log(f"--- last {len(tail)} log line(s) ---", tag=proc.name)
        for line in tail:
            log(line.rstrip("\n"), tag=proc.name)

    def cleanup(self) -> None:
        if not self.procs:
            return
        for proc in self.procs:
            self._report_exit(proc)
        living = sum(1 for proc in self.procs if proc.alive())
        if living:
            log(f"stopping {living} background process(es)")
        for proc in reversed(self.procs):
            if proc.alive():
                log(f"    kill {proc.name} (pid={proc.pid})")
                self._kill_session(proc.pid, signal.SIGKILL)
        for proc in self.procs:
            self._wait_and_close(proc)
        self.procs.clear()

    @staticmethod
    def _kill_session(pid: int, sig: int) -> None:
        try:
            os.killpg(pid, sig)
        except ProcessLookupError:
            return
        except PermissionError:
            try:
                os.kill(pid, sig)
            except ProcessLookupError:
                return

    def install_signal_handlers(self) -> None:
        if self._signals_installed:
            return
        self._signals_installed = True

        def _handler(signum, _frame):
            log(f"caught signal {signum}; cleaning up")
            try:
                self.cleanup()
            finally:
                self.restore_signal_handlers()
            raise SystemExit(128 + signum)

        self._signal_handler = _handler
        for s in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            self._previous_signal_handlers[s] = signal.getsignal(s)
            signal.signal(s, _handler)

    def restore_signal_handlers(self) -> None:
        if not self._signals_installed:
            return
        for sig, previous in self._previous_signal_handlers.items():
            if signal.getsignal(sig) is self._signal_handler:
                signal.signal(sig, previous)
        self._previous_signal_handlers.clear()
        self._signal_handler = None
        self._signals_installed = False


__all__ = ["Proc", "ProcessManager"]
