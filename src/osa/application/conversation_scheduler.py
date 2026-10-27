"""Lease inbound events, then schedule bounded batches per conversation.

Events stay processing in the durable inbox until their batch terminates.
Cancellation leaves them recoverable by the store's existing startup recovery.
No model call occupies the receiver or another conversation's scheduler.
"""
from __future__ import annotations

import asyncio
import logging
import math
import time
from collections import deque
from dataclasses import dataclass, field

from osa.domain.entities import Action, GroupPolicy

logger = logging.getLogger(__name__)


@dataclass
class PendingTurn:
    events: list = field(default_factory=list)
    direct_ids: set[str] = field(default_factory=set)
    followup_ids: set[str] = field(default_factory=set)
    first_at: float = 0
    last_at: float = 0
    not_before: float = 0
    deferrals: int = 0
    active: bool = False
    policy: GroupPolicy = field(default_factory=GroupPolicy)
    latencies: deque = field(default_factory=lambda: deque(maxlen=30))
    # A name call starts a short conversational turn.  Follow-up messages
    # from the same person should remain addressed to the bot even when the
    # follow-up no longer repeats the name (for example: "nova" → "读一下群公告").
    direct_actor: str | None = None
    direct_until: float = 0

    @staticmethod
    def control(event):
        return event.event_type != 'message.created' or (event.text.startswith('/') and not event.text.startswith(('/工作 ','/研究 ')))

    def due(self, now):
        if not self.events or self.active:
            return None
        if self.control(self.events[0]):
            return 'event'
        if self.direct_ids or self.followup_ids:
            return 'direct'
        if now < self.not_before:
            return None
        if self.deferrals:
            return 'wait_timeout'
        if not self.policy.enabled or not self.policy.initiative:
            return 'policy'
        threshold = math.ceil(1 / max(.01, min(1, self.policy.talk_frequency)))
        age = now - self.first_at
        idle = now - self.last_at
        if age >= self.policy.batch_max_wait_seconds:
            return 'max_wait'
        if idle < self.policy.batch_quiet_seconds:
            return None
        if len(self.events) >= threshold:
            return 'message_count'
        samples = [duration for ts, duration in self.latencies if now - ts < 600]
        if samples and len(self.events) + idle / max(.1, sum(samples) / len(samples)) >= threshold:
            return 'idle_compensation'
        return None


