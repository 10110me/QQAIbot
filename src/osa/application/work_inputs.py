"""Scope-bound QQ attachments. Archive entries are read as data, never executed."""
import asyncio
import base64
import hashlib
import io
import logging
import re
import zipfile
import tarfile
from pathlib import Path, PurePosixPath

from osa.domain.features import Record, ParserRejected

logger=logging.getLogger(__name__)
MAX_BYTES=10*1024*1024


def attachment_text(content,name):
    result=[];total=0
    if name.lower().endswith(('.rar','.7z','.exe','.dll','.png','.jpg','.jpeg','.mp4','.mp3')):
        return [{'name':name,'status':'stored_only','bytes':len(content),'note':'已保留原文件，未解析此二进制格式'}]
    def add(filename,data):
        nonlocal total
        path=PurePosixPath(filename.replace('\\','/'))
        if path.is_absolute() or '..' in path.parts or re.match(r'^[A-Za-z]:',filename):
            raise ValueError('Archive contains unsafe path')
        if len(data)>500000 or total+len(data)>2000000:raise ValueError('Archive text size limit exceeded')
        total+=len(data)
        if b'\x00' in data[:8192]:
            result.append({'name':filename,'status':'binary','bytes':len(data)});return
        for encoding in ['utf-8-sig','gb18030']:
            try:text=data.decode(encoding);break
            except UnicodeDecodeError:text=None
        result.append({'name':filename,'text':text[:100000] if text is not None else None,
                       'status':'text' if text is not None else 'binary','bytes':len(data),
                       'truncated':text is not None and len(text)>100000})
    stream=io.BytesIO(content)
    if zipfile.is_zipfile(stream):
        with zipfile.ZipFile(stream) as archive:
            files=[f for f in archive.infolist() if not f.is_dir()]
            if len(files)>80 or sum(f.file_size for f in files)>2000000:raise ValueError('Archive expansion limit exceeded')
            for file in files:
                if file.flag_bits&1 or (file.external_attr>>16)&0o170000==0o120000:raise ValueError('Encrypted or symbolic archive entry')
                add(file.filename,archive.read(file))
    elif name.lower().endswith(('.tar','.tar.gz','.tgz','.tar.bz2','.tar.xz')):
        with tarfile.open(fileobj=io.BytesIO(content),mode='r:*') as archive:
            for index,file in enumerate(archive):
                if index>=80 or file.size>500000 or total+file.size>2000000:raise ValueError('Archive expansion limit exceeded')
                if file.isdir():continue
                if not file.isfile():raise ValueError('Links and special archive entries denied')
                add(file.name,archive.extractfile(file).read())
    else:
        if len(content)>500000:return [{'name':name,'status':'stored_only','bytes':len(content),'note':'超过文本解析上限，保留原文件'}]
        add(name,content)
    return result


