"""
Shared State：两个大脑共享的游戏状态
由 WebSocket 接收 Node.js 推送后持续更新
"""
from dataclasses import dataclass, field
from typing import Optional
import threading


@dataclass
class SharedState:
    # ── 游戏数据（Node.js 每 500ms 更新） ──
    health: float = 20.0
    food: float = 20.0
    position: dict = field(default_factory=dict)
    time_of_day: int = 0
    dimension: str = "overworld"
    inventory: dict = field(default_factory=dict)       # name → count
    nearby_entities: list = field(default_factory=list)  # [{type, distance, health}]
    nearby_blocks: list = field(default_factory=list)    # [{type, distance}]

    # ── 任务协调 ──
    current_goal: str = "survive and build a shelter"
    pending_command: Optional[str] = None  # 观众发来的游戏命令，等当前技能完成后切换
    last_failure: Optional[str] = None

    # ── 事件队列（Node.js 推送，Interaction Brain 消费） ──
    _events: list = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def update_from_ws(self, data: dict):
        """接收 Node.js WebSocket 推送的状态数据"""
        self.health = data.get("health", self.health)
        self.food = data.get("food", self.food)
        self.position = data.get("position", self.position)
        self.time_of_day = data.get("time_of_day", self.time_of_day)
        self.dimension = data.get("dimension", self.dimension)
        self.inventory = data.get("inventory", self.inventory)
        self.nearby_entities = data.get("nearby_entities", self.nearby_entities)
        self.nearby_blocks = data.get("nearby_blocks", self.nearby_blocks)
        # 游戏事件放入队列
        for event in data.get("events", []):
            with self._lock:
                self._events.append(event)

    def pop_events(self) -> list:
        """取出并清空事件队列"""
        with self._lock:
            events, self._events = self._events, []
        return events

    def push_event(self, event_type: str, data: dict = None):
        """Python 侧主动推入事件（如任务完成）"""
        with self._lock:
            self._events.append({"type": event_type, "data": data or {}})

    def to_llm_context(self) -> str:
        """序列化为 LLM Planner 的输入上下文（只留规划需要的信息）"""
        inv_str = ", ".join(f"{k} x{v}" for k, v in self.inventory.items() if v > 0) or "empty"

        threats = [e for e in self.nearby_entities
                   if e["type"] in HOSTILE_MOBS and e["distance"] < 16]
        threats_str = ", ".join(f"{e['type']}({e['distance']}m)" for e in threats) or "none"

        resources_str = ", ".join(
            f"{b['type']}({b['distance']}m)" for b in self.nearby_blocks
        ) or "none"

        time_str = "day" if self.time_of_day < 13000 else "night"

        pos = self.position
        pos_str = f"x={pos.get('x',0)}, y={pos.get('y',0)}, z={pos.get('z',0)}"

        return (
            f"goal: {self.current_goal}\n"
            f"position: {pos_str}\n"
            f"health: {self.health}/20, food: {self.food}/20, time: {time_str}\n"
            f"inventory: {inv_str}\n"
            f"threats_nearby: {threats_str}\n"
            f"resources_nearby: {resources_str}\n"
            f"last_failure: {self.last_failure or 'none'}"
        )


HOSTILE_MOBS = {
    "zombie", "skeleton", "creeper", "spider", "witch", "enderman",
    "blaze", "ghast", "slime", "phantom", "drowned", "husk", "stray", "pillager"
}

# 全局单例
state = SharedState()
