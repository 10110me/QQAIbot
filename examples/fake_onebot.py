"""Connect a synthetic QQ peer; no real account or paid model required."""
import asyncio
import json
import os
import time

from dotenv import load_dotenv
from websockets.asyncio.client import connect


async def main() -> None:
    load_dotenv()
    bot_id = os.environ.get("OSA_BOT_ID", "99999")
    groups = json.loads(os.environ.get("OSA_ALLOWED_GROUPS", '["10001"]'))
    if not groups:
        raise SystemExit("Configure at least one allowed demo group")
    async with connect("ws://127.0.0.1:8080/onebot/v11/ws", additional_headers={
        "Authorization": "Bearer " + os.environ["OSA_NAPCAT_TOKEN"], "X-Self-ID": bot_id,
    }) as websocket:
        event = {"time": int(time.time()), "self_id": int(bot_id), "post_type": "message",
                 "message_type": "group", "group_id": int(groups[0]), "user_id": 12345,
                 "message_id": 1, "message": [
                     {"type": "at", "data": {"qq": bot_id}},
                     {"type": "text", "data": {"text": "你好，这是模拟消息"}},
                 ]}
        # At-least-once transport deliberately repeats the same event.
        await websocket.send(json.dumps(event))
        await websocket.send(json.dumps(event))
        print("Sent a duplicated synthetic event; expected one bot reply.")
        async for raw in websocket:
            request = json.loads(raw)
            if request.get("action") in {"send_group_msg", "send_private_msg"}:
                print("Bot reply:", request["params"]["message"])
                await websocket.send(json.dumps({"status": "ok", "retcode": 0,
                    "echo": request["echo"], "data": {"message_id": 9001}}))
                print("Acknowledged synthetic QQ message 9001. Ctrl+C to disconnect.")
            elif request.get("action") == "_get_group_notice":
                await websocket.send(json.dumps({"status": "ok", "retcode": 0, "echo": request["echo"],
                    "data": [{"notice_id": "demo", "message": {"text": "演示群规：友好交流。"}}]}))
            else:
                await websocket.send(json.dumps({"status": "ok", "retcode": 0,
                    "echo": request.get("echo"), "data": {"files": [], "folders": []}}))


if __name__ == "__main__":
    asyncio.run(main())
