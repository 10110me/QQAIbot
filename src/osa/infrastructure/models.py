from osa.application.prompt_catalog import CATALOG
import asyncio
import logging
import json
import time
from typing import Any

import httpx

from osa.domain.entities import ModelResult, UpstreamUnavailable
from osa.domain.ports import ModelProvider
from osa.domain.features import Record
from osa.domain.context import model_context
from osa.infrastructure.api_limits import api_slot
from osa.application.token_context import estimate_text_tokens
import uuid


class FakeModel:
    async def generate(self, messages: list[dict[str, str]], purpose: str = "chat") -> ModelResult:
        if purpose == "decision":
            system='\n'.join(m.get('content','') for m in messages if m.get('role')=='system')
            if 'work_decision' in system:
                return ModelResult('{"mode":"chat","reason":"demo"}', 'fake','fake-work-decision')
            if "agent_decision" in system or "THINK|SKIP|DEFER" in system:
                direct = json.loads(messages[-1]['content']).get('direct', False)
                return ModelResult(json.dumps({'action': 'THINK' if direct else 'SKIP', 'reason_code': 'demo_direct' if direct else 'demo_silence'}), "fake", "fake-decision")
            return ModelResult('{"action":"IGNORE"}', "fake", "fake-decision")
        if purpose == "thinking":
            return ModelResult(json.dumps({"final_action":"REPLY", "reply_brief":{"intent":"demo", "key_points":["演示模式已收到请求；尚未调用真实模型或工具。"], "evidence_ids":[], "limitations":[]}}, ensure_ascii=False), "fake", "fake-thinking")
        if purpose == "observer":
            return ModelResult('{"findings":[]}', "fake", "fake-observer")
        if purpose == "reply":
            brief = json.loads(messages[-1]["content"])
            return ModelResult("[演示模型] "+"；".join(brief["key_points"]+brief.get("limitations", [])), "fake", "fake-reply")
        if purpose == "memory":
            return ModelResult('{"summary":"演示整理，仅记录来源，不自动确认事实。","facts":[]}', "fake", "fake-memory")
        user = next((m["content"] for m in reversed(messages) if m["role"] == "user"), "")
        return ModelResult(f"[演示模型] 收到：{user[:120]}", "fake", "fake-chat")

    async def close(self) -> None:
        pass


class OpenAICompatibleModel:
    def __init__(self, base_url: str, api_key: str, model: str, timeout: float, decision_model: str | None = None) -> None:
        self.model = model
        self.base_url = base_url
        self.decision_model = decision_model or model
        self.client = httpx.AsyncClient(
            base_url=base_url.rstrip("/") + "/", timeout=timeout,
            headers={"Authorization": f"Bearer {api_key}"},
        )

    async def generate(self, messages: list[dict[str, str]], purpose: str = "chat") -> ModelResult:
        model = self.decision_model if purpose == "decision" else self.model
        context=model_context.get();call_id=uuid.uuid4().hex
        keep=context.get('collect_messages',True)
        log=logging.getLogger(__name__)
        log.info('model_request purpose=%s provider=%s model=%s event_id=%s',purpose,'openai-compatible',model,context.get('event_id'),
                 extra={'model_call':{'id':call_id,'model':model,'provider':'openai-compatible','purpose':purpose,'stream':False,
                     'status':'requesting','started_at':time.time(),'event_id':context.get('event_id'),
                     'retry_count':context.get('model_retry_count',0)}})
        try:
            started = time.monotonic()
            async with api_slot(self.base_url):
                started = time.monotonic()
                response = await self.client.post("chat/completions", json={
                    "model": model, "messages": messages, "temperature": 0.2 if purpose == "decision" else 0.7,
                    "max_tokens": 2400 if purpose in {"thinking", "observer"} else 600, "stream": False,
                })
            response.raise_for_status()
            data = response.json()
            content = data["choices"][0]["message"].get("content")
            if not isinstance(content, str) or not content.strip():
                raise UpstreamUnavailable("Model returned empty or unsupported content")
            usage = data.get("usage", {})
            result = ModelResult(content.strip(), "openai-compatible", model,
                               int(usage.get("prompt_tokens", 0)), int(usage.get("completion_tokens", 0)))
            elapsed = max(time.monotonic() - started, .001)
            log.info('model_response purpose=%s provider=%s model=%s',purpose,'openai-compatible',model,
                     extra={'model_call':{'id':call_id,'status':'completed','finished_at':time.time(),
                         'response':content if keep else '[群策略未开启消息留存，未记录回复正文]',
                         'input_tokens':result.input_tokens,'output_tokens':result.output_tokens,
                         'first_response_ms':round(elapsed*1000),
                         'output_tokens_per_second':round((result.output_tokens or estimate_text_tokens(content))/elapsed,1),
                         'speed_estimated':not bool(result.output_tokens),
                         'retry_count':context.get('model_retry_count',0)}})
            return result
        except Exception as exc:
            log.warning('model_response_failed purpose=%s provider=%s model=%s error_type=%s',purpose,'openai-compatible',model,type(exc).__name__,
                        extra={'model_call':{'id':call_id,'status':'failed','error_type':type(exc).__name__,
                            'http_status':getattr(getattr(exc,'response',None),'status_code',None),'finished_at':time.time()}})
            raise

    async def close(self) -> None:
        await self.client.aclose()


