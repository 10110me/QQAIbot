import asyncio
import re


def moderation_arguments(arguments):
    targets = arguments.get("target_ids", arguments.get("mentions"))
    if not isinstance(targets, list) or not targets or len(targets) > 50:
        raise ValueError("请提供 1 至 50 个目标 QQ 号（target_ids 或 mentions 数组）")
    if any(isinstance(target, bool) or not str(target).isascii()
           or not str(target).isdecimal() or int(str(target)) <= 0 for target in targets):
        raise ValueError("目标必须是有效的数字 QQ 号，不能使用昵称或全体成员")
    targets = list(dict.fromkeys(str(int(str(target))) for target in targets))
    message = str(arguments.get("message") or "").strip()
    operation = arguments.get("action")
    command = re.match(r"^(/解除禁言|/unmute|/禁言|/mute)(?=\s|\[|$)", message)
    if operation is None:
        if not command:
            raise ValueError("外部禁言请使用 action=mute/unmute；支持批量普通禁言和解禁")
        operation = "unmute" if command[1] in {"/解除禁言", "/unmute"} else "mute"
    if operation not in {"mute", "unmute"}:
        raise ValueError("action 只能是 mute 或 unmute")
    duration = arguments.get("duration_seconds")
    if duration is None:
        remaining = message[command.end():] if command else ""
        remaining = re.sub(r"\[CQ:at,[^\]]+\]|@(?:QQ)?\d+", " ", remaining)
        parts = [part for part in remaining.split() if part not in targets]
        if parts and (len(parts) != 1 or not parts[0].isascii() or not parts[0].isdecimal()):
            raise ValueError("禁言时长格式不清楚，请填写 duration_seconds（秒）")
        duration = int(parts[0]) * 60 if parts else 60
    if isinstance(duration, bool) or not isinstance(duration, int):
        raise ValueError("duration_seconds 必须是整数秒")
    if operation == "unmute":
        duration = 0
    elif not 1 <= duration <= 30 * 86400:
        raise ValueError("禁言时长必须在 1 秒到 30 天之间")
    return {"action": operation, "target_ids": targets, "duration_seconds": duration}


async def call_moderation(features, store, destination, actor, context, arguments):
    try:
        normalized = moderation_arguments(arguments)
    except ValueError as error:
        return {"ok": False, "error": "INVALID_MODERATION_ARGUMENTS", "detail": str(error)}
    platform = getattr(features, "platform", None)
    if platform is None or not getattr(platform, "ready", False):
        return {"ok": False, "error": "BOT_OFFLINE", "detail": "机器人未连接，未执行禁言"}
    group = destination.id
    try:
        login, members = await asyncio.gather(
            platform.call("get_login_info", {}),
            platform.call("get_group_member_list", {"group_id": int(group), "no_cache": True}))
    except Exception:
        return {"ok": False, "error": "MEMBER_CHECK_FAILED", "detail": "无法核实群成员和机器人权限，未执行禁言"}
    bot_id = str(login.get("user_id") or "") if isinstance(login, dict) else ""
    if not bot_id or not isinstance(members, list) or any(
            not isinstance(member, dict) or not member.get("user_id") for member in members):
        return {"ok": False, "error": "MEMBER_CHECK_FAILED", "detail": "群成员或机器人身份查询结果无效，未执行禁言"}
    by_id = {str(member["user_id"]): member for member in members}
    bot_role = by_id.get(bot_id, {}).get("role")
    if bot_role not in {"admin", "owner"}:
        return {"ok": False, "error": "BOT_PERMISSION_DENIED", "detail": "实际执行禁言的机器人不在群中或没有管理员/群主权限"}
    if actor not in by_id:
        return {"ok": False, "error": "REQUESTER_NOT_IN_GROUP", "detail": "请求人不在当前群中，未执行禁言"}
    results, eligible = [], []
    for target in normalized["target_ids"]:
        role = by_id.get(target, {}).get("role")
        if target not in by_id:
            error, detail = "NOT_IN_GROUP", "该 QQ 号不在当前群中（无法确认账号是否存在）"
        elif target == bot_id:
            error, detail = "SELF_TARGET", "不能禁言机器人自己"
        elif role not in {"member", "admin", "owner"}:
            error, detail = "UNKNOWN_ROLE", "无法确认目标的群权限"
        elif role == "owner" or (bot_role == "admin" and role == "admin"):
            error, detail = "TARGET_PERMISSION_DENIED", "不能操作群主或同级管理员"
        else:
            eligible.append(target)
            continue
        results.append({"user_id": target, "status": "skipped", "error": error, "detail": detail})
    if eligible:
        response = await features.qqbot_tools.call("moderation.mute", {**normalized, "target_ids": eligible},
            user_id=actor, group_id=group, request_id=context["source_id"])
        actions = response.get("actions") if isinstance(response.get("actions"), list) else []
        checks = {str(item.get("user_id")): item for item in response.get("results", []) if isinstance(item, dict)}
        planned = {}
        for action in actions:
            if not isinstance(action, dict) or action.get("type") != "set_group_ban":
                continue
            target = str(action.get("user_id"))
            if (target in eligible and target not in planned and str(action.get("group_id")) == group
                    and type(action.get("duration")) is int and action["duration"] == normalized["duration_seconds"]):
                planned[target] = action
        for target in eligible:
            if target not in planned:
                check = checks.get(target, {})
                results.append({"user_id": target, "status": "skipped", "error": check.get("error", "NO_AUTHORIZED_ACTION"),
                                "detail": str(check.get("detail") or "qqbot 未授权此目标，未执行")[:300]})
                continue
            current = await store.policy(destination)
            if not current.enabled or await store.paused() or not await store.event_active(context["source_id"]):
                results.append({"user_id": target, "status": "skipped", "error": "POLICY_OR_SOURCE_REVOKED",
                                "detail": "请求或群授权已失效，未执行"})
                continue
            try:
                await platform.call("set_group_ban", {"group_id": int(group), "user_id": int(target),
                                                       "duration": normalized["duration_seconds"]})
            except Exception as error:
                from osa.domain.entities import DeliveryUnknown
                unknown = isinstance(error, (DeliveryUnknown, TimeoutError))
                results.append({"user_id": target, "status": "unknown" if unknown else "failed",
                                "error": "DELIVERY_UNKNOWN" if unknown else "BAN_REJECTED",
                                "detail": "平台未确认结果，请核实后再操作，不要盲目重试" if unknown else "平台拒绝操作，目标可能退群或权限已变化"})
            else:
                results.append({"user_id": target, "status": "applied", "duration_seconds": normalized["duration_seconds"]})
    results.sort(key=lambda item: normalized["target_ids"].index(item["user_id"]))
    applied = sum(item["status"] == "applied" for item in results)
    result = {"ok": applied == len(results), "partial": 0 < applied < len(results), "tool": "moderation.mute",
              "action": normalized["action"], "bot_role": bot_role, "results": results,
              "summary": {"requested": len(results), "applied": applied, "not_confirmed": len(results) - applied},
              "note": "只有 applied 表示平台确认执行；逐人说明未执行或结果未知的原因，不能宣称全部成功"}
    if not result["ok"]:
        result.update(error="BATCH_PARTIAL_FAILURE" if applied else "BATCH_NOT_APPLIED", retryable=False)
    return result
