"""Owner-only hot configuration API used by the management console."""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from osa.infrastructure.runtime_config import (
    TASK_NAMES,
    ModelConfig,
    OutputFilterConfig,
    PersonaProfile,
    PromptTemplate,
    ProviderConfig,
    RuntimeConfiguration,
    TaskConfig,
)
from osa.interfaces.api import require_admin, response

router = APIRouter(prefix="/api/v1/config", dependencies=[Depends(require_admin)])


def store(request: Request):
    return request.app.state.runtime_configuration


def save(request: Request, value: RuntimeConfiguration, revision: int | None = None) -> dict:
    try:
        return response(store(request).replace(value, revision).revision)
    except ValueError as exc:
        code = str(exc) if str(exc).isupper() else "INVALID_CONFIGURATION"
        raise HTTPException(409 if code == "CONFIG_REVISION_CONFLICT" else 422, code) from exc


class ProviderInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    base_url: str
    api_key: str | None = Field(default=None, max_length=10000)
    client_type: str = "openai"
    auth_type: str = "bearer"
    auth_header_name: str = "Authorization"
    auth_header_prefix: str = "Bearer "
    auth_query_name: str = ""
    model_list_endpoint: str = "models"
    max_retry: int = 2
    timeout: float = 30
    retry_interval: float = 0.8
    default_headers: dict[str, str] = Field(default_factory=dict)
    default_query: dict[str, str] = Field(default_factory=dict)
    enabled: bool = True
    revision: int | None = None


class ConfigurationInput(BaseModel):
    configuration: dict[str, Any]
    revision: int


@router.get("")
async def configuration(request: Request) -> dict:
    return response(store(request).public())


@router.put("")
async def replace_configuration(payload: ConfigurationInput, request: Request) -> dict:
    raw = payload.configuration
    current = store(request).snapshot()
    secrets = {item.id: item.api_key for item in current.providers}
    for provider in raw.get("providers", []):
        if (
            provider.get("api_key") in {None, "", "********", "**********"}
            and provider.get("id") in secrets
        ):
            provider["api_key"] = secrets[provider["id"]].get_secret_value()
        provider.pop("has_api_key", None)
    value = RuntimeConfiguration.model_validate(raw)
    save(request, value, payload.revision)
    return response(store(request).public())


@router.put("/providers/{provider_id}")
async def put_provider(provider_id: str, payload: ProviderInput, request: Request) -> dict:
    current = store(request).snapshot()
    previous = next((item for item in current.providers if item.id == provider_id), None)
    data = payload.model_dump(exclude={"revision"})
    if payload.api_key in {None, "", "********", "**********"} and previous:
        data["api_key"] = previous.api_key
    provider = ProviderConfig(id=provider_id, **data)
    current.providers = [item for item in current.providers if item.id != provider_id] + [provider]
    save(request, current, payload.revision)
    return response(
        next(row for row in store(request).public()["providers"] if row["id"] == provider_id)
    )


@router.delete("/providers/{provider_id}")
async def delete_provider(provider_id: str, request: Request, revision: int | None = None) -> dict:
    current = store(request).snapshot()
    if any(item.api_provider == provider_id for item in current.models):
        raise HTTPException(409, "PROVIDER_IN_USE")
    current.providers = [item for item in current.providers if item.id != provider_id]
    save(request, current, revision)
    return response({"deleted": True})


@router.post("/providers/{provider_id}/test")
async def test_provider(provider_id: str, request: Request) -> dict:
    provider = request.app.state.configured_provider
    if provider is None:
        raise HTTPException(409, "LIVE_MODEL_PROVIDER_DISABLED")
    try:
        return response(await provider.test_provider(provider_id))
    except (KeyError, ValueError):
        raise HTTPException(404, "PROVIDER_NOT_FOUND") from None
    except Exception as exc:
        raise HTTPException(502, "PROVIDER_TEST_FAILED") from exc


@router.get("/providers/{provider_id}/models")
async def fetch_models(provider_id: str, request: Request) -> dict:
    provider = request.app.state.configured_provider
    if provider is None:
        raise HTTPException(409, "LIVE_MODEL_PROVIDER_DISABLED")
    try:
        return response(await provider.fetch_models(provider_id))
    except KeyError as exc:
        raise HTTPException(404, "PROVIDER_NOT_FOUND") from exc
    except Exception as exc:
        raise HTTPException(502, "MODEL_LIST_FETCH_FAILED") from exc