class WorkInputs:
    def __init__(self,features,platform,store):
        self.features,self.platform,self.store=features,platform,store

    async def observe(self,event):
        if event.destination.kind=='group' and not getattr(self.features.settings, 'group_file_download_enabled', True):
            return
        if event.destination.kind=='group' and not (await self.store.policy(event.destination)).sandbox:return
        group=event.destination.id if event.destination.kind=='group' else None
        for segment in event.segments:
            if segment['type']!='file':continue
            data=segment['data'];file_id=data.get('file_id') or data.get('id')
            if not file_id:continue
            key=hashlib.sha256((event.id+':'+file_id).encode()).hexdigest()
            if await self.features.repo.get(key):continue
            payload={'source_id':event.id,'message_id':event.message_id,'conversation':event.destination.key,
                     'name':Path(data.get('name') or 'attachment').name[:150],'file_id':file_id,'occurred_at':event.occurred_at}
            await self.features.repo.put(Record(key,'work_input',event.actor_id,group,'downloading',payload))
            try:
                if int(data.get('size') or 0)>MAX_BYTES:raise ValueError('Attachment exceeds 10 MiB')
                args={'file_id':file_id}
                action='get_private_file_url'
                if group:
                    # Match the installed NapCat 4.18.1 payload. File IDs
                    # refer to NapCat's process-local cache.
                    action='get_group_file_url';args.update(group_id=int(group))
                try:
                    remote=await self.platform.call(action,args)
                except Exception as exc:
                    # NapCat/OneBot deployments may not expose the group
                    # extension even though file listing works.  Fall back to
                    # the standard get_file URL action before marking input
                    # failed.
                    if group and action == 'get_group_file_url':
                        try:
                            remote=await self.platform.call('get_file', {'file_id': str(file_id)})
                        except Exception:
                            remote=await self.platform.call('get_file', {'file': str(file_id)})
                    else:
                        raise exc
                url=remote.get('url') or remote.get('file_url')
                if not url:raise ValueError('File download URL unavailable')
                content,mime,_=await self.features.downloader.fetch(url,max_bytes=MAX_BYTES)
                await self.features.objects.put(key,content,mime)
                if payload['name'].lower().endswith(('.pdf','.docx')):
                    try:
                        pages=await self.features.parser.parse(content,payload['name'])
                        parsed=[{'name':payload['name'],'text':'\n'.join(page['text'] for page in pages)[:100000],
                            'status':'text','truncated':sum(len(page['text']) for page in pages)>100000}]
                    except ParserRejected:
                        parsed=[{'name':payload['name'],'status':'stored_only','note':'未能解析正文，保留原文件供工作模型或 DeepSeek 读取'}]
                else:
                    parsed=await asyncio.to_thread(attachment_text,content,payload['name'])
                await self.features.repo.patch(key,status='ready',payload={**payload,'object_key':key,
                    'sha256':hashlib.sha256(content).hexdigest(),'bytes':len(content),'mime_type':mime,'entries':parsed})
                logger.info('work_input_ready event_id=%s bytes=%s',event.id,len(content))
            except Exception as exc:
                await self.features.repo.patch(key,status='failed',payload={**payload,'error':type(exc).__name__})
                logger.warning('work_input_failed event_id=%s error_type=%s',event.id,type(exc).__name__)

    async def capture_media(self, event):
        """Copy incoming QQ media to the same immutable input store as files."""
        group = event.destination.id if event.destination.kind == 'group' else None
        policy = await self.store.policy(event.destination)
        if group and not policy.media_read:
            return []
        images=getattr(self.features,'images',None)
        if images:
            selected=await images.rows(event,wait_seconds=0)
            values=[]
            for image in selected:
                p=image.payload
                if not p.get('object_key'):continue
                key=hashlib.sha256((event.id+':image-share:'+image.id).encode()).hexdigest()
                row=await self.features.repo.get(key)
                if row is None:
                    raw=await self.features.objects.get(p['object_key'])
                    await self.features.objects.put(key,raw,p['mime_type'])
                    name='QQ-image.'+p['mime_type'].split('/')[-1].replace('jpeg','jpg')
                    row=await self.features.repo.put(Record(key,'work_input',event.actor_id,group,'ready',{
                        'source_id':p['source_id'],'message_id':p['message_id'],'conversation':event.destination.key,
                        'occurred_at':p['occurred_at'],'name':name,'object_key':key,'sha256':p['sha256'],
                        'bytes':p['bytes'],'mime_type':p['mime_type'],
                        'entries':[{'name':name,'status':'stored_only','bytes':p['bytes']}]}))
                values.append(row)
            if values:return values
        if event.id not in self.features.media_inputs:
            candidates = [source for source in getattr(self.features, 'media_events', {}).values()
                if source.id in self.features.media_inputs and source.destination == event.destination
                and source.message_id == event.reply_to_id]
            values = []
            for source in sorted(candidates, key=lambda e: e.occurred_at, reverse=True)[:3]:
                if await self.store.event_active(source.id):
                    values += await self.capture_media(source)
            return values
        if not await self.store.event_active(event.id):
            raise PermissionError('Media source recalled')
        values = []
        for index, item in enumerate(self.features.media_inputs.get(event.id, [])[:3]):
            # ImageContext owns image download, retry and original storage. A
            # failed image must not be downloaded again through the 10 MiB file
            # path and abort preparation of the entire thinking turn.
            if images and item.get('kind')=='image':continue
            key = hashlib.sha256((event.id + ':media:' + str(index)).encode()).hexdigest()
            row = await self.features.repo.get(key)
            if row and row.status == 'ready':
                values.append(row)
                continue
            raw, mime, _ = await self.features.downloader.fetch(item['url'], max_bytes=MAX_BYTES)
            suffix = {'image/png': '.png', 'image/jpeg': '.jpg', 'image/gif': '.gif',
                      'image/webp': '.webp', 'video/mp4': '.mp4', 'audio/mpeg': '.mp3'}.get(mime.split(';')[0], '.bin')
            name = 'QQ-' + str(item.get('kind', 'media')) + suffix
            await self.features.objects.put(key, raw, mime)
            row = await self.features.repo.put(Record(key, 'work_input', event.actor_id, group, 'ready', {
                'source_id': event.id, 'message_id': event.message_id, 'conversation': event.destination.key,
                'occurred_at': event.occurred_at, 'name': name, 'object_key': key,
                'sha256': hashlib.sha256(raw).hexdigest(), 'bytes': len(raw), 'mime_type': mime,
                'entries': [{'name': name, 'status': 'stored_only', 'bytes': len(raw)}]
            }, key))
            values.append(row)
        return values

    async def consultation_attachments(self, context, attachments):
        values, seen = [], set()
        for attachment in attachments:
            raw = base64.b64decode(attachment.get('data_base64') or '', validate=True)
            digest = hashlib.sha256(raw).hexdigest()
            if digest not in seen:
                seen.add(digest)
                values.append(attachment)
            if len(values) >= 4:
                break
        for item in context.get('dependency_snapshot', {}).get('shared_files', []):
            if len(values) >= 4:
                break
            row = await self.features.repo.get(item.get('id', ''))
            if not row or row.status != 'ready' or row.owner_id != context['actor_id'] or row.group_id != context.get('group_id'):
                raise PermissionError('Shared input unavailable')
            if not await self.store.event_active(row.payload['source_id']):
                raise PermissionError('Shared input recalled')
            raw = await self.features.objects.get(row.payload['object_key'])
            if hashlib.sha256(raw).hexdigest() != row.payload['sha256']:
                raise PermissionError('Shared input integrity mismatch')
            if (row.payload.get('mime_type','').startswith('image/') or len(raw) <= 5_000_000) and row.payload['sha256'] not in seen:
                seen.add(row.payload['sha256'])
                values.append({'name': row.payload['name'], 'mime_type': row.payload.get('mime_type', 'application/octet-stream'),
                               'data_base64': base64.b64encode(raw).decode('ascii')})
        return values

    async def recent(self,event):
        rows=await self.features.repo.find('work_input',owner_id=event.actor_id,
            group_id=event.destination.id if event.destination.kind=='group' else None,limit=20,
            private_only=event.destination.kind=='private')
        values=[]
        for row in rows:
            if row.payload.get('conversation')!=event.destination.key:continue
            if not await self.store.event_active(row.payload['source_id']):continue
            if row.payload.get('message_id')==event.reply_to_id or 0<=event.occurred_at-row.payload.get('occurred_at',row.created_at)<=1200 or row.payload['source_id']==event.id:
                values.append(row)
        return values[:3]

    async def bind(self,task,event):
        rows=await self.recent(event)
        return await self.bind_rows(task, rows)

    async def bind_rows(self, task, rows):
        binding_id=hashlib.sha256(('work-inputs:'+task['id']).encode()).hexdigest()
        existing=await self.features.repo.get(binding_id)
        input_ids=list(existing.payload.get('inputs', [])) if existing else []
        for row in rows:
            if row.id not in input_ids:
                input_ids.append(row.id)
        binding=Record(binding_id, 'work_input_binding',task['owner_id'],task.get('group_id'),'active',
            {'task_id':task['id'],'inputs':input_ids},'work-inputs:'+task['id'])
        if existing:
            await self.features.repo.patch(binding_id, payload=binding.payload, status='active')
        else:
            await self.features.repo.put(binding)
        return rows

    async def for_task(self,task,generation):
        key=hashlib.sha256(('work-inputs:'+task['id']).encode()).hexdigest()
        binding=await self.features.repo.get(key)
        if not binding:return []
        if not re.fullmatch(r'[a-f0-9]{32}',task['id']) or not isinstance(generation,int) or generation<1:raise PermissionError('Invalid work directory')
        if binding.owner_id!=task['owner_id'] or binding.group_id!=task.get('group_id'):raise PermissionError('Input scope mismatch')
        values=[]
        directory=Path(self.features.settings.workspace_dir)/task['id']/f'generation-{generation}'/'inputs'
        for input_id in binding.payload['inputs']:
            row=await self.features.repo.get(input_id)
            if not row or row.owner_id!=task['owner_id'] or row.group_id!=task.get('group_id'):raise PermissionError('Input unavailable')
            if not await self.store.event_active(row.payload['source_id']):raise PermissionError('Input recalled')
            if row.status!='ready':raise ValueError('Input could not be read; ask the user to upload a supported readable file')
            content=await self.features.objects.get(row.payload['object_key'])
            if hashlib.sha256(content).hexdigest()!=row.payload['sha256']:raise PermissionError('Input integrity mismatch')
            await asyncio.to_thread(directory.mkdir,parents=True,exist_ok=True)
            filename=hashlib.sha256(input_id.encode()).hexdigest()[:12]+'-'+re.sub(r'[^\w.\-\u4e00-\u9fff]','_',row.payload['name'])
            await asyncio.to_thread((directory/filename).write_bytes,content)
            values.append({'name':row.payload['name'],'sha256':row.payload['sha256'],'bytes':len(content),
                'workspace_input':'inputs/'+filename,'entries':row.payload['entries']})
        return values
