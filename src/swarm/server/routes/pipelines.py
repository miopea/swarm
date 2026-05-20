"""Pipeline routes — CRUD, lifecycle, and step management."""

from __future__ import annotations

from typing import TYPE_CHECKING

from aiohttp import web

from swarm.pipelines.models import PipelineStep, StepType
from swarm.server.helpers import get_daemon, json_error

if TYPE_CHECKING:
    from swarm.pipelines.engine import PipelineEngine


def register(app: web.Application) -> None:
    app.router.add_get("/api/pipelines", handle_list)
    app.router.add_post("/api/pipelines", handle_create)
    # Service catalog endpoint feeds the pipeline-editor's "Automated step"
    # dropdown. Listed BEFORE the {pipeline_id} routes so "/services" isn't
    # consumed as a pipeline id by aiohttp's URL dispatcher.
    app.router.add_get("/api/pipelines/services", handle_services)
    # Schedule preview backs the P2 cron builder — same ordering caveat as
    # /services above.
    app.router.add_post("/api/pipelines/schedule/preview", handle_schedule_preview)
    app.router.add_get("/api/pipelines/{pipeline_id}", handle_get)
    app.router.add_put("/api/pipelines/{pipeline_id}", handle_update)
    app.router.add_delete("/api/pipelines/{pipeline_id}", handle_delete)
    app.router.add_post("/api/pipelines/{pipeline_id}/start", handle_start)
    app.router.add_post("/api/pipelines/{pipeline_id}/pause", handle_pause)
    app.router.add_post("/api/pipelines/{pipeline_id}/resume", handle_resume)
    app.router.add_post(
        "/api/pipelines/{pipeline_id}/steps/{step_id}/complete",
        handle_complete_step,
    )
    app.router.add_post(
        "/api/pipelines/{pipeline_id}/steps/{step_id}/fail",
        handle_fail_step,
    )
    app.router.add_post(
        "/api/pipelines/{pipeline_id}/steps/{step_id}/skip",
        handle_skip_step,
    )


def _get_engine(request: web.Request) -> PipelineEngine:
    daemon = get_daemon(request)
    engine = getattr(daemon, "pipeline_engine", None)
    if engine is None:
        raise web.HTTPServiceUnavailable(
            text='{"error":"pipeline engine not initialized"}',
            content_type="application/json",
        )
    return engine


async def handle_list(request: web.Request) -> web.Response:
    engine = _get_engine(request)
    pipelines = engine.list_all()
    return web.json_response([p.to_dict() for p in pipelines])


async def handle_services(request: web.Request) -> web.Response:
    """List registered automated-step services with example configs.

    Feeds the pipeline editor's automated-step UI so the operator can pick
    a service from a dropdown and pre-fill a sensible config skeleton —
    closing the gap where the type=automated path was effectively unusable
    from the dashboard.
    """
    daemon = get_daemon(request)
    registry = getattr(daemon, "service_registry", None)
    if registry is None:
        return web.json_response({"services": []})
    return web.json_response({"services": registry.describe()})


async def handle_schedule_preview(request: web.Request) -> web.Response:
    """Project a cron expression into a human description + next firings.

    Backs the P2 schedule builder so the operator sees "Daily at 14:30"
    and the next 5 fire timestamps as they edit — no client-side cron
    library required, and the preview lines up with what the engine will
    actually fire on because it shares the same croniter/zoneinfo path.
    """
    body = await request.json() if request.can_read_body else {}
    expr = (body.get("schedule") or "").strip()
    tz = (body.get("timezone") or "").strip()
    count = int(body.get("count") or 5)
    from swarm.pipelines.schedule import preview_schedule

    return web.json_response(preview_schedule(expr, tz=tz, count=count))


async def handle_get(request: web.Request) -> web.Response:
    engine = _get_engine(request)
    pipeline_id = request.match_info["pipeline_id"]
    pipeline = engine.get(pipeline_id)
    if not pipeline:
        return json_error("Pipeline not found", 404)
    return web.json_response(pipeline.to_dict())


async def handle_create(request: web.Request) -> web.Response:
    engine = _get_engine(request)
    body = await request.json()
    name = body.get("name", "").strip()
    if not name:
        return json_error("name is required")

    # If template_name is provided, create from template
    template_name = body.get("template_name", "").strip()
    if template_name:
        try:
            pipeline = engine.create_from_template(
                template_name,
                template_dir=body.get("template_dir"),
            )
        except (FileNotFoundError, ValueError) as e:
            return json_error(str(e))
        return web.json_response(pipeline.to_dict(), status=201)

    # Otherwise create with inline steps
    steps: list[PipelineStep] = []
    for sd in body.get("steps", []):
        step_id = sd.get("id", "").strip()
        step_name = sd.get("name", "").strip()
        if not step_id or not step_name:
            return json_error("each step needs id and name")
        steps.append(
            PipelineStep(
                id=step_id,
                name=step_name,
                step_type=StepType(sd.get("type", "agent")),
                description=sd.get("description", ""),
                depends_on=sd.get("depends_on", []),
                task_type=sd.get("task_type", "chore"),
                assigned_worker=sd.get("assigned_worker"),
                service=sd.get("service", ""),
                config=sd.get("config", {}),
                schedule=sd.get("schedule", ""),
            )
        )

    pipeline = engine.create(
        name=name,
        description=body.get("description", ""),
        steps=steps,
        tags=body.get("tags", []),
        timezone=(body.get("timezone") or "").strip(),
    )
    return web.json_response(pipeline.to_dict(), status=201)


