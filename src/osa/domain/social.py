import re

from osa.domain.entities import Action, Decision, Event, GroupPolicy
from osa.domain.message import at_targets


class SocialDecisionEngine:
    """Conservative M1 strategy; unsolicited participation stays disabled."""

    def decide(self, event: Event, policy: GroupPolicy, reply_to_bot: bool, called_by_name: bool = False) -> Decision:
        if not policy.enabled:
            return Decision(Action.IGNORE, "conversation_disabled")
        if event.event_type != "message.created":
            return Decision(Action.IGNORE, "non_message_event")
        if event.destination.kind == "private":
            return Decision(Action.REPLY, "private_request")
        # An @ segment is an explicit addressing signal.  If it points at a
        # different member, never let name matching or initiative turn it into
        # a bot reply.  A message that also @s the bot remains direct below.
        targets = at_targets(event)
        if targets and not event.mentioned:
            return Decision(Action.IGNORE, "mention_other_user")
        if event.text.startswith(("/工作 ", "/研究 ")):
            return Decision(Action.REPLY, "explicit_task_request")
        if event.mentioned:
            return Decision(Action.REPLY, "direct_mention")
        if reply_to_bot:
            return Decision(Action.REPLY, "reply_to_bot")
        if called_by_name:
            return Decision(Action.REPLY, "name_call")
        if event.reply_to_id:
            return Decision(Action.IGNORE, "reply_to_other_member")
        if re.search(r"你(?!们)|您", event.text):
            return Decision(Action.IGNORE, "ambiguous_second_person")
        return Decision(Action.IGNORE, "humans_conversation")
