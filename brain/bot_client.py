"""
Bot Client：向 Node.js HTTP Server 发送技能调用，接收 WebSocket 状态
"""
import asyncio
import json
import aiohttp
import websockets
from shared_state import state

BOT_API = "http://127.0.0.1:3000"
BOT_WS  = "ws://127.0.0.1:3000/state"


async def call_skill(name: str, args: dict = None) -> dict:
    """
    同步调用一个技能，等待执行完成。
    返回: {"status": "success"|"failed", "output": ..., "reason": ...}
    """
    payload = {"name": name, "args": args or {}}
    async with aiohttp.ClientSession() as session:
        async with session.post(f"{BOT_API}/skill", json=payload, timeout=aiohttp.ClientTimeout(total=120)) as resp:
            return await resp.json()


async def ping() -> bool:
    """检查 Node.js bot 是否在线"""
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{BOT_API}/ping", timeout=aiohttp.ClientTimeout(total=3)) as resp:
                data = await resp.json()
                return data.get("ok", False)
    except Exception:
        return False


async def state_listener():
    """持续接收 Node.js 推送的游戏状态，更新 shared_state"""
    while True:
        try:
            async with websockets.connect(BOT_WS) as ws:
                print("[WS] 已连接到 Bot Server")
                async for message in ws:
                    data = json.loads(message)
                    state.update_from_ws(data)
        except Exception as e:
            print(f"[WS] 断开，2秒后重连: {e}")
            await asyncio.sleep(2)
