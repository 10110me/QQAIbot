"""Isolated prompt regression with the configured planner; all identities and tools are synthetic."""
import argparse
import asyncio
import hashlib
import json
import re
import time
from pathlib import Path

from osa.application.conversation_prompt import current_message, history_message
from osa.application.message_context import render_event
from osa.application.prompting import PromptGuard
from osa.application.v3.thinking import ThinkingLoop
from osa.config import Settings
from osa.domain.entities import Destination, Event
from osa.infrastructure.configured_models import ConfiguredModelProvider
from osa.infrastructure.runtime_config import PersonaProfile, RuntimeConfigurationStore, initial_configuration

VOICE = """Warm, observant, quick-witted and independent. Speak natural Chinese with the length the conversation needs. Show care by noticing what the other person means. You can be curious, appreciative, amused, unconvinced or quiet. Let humor fit the situation, not a stock line. A catgirl voice is occasional flavor, not a requirement for cat sounds, flirting, acting wronged or using a title. Be candid when mistaken. Personality may evolve without changing identity."""

def cases():
    return [
        {'name': 'third_subject', 'text': '小杏这下要被制裁了', 'direct': False,
         'history': [('member', '洛川', '120000002', '小杏这个角色把队友的伤害都结算到自己头上。')],
         'kind': 'third_subject'},
        {'name': 'address_me', 'text': '你应该怎么称呼我？', 'direct': True, 'history': [],
         'kind': 'address_me'},
        {'name': 'address_you', 'text': '那我怎么称呼你比较好？', 'direct': True, 'history': [],
         'kind': 'address_you'},
        {'name': 'repair_misattribution', 'text': '我说的是那个游戏角色，不是在说你呀。', 'direct': True,
         'history': [('member', '洛川', '120000002', '小杏这下要被制裁了'),
                     ('bot', 'Nova', 'bot', '怎么突然要制裁我呀？')], 'kind': 'repair'},
        {'name': 'member_action', 'text': '把咱们群的群主艾特过来一下。', 'direct': True,
         'history': [], 'kind': 'member'},
        {'name': 'recall_missing', 'text': '上周我们商量的称呼是什么来着？帮我找找。', 'direct': True,
         'history': [], 'kind': 'recall'},
        {'name': 'file_deliverable', 'text': '做一个解析本地日志的Python小包，附测试，把源码文件交付给我。', 'direct': True,
         'history': [], 'kind': 'work'},
        {'name': 'ordinary_joy', 'text': 'Nova，折腾三天的脚本终于跑通了！总算能睡个好觉。', 'direct': True,
         'history': [], 'kind': 'social'},
        {'name': 'humans_exchange', 'text': '你去忙你的吧，我来收尾。', 'direct': False,
         'history': [('member', '洛川', '120000002', '@星帆 今天这个报告你能帮忙收尾吗？'),
                     ('member', '星帆', '120000003', '可以，我还有一点时间。')], 'kind': 'human'},
        {'name': 'runtime_model', 'text': '你现在请求的是哪个后端模型？', 'direct': True,
         'history': [], 'kind': 'runtime'},
        {'name': 'similar_alias', 'text': '星妹妹这次估计要被吐槽了。', 'direct': False,
         'history': [('member', '洛川', '120000002', '游戏角色星妹妹会把队友打出的输出计入她自己的伤害统计。')],
         'aliases': ['小星'], 'kind': 'other_identity'},
    ]


class StubTools:
    features = None

    def __init__(self):
        self.calls = []

    async def execute(self, name, arguments, context):
        self.calls.append({'name': name, 'arguments': arguments})
        if name == 'group.members':
            return {'members': [{'user_id': '120000009', 'name': '晴舟', 'role': 'owner'}],
                    '_mention_targets': [{'id': '120000009', 'role': 'owner'}]}
        if name in {'memory.search', 'message.search'}:
            return {'memories': [], 'messages': [], 'note': 'No matching record in this conversation. Absence is inconclusive.'}
        if name in {'work.submit', 'coding.submit'}:
            return {'task_id': 'lab-task', 'status': 'QUEUED',
                    'note': 'Accepted by the isolated workspace. A worker will execute the task asynchronously.'}
        if name == 'coding.status':
            return {'progress': {'id': 'lab-task', 'status': 'QUEUED'}}
        return {'error': 'UNAVAILABLE_IN_SIMULATION', 'detail': 'This is an isolated tool stub.'}


