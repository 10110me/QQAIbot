import asyncio
import base64
import json

import httpx
from osa.infrastructure.api_limits import api_slot


class OpenAIMediaProvider:
    def __init__(self, base_url: str, key: str, vision: str | None, audio: str, tts: str) -> None:
        self.url, self.key, self.vision, self.audio, self.tts = base_url.rstrip("/"), key, vision, audio, tts

    async def analyze_image(self, content: bytes, mime: str, question: str) -> str:
        from osa.application.prompting import PromptGuard
        guard=getattr(self,'prompt_guard',None) or PromptGuard()
        if not self.vision:
            raise ValueError("Vision model not configured")
        async with httpx.AsyncClient(timeout=60) as client:
            async with api_slot(self.url):
                response = await client.post(self.url + "/chat/completions", headers={"Authorization": "Bearer " + self.key},
                    json={"model": self.vision, "messages": [{"role": "system", "content": guard.slot('image')},
                        {"role": "user", "content": [{"type": "text", "text": question or "描述图片中的主要信息。"},
                            {"type": "image_url", "image_url": {"url": f"data:{mime};base64," + base64.b64encode(content).decode()}}]}], "max_tokens": 1000})
            response.raise_for_status()
            return response.json()["choices"][0]["message"]["content"]

    async def transcribe(self, content: bytes, name: str, mime: str) -> str:
        async with httpx.AsyncClient(timeout=90) as client:
            async with api_slot(self.url):
                response = await client.post(self.url + "/audio/transcriptions", headers={"Authorization": "Bearer " + self.key},
                    data={"model": self.audio}, files={"file": (name, content, mime)})
            response.raise_for_status()
            return response.json()["text"]

    async def speech(self, text: str, voice: str = "alloy") -> bytes:
        async with httpx.AsyncClient(timeout=60) as client:
            async with api_slot(self.url):
                response = await client.post(self.url + "/audio/speech", headers={"Authorization": "Bearer " + self.key},
                    json={"model": self.tts, "voice": voice, "input": text[:4000], "response_format": "mp3"})
            response.raise_for_status()
            return response.content


class RoutedMediaProvider:
    """Use the same live provider/model/task routing as MaiBot's VLM task."""
    supports_task_routing=True
    def __init__(self, model, fallback):
        self.model,self.fallback=model,fallback
        self.prompt_guard=None
        self._pending={}

    async def analyze_image(self, content, mime, question='', *, emoji=False):
        from osa.domain.context import model_context
        context=model_context.get()
        scope=('group', context['group_id']) if context.get('group_id') else (
            'private', context['actor_id']) if context.get('actor_id') else ('isolated', id(asyncio.current_task()))
        key=(scope, bool(emoji))
        future=asyncio.get_running_loop().create_future()
        queue=self._pending.setdefault(key, [])
        queue.append((content,mime,question,future))
        if len(queue)==1:
            asyncio.create_task(self._flush(key))
        return await future

    async def _describe_one(self, content, mime, question, emoji, purpose=None):
        from osa.application.prompting import PromptGuard
        guard=self.prompt_guard or PromptGuard()
        request=question.strip() or '请用中文描述图片主体、场景和可辨文字。'
        result=await self.model.generate([
            {'role':'system','content':guard.slot('image')},
            {'role':'user','content':[{'type':'text','text':request},
                {'type':'image_url','image_url':{'url':f'data:{mime};base64,'+base64.b64encode(content).decode('ascii')}}]}],
            purpose or ('emoji' if emoji else 'vision'))
        return result.text

    async def analyze_video(self, content: bytes, mime: str, question: str = '') -> str:
        from osa.application.prompting import PromptGuard
        if not content or len(content) > 20_000_000:
            raise ValueError('Direct video input must be nonempty and at most 20 MB')
        mime = mime.split(';', 1)[0].strip().lower()
        if mime == 'application/octet-stream' and content[4:8] == b'ftyp':
            mime = 'video/mp4'
        if mime == 'video/quicktime':
            mime = 'video/mov'
        if not mime.startswith('video/') or any(character in mime for character in ('\r', '\n', ',')):
            raise ValueError('Unsupported video MIME type')
        guard = self.prompt_guard or PromptGuard()
        result = await self.model.generate([
            {'role': 'system', 'content': guard.slot('video')},
            {'role': 'user', 'content': [
                {'type': 'text', 'text': question.strip() or '请按时间顺序描述视频中的主要内容和变化。'},
                {'type': 'video_url', 'video_url': {'url': f'data:{mime};base64,'
                    + base64.b64encode(content).decode('ascii')}}]}], 'video')
        return result.text

    async def _flush(self, key):
        await asyncio.sleep(.12)
        items=self._pending.pop(key, [])
        if not items:
            return
        emoji=key[1]
        try:
            if len(items)==1:
                descriptions=[await self._describe_one(*items[0][:3], emoji)]
            elif emoji:
                descriptions=await asyncio.gather(*[self._describe_one(*item[:3], emoji) for item in items],
                                                  return_exceptions=True)
            else:
                from osa.application.prompting import PromptGuard
                guard=self.prompt_guard or PromptGuard()
                content=[{'type':'text','text':
                    '按输入顺序分别描述以下图片，只返回 JSON 字符串数组，每张图片对应一个元素。不要猜测看不清的内容。'}]
                for index,(raw,mime,question,_) in enumerate(items,1):
                    content.extend([{'type':'text','text':f'第{index}张：{question}'},
                        {'type':'image_url','image_url':{'url':f'data:{mime};base64,'+base64.b64encode(raw).decode('ascii')}}])
                result=await self.model.generate([
                    {'role':'system','content':guard.slot('image')},
                    {'role':'user','content':content}], 'emoji' if emoji else 'vision')
                response_text=result.text.strip()
                if response_text.startswith('```') and response_text.endswith('```'):
                    response_text=response_text.split('\n',1)[-1].rsplit('```',1)[0].strip()
                descriptions=json.loads(response_text)
                if isinstance(descriptions,dict):
                    descriptions=descriptions.get('descriptions')
                if not isinstance(descriptions,list) or len(descriptions)!=len(items) or any(
                        not isinstance(value,str) or not value.strip() for value in descriptions):
                    raise ValueError('Invalid batched image descriptions')
        except asyncio.CancelledError:
            for *_,future in items:
                if not future.done():future.cancel()
            raise
        except Exception:
            results=await asyncio.gather(*[self._describe_one(*item[:3], emoji) for item in items],
                                         return_exceptions=True)
            descriptions=results
        for item,description in zip(items,descriptions):
            future=item[3]
            if future.done():continue
            if isinstance(description,BaseException):future.set_exception(description)
            else:future.set_result(description)

    async def transcribe(self,*args,**kwargs):return await self.fallback.transcribe(*args,**kwargs)
    async def speech(self,*args,**kwargs):return await self.fallback.speech(*args,**kwargs)
