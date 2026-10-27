"""MaiBot-style provider/model/task routing with bounded failover."""

from __future__ import annotations

import asyncio
import json
import logging
import random
import time
import uuid
from collections import defaultdict
from typing import Any

import httpx

logger = logging.getLogger(__name__)

from osa.domain.entities import ModelResult, UpstreamUnavailable
from osa.application.token_context import ContextBudgetExceeded, estimate_messages_tokens, estimate_text_tokens, fit_messages, input_token_budget
from osa.infrastructure.api_limits import api_slot
from osa.infrastructure.runtime_config import (
    ModelConfig,
    ProviderConfig,
    RuntimeConfigurationStore,
    TaskConfig,
)

PURPOSE_TASK = {
    "chat": "replyer",
    "reply": "replyer",
    "decision": "decision",
    "thinking": "planner",
    "observer": "observer",
    "work": "work",
    "memory": "memory",
    "reasoning": "utils",
    "research": "web_search",
    "vision": "vlm",
    "video": "video",
    "emoji": "emoji",
    "voice": "voice",
    "embedding": "embedding",
}


class ModelResponseError(ValueError):
    """Safe response diagnostics; never includes upstream text or hidden reasoning."""
    def __init__(self, code, **diagnostics):
        super().__init__(code)
        self.code, self.diagnostics = code, diagnostics


class ConfiguredTaskModel:
    """Hot-configured optional model with the previous deployment as fallback."""
    def __init__(self, provider, store, purpose, legacy=None):
        self.provider,self.store,self.purpose,self.legacy=provider,store,purpose,legacy

    @property
    def available(self):
        task=self.store.snapshot().task_configs.get(self.purpose)
        return bool(self.provider is not None and task and task.model_list) or self.legacy is not None

    async def generate(self,messages,purpose="observer"):
        task=self.store.snapshot().task_configs.get(self.purpose)
        if self.provider is not None and task and task.model_list:
            return await self.provider.generate(messages,self.purpose)
        if self.legacy is not None:
            return await self.legacy.generate(messages,purpose)
        raise UpstreamUnavailable("No model configured for this task")


