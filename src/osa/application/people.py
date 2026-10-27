"""Group-scoped member names and evidenced nicknames, never persona rules."""
import hashlib
from osa.domain.features import Record


class PeopleService:
    def __init__(self, repo, store):
        self.repo, self.store = repo, store
        self.bot_id = ''

    async def observe(self, event, policy):
        if not policy.collect_messages or not event.actor_name or event.event_type != 'message.created':
            return
        group = event.destination.id if event.destination.kind == 'group' else None
        key = 'person:'+event.destination.key+':'+event.actor_id
        identity = hashlib.sha256(key.encode()).hexdigest()
        payload = {'name':event.actor_name, 'nickname':event.actor_nickname, 'source_ids':[event.id]}
        old = await self.repo.get(identity)
        if old:
            await self.repo.patch(identity, status='active', payload=payload)
        else:
            await self.repo.put(Record(identity, 'person_name', event.actor_id, group, 'active', payload, key))

    async def members(self, event):
        policy = await self.store.policy(event.destination)
        group = event.destination.id if event.destination.kind == 'group' else None
        members = {event.actor_id:{'qq':event.actor_id,'name':event.actor_name or event.actor_nickname,'aliases':[]}}
        if not policy.collect_messages:
            return list(members.values()), []
        dependencies = []
        for kind in ['person_name', 'person_alias']:
            rows = await self.repo.find(kind, group_id=group, status='active', limit=200, private_only=group is None)
            for row in rows:
                if group is None and row.owner_id != event.actor_id:
                    continue
                ids = row.payload.get('source_ids', [])
                if not ids or any([not await self.store.event_active(sid) for sid in ids]):
                    continue
                person = members.setdefault(row.owner_id, {'qq':row.owner_id,'name':'','aliases':[]})
                if kind == 'person_name':
                    if row.owner_id != event.actor_id or not person['name']:
                        person['name'] = row.payload.get('name','')
                elif policy.memory_extract:
                    person['aliases'].append(row.payload['alias'])
                dependencies.extend(ids)
        return list(members.values())[:80], list(dict.fromkeys(dependencies))

    async def learn_alias(self, destination, item, batch):
        if not isinstance(item, dict):
            return
        subject, alias = str(item.get('subject_id','')), item.get('alias')
        if not subject.isdecimal() or subject == self.bot_id:
            return
        if not isinstance(alias,str) or not 1 <= len(alias.strip()) <= 24 or any(c in alias for c in '\n\r<>'):
            return
        alias = alias.strip()
        sources = item.get('source_ids',[])
        if not isinstance(sources,list) or not sources:
            return
        events = {e.id:e for e in batch}
        target_names = {e.actor_name for e in batch if e.actor_id == subject and e.actor_name}
        for sid in sources:
            e = events.get(sid)
            if not e or alias not in e.text or not await self.store.event_active(sid):
                return
            targeted = e.actor_id == subject or any(s['type']=='at' and str(s['data'].get('qq'))==subject for s in e.segments)
            targeted |= any(other.actor_id==subject and other.message_id==e.reply_to_id for other in batch)
            targeted |= any(name != alias and name in e.text for name in target_names)
            if not targeted:
                return
        key='alias:'+destination.key+':'+subject+':'+alias
        await self.repo.put(Record(hashlib.sha256(key.encode()).hexdigest(),'person_alias',subject,
            destination.id if destination.kind=='group' else None,'active',{'alias':alias,'source_ids':sources,'trust_status':'group_usage'},key))
