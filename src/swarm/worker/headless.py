"""Headless Claude service handler for automated pipeline steps."""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar

from swarm.logging import get_logger
from swarm.services.registry import ServiceContext, ServiceResult

_log = get_logger("worker.headless")

_DEFAULT_TIMEOUT = 120
_WORK_DIR = Path.home() / ".swarm" / "headless"


@dataclass
class HeadlessClaude:
    """Run a headless LLM subprocess as a pipeline service.

    NOT a Worker subclass — this is a service handler.  Results appear in
    the pipeline view, not the worker sidebar.
    """

    description = "Run a one-shot headless Claude/Gemini/Codex subprocess."
    example_config: ClassVar[dict[str, Any]] = {
        "prompt": "Summarize the work shipped today.",
        "provider": "claude",
        "output_format": "json",
        "max_turns": 5,
        "timeout": 120,
    }

    async def execute(
        self,
        config: dict[str, Any],
        context: ServiceContext,
    ) -> ServiceResult:
        prompt = config.get("prompt", "")
        if not prompt:
            return ServiceResult(success=False, error="Missing prompt in config")

        output_format = config.get("output_format", "json")
        max_turns: int | None = config.get("max_turns")
        timeout = config.get("timeout", _DEFAULT_TIMEOUT)
        provider_name = config.get("provider", "claude")
        cwd = config.get("cwd")

        from swarm.providers import get_provider

        provider = get_provider(provider_name)
        args = provider.headless_command(prompt, output_format, max_turns)

        # Clean environment — strip provider-specific vars
        prefixes = provider.env_strip_prefixes()
        env = {k: v for k, v in os.environ.items() if not any(k.startswith(p) for p in prefixes)}

        work_dir = Path(cwd) if cwd else _WORK_DIR
        work_dir.mkdir(parents=True, exist_ok=True)

        _log.info(
            "headless invoke: provider=%s, cwd=%s, timeout=%d",
            provider_name,
            work_dir,
            timeout,
        )

        proc = await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(work_dir),
            env=env,
        )

        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except TimeoutError:
            proc.kill()
            await proc.wait()
            _log.warning("headless timed out after %ds", timeout)
            return ServiceResult(success=False, error=f"Timed out after {timeout}s")
        except asyncio.CancelledError:
            proc.kill()
            await proc.wait()
            raise

        if proc.returncode != 0:
            err_text = stderr.decode(errors="replace").strip()
            _log.error("headless exited %d: %s", proc.returncode, err_text)
            return ServiceResult(
                success=False,
                error=f"Process exited with code {proc.returncode}: {err_text}",
            )

        text, session_id = provider.parse_headless_response(stdout)

        result_data: dict[str, Any] = {"result": text}
        if session_id:
            result_data["session_id"] = session_id

        _log.info("headless complete: %d bytes output", len(text))
        return ServiceResult(data=result_data)
