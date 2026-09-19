"""Cooperative shutdown and ownership of background work."""

from __future__ import annotations

import os
import queue
import signal
import subprocess
import threading
import time
from concurrent.futures import Future


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


class ActionPool:
    """Bounded daemon workers: an uncooperative plugin cannot block Python exit.

    Running Python functions cannot be forcibly cancelled. They receive the
    runtime stop token; owned child processes are terminated separately.
    """

    def __init__(self, max_workers: int):
        if max_workers < 1:
            raise ValueError("max_workers must be positive")
        self._jobs = queue.Queue()
        self._lock = threading.Lock()
        self._closed = False
        self._threads = []
        for index in range(max_workers):
            thread = threading.Thread(
                target=self._run, name=f"jarvis-action-{index}", daemon=True
            )
            thread.start()
            self._threads.append(thread)

    def submit(self, fn, *args) -> Future:
        with self._lock:
            if self._closed:
                raise RuntimeError("runtime_stopping")
            future = Future()
            self._jobs.put((future, fn, args))
            return future

    def _run(self):
        while (job := self._jobs.get()) is not None:
            future, fn, args = job
            if not future.set_running_or_notify_cancel():
                continue
            try:
                future.set_result(fn(*args))
            except BaseException as exc:
                future.set_exception(exc)

    def shutdown(self, *, timeout: float = 2.0):
        with self._lock:
            if not self._closed:
                self._closed = True
                while True:
                    try:
                        job = self._jobs.get_nowait()
                    except queue.Empty:
                        break
                    if job is not None:
                        job[0].cancel()
                for _ in self._threads:
                    self._jobs.put(None)
        deadline = time.monotonic() + timeout
        for thread in self._threads:
            thread.join(max(0, deadline - time.monotonic()))


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
