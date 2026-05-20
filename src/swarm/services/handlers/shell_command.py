"""Shell command service handler — run a command and capture output."""

from __future__ import annotations

import asyncio
from typing import Any, ClassVar

from swarm.logging import get_logger
from swarm.services.registry import ServiceContext, ServiceResult

_log = get_logger("services.shell_command")
_DEFAULT_TIMEOUT = 60


class ShellCommand:
    """Execute a shell command as an automated pipeline step."""

    description = "Run a shell command and capture stdout/stderr."
    example_config: ClassVar[dict[str, Any]] = {
        "command": "echo hello",
        "timeout": 60,
        "cwd": "",
    }

    async def execute(self, config: dict[str, Any], context: ServiceContext) -> ServiceResult:
        command = config.get("command", "")
        if not command:
            return ServiceResult(success=False, error="command is required")

        timeout = config.get("timeout", _DEFAULT_TIMEOUT)
        cwd = config.get("cwd") or None

        try:
            proc = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            stdout_str = stdout.decode("utf-8", errors="replace").strip()
            stderr_str = stderr.decode("utf-8", errors="replace").strip()

            if proc.returncode != 0:
                return ServiceResult(
                    success=False,
                    data={
                        "stdout": stdout_str,
                        "stderr": stderr_str,
                        "returncode": proc.returncode,
                    },
                    error=f"Command exited with code {proc.returncode}",
                )

            return ServiceResult(
                success=True,
                data={"stdout": stdout_str, "stderr": stderr_str, "returncode": 0},
            )
        except TimeoutError:
            return ServiceResult(success=False, error=f"Command timed out after {timeout}s")
        except Exception as e:
            _log.warning("shell command failed: %s", e)
            return ServiceResult(success=False, error=str(e))
