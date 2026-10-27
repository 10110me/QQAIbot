from contextvars import ContextVar

model_context: ContextVar[dict] = ContextVar("osa_model_context", default={})