class RevisionedModel(ModelConfig):
    revision: int | None = None


@router.put("/models/{model_id:path}")
async def put_model(model_id: str, payload: RevisionedModel, request: Request) -> dict:
    current = store(request).snapshot()
    model = ModelConfig(**payload.model_dump(exclude={"revision"}))
    previous = next((item for item in current.models if item.model_identifier == model_id), None)
    if model.model_identifier != model_id:
        if previous is None:
            raise HTTPException(404, "MODEL_NOT_FOUND")
        if any(item.model_identifier == model.model_identifier for item in current.models):
            raise HTTPException(409, "MODEL_IDENTIFIER_EXISTS")
        for task in current.task_configs.values():
            task.model_list = [model.model_identifier if value == model_id else value for value in task.model_list]
    if previous is None:
        current.models.append(model)
    else:
        current.models = [model if item.model_identifier == model_id else item for item in current.models]
    save(request, current, payload.revision)
    return response(model.model_dump(mode="json"))


@router.delete("/models/{model_id:path}")
async def delete_model(model_id: str, request: Request, revision: int | None = None) -> dict:
    current = store(request).snapshot()
    if request.app.state.v3:
        for name in ("decision", "replyer"):
            task = current.task_configs.get(name)
            if task:
                task.model_list = [value for value in task.model_list if value != model_id]
    if any(model_id in item.model_list for item in current.task_configs.values()):
        raise HTTPException(409, "MODEL_IN_USE")
    current.models = [item for item in current.models if item.model_identifier != model_id]
    save(request, current, revision)
    return response({"deleted": True})


class RevisionedTask(TaskConfig):
    revision: int | None = None


class ModelReplacementInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_model: str = Field(min_length=1, max_length=200)
    target_model: str = Field(min_length=1, max_length=200)
    revision: int


@router.post("/tasks/replace-model")
async def replace_task_model(payload: ModelReplacementInput, request: Request) -> dict:
    current = store(request).snapshot()
    if payload.revision != current.revision:
        raise HTTPException(409, "CONFIG_REVISION_CONFLICT")
    if payload.source_model == payload.target_model:
        raise HTTPException(422, "SAME_REPLACEMENT_MODEL")
    identifiers = {model.model_identifier for model in current.models}
    if payload.source_model not in identifiers or payload.target_model not in identifiers:
        raise HTTPException(404, "MODEL_NOT_FOUND")
    updated = []
    for name, task in current.task_configs.items():
        if payload.source_model not in task.model_list:
            continue
        task.model_list = list(dict.fromkeys(
            payload.target_model if identifier == payload.source_model else identifier
            for identifier in task.model_list))
        updated.append(name)
    if updated:
        save(request, current, payload.revision)
    return response({"updated_tasks": updated, "updated_count": len(updated),
                     "revision": store(request).snapshot().revision})


@router.put("/tasks/{task_name}")
async def put_task(task_name: str, payload: RevisionedTask, request: Request) -> dict:
    if task_name not in TASK_NAMES:
        raise HTTPException(422, "UNKNOWN_MODEL_TASK")
    current = store(request).snapshot()
    current.task_configs[task_name] = TaskConfig.model_validate(
        payload.model_dump(exclude={"revision"})
    )
    save(request, current, payload.revision)
    return response(current.task_configs[task_name].model_dump(mode="json"))


class RevisionedPersona(PersonaProfile):
    revision: int | None = None


class PersonaNoteState(BaseModel):
    active: bool


@router.get('/persona/growth')
async def persona_growth(request: Request, conversation: str | None = None) -> dict:
    notes = await asyncio.to_thread(store(request).persona_growth.snapshot, conversation, True)
    conversations = await asyncio.to_thread(store(request).persona_growth.conversations)
    return response({'enabled': store(request).snapshot().persona.self_update_enabled,
                     'items': notes, 'conversations': conversations})


@router.put('/persona/growth/{note_id}')
async def set_persona_note(note_id: str, payload: PersonaNoteState, request: Request) -> dict:
    try:
        note = await asyncio.to_thread(store(request).persona_growth.set_active, note_id, payload.active)
    except KeyError:
        raise HTTPException(404, 'PERSONA_NOTE_NOT_FOUND')
    return response(note)