async def handle_update(request: web.Request) -> web.Response:
    engine = _get_engine(request)
    pipeline_id = request.match_info["pipeline_id"]
    body = await request.json()

    # Optional step replacement — only allowed when the pipeline is DRAFT or
    # PAUSED. The engine raises ValueError if the status forbids edits; we
    # surface that as 409 so the UI can show a useful message without
    # making the operator guess at why their save bounced.
    steps_payload = body.get("steps")
    steps_arg: list[PipelineStep] | None = None
    if steps_payload is not None:
        steps_arg = []
        for sd in steps_payload:
            step_id = (sd.get("id") or "").strip()
            step_name = (sd.get("name") or "").strip()
            if not step_id or not step_name:
                return json_error("each step needs id and name")
            try:
                step_type = StepType(sd.get("step_type") or sd.get("type") or "agent")
            except ValueError:
                return json_error(f"invalid step_type for step {step_id!r}")
            steps_arg.append(
                PipelineStep(
                    id=step_id,
                    name=step_name,
                    step_type=step_type,
                    description=sd.get("description", ""),
                    depends_on=sd.get("depends_on", []),
                    task_type=sd.get("task_type", "chore"),
                    assigned_worker=sd.get("assigned_worker"),
                    service=sd.get("service", ""),
                    config=sd.get("config", {}),
                    schedule=sd.get("schedule", ""),
                )
            )

    try:
        result = engine.update(
            pipeline_id,
            name=body.get("name"),
            description=body.get("description"),
            tags=body.get("tags"),
            steps=steps_arg,
            timezone=body.get("timezone"),
        )
    except ValueError as e:
        return json_error(str(e), 409)
    if not result:
        return json_error("Pipeline not found", 404)
    return web.json_response(result.to_dict())


async def handle_delete(request: web.Request) -> web.Response:
    engine = _get_engine(request)
    pipeline_id = request.match_info["pipeline_id"]
    if not engine.remove(pipeline_id):
        return json_error("Pipeline not found", 404)
    return web.json_response({"ok": True})


async def handle_start(request: web.Request) -> web.Response:
    engine = _get_engine(request)
    pipeline_id = request.match_info["pipeline_id"]
    try:
        ready = engine.start_pipeline(pipeline_id)
    except ValueError as e:
        return json_error(str(e))
    return web.json_response(
        {
            "ok": True,
            "ready_steps": [s.id for s in ready],
        }
    )


async def handle_pause(request: web.Request) -> web.Response:
    engine = _get_engine(request)
    pipeline_id = request.match_info["pipeline_id"]
    try:
        engine.pause_pipeline(pipeline_id)
    except ValueError as e:
        return json_error(str(e))
    return web.json_response({"ok": True})


async def handle_resume(request: web.Request) -> web.Response:
    engine = _get_engine(request)
    pipeline_id = request.match_info["pipeline_id"]
    try:
        ready = engine.resume_pipeline(pipeline_id)
    except ValueError as e:
        return json_error(str(e))
    return web.json_response(
        {
            "ok": True,
            "ready_steps": [s.id for s in ready],
        }
    )


async def handle_complete_step(request: web.Request) -> web.Response:
    engine = _get_engine(request)
    pipeline_id = request.match_info["pipeline_id"]
    step_id = request.match_info["step_id"]
    body = await request.json() if request.can_read_body else {}
    try:
        ready = engine.complete_step(pipeline_id, step_id, result=body.get("result"))
    except ValueError as e:
        return json_error(str(e))
    return web.json_response(
        {
            "ok": True,
            "ready_steps": [s.id for s in ready],
        }
    )


async def handle_fail_step(request: web.Request) -> web.Response:
    engine = _get_engine(request)
    pipeline_id = request.match_info["pipeline_id"]
    step_id = request.match_info["step_id"]
    body = await request.json() if request.can_read_body else {}
    try:
        engine.fail_step(pipeline_id, step_id, error=body.get("error", ""))
    except ValueError as e:
        return json_error(str(e))
    return web.json_response({"ok": True})


async def handle_skip_step(request: web.Request) -> web.Response:
    engine = _get_engine(request)
    pipeline_id = request.match_info["pipeline_id"]
    step_id = request.match_info["step_id"]
    try:
        ready = engine.skip_step(pipeline_id, step_id)
    except ValueError as e:
        return json_error(str(e))
    return web.json_response(
        {
            "ok": True,
            "ready_steps": [s.id for s in ready],
        }
    )