class ConversationScheduler:
    def __init__(self, store, runtime, concurrency=10, clock=time.monotonic, direct_followup_seconds=90):
        self.store, self.runtime, self.concurrency = store, runtime, concurrency
        self.clock = clock
        self.direct_followup_seconds = max(1, float(direct_followup_seconds))
        self.turns: dict[str, PendingTurn] = {}
        self.running = {}

    async def accept(self, events):
        new=[]
        for event in events:
            state = self.turns.setdefault(event.destination.key, PendingTurn())
            if any(e.id == event.id for e in state.events):continue
            now=self.clock()
            if not state.events:state.first_at=now
            state.last_at=now
            state.events.append(event)
            new.append((event,state))
        for event,state in new:
            try:
                state.policy = await self.store.policy(event.destination)
                reply = await self.store.reply_is_bot(event)
                direct = self.runtime.social.decide(event, state.policy, reply,
                    self.runtime.prompt_guard.called_by_name(event.text)).action == Action.REPLY
                if direct:state.direct_ids.add(event.id)
                # Keep the requester scoped to the follow-up window.  This is
                # deliberately short and actor-bound, so an unrelated group
                # message cannot inherit somebody else's bot invocation.
                if direct:
                    state.direct_actor = event.actor_id
                    state.direct_until = now + self.direct_followup_seconds
                elif (state.direct_actor == event.actor_id and now <= state.direct_until
                      and not any(s.get('type') == 'at' for s in event.segments)
                      and not event.reply_to_id
                      and event.event_type == 'message.created'):
                    state.followup_ids.add(event.id)
                await self.runtime.streams.observe(event)
                from osa.application.flow import stage
                await stage(getattr(self.runtime, 'flow', None), event.id, 'scheduled',
                            trigger='direct' if direct else 'followup' if event.id in state.followup_ids else 'batch_wait')
            except asyncio.CancelledError:raise
            except Exception as exc:
                logger.warning('conversation_batch_failed conversation=%s error_type=%s',event.destination.key,type(exc).__name__)

    async def _execute(self, key, state, events, direct_ids, trigger):
        started = self.clock()
        try:
            if PendingTurn.control(events[0]):
                await self.runtime.process(events[0])
                outcome = {'action': 'EVENT'}
            else:
                logger.info('conversation_batch_started conversation=%s message_count=%s trigger=%s queue_age=%.1f',
                            key, len(events), trigger, max(0, time.time()-events[0].occurred_at))
                outcome = await self.runtime.process_batch(events, direct_ids, trigger,
                                                          allow_defer=state.deferrals < 1)
            now = self.clock()
            action = outcome.get('action', 'SKIP')
            if action == 'DEFER' and state.deferrals < 1:
                # A timed reconsideration is bounded to one retry, even with no new input.
                state.events = events + state.events
                state.deferrals += 1
                state.first_at = started
                state.not_before = now + max(1, min(30, outcome.get('wait_seconds', state.policy.decision_wait_seconds)))
                logger.info('conversation_batch_waiting conversation=%s message_count=%s wait_seconds=%.1f',
                            key, len(state.events), state.not_before - now)
            else:
                state.deferrals = 0
                if action == 'REPLY':
                    state.latencies.append((now, max(.1, now - started)))
                state.not_before = max(now, started + state.policy.decision_cooldown_seconds)
                logger.info('conversation_batch_finished conversation=%s message_count=%s action=%s cooldown_seconds=%.1f',
                            key, len(events), action, max(0, state.not_before - now))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning('conversation_batch_failed conversation=%s error_type=%s', key, type(exc).__name__)
            for event in events:
                await self.store.finish_event(event.id, type(exc).__name__)
            state.not_before = self.clock() + state.policy.decision_cooldown_seconds
        finally:
            state.active = False

    async def tick(self):
        for task in list(self.running):
            if task.done():
                self.running.pop(task)
                try:task.result()
                except asyncio.CancelledError:raise
                except Exception as exc:logger.warning('inbox_task_failed error_type=%s',type(exc).__name__)
        # Bound leased but unfinished events. Unclaimed input remains durable in the store.
        capacity = 2048 - sum(len(s.events) for s in self.turns.values())
        if capacity > 0:
            await self.accept(await self.store.claim_events(min(64, capacity)))
        for key, state in sorted(self.turns.items(), key=lambda item: (
                0 if item[1].events and PendingTurn.control(item[1].events[0]) else
                1 if item[1].direct_ids or item[1].followup_ids else 2, item[1].first_at)):
            if len(self.running) >= self.concurrency:
                break
            if state.events:
                state.policy = await self.store.policy(state.events[0].destination)
            # Expired incidental chat must not spend another model turn and
            # delay current requests. Explicit mentions, commands and file/
            # control events remain durable even when they have waited.
            stale = [e for e in state.events if e.id not in state.direct_ids
                     and not PendingTurn.control(e)
                     and all(s.get('type') in {'text','at','reply'} for s in e.segments)
                     and time.time()-e.occurred_at > state.policy.reply_ttl_seconds]
            if stale:
                stale_ids = {e.id for e in stale}
                state.events = [e for e in state.events if e.id not in stale_ids]
                state.followup_ids.difference_update(stale_ids)
                for event in stale:
                    await self.store.record_decision(event, Action.IGNORE, 'batch_expired', {}, None)
                    await self.store.finish_event(event.id)
                logger.info('conversation_batch_expired conversation=%s message_count=%s', key, len(stale))
            trigger = state.due(self.clock())
            if trigger is None:
                continue
            if trigger not in {'direct', 'event'} and self.clock() - state.first_at < 30:
                image_sources = [event.id for event in state.events
                                 if any(segment.get('type') in {'image', 'mface'} for segment in event.segments)]
                images = getattr(getattr(self.runtime, 'features', None), 'images', None)
                if image_sources and images and images.pending_for(image_sources):
                    continue
            # Keep control events separate; never consume a recall/file event as chat text.
            if PendingTurn.control(state.events[0]):
                count = 1
            else:
                count = next((i for i, e in enumerate(state.events) if PendingTurn.control(e)), len(state.events))
                # Implicit same-user follow-ups are context, not a queue of
                # separate slow model turns.
                # One planning round sees all waiting invocations. Tool
                # scope is selected using a real request ID, never by copying
                # another participant's text into the anchor's identity.
                count = min(count, 40)
            events, state.events = state.events[:count], state.events[count:]
            direct_ids = state.direct_ids.intersection(e.id for e in events)
            if not direct_ids:
                direct_ids = state.followup_ids.intersection(e.id for e in events)
            state.direct_ids.difference_update(direct_ids)
            state.followup_ids.difference_update(e.id for e in events)
            state.active = True
            state.first_at = self.clock()
            task = asyncio.create_task(self._execute(key, state, events, direct_ids, trigger))
            self.running[task] = key
        for key in list(self.turns):
            state = self.turns[key]
            if not state.events and not state.active and self.clock() - state.last_at > 3600:
                self.turns.pop(key)

    async def run(self):
        try:
            while True:
                try:
                    await self.tick()
                except asyncio.CancelledError:raise
                except Exception as exc:
                    logger.warning('inbox_claim_failed error_type=%s',type(exc).__name__)
                    await asyncio.sleep(1)
                await asyncio.sleep(.1)
        finally:
            for task in self.running:
                task.cancel()
            await asyncio.gather(*self.running, return_exceptions=True)
