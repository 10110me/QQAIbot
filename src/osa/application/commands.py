import hashlib

from osa.domain.entities import Event, Memory, Scope
from osa.domain.ports import Store
from osa.domain.work import clean_text

HELP = """记忆命令：
/记忆 记住 内容（当前群或私聊范围）
/记忆 开启跨群（仅开启本人检索）
/记忆 允许本群（明确允许在当前群披露自己的共享记忆）
/记忆 共享 内容（保存本人跨群事实，需先开启跨群）
/记忆 查看
/记忆 删除 ID
/记忆 更正 ID 新内容
/记忆 撤销跨群
跨群检索还需目的群管理员开启 cross_memory。私聊记忆不会自动进入群。"""


class MemoryCommands:
    def __init__(self, store: Store) -> None:
        self.store = store

    async def handle(self, event: Event) -> str | None:
        if event.text == "/隐私":
            return "仅处理已授权群和已启用私聊。跨群个人记忆默认关闭；群资料仅在原群使用。用 /记忆 查看、删除或撤销跨群管理自己的记忆。原始聊天保留期限由部署者管理；删除记忆不会撤回已发到 QQ 的内容。"
        if not event.text.startswith("/记忆"):
            return None
        command = event.text.removeprefix("/记忆").strip()
        if command in {"开启向量", "撤销向量"}:
            return None  # Full-feature service controls external embedding consent.
        consent = await self.store.consent(event.actor_id)
        if command == "开启跨群":
            await self.store.set_consent(event.actor_id, True)
            return "已开启本人跨群检索。尚未授权任何群公开披露；如需要，请在目标群明确执行 /记忆 允许本群。"
        if command == "允许本群":
            if event.destination.kind != "group":
                return "请在你希望允许披露共享记忆的目标群执行此命令。"
            if not consent.enabled:
                return "请先执行 /记忆 开启跨群。"
            await self.store.set_consent(event.actor_id, True, event.destination.id)
            return "已允许在当前群披露你主动保存的跨群个人记忆；群管理员仍需开启 cross_memory。"
        if command == "撤销跨群":
            await self.store.set_consent(event.actor_id, False)
            return "已撤销跨群检索和群披露授权，新检索立即生效。"
        if command == "查看":
            memories = await self.store.visible_memories(event.actor_id, event.destination, "", 100)
            memories = [memory for memory in memories if memory.subject_id == event.actor_id]
            return "\n".join(f"{memory.id} [{memory.scope}] {memory.content}" for memory in memories) or "当前可见范围内没有你的记忆。"
        if command.startswith("删除 "):
            memory_id = command.removeprefix("删除 ").strip()
            removed = await self.store.delete_memory(memory_id, event.actor_id)
            return "已删除记忆并清理相关回放快照。" if removed else "未找到你有权删除的记忆。"
        if command.startswith("更正 "):
            parts = command.split(maxsplit=2)
            if len(parts) != 3:
                return "格式：/记忆 更正 ID 新内容"
            if clean_text(parts[2]) != parts[2]:return "不能将凭据或密钥保存为长期记忆。"
            updated = await self.store.correct_memory(parts[1], event.actor_id, parts[2][:2000], event.id)
            return "已更正并更新来源，旧候选回复已取消。" if updated else "未找到你有权更正的记忆。"
        if command.startswith(("记住 ", "共享 ")):
            shared = command.startswith("共享 ")
            if shared and not consent.enabled:
                return "请先执行 /记忆 开启跨群；普通记忆不会自动转成跨群记忆。"
            if not (await self.store.policy(event.destination)).collect_messages:
                return "当前群关闭了消息保存，无法创建可追溯的记忆。"
            content = command.split(" ", 1)[1].strip()[:2000]
            if clean_text(content) != content:return "不能将凭据或密钥保存为长期记忆。"
            if not content:
                return "请提供要保存的内容。"
            scope = Scope.PERSONAL_OWNER if shared else (Scope.LONG_GROUP if event.destination.kind == "group" else Scope.PRIVATE_USER)
            memory_id = hashlib.sha256(f"memory:{event.id}".encode()).hexdigest()[:32]
            saved = await self.store.add_memory(Memory(memory_id, content, scope, event.actor_id,
                event.destination.id if event.destination.kind == "group" else None, event.id))
            return f"{'已记住' if saved.id == memory_id else '已有相同记忆'}，范围：{scope}，ID：{saved.id}。"
        return HELP
