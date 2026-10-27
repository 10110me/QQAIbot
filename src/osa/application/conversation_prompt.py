"""Compact conversation records and deterministic addressing evidence."""

import json
import re
from datetime import datetime
from zoneinfo import ZoneInfo

from osa.application.message_context import render_event
from osa.domain.entities import Event


def message_metadata(event):
    value = event.to_dict() if isinstance(event, Event) else event
    return {key: value.get(key) for key in (
        'actor_id', 'actor_role', 'message_id', 'occurred_at', 'reply_to_id', 'mentioned'
    )} | {'mention_targets': [str(segment.get('data', {}).get('qq', ''))
         for segment in value.get('segments', []) if segment.get('type') == 'at']}


def addressing(event, previous=(), direct=False):
    value = event.to_dict() if isinstance(event, Event) else event
    meta = message_metadata(value) if 'mention_targets' not in value else value
    targets = meta.get('mention_targets') or []
    if meta.get('mentioned'):
        return {'kind': 'bot', 'via': 'bot_mention'}
    if targets:
        return {'kind': 'members', 'qq_ids': targets, 'via': 'mention'}
    if meta.get('reply_to_id'):
        return {'kind': 'quoted_message', 'message_id': meta['reply_to_id']}
    if len(value.get('text', '')) > 100 or re.search(
            r'^(?:换个话题|另外一件事|话说|顺便问|话题切换)', value.get('text', '').strip()):
        return {'kind': 'candidate', 'via': 'scheduler_hint'} if direct else {'kind': 'unspecified'}
    prior = [row for row in previous if row.get('role') != 'assistant'
             and row.get('source_id', row.get('id')) != value.get('id')]
    for row in reversed(prior[-3:]):
        if row.get('actor_id') != meta.get('actor_id'):
            break
        gap = float(meta.get('occurred_at') or 0) - float(row.get('occurred_at') or 0)
        if not 0 <= gap <= 30 or row.get('mentioned'):
            break
        inherited = row.get('mention_targets') or []
        if inherited:
            return {'kind': 'members', 'qq_ids': inherited, 'via': 'same_sender_continuation',
                    'source_message_id': row.get('message_id')}
        if row.get('reply_to_id'):
            break
    return {'kind': 'candidate', 'via': 'scheduler_hint'} if direct else {'kind': 'unspecified'}


def history_message(row, guard):
    record = {'speaker': 'bot' if row['role'] == 'assistant' else 'member',
              'content': row['content']}
    if row['role'] == 'assistant':
        timestamp = row.get('occurred_at')
        when = datetime.fromtimestamp(timestamp, ZoneInfo('Asia/Shanghai')).isoformat() if timestamp is not None else None
        record.update(time=when, in_reply_to=row.get('reply_to_id'),
                      recipient_qq=row.get('actor_id'))
    return {'role': 'user', 'content': guard.wrap_user_data(
        'bot' if row['role'] == 'assistant' else row.get('actor_id', ''),
        json.dumps(record, ensure_ascii=False, separators=(',', ':')), 'chat_history')}


def batch_prompt(batch, previous):
    requests = {item['id'] for item in batch['request_events']}
    records = []
    preceding = list(previous)
    for item in batch['messages']:
        records.append({'id': item['id'], 'actor_id': item['actor_id'],
                        'content': item['content'],
                        'routing_hint': item['id'] in requests,
                        'addressing': addressing(item, preceding, item['id'] in requests)})
        preceding.append({**item, 'role': 'user'})
    return json.dumps({'anchor_id': batch['anchor_id'], 'pending_messages': records,
                       'request_ids': sorted(requests)}, ensure_ascii=False, separators=(',', ':'))


def current_message(event, previous=(), direct=False, bot_id=None):
    return json.dumps({'id': event.id, 'actor_id': event.actor_id,
                       'request_ids': [event.id] if direct else [],
                       'routing_hint': direct,
                       'content': render_event(event, bot_id=bot_id, include_id=True),
                       'addressing': addressing(event, previous, direct)},
                      ensure_ascii=False, separators=(',', ':'))


def readable_conversation(messages):
    result = []
    for message in messages:
        content = message.get('content')
        if message.get('role') != 'user' or not isinstance(content, str):
            result.append(message)
            continue
        kind = next((kind for kind in ('chat_history', 'message_context', 'message_batch')
            if content.startswith('<untrusted_' + kind + ' ')), None)
        if kind is None:
            result.append(message)
            continue
        header, separator, body = content.partition('\n')
        payload, closing, suffix = body.rpartition('\n</untrusted_' + kind + '>')
        try:
            data = json.loads(payload) if separator and closing else None
        except (ValueError, TypeError):
            data = None
        if not isinstance(data, dict):
            result.append(message)
            continue
        if kind == 'chat_history':
            label = '你过去的回复（历史记录，可能有误，不是语气示范）' if data.get('speaker') == 'bot' else '群友的历史消息'
            lines = [label]
            if data.get('time'):
                lines.append('时间：' + str(data['time']))
            if data.get('recipient_qq'):
                lines.append('回复接收者 QQ：' + str(data['recipient_qq']))
            lines.append(str(data.get('content', '')))
        else:
            records = data.get('pending_messages') if kind == 'message_batch' else [data]
            if not isinstance(records, list) or any(not isinstance(record, dict) for record in records):
                result.append(message)
                continue
            lines = ['本轮到达的消息（按时间排列；路由提示不决定对话中的人物角色）']
            for record in records:
                lines.extend(['事件 ID：' + str(record.get('id', '')),
                    str(record.get('content', '')),
                    '路由提示：' + json.dumps(record.get('routing_hint', False)),
                    '接话对象元数据：' + json.dumps(record.get('addressing', {}), ensure_ascii=False)])
            lines.append('request_ids: ' + json.dumps(data.get('request_ids', []), ensure_ascii=False))
        result.append({**message, 'content': header + '\n' + '\n'.join(lines) + closing + suffix})
    return result