class ConfiguredModelProvider:
    handles_retries = True
    def __init__(self, store: RuntimeConfigurationStore) -> None:
        self.store = store
        from osa.application.prompting import PromptGuard
        self.prompt_guard=PromptGuard(store)
        self.client = httpx.AsyncClient(follow_redirects=False)
        self.usage: dict[str, int] = defaultdict(int)
        self.failures: dict[str, int] = defaultdict(int)
        self.latency: dict[str, float] = defaultdict(float)
        self.cooldowns = {}
        self.routing_revision = None
        self.flow = None

    def _candidates(
        self, purpose: str
    ) -> tuple[list[tuple[ModelConfig, ProviderConfig]], TaskConfig]:
        config = self.store.snapshot()
        if config.revision != self.routing_revision:
            self.cooldowns.clear()
            self.routing_revision = config.revision
        task_name = PURPOSE_TASK.get(purpose, purpose)
        # Documents saved before decision and thinking had separate cards keep
        # their planner route until the operator changes the new decision card.
        if task_name == 'decision' and task_name not in config.task_configs:
            task_name = 'planner'
        task = config.task_configs.get(task_name, TaskConfig())
        models = {item.model_identifier: item for item in config.models if item.enabled}
        providers = {item.id: item for item in config.providers if item.enabled}
        result = [
            (models[name], providers[models[name].api_provider])
            for name in task.model_list
            if name in models and models[name].api_provider in providers
        ]
        if not result:
            raise UpstreamUnavailable("No model configured for this task")
        if task.selection_strategy == "random":
            random.shuffle(result)
        elif task.selection_strategy == "balance":
            result.sort(
                key=lambda pair: (
                    self.usage[pair[0].model_identifier]
                    + self.failures[pair[0].model_identifier] * 1000
                    + int(self.latency[pair[0].model_identifier] > task.slow_threshold) * 300
                )
            )
        return result, task

    @staticmethod
    def _auth(provider: ProviderConfig) -> tuple[dict[str, str], dict[str, str]]:
        key = provider.api_key.get_secret_value()
        headers, query = dict(provider.default_headers), dict(provider.default_query)
        if key and provider.auth_type in {"bearer", "api_key", "custom"}:
            headers[provider.auth_header_name] = provider.auth_header_prefix + key
        elif key and provider.auth_type == "none" and provider.auth_query_name:
            query[provider.auth_query_name] = key
        return headers, query

    async def generate(self, messages: list[dict[str, str]], purpose: str = "chat") -> ModelResult:
        candidates, task = self._candidates(purpose)
        multiple = len(candidates) > 1
        scope = lambda pair: (purpose, pair[1].id, pair[0].model_identifier)
        ready = [pair for pair in candidates if self.cooldowns.get(scope(pair),0) <= time.monotonic()]
        if ready:
            for pair in candidates:
                if pair not in ready:
                    logger.info('model_cooldown purpose=%s provider=%s model=%s wait_seconds=%.1f',
                                purpose,pair[1].id,pair[0].model_identifier,self.cooldowns[scope(pair)]-time.monotonic())
            candidates = ready
        errors: list[str] = []
        last_error = None
        failed_attempts = 0
        for model, provider in candidates:
            # Descriptions are on the chat path. If another configured VLM is
            # available, try it before repeatedly waiting on a broken endpoint.
            retries = model.max_retry
            if retries is None:
                retries = 0 if purpose in {'vision','emoji'} and len(candidates)>1 else provider.max_retry
            attempts = retries + 1
            for attempt in range(attempts):
                started = time.monotonic()
                try:
                    if failed_attempts and self.flow is not None:
                        from osa.domain.context import model_context
                        context = model_context.get()
                        await self.flow.emit(context.get('event_id'), 'model.retry',
                                             step=context.get('thinking_step', 0),
                                             session_id=context.get('thinking_session_id', ''),
                                             retry_count=failed_attempts, model=model.model_identifier)
                    result = await self._request(provider, model, task, messages, purpose, failed_attempts)
                    elapsed = time.monotonic() - started
                    self.usage[model.model_identifier] += (
                        result.input_tokens + result.output_tokens + 1
                    )
                    self.latency[model.model_identifier] = elapsed
                    self.failures[model.model_identifier] = max(
                        0, self.failures[model.model_identifier] - 1
                    )
                    self.cooldowns.pop((purpose,provider.id,model.model_identifier),None)
                    return result
                except (TimeoutError, httpx.HTTPError, ValueError, KeyError) as exc:
                    last_error = exc
                    failed_attempts += 1
                    status = getattr(getattr(exc, "response", None), "status_code", None)
                    retryable = not isinstance(exc, ContextBudgetExceeded) and (
                        not isinstance(exc, httpx.HTTPStatusError) or status in {408, 409, 425, 429}
                        or (status is not None and status >= 500))
                    logger.warning("model_attempt_failed purpose=%s provider=%s model=%s attempt=%s error_type=%s status=%s",
                                   purpose, provider.id, model.model_identifier, attempt+1, type(exc).__name__,
                                   status)
                    self.failures[model.model_identifier] += 1
                    errors.append(type(exc).__name__)
                    if multiple and status == 429:
                        logger.warning("model_failover purpose=%s provider=%s model=%s attempts=%s retryable=%s",
                                       purpose, provider.id, model.model_identifier, attempt + 1, retryable)
                        break
                    if attempt + 1 < attempts and retryable:
                        delay = min(2 ** attempt, 16)
                        logger.info("model_retry_scheduled purpose=%s provider=%s model=%s next_attempt=%s delay=%.2f",
                                    purpose, provider.id, model.model_identifier, attempt + 2,
                                    delay)
                        await asyncio.sleep(delay)
                    elif attempt + 1 >= attempts or not retryable:
                        logger.warning("model_failover purpose=%s provider=%s model=%s attempts=%s retryable=%s",
                                       purpose, provider.id, model.model_identifier, attempt + 1, retryable)
                        if not retryable:
                            break
            if multiple:
                # Don't spend the same retry budget on a broken model at every
                # file/read/test step while a configured fallback is working.
                self.cooldowns[(purpose,provider.id,model.model_identifier)] = time.monotonic()+180
        raise UpstreamUnavailable("All configured models failed: " + ",".join(errors[-4:])) from last_error

    async def _request(
        self,
        provider: ProviderConfig,
        model: ModelConfig,
        task: TaskConfig,
        messages: list[dict[str, str]],
        purpose: str,
        retry_count: int = 0,
    ) -> ModelResult:
        messages=self.prompt_guard.prepare_messages(messages,purpose)
        if purpose == 'thinking':
            messages = [messages[0], {'role': 'system', 'content':
                '【已核实的请求元数据：可公开的运行事实】\n'
                + json.dumps({'requested_model_id': model.model_identifier}, ensure_ascii=False)
                + '\n相关时可以公开这些事实，不要编造保密规则或转移话题。'
                '模型 ID 表示配置中实际发出的 API 请求标识，不代表已经独立核实上游内部模型，也不是你的人设身份。'},
                *messages[1:]]
            messages = self.prompt_guard.consolidate_system_messages(messages)
        headers, query = self._auth(provider)
        payload: dict[str, Any] = {
            "model": model.model_identifier,
            "messages": messages,
            "temperature": model.temperature if model.temperature is not None else task.temperature,
            "max_tokens": model.max_tokens or task.max_tokens,
            **model.extra_params,
            "stream": model.force_stream_mode,
        }
        hard_input_budget = input_token_budget(model.context_window_tokens, int(payload["max_tokens"]))
        input_budget = min(hard_input_budget, 20000) if purpose == "thinking" else hard_input_budget
        try:
            messages, dropped_messages = fit_messages(messages, input_budget)
        except ContextBudgetExceeded:
            if input_budget == hard_input_budget:
                raise
            input_budget = hard_input_budget
            messages, dropped_messages = fit_messages(messages, input_budget)
        payload["messages"] = messages
        estimated_input_tokens = estimate_messages_tokens(messages)
        if dropped_messages:
            logger.info('model_context_trimmed purpose=%s model=%s dropped_messages=%s estimated_input_tokens=%s budget=%s',
                        purpose, model.model_identifier, dropped_messages, estimated_input_tokens, input_budget)
        url = provider.base_url.rstrip("/") + "/chat/completions"
        # Provider timeout bounds an idle network operation; the task owns the
        # total generation deadline. A healthy SSE stream may exceed one idle
        # interval while the model is still generating its answer.
        timeout = task.hard_timeout
        network_timeout = min(provider.timeout, timeout)
        from osa.domain.context import model_context
        context = model_context.get()
        keep_content = context.get('collect_messages', True)
        call_id = uuid.uuid4().hex
        context['flow_call_id'] = call_id
        detail = {'id':call_id,'provider':provider.id,'provider_name':provider.name,
                  'model':model.model_identifier,'purpose':purpose,'stream':model.force_stream_mode,
                  'temperature':payload['temperature'],'max_tokens':payload['max_tokens'],
                  'context_window_tokens':model.context_window_tokens,'input_token_budget':input_budget,
                  'hard_input_token_budget':hard_input_budget,
                  'estimated_input_tokens':estimated_input_tokens,'dropped_messages':dropped_messages,
                  'event_id':context.get('event_id'),'task_id':context.get('task_id'),
                  'retry_count':retry_count,'status':'requesting'}
        try:
            async with api_slot(provider.base_url):
                detail['started_at'] = time.time()
                logger.info('model_request purpose=%s provider=%s model=%s event_id=%s',purpose,provider.id,model.model_identifier,context.get('event_id'),
                            extra={'model_call':detail})
                request_started = time.monotonic()
                async with asyncio.timeout(timeout):
                    if model.force_stream_mode:
                        content, usage, first_response_ms, generation_seconds = await self._stream(
                            url, headers, query, payload, network_timeout, request_started)
                    else:
                        response = await self.client.post(url,headers=headers,params=query,json=payload,timeout=network_timeout)
                        response.raise_for_status()
                        data = response.json()
                        choice = data['choices'][0]
                        if choice.get('finish_reason') == 'length':
                            raise ModelResponseError('MODEL_OUTPUT_TRUNCATED')
                        message = choice['message']
                        content = message.get('content')
                        if not content and message.get('reasoning_content'):
                            raise ModelResponseError('MODEL_REASONING_WITHOUT_ANSWER')
                        usage = data.get('usage', {})
                        generation_seconds = time.monotonic() - request_started
                        first_response_ms = round(generation_seconds * 1000)
                if not isinstance(content, str) or not content.strip():
                    raise ModelResponseError('MODEL_EMPTY_RESPONSE')
                result=ModelResult(content.strip(),provider.id,model.model_identifier,
                                   int(usage.get('prompt_tokens',0)),int(usage.get('completion_tokens',0)))
                output_tokens = result.output_tokens or estimate_text_tokens(content)
                logger.info('model_response purpose=%s provider=%s model=%s event_id=%s',purpose,provider.id,model.model_identifier,context.get('event_id'),
                            extra={'model_call':{'id':call_id,'status':'completed','finished_at':time.time(),
                                'response':content if keep_content else '[群策略未开启消息留存，未记录回复正文]',
                                'input_tokens':result.input_tokens,'output_tokens':result.output_tokens,
                                'first_response_ms':first_response_ms,
                                'output_tokens_per_second':round(output_tokens / max(generation_seconds, .001), 1),
                                'speed_estimated':not bool(result.output_tokens), 'retry_count':retry_count}})
                return result
        except Exception as exc:
            logger.warning('model_response_failed purpose=%s provider=%s model=%s error_type=%s',
                           purpose,provider.id,model.model_identifier,type(exc).__name__,
                           extra={'model_call':{'id':call_id,'status':'failed','finished_at':time.time(),
                               'error_type':type(exc).__name__,
                               'retry_count':retry_count,
                               'error_code':getattr(exc,'code',None),
                               'response_diagnostics':getattr(exc,'diagnostics',{}),
                               'http_status':getattr(getattr(exc,'response',None),'status_code',None)}})
            raise

    async def _stream(
        self,
        url: str,
        headers: dict[str, str],
        query: dict[str, str],
        payload: dict[str, Any],
        timeout_seconds: float,
        request_started: float,
    ) -> tuple[str, dict[str, Any], int, float]:
        chunks: list[str] = []
        first_content_at = None
        usage: dict[str, Any] = {}
        diagnostics = {'chunks': 0, 'reasoning_chars': 0, 'finish_reason': None, 'done': False}
        async with self.client.stream(
            "POST", url, headers=headers, params=query, json=payload, timeout=timeout_seconds
        ) as response:
            response.raise_for_status()
            # Some compatible gateways ignore stream=true and return normal JSON.
            if 'application/json' in response.headers.get('content-type', ''):
                await response.aread()
                data = response.json()
                if data.get('error'):
                    raise ModelResponseError('MODEL_STREAM_ERROR')
                choice = (data.get('choices') or [{}])[0]
                if choice.get('finish_reason') == 'length':
                    raise ModelResponseError('MODEL_OUTPUT_TRUNCATED')
                content = choice.get('message', {}).get('content')
                if not isinstance(content, str) or not content.strip():
                    raise ModelResponseError('MODEL_EMPTY_RESPONSE')
                elapsed = time.monotonic() - request_started
                return content, data.get('usage') or {}, round(elapsed * 1000), elapsed
            async for line in response.aiter_lines():
                if not line.startswith("data:"):
                    continue
                raw = line[5:].strip()
                if raw == "[DONE]":
                    diagnostics['done'] = True
                    break
                data = json.loads(raw)
                diagnostics['chunks'] += 1
                if data.get('error'):
                    raise ModelResponseError('MODEL_STREAM_ERROR', **diagnostics)
                usage = data.get("usage") or usage
                choices = data.get('choices') or []
                if not choices:
                    continue  # OpenAI usage-only trailer has choices=[].
                choice = choices[0]
                diagnostics['finish_reason'] = choice.get('finish_reason') or diagnostics['finish_reason']
                delta = choice.get("delta") or choice.get('message') or {}
                # reasoning_content is intentionally never returned to users.
                if isinstance(delta.get('reasoning_content'), str):
                    diagnostics['reasoning_chars'] += len(delta['reasoning_content'])
                if isinstance(delta.get("content"), str):
                    if delta['content'] and first_content_at is None:
                        first_content_at = time.monotonic()
                    chunks.append(delta["content"])
        if diagnostics['finish_reason'] == 'length':
            raise ModelResponseError('MODEL_OUTPUT_TRUNCATED', **diagnostics)
        if not ''.join(chunks).strip():
            raise ModelResponseError('MODEL_REASONING_WITHOUT_ANSWER' if diagnostics['reasoning_chars'] else 'MODEL_EMPTY_RESPONSE', **diagnostics)
        finished_at = time.monotonic()
        return "".join(chunks), usage, round(((first_content_at or finished_at) - request_started) * 1000), finished_at - (first_content_at or request_started)

    async def fetch_models(self, provider_id: str) -> list[str]:
        config = self.store.snapshot()
        provider = next((item for item in config.providers if item.id == provider_id), None)
        if provider is None:
            raise KeyError(provider_id)
        headers, query = self._auth(provider)
        endpoint = provider.model_list_endpoint.lstrip("/")
        async with api_slot(provider.base_url):
            response = await self.client.get(
                provider.base_url.rstrip("/") + "/" + endpoint,
                headers=headers,
                params=query,
                timeout=min(provider.timeout, 30),
            )
        response.raise_for_status()
        data = response.json()
        rows = data.get("data", data.get("models", [])) if isinstance(data, dict) else data
        values = []
        for row in rows if isinstance(rows, list) else []:
            value = row.get("id") or row.get("name") if isinstance(row, dict) else row
            if isinstance(value, str) and 0 < len(value) <= 150:
                values.append(value)
        return sorted(set(values))[:1000]

    async def test_provider(self, provider_id: str) -> dict[str, Any]:
        started = time.monotonic()
        models = await self.fetch_models(provider_id)
        return {
            "ok": True,
            "latency_ms": round((time.monotonic() - started) * 1000),
            "model_count": len(models),
            "models": models[:20],
        }

    async def close(self) -> None:
        await self.client.aclose()