class ModelRouter:
    """Model routing, retries and circuit breaking.

    ``daily_limit <= 0`` means unlimited model calls. Usage records are still
    written when a repository is configured so the log and cost views remain
    useful; they are simply not used to reject a request in unlimited mode.
    """

    def __init__(self, provider: ModelProvider, daily_limit: int = 0, budget_scope: str = "social") -> None:
        self.provider = provider
        self.daily_limit = daily_limit
        self.budget_scope = budget_scope
        self._day = ""
        self._calls = 0
        self._failures: dict[str, int] = {}
        self._open_until: dict[str, float] = {}
        self._lock = asyncio.Lock()
        self.repo = None
        self.overrides: dict[str, ModelProvider] = {}
        self.retired: list[ModelProvider] = []
        self.prompt_guard = None

    async def generate(self, messages: list[dict[str, str]], purpose: str = "chat") -> ModelResult:
        if self.prompt_guard is not None:
            messages=self.prompt_guard.prepare_messages(messages,purpose)
        async with self._lock:
            day = time.strftime("%Y-%m-%d", time.gmtime())
            if day != self._day:
                self._day, self._calls = day, 0
            if not getattr(self.provider, 'handles_retries', False) and time.monotonic() < self._open_until.get(purpose, 0):
                raise UpstreamUnavailable("Model circuit open")
            if self.daily_limit > 0 and self._calls >= self.daily_limit:
                raise UpstreamUnavailable("Daily model request budget reached")
            usage = None
            metadata = model_context.get()
            if self.repo and self.daily_limit > 0:
                records = await self.repo.find("model_usage", limit=100000)
                if sum(row.payload.get("day") == day and row.payload.get("budget_scope", "social") == self.budget_scope for row in records) >= self.daily_limit:
                    raise UpstreamUnavailable("Persistent daily model request budget reached")
            if self.repo:
                usage = await self.repo.put(Record(uuid.uuid4().hex, "model_usage", metadata.get("actor_id"), metadata.get("group_id"),
                    "reserved", {"day": day, "purpose": purpose, "budget_scope": self.budget_scope, "event_id": metadata.get("event_id")}))
            self._calls += 1
        started = time.monotonic()
        provider = self.overrides.get(purpose, self.provider)
        result = None
        last_error = None
        # A model completion is a read-only upstream request.  Retry only
        # transport/rate-limit/server failures; malformed responses and
        # policy errors fail immediately.  The retry count is deliberately
        # small so a broken provider cannot stall a QQ conversation.
        max_attempts = 1 if getattr(provider, 'handles_retries', False) is True else 3
        for attempt in range(max_attempts):
            try:
                if getattr(provider, 'handles_retries', False):
                    result = await provider.generate(messages, purpose)
                else:
                    retry_context = model_context.get()
                    previous_retry = retry_context.get('model_retry_count')
                    retry_context['model_retry_count'] = attempt
                    try:
                        result = await provider.generate(messages, purpose)
                    finally:
                        if previous_retry is None:
                            retry_context.pop('model_retry_count', None)
                        else:
                            retry_context['model_retry_count'] = previous_retry
                break
            except Exception as exc:
                last_error = exc
                status = getattr(getattr(exc, "response", None), "status_code", None)
                retryable = isinstance(exc, (httpx.TimeoutException, httpx.NetworkError, UpstreamUnavailable)) or status == 429 or (isinstance(status, int) and status >= 500)
                if not retryable or attempt + 1 >= max_attempts:
                    break
                logging.getLogger(__name__).warning("model_retry purpose=%s attempt=%s error_type=%s", purpose, attempt + 1, type(exc).__name__)
                await asyncio.sleep(0.35 * (2 ** attempt))
        if result is None:
            if not getattr(provider, 'handles_retries', False):
                self._failures[purpose] = self._failures.get(purpose, 0) + 1
                if self._failures[purpose] >= 3:
                    self._open_until[purpose] = time.monotonic() + 60
            if usage:
                await self.repo.patch(usage.id, status="failed", payload={**usage.payload, "duration_seconds": time.monotonic()-started})
            raise UpstreamUnavailable("Model request failed") from last_error
        self._failures[purpose] = 0
        if usage:
            await self.repo.patch(usage.id, status="succeeded", payload={**usage.payload, "model": result.model,
                "provider": result.provider, "input_tokens": result.input_tokens, "output_tokens": result.output_tokens,
                "duration_seconds": time.monotonic()-started})
        return result

    async def decision(self, text: str) -> dict[str, Any]:
        result = await self.generate([
            {"role": "system", "content": CATALOG['legacy_decision']['default']},
            {"role": "user", "content": text},
        ], "decision")
        data = json.loads(result.text)
        if data.get("action") not in {"IGNORE", "REPLY"}:
            raise ValueError("Invalid decision schema")
        return data

    async def close(self) -> None:
        await self.provider.close()
        for provider in [*self.overrides.values(), *self.retired]:
            await provider.close()
