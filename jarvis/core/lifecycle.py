"""Cooperative shutdown and ownership of background work."""

from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
import uuid


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
        self._owners = {}
        self._cancelled = set()
        self._calls = {}

    def reserve_call(self, agent_id, call_id):
        with self._lock:
            owner = (agent_id, call_id, uuid.uuid4().hex)
            self._calls[(agent_id, call_id)] = owner
            return owner

    def start(self, *args, owner=None, **kwargs):
        with self._lock:
            if self._closed or owner in self._cancelled:
                raise RuntimeError("runtime_stopping")
            proc = subprocess.Popen(*args, **kwargs, start_new_session=True)
            self._processes.add(proc)
            self._owners[proc] = owner
            return proc

    def finish(self, proc):
        with self._lock:
            self._processes.discard(proc)
            self._owners.pop(proc, None)

    def cancel_call(self, agent_id, call_id):
        with self._lock:
            owner = self._calls.get((agent_id, call_id))
            if owner is None:
                return
            self._cancelled.add(owner)
            processes = [proc for proc in self._processes if self._owners.get(proc) == owner]
        for proc in processes:
            terminate_process(proc, group=True)

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
