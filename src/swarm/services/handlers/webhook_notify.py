"""Webhook notification service handler — POST to a URL on step events."""

from __future__ import annotations

import json
from typing import Any, ClassVar

import aiohttp

from swarm.logging import get_logger
from swarm.services.registry import ServiceContext, ServiceResult

_log = get_logger("services.webhook_notify")
_TIMEOUT = 10


class WebhookNotify:
    """POST JSON to a configured URL when a pipeline step executes."""

    description = "POST a JSON payload to a configured URL."
    example_config: ClassVar[dict[str, Any]] = {
        "url": "https://example.com/hook",
        "headers": {"X-Auth": "token"},
        "extra": {"source": "swarm"},
    }

    async def execute(self, config: dict[str, Any], context: ServiceContext) -> ServiceResult:
        url = config.get("url", "")
        if not url:
            return ServiceResult(success=False, error="url is required")

        headers = config.get("headers", {})
        headers.setdefault("Content-Type", "application/json")

        payload = {
            "pipeline_id": context.pipeline_id,
            "pipeline_name": context.pipeline_name,
            "step_id": context.step_id,
            "step_name": context.step_name,
            **config.get("extra", {}),
        }

        try:
            timeout = aiohttp.ClientTimeout(total=_TIMEOUT)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(url, json=payload, headers=headers) as resp:
                    body = await resp.text()
                    if resp.status >= 400:
                        return ServiceResult(
                            success=False,
                            error=f"HTTP {resp.status}: {body[:200]}",
                        )
                    try:
                        data = json.loads(body)
                    except json.JSONDecodeError:
                        data = {"response": body[:500]}
                    return ServiceResult(success=True, data=data)
        except Exception as e:
            _log.warning("webhook POST to %s failed: %s", url, e)
            return ServiceResult(success=False, error=str(e))
