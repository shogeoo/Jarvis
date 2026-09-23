import ctypes
import os
import signal
import subprocess
from pathlib import Path

from jarvis.capabilities import action_definition
from jarvis.core.protocol import object_schema


def run(data, context):
    command = data["command"]
    if not command.strip():
        raise ValueError("command must not be empty")
    project_root = Path(context.config.project_root).resolve()
    cwd = project_root
    if data.get("cwd"):
        cwd = (project_root / data["cwd"]).resolve()
        if cwd != project_root and project_root not in cwd.parents:
            raise ValueError("cwd is outside the project")
    def child_setup():
        os.setsid()
        ctypes.CDLL("libc.so.6", use_errno=True).prctl(1, signal.SIGKILL)

    process = subprocess.Popen(
        command,
        shell=True,
        executable="/bin/bash",
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        preexec_fn=child_setup,
    )
    stdout, stderr = process.communicate()
    return {
        "exit_code": process.returncode,
        "stdout": stdout,
        "stderr": stderr,
    }


def create_action():
    return action_definition(
        "Run one non-interactive bash command synchronously. There is no action timeout; stdout and stderr are returned after the command exits.",
        object_schema({"command": {"type": "string"}, "cwd": {"type": ["string", "null"]}}),
        object_schema(
            {
                "exit_code": {"type": "integer"},
                "stdout": {"type": "string"},
                "stderr": {"type": "string"},
            }
        ),
        run,
    )