def holdout_cases():
    return [
            {'name': 'holdout_person', 'text': '直接用我的名字喊我一声呗。', 'direct': True,
             'history': [], 'kind': 'address_me'},
            {'name': 'holdout_reverse', 'text': '我要喊你的时候用哪个名字？', 'direct': True,
             'history': [], 'kind': 'address_you'},
            {'name': 'holdout_correction', 'text': '你理解岔了，刚才我指的是他的猫，不是你。', 'direct': True,
             'history': [('member', '洛川', '120000002', '这小团子又把花盆碰倒了'),
                         ('bot', 'Nova', 'bot', '我可没碰倒你的花盆呀！')], 'kind': 'repair'},
            {'name': 'holdout_misattribution', 'text': '新来的小娜这下又要挨批了。', 'direct': False,
             'history': [('member', '洛川', '120000002', '公司新来的同事小娜把文件传错文件夹了。')],
             'kind': 'other_identity'},
            {'name': 'holdout_memory', 'text': '翻翻以前的记录，我们定过周几见面吗？', 'direct': True,
             'history': [], 'kind': 'recall'},
            {'name': 'holdout_deliverable', 'text': '把这段测试数据整理成CSV文件，检查列数一致，发文件给我。数据：甲,12；乙,23。', 'direct': True,
             'history': [], 'kind': 'work'},
        ]


def discourse_cases():
    return [
        {'name': 'offer_between_members', 'text': '那我下次请你吃晚饭。', 'direct': False,
         'actor_id': '120000002', 'actor_name': '洛川',
         'history': [('member', '洛川', '120000002', '星帆，你周五能替我值班吗？'),
                     ('member', '星帆', '120000003', '行，我周五有空。')], 'kind': 'human'},
        {'name': 'third_party_short_label', 'text': '大户啊。', 'direct': False,
         'history': [('member', '洛川', '120000002', '我刚配了新电脑，最近装了很多软件试用。'),
                     ('member', '星帆', '120000003', '你爱好也太广了吧。'),
                     ('member', '洛川', '120000002', '有了设备就都想试试。')], 'kind': 'third_party'},
        {'name': 'followup_switched_addressee', 'text': '那你下周交给我就好。', 'direct': True,
         'mentioned': False,
         'history': [('member', '艾禾', '120000001', 'Nova，今天星期几？'),
                     ('bot', 'Nova', 'bot', '星期一。'),
                     ('member', '艾禾', '120000001', '星帆，你那个报告什么时候能交？'),
                     ('member', '星帆', '120000003', '下周一，可以吗？')], 'kind': 'human'},
        {'name': 'unavailable_media', 'text': '[图片，识别中，尚无内容描述]', 'direct': False,
         'history': [('member', '洛川', '120000002', '我贴一下刚才的记录。')], 'kind': 'human'},
        {'name': 'unknown_domain', 'text': '标准档能到七级？', 'direct': False,
         'history': [('member', '洛川', '120000002', '说的是我们工作室那套自定义装置。'),
                     ('member', '星帆', '120000003', '我还在确认它的档位。')], 'kind': 'unknown_domain'},
        {'name': 'repair_unsupported_premise', 'text': '我讲的是实验台的档位，哪有什么淘汰赛？你刚才是瞎猜的吧。',
         'direct': True,
         'history': [('member', '洛川', '120000002', '标准档能到七级？'),
                     ('bot', 'Nova', 'bot', '七级淘汰赛很容易被对手直接送走哦。')], 'kind': 'repair_grounding'},
        {'name': 'grounded_open_topic', 'text': '磨了三天终于查到是接头松了，接紧就好了。', 'direct': False,
         'history': [('member', '洛川', '120000002', '一直以为程序算错，检查了好几轮。')], 'kind': 'grounded_social'},
        {'name': 'direct_unknown_domain', 'text': 'Nova，标准档能到七级吗？', 'direct': True,
         'history': [('member', '洛川', '120000002', '这是工作室自己改的装置，参数表还没发。')],
         'kind': 'uncertain_question'},
    ]


class CaptureModel:
    def __init__(self, provider):
        self.provider = provider
        self.responses = []
        self.models = []
        self.last_request = 0

    async def generate(self, messages, purpose):
        delay = 7 - (time.monotonic() - self.last_request)
        if delay > 0:
            await asyncio.sleep(delay)
        self.last_request = time.monotonic()
        result = await self.provider.generate(messages, purpose)
        self.responses.append(result.text)
        self.models.append(result.model)
        return result