@router.put("/persona")
async def put_persona(payload: RevisionedPersona, request: Request) -> dict:
    current = store(request).snapshot()
    current.persona = PersonaProfile.model_validate(payload.model_dump(exclude={"revision"}))
    save(request, current, payload.revision)
    return response(current.persona.model_dump(mode="json"))


class RevisionedPrompt(PromptTemplate):
    revision: int | None = None


@router.put("/prompts/{prompt_name}")
async def put_prompt(prompt_name: str, payload: RevisionedPrompt, request: Request) -> dict:
    current = store(request).snapshot()
    prompt = PromptTemplate.model_validate(
        {**payload.model_dump(exclude={"revision", "name"}), "name": prompt_name}
    )
    current.prompts = [item for item in current.prompts if item.name != prompt_name] + [prompt]
    save(request, current, payload.revision)
    return response(prompt.model_dump(mode="json"))


@router.delete("/prompts/{prompt_name}")
async def delete_prompt(prompt_name: str, request: Request, revision: int | None = None) -> dict:
    current = store(request).snapshot()
    if prompt_name in current.persona.active_prompt_names:
        raise HTTPException(409, "PROMPT_IN_USE")
    current.prompts = [item for item in current.prompts if item.name != prompt_name]
    save(request, current, revision)
    return response({"deleted": True})


class RevisionedFilter(OutputFilterConfig):
    revision: int | None = None


class SlotInput(BaseModel):
    model_config=ConfigDict(extra='forbid')
    content: str = Field(min_length=1,max_length=50000)
    revision: int


@router.get('/prompt-slots')
async def prompt_slots(request: Request):
    from osa.application.prompt_catalog import CATALOG,VARIABLES
    from osa.application.prompt_protocols import output_contract
    current=store(request).snapshot()
    return response({'revision':current.revision,'variables':VARIABLES,
        'slots':[{'id':key,**spec,'content':current.prompt_slots.get(key,spec['default']),
                  'customized':key in current.prompt_slots,'output_contract':output_contract(key)} for key,spec in CATALOG.items()]})


@router.put('/prompt-slots/{slot_id}')
async def put_slot(slot_id: str,payload: SlotInput,request: Request):
    from osa.application.prompt_catalog import CATALOG,validate_template
    if slot_id not in CATALOG:raise HTTPException(404,'PROMPT_SLOT_NOT_FOUND')
    try:validate_template(payload.content)
    except ValueError as exc:raise HTTPException(422,str(exc)) from None
    current=store(request).snapshot();current.prompt_slots[slot_id]=payload.content
    save(request,current,payload.revision)
    return response({'saved':True})


@router.post('/prompt-slots/{slot_id}/preview')
async def preview_slot(slot_id: str,payload: SlotInput,request: Request):
    from osa.application.prompt_catalog import CATALOG,validate_template,render_prompt
    if slot_id not in CATALOG:raise HTTPException(404,'PROMPT_SLOT_NOT_FOUND')
    try:validate_template(payload.content)
    except ValueError as exc:raise HTTPException(422,str(exc)) from None
    current=store(request).snapshot();current.prompt_slots[slot_id]=payload.content
    class Draft:
        def snapshot(self):return current
    return response({'text':render_prompt(Draft(),slot_id,人设='[请求时填入完整人设]',研究问题='[当前研究问题]',搜索结果='[搜索返回]',用户请求='[当前消息]',附件列表='[授权附件列表]')})


@router.put("/output-filter")
async def put_filter(payload: RevisionedFilter, request: Request) -> dict:
    current = store(request).snapshot()
    current.output_filter = OutputFilterConfig.model_validate(
        payload.model_dump(exclude={"revision"})
    )
    save(request, current, payload.revision)
    return response(current.output_filter.model_dump(mode="json"))


class FilterPreview(BaseModel):
    text: str = Field(max_length=20000)


@router.post("/output-filter/preview")
async def preview_filter(payload: FilterPreview, request: Request) -> dict:
    try:
        return response(
            {"accepted": True, "text": request.app.state.prompt_guard.filter_output(payload.text)}
        )
    except ValueError:
        return response({"accepted": False, "text": ""})
