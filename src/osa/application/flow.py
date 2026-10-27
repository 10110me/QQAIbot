"""Optional console telemetry, independent of conversational behavior."""
from osa.domain.context import model_context


async def stage(journal, source, kind, **fields):
    if journal is not None:
        await journal.emit(source, kind, **fields)


def model_metadata(result):
    return {'model': getattr(result, 'model', ''), 'provider': getattr(result, 'provider', ''),
            'call_id': model_context.get().get('flow_call_id', '')}
