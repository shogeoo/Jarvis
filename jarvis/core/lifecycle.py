"""Cooperative shutdown and ownership of background work."""

from __future__ import annotations

import os
import signal
import subprocess
import threading
import time


def terminate_process(proc: subprocess.Popen, *, group: bool = False) -> None:
    """Terminate only an owned process (or its explicitly created session)."""
    def send(sig):
        try:
            if group:
                os.killpg(proc.pid, sig)
            elif proc.poll() is None:
                proc.send_signal(sig)
        except ProcessLookupError:
            pass

    send(signal.SIGTERM)
    try:
        proc.wait(timeout=2)
    except subprocess.TimeoutExpired:
        send(signal.SIGKILL)
        proc.wait(timeout=2)
    finally:
        # The session leader may exit before its children.
        if group:
            send(signal.SIGKILL)


class ProcessManager:
    """Own shell commands, including their children, across runtime shutdown."""

    def __init__(self):
        self._lock = threading.Lock()
        self._closed = False
        self._processes = set()

    def start(self, *args, **kwargs):
        with self._lock:
            if self._closed:
                raise RuntimeError("runtime_stopping")
            proc = subprocess.Popen(*args, **kwargs, start_new_session=True)
            self._processes.add(proc)
            return proc

    def finish(self, proc):
        with self._lock:
            self._processes.discard(proc)

    def stop(self):
        with self._lock:
            self._closed = True
            processes = list(self._processes)
        workers = []
        for proc in processes:
            thread = threading.Thread(
                target=terminate_process, args=(proc,),
                kwargs={"group": True}, daemon=True,
            )
            thread.start()
            workers.append(thread)
        deadline = time.monotonic() + 4.5
        for thread in workers:
            thread.join(max(0, deadline - time.monotonic()))
