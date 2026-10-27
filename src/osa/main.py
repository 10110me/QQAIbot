import asyncio
import logging
import secrets
import os
from contextlib import asynccontextmanager
from typing import Any

import uvicorn
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.responses import Response
from pathlib import Path
from starlette.exceptions import HTTPException

from osa.application.resources import ResourceService
from osa.application.prompting import PromptGuard
from osa.application.runtime import AgentRuntime, OutboxDispatcher, inbox_loop, outbox_loop
from osa.config import Settings
from osa.domain.ports import ModelProvider
from osa.infrastructure.models import FakeModel, ModelRouter, OpenAICompatibleModel
from osa.infrastructure.configured_models import ConfiguredModelProvider
from osa.infrastructure.runtime_config import RuntimeConfigurationStore, initial_configuration
from osa.infrastructure.onebot import NapCatResources, OneBotConnection, normalize
from osa.interfaces.api import response, router

logger = logging.getLogger(__name__)


def create_app(settings: Settings | None = None) -> FastAPI:
    config = settings or Settings()  # type: ignore[call-arg]
    if config.database_url == "memory://":
        from osa.infrastructure.memory_store import InMemoryStore
        store: Any = InMemoryStore()
    else:
        from osa.infrastructure.store import SQLStore
        store = SQLStore(config.database_url, pool_size=config.database_pool_size)
    store.permissions.global_group_memory = config.global_group_memory
    platform = OneBotConnection(config.ws_action_timeout)
    runtime_configuration = RuntimeConfigurationStore(
        config.data_dir, initial_configuration(config), memory_only=config.database_url == "memory://")
    from osa.infrastructure.operations import Allowlist, ConsoleLogs
    allowlist = Allowlist(config.data_dir, config.allowed_groups, config.database_url == "memory://")
    config.allowed_groups = list(allowlist.value["groups"])
    console_logs = ConsoleLogs(config.data_dir, lambda: [config.admin_token.get_secret_value(),
        config.napcat_token.get_secret_value(), config.model_api_key.get_secret_value(), config.observer_api_key.get_secret_value(),
        *runtime_configuration.secret_values(), *[a.token.get_secret_value() for a in config.admin_accounts]],
        config.database_url == "memory://")
    from osa.infrastructure.flow_journal import FlowJournal
    flow_journal = FlowJournal(config.data_dir, config.database_url == 'memory://', config.retention_days)
    configured_provider = None if config.model_provider == "fake" else ConfiguredModelProvider(runtime_configuration)
    if configured_provider:
        configured_provider.flow = flow_journal
    work_model = ModelRouter(configured_provider,config.max_daily_model_calls,"work") if configured_provider else None
    provider: ModelProvider = FakeModel() if config.model_provider == "fake" else configured_provider
    assert provider is not None
    model = ModelRouter(provider, config.max_daily_model_calls)
    prompt_guard = PromptGuard(runtime_configuration)
    model.prompt_guard = prompt_guard
    if work_model:
        work_model.prompt_guard = prompt_guard
    resources = ResourceService(store, NapCatResources(platform))
    features = None
    if config.features_enabled:
        from osa.infrastructure.features.wiring import wire_features
        features = wire_features(config, store, model, resources, platform)
        features.research.prompt_guard = prompt_guard
        features.media.provider.prompt_guard = prompt_guard
        from osa.application.work_inputs import WorkInputs
        features.work_inputs=WorkInputs(features,platform,store)
    v2 = None
    if config.v2_enabled:
        if config.v3_enabled:
            from osa.infrastructure.v3.registry import Registry
        else:
            from osa.infrastructure.v2.registry import Registry
        from osa.application.v2.service import V2Service
        v2 = V2Service(Registry(config.v2_database_url), config)
    runtime = AgentRuntime(store, model, resources, config.max_reply_chars, config.max_context_chars,
                           features, prompt_guard=prompt_guard)
    dispatcher = OutboxDispatcher(store, platform, frozenset(config.allowed_groups), config.private_enabled,
                                  features, prompt_guard=prompt_guard)

    runtime.v2 = dispatcher.v2 = v2
    if v2:
        v2.model=model
        v2.store=store
        v2.prompt_guard=prompt_guard
        v2.work_inputs=features.work_inputs if features else None
    v3 = observer = observer_provider = None
    if config.v3_enabled:
        from osa.application.v3.service import V3Service
        from osa.infrastructure.v3.observer import SnapshotObserver
        if config.observer_model:
            observer_provider = ModelRouter(FakeModel() if config.demo_mode else OpenAICompatibleModel(
                config.observer_base_url or config.model_base_url, config.observer_api_key.get_secret_value(),
                config.observer_model, config.model_timeout), config.max_daily_observer_calls, "observer")
            if features:
                observer_provider.repo = features.repo
            observer_provider.prompt_guard = prompt_guard
        from osa.infrastructure.configured_models import ConfiguredTaskModel
        observer_configured = ModelRouter(configured_provider,config.max_daily_observer_calls,"observer") if configured_provider else None
        if features and observer_configured:
            observer_configured.repo = features.repo
        if observer_configured:
            observer_configured.prompt_guard = prompt_guard
        observer = SnapshotObserver(v2.registry, ConfiguredTaskModel(observer_configured,
            runtime_configuration,"observer",observer_provider))
        v3 = V3Service(config, model, v2, store, observer, prompt_guard,
                        features.research.browser if features else None, features, resources)
    runtime.v3 = v3
    runtime.flow = dispatcher.flow = flow_journal
    if v3:
        v3.flow = v3.thinking.flow = flow_journal
    if features and v2:
        features.consolidation.v2 = v2
        v2.embedder = features.knowledge.embedder

    @asynccontextmanager
    async def lifespan(app: FastAPI):  # type: ignore[no-untyped-def]
        log_root = logging.getLogger("osa")
        previous_level = log_root.level
        log_root.setLevel(logging.INFO)
        log_root.addHandler(console_logs)
        await store.initialize(config.allowed_groups, config.auto_create_schema)
        logger.info("application_started groups=%s", len(config.allowed_groups))
        if v2:
            from dataclasses import asdict
            from osa.domain.entities import Destination
            for group in config.allowed_groups:
                await v2.call("set_policy", group, asdict(await store.policy(Destination("group",group))))
        tasks = [asyncio.create_task(inbox_loop(store, runtime, config.worker_concurrency)),
                 asyncio.create_task(outbox_loop(dispatcher))]
        if v2:tasks.append(asyncio.create_task(v2.maintenance(store)))
        if v3:tasks.append(asyncio.create_task(v3.loop()))
        if features:
            for row in await features.repo.find("model_route", status="active"):
                await apply_model_route(row.payload)
            await features.tasks.recover()
            tasks.extend([asyncio.create_task(features.tasks.loop(config.task_concurrency)), asyncio.create_task(features.scheduler())])
            if features.qqbot_tools.enabled:
                tasks.append(asyncio.create_task(features.qqbot_tools.refresh_loop()))
        try:
            yield
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            if features:
                await features.images.shutdown()
                await features.learning.shutdown()
            await model.close()
            if observer_provider:
                await observer_provider.close()
            await store.close()
            logger.info("application_stopped")
            log_root.removeHandler(console_logs)
            log_root.setLevel(previous_level)
            console_logs.close()
            flow_journal.close()

    app = FastAPI(title="TideTalk · 汐言", version="0.4.0", lifespan=lifespan)
    app.state.settings, app.state.store, app.state.platform = config, store, platform
    app.state.runtime, app.state.resources = runtime, resources
    app.state.features = features
    app.state.runtime_configuration = runtime_configuration
    app.state.configured_provider = configured_provider
    app.state.work_model = work_model
    if features and work_model:
        work_model.repo = features.repo
    app.state.prompt_guard = prompt_guard
    app.state.allowlist, app.state.dispatcher, app.state.console_logs = allowlist, dispatcher, console_logs
    app.state.v2 = v2
    app.state.flow_journal = flow_journal
    app.state.v3, app.state.observer = v3, observer
    async def apply_model_route(value: dict) -> None:
        key = os.environ.get(value["api_key_env"], "")
        if value["api_key_env"] == "OSA_MODEL_API_KEY":
            key = key or config.model_api_key.get_secret_value()
        if not key:
            raise HTTPException(409, "MODEL_KEY_ENV_NOT_CONFIGURED")
        provider = OpenAICompatibleModel(value["base_url"], key, value["model"], config.model_timeout)
        old = model.overrides.get(value["purpose"])
        if old:
            model.retired.append(old)
        model.overrides[value["purpose"]] = provider
    app.state.apply_model_route = apply_model_route
    app.include_router(router)
    from osa.interfaces.browser import router as browser_router
    from osa.interfaces.features import router as features_router
    app.include_router(browser_router)
    app.include_router(features_router)
    from osa.interfaces.v2 import router as v2_router, echo_router
    app.include_router(v2_router)
    app.include_router(echo_router)
    from osa.interfaces.v3 import router as v3_router
    app.include_router(v3_router)
    from osa.interfaces.configuration import router as configuration_router
    app.include_router(configuration_router)
    from osa.interfaces.operations import router as operations_router
    app.include_router(operations_router)
    from osa.interfaces.work_models import router as work_models_router
    app.include_router(work_models_router)

    @app.get("/console-operations.js")
    async def console_operations() -> Response:
        return Response((Path(__file__).parent / "interfaces/web/operations.js").read_text(encoding="utf-8"),
                        media_type="application/javascript")

    @app.get("/console-simple.js")
    async def console_simple() -> Response:
        return Response((Path(__file__).parent / "interfaces/web/simple.js").read_text(encoding="utf-8"),
                        media_type="application/javascript")

    @app.get('/console-prompt-slots.js')
    async def console_prompt_slots() -> Response:
        return Response((Path(__file__).parent/'interfaces/web/prompt-slots.js').read_text(encoding='utf-8'),media_type='application/javascript')

    @app.middleware("http")
    async def operation_audit(request: Request, call_next):
        result = await call_next(request)
        if request.url.path.startswith("/api/") and (request.method != "GET" or result.status_code >= 400):
            logger.log(logging.WARNING if result.status_code >= 400 else logging.INFO,
                       "console_request method=%s path=%s status=%s", request.method, request.url.path, result.status_code)
        return result
    @app.get("/console-v3.js")
    async def console_v3() -> Response:
        return Response((Path(__file__).parent / "interfaces/web/v3.js").read_text(encoding="utf-8"), media_type="application/javascript")
    @app.get("/console-v2.js")
    async def console_v2() -> Response:
        return Response((Path(__file__).parent / "interfaces/web/v2.js").read_text(encoding="utf-8"), media_type="application/javascript")

    @app.get("/console-extra.js")
    async def console_extra() -> Response:
        return Response((Path(__file__).parent / "interfaces/web/full.js").read_text(encoding="utf-8"), media_type="application/javascript")

    @app.get("/console-config.js")
    async def console_config() -> Response:
        return Response((Path(__file__).parent / "interfaces/web/configuration.js").read_text(encoding="utf-8"),
                        media_type="application/javascript")

    @app.get("/template-bridge.js")
    async def template_bridge() -> Response:
        return Response((Path(__file__).parent / "interfaces/web/template-bridge.js").read_text(encoding="utf-8"),
                        media_type="application/javascript")

    @app.get("/template-handlers.js")
    async def template_handlers() -> Response:
        return Response((Path(__file__).parent / "interfaces/web/template-handlers.js").read_text(encoding="utf-8"),
                        media_type="application/javascript")

    @app.get("/template-tailwind.js")
    async def template_tailwind() -> Response:
        return Response((Path(__file__).parent / "interfaces/web/template-tailwind.js").read_text(encoding="utf-8"),
                        media_type="application/javascript")

    @app.get("/console-redesign.css")
    async def console_redesign() -> Response:
        return Response((Path(__file__).parent / "interfaces/web/redesign.css").read_text(encoding="utf-8"),
                        media_type="text/css")

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException) -> JSONResponse:
        return JSONResponse({**response(None), "error": {"code": str(exc.detail)}}, status_code=exc.status_code)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        # Pydantic's default error payload can echo sensitive input.
        return JSONResponse({**response(None), "error": {"code": "INVALID_INPUT"}}, status_code=422)

    @app.exception_handler(Exception)
    async def internal_error(request: Request, exc: Exception) -> JSONResponse:
        logger.warning("api_failed error_type=%s", type(exc).__name__)
        return JSONResponse({**response(None), "error": {"code": "UPSTREAM_OR_INTERNAL_ERROR"}}, status_code=503)

    async def ingest(payload: dict[str, Any]) -> None:
        try:
            event = normalize(payload, config.bot_id, config.adapter_id)
        except (ValueError, TypeError, KeyError):
            logger.warning("malformed_onebot_event")
            return
        if event is None:
            return
        if event.destination.kind == "group" and event.destination.id not in config.allowed_groups:
            return
        if event.destination.kind == "private" and not config.private_enabled:
            return
        if len(event.text) > config.max_input_chars:
            logger.info("event_rejected_too_long event_id=%s", event.id)
            return
        policy = await store.policy(event.destination)
        if not policy.enabled:
            return
        from osa.domain.context import model_context
        model_context.set({"event_id":event.id,"actor_id":event.actor_id,"group_id":event.destination.id if event.destination.kind=="group" else None,"collect_messages":policy.collect_messages})
        media_sources = []
        if features and not await store.event_active(event.id):
            if policy.media_read or event.destination.kind == "private":
                from osa.infrastructure.onebot import parse_segments
                for segment in parse_segments(payload.get("message", [])):
                    if segment.get("type") in {"image", "record", "video"}:
                        data = segment.get("data", {})
                        url = str(data.get("url") or data.get("file") or "")
                        if url.startswith(("http://", "https://")):
                            media_sources.append({"url": url, "kind": segment["type"],"emoji":str(data.get('sub_type'))=='1'})
        if await store.publish(event, policy.collect_messages):
            if features:
                await features.observe(event, media_sources)
            logger.info("message_ingested conversation=%s event_id=%s", event.destination.key, event.id)
            if v2:
                await v2.observe(event, policy)
            if event.event_type == "message.recalled":
                await store.recall(event)
                await runtime.streams.recalled(event)

    app.state.ingest = ingest

    @app.websocket("/onebot/v11/ws")
    async def onebot(websocket: WebSocket) -> None:
        expected = "Bearer " + config.napcat_token.get_secret_value()
        authorization = websocket.headers.get("authorization", "")
        if not secrets.compare_digest(authorization, expected) or websocket.headers.get("x-self-id") != config.bot_id:
            await websocket.close(code=1008)
            return
        try:
            await platform.serve(websocket, ingest)
        except WebSocketDisconnect:
            logger.info("onebot_disconnected")
        except Exception as exc:
            logger.warning("onebot_connection_failed error_type=%s", type(exc).__name__)
            await websocket.close(code=1011)

    return app


def run() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    config = Settings()  # type: ignore[call-arg]
    uvicorn.run(create_app(config), host=config.host, port=config.port, workers=1, access_log=False)


if __name__ == "__main__":
    run()
