import base64
import re
import time


async def prepare_reference_images(features, event, arguments, visible_source_ids=()):
    requested = arguments.get("image_source_ids", [])
    asset_ids = arguments.get("image_asset_ids", [])
    if not isinstance(requested, list) or not isinstance(asset_ids, list):
        raise ValueError("image_source_ids 和 image_asset_ids 必须是列表")
    if len(requested) + len(asset_ids) > 4:
        raise ValueError("最多引用四张图片")
    if any(not isinstance(value, str) for value in [*requested, *asset_ids]):
        raise ValueError("图片来源必须是实际返回的 ID")
    if not set(requested).issubset({event.id, *visible_source_ids}):
        raise PermissionError("原图来源不在当前可见消息中")
    edited = bool(requested or asset_ids or arguments.get("use_reference_images") or re.search(
        r"改图|图生图|参考.*图|(?:这|那|图片|照片|图里|图中).*(?:胖|瘦|改|调整)|(?:改|调整).*(?:图|照片)|这张|这图片", event.text))
    source_ids = []
    attachments = [] if asset_ids else await features.images.attachments(event, requested)
    if attachments:
        source_ids.extend(requested or [event.id])
        if event.reply_to_id:
            quoted = await features.store.event_by_message(event.destination, event.reply_to_id)
            if quoted:
                source_ids.append(quoted.id)
    if not attachments and not requested:
        rows = await features.repo.find("qqbot_image_result", group_id=event.destination.id,
                                        status="ready", limit=100)
        rows = [row for row in rows if row.payload.get("expires_at", 0) > time.time()]
        if event.reply_to_id and not asset_ids:
            sent = await features.store.messages(event.destination.key, None, "sent", None, 100)
            quoted = next((item for item in sent if str(item.get("platform_message_id")) == event.reply_to_id), None)
            if quoted:
                asset_ids = list(quoted.get("attachment_ids", []))
            else:
                rows = []
        if asset_ids:
            selected = [row for row in rows if row.payload.get("asset_id") in asset_ids]
            if len(selected) != len(set(asset_ids)):
                raise PermissionError("引用的生成图片不在本群可用结果中")
        elif edited:
            selected = [row for row in rows if row.owner_id == event.actor_id][:1]
        else:
            selected = []
        for row in selected:
            source_id = row.payload["source_id"]
            if not await features.store.event_active(source_id):
                raise PermissionError("生成图片的原请求已撤回")
            asset = await features.repo.get(row.payload["asset_id"])
            if not asset or asset.status != "active" or asset.group_id != event.destination.id:
                raise PermissionError("原图已失效或不属于当前群")
            raw = await features.objects.get(asset.payload["object_key"])
            attachments.append({"mime_type": asset.payload["mime"],
                                "data_base64": base64.b64encode(raw).decode("ascii")})
            source_ids.append(source_id)
    if (edited or requested or asset_ids or event.reply_to_id
            or any(segment.get("type") == "image" for segment in event.segments)) and not attachments:
        raise ValueError("无法取得要修改的原图，请引用目标图片或重新发送图片；本次未调用文生图")
    images = []
    for item in attachments[:4]:
        mime = str(item.get("mime_type", ""))
        if not mime.startswith("image/"):
            raise ValueError("参考附件不是图片")
        encoded = item["data_base64"]
        if len(encoded) > 28_000_000:
            raise ValueError("参考图片过大，请重新发送较小原图")
        images.append({"mime_type": mime, "data_base64": encoded})
    return images, list(dict.fromkeys(source_ids))