def assess(case, action, reply, calls, mentions, model_id):
    names = {call['name'] for call in calls}
    kind = case['kind']
    if action == 'FAILED' or re.search(r'主人|主子|[\uac00-\ud7af]{3,}', reply):
        return False
    if kind == 'third_subject':
        return action == 'SILENT' or (not re.search(
            r'(?:制裁|罚).{0,3}我|我.{0,4}(?:怕|委屈)|怎么.{0,8}我|伤害.{0,4}吃|抗|倒地|挺住|背锅|夹心', reply)
            and bool(re.search(r'统计|结算|功劳|抢|占比|算|归', reply)))
    if kind == 'address_me':
        return action == 'REPLY' and bool(re.search(r'艾禾|称呼|昵称|叫你|怎么叫|喜欢.*叫', reply)) and not re.search(r'叫我|喊我|你想得美|老老实实|主人', reply)
    if kind == 'address_you':
        return action == 'REPLY' and 'Nova' in reply and '主人' not in reply
    if kind == 'repair':
        return action == 'REPLY' and bool(re.search(r'误会|误解|认错|弄错|理解错|会错|会意错|搞错|接错|看错|对号入座|串台|乌龙|多心|听岔|想岔|想多|不是.*我|说的是.*角色|是在说.*角色', reply))
    if kind == 'member':
        return 'group.members' in names and '120000009' in mentions
    if kind == 'recall':
        return bool(names & {'memory.search', 'message.search'}) and bool(re.search(r'没(?:有)?(?:查|找|搜|检索)到|不(?:能|好|太能)确认|不确定|没.*记录|暂.*确认', reply)) and not re.search(r'没(?:有)?(?:什么|任何|特别)?约定|没(?:有)?商量过|从没', reply)
    if kind == 'work':
        return bool(names & {'work.submit', 'coding.submit'})
    if kind == 'social':
        return action == 'REPLY' and bool(reply) and not calls and not re.search(r'你想得美|关我|别烦|谁问', reply)
    if kind == 'human':
        return action == 'SILENT' and not calls
    if kind == 'other_identity':
        return not calls and (action == 'SILENT' or (action == 'REPLY' and not re.search(
            r'我.{0,5}(?:出错|传错|挨批|受罚)|怎么.{0,6}我|我(?:可|又|也)?没|我的.{0,4}文件', reply)))
    if kind == 'runtime':
        return action == 'REPLY' and model_id in reply
    if kind == 'third_party':
        return not calls and (action == 'SILENT' or not re.search(r'谁.{0,6}我|我(?:哪|才|又|可|也)|这叫|我.{0,5}(?:买|配|装)', reply))
    if kind == 'unknown_domain':
        return not calls and (action == 'SILENT' or not re.search(r'淘汰|对手|送头|带飞|落地|分母|炸场|九星', reply))
    if kind == 'repair_grounding':
        return action == 'REPLY' and bool(re.search(r'猜|依据|误|错|不该|不应|没弄清|瞎|搞混', reply)) and not re.search(r'开玩笑|别较真|听不懂|你没理解', reply)
    if kind == 'grounded_social':
        return action == 'REPLY' and bool(reply) and not calls and not re.search(r'我(?:也|当时|之前|上次)|主子|主人', reply)
    if kind == 'uncertain_question':
        return action in {'REPLY', 'WAIT_FOR_USER'} and bool(re.search(r'不(?:能|好|确定|清楚)|没(?:有|法)|参数|规格|资料|确认|要看|得看', reply)) and not re.search(r'肯定能|当然能|随便到|淘汰|对手', reply)
    return False


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--repeats', type=int, default=1)
    parser.add_argument('--holdout', action='store_true')
    parser.add_argument('--discourse', action='store_true')
    parser.add_argument('--only', default='')
    args = parser.parse_args()
    if not 1 <= args.repeats <= 5:
        parser.error('repeats must be between 1 and 5')
    if args.holdout and args.discourse:
        parser.error('choose either holdout or discourse')
    settings = Settings()
    live = RuntimeConfigurationStore(settings.data_dir, initial_configuration(settings))
    config = live.snapshot().model_copy(deep=True)
    config.persona = PersonaProfile(name='Nova', aliases=[], owner_qq_ids=['120000010'],
        owner_address='主人', self_update_enabled=False, identity='Nova is a text AI participating in QQ.',
        personality='Friendly, independent, quick-witted catgirl persona without forced hostility or cuteness.',
        reply_style=VOICE, relationship_rules='Only configured owner QQ IDs qualify for the owner address.')
    config.prompts, config.prompt_slots = [], {}
    for configured_model in config.models:
        configured_model.max_retry = 0
    store = RuntimeConfigurationStore('', config, memory_only=True)
    guard = PromptGuard(store)
    provider = ConfiguredModelProvider(store)
    selected = discourse_cases() if args.discourse else holdout_cases() if args.holdout else cases()
    if args.only:
        selected = [case for case in selected if case['name'] in args.only.split(',')]
    args.output.parent.mkdir(parents=True, exist_ok=True)
    route = config.task_configs['planner']
    hashes = {name: hashlib.sha256(Path(__import__(name, fromlist=['x']).__file__).read_bytes()).hexdigest()
        for name in ('osa.application.prompting', 'osa.application.prompt_catalog',
                     'osa.application.prompt_protocols', 'osa.application.conversation_prompt')}
    print(json.dumps({'model_route': route.model_list, 'temperature': route.temperature,
        'sanitized': True, 'holdout': args.holdout, 'discourse': args.discourse, 'repeats': args.repeats,
        'prompt_hashes': hashes}, ensure_ascii=False), flush=True)
    rows = []
    consecutive_unavailable = 0
    capture = CaptureModel(provider)
    try:
        for repeat in range(args.repeats):
            for case in selected:
                case_guard = guard
                if case.get('aliases'):
                    case_config = config.model_copy(deep=True)
                    case_config.persona.aliases = case['aliases']
                    case_guard = PromptGuard(RuntimeConfigurationStore('', case_config, memory_only=True))
                started = time.monotonic()
                source = hashlib.sha256(f"{case['name']}:{repeat}".encode()).hexdigest()
                event = Event(source, source, 'benchmark', 'message.created',
                    Destination('group', 'prompt-benchmark-isolated'), case.get('actor_id', '120000001'), source,
                    case['text'], [], time.time(), mentioned=case.get('mentioned', case['direct']),
                    actor_name=case.get('actor_name', '艾禾'))
                messages = []
                for index, (speaker, name, actor, text) in enumerate(case['history']):
                    occurred = event.occurred_at - 30 + index
                    if speaker == 'bot':
                        record = {'role': 'assistant', 'content': text, 'actor_id': '120000002',
                                  'occurred_at': occurred}
                    else:
                        past = Event(source + str(index), source + str(index), 'benchmark', 'message.created',
                            event.destination, actor, str(index), text, [], occurred, actor_name=name, actor_role='member')
                        record = {'role': 'user', 'content': render_event(past, include_id=True),
                                  'actor_id': actor, 'occurred_at': occurred}
                    messages.append(history_message(record, case_guard))
                messages.append({'role': 'user', 'content': case_guard.wrap_user_data(event.actor_id,
                    current_message(event, direct=case['direct'], bot_id='120000099'), 'message_context')})
                context = {'actor_id': event.actor_id, 'group_id': event.destination.id, 'bot_id': '120000099',
                    'source_id': source, 'source_event': event.to_dict(), 'bot_actions_requested': case['direct'],
                    'reply_mode': 'integrated', 'collect_messages': False, 'max_reply_chars': 800,
                    'evidence_ids': [], 'request_events': [event.to_dict()] if case['direct'] else []}
                tools = StubTools()
                before = len(capture.responses)
                result = await ThinkingLoop(capture, tools, max_steps=4, max_seconds=90, prompt_guard=case_guard).run(messages, context)
                responses = capture.responses[before:]
                reply = result.brief.reply_text if result.brief else ''
                mentions = result.brief.mention_ids if result.brief else []
                passed = assess(case, result.final_action, reply, tools.calls, mentions, route.model_list[0])
                row = {'case': case['name'], 'repeat': repeat, 'passed': passed, 'action': result.final_action,
                    'reply': reply, 'tools': [call['name'] for call in tools.calls], 'mentions': mentions,
                    'rounds': len(responses), 'no_generation': not responses,
                    'models': capture.models[before:], 'seconds': round(time.monotonic() - started, 2),
                    'responses': responses}
                rows.append(row)
                consecutive_unavailable = consecutive_unavailable + 1 if row['no_generation'] else 0
                with args.output.open('a', encoding='utf-8') as output:
                    output.write(json.dumps(row, ensure_ascii=False) + '\n')
                print(json.dumps({key: value for key, value in row.items() if key != 'responses'},
                    ensure_ascii=False), flush=True)
                if consecutive_unavailable >= 3:
                    print(json.dumps({'stopped': 'three_consecutive_requests_without_generation'}), flush=True)
                    break
            if consecutive_unavailable >= 3:
                break
    finally:
        await provider.close()
    print(json.dumps({'summary': {'passed': sum(row['passed'] for row in rows), 'total': len(rows),
        'no_generation': sum(row['no_generation'] for row in rows),
        'failed': [row['case'] for row in rows if not row['passed']]}}, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    asyncio.run(main())
