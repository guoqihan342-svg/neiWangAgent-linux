"""
graph.py — LangGraph 风格的状态图引擎 v0.3.0

借鉴 LangGraph 核心设计思想，用纯 Python 实现状态图抽象：
- add_node(name, func): 注册处理节点，func 接收 State dict 返回 State dict
- add_edge(src, dst): 无条件跳转
- add_conditional_edges(src, router, route_map): 条件分支，router(state) → key → route_map[key] → 下一节点
- compile(): 编译为可执行状态机，支持 checkpoint 断点续跑

与旧版 orchestrator 20+ 个硬编码状态的对比：
  旧: if state == State.X: ... elif state == State.Y: ... （线性，不可复用）
  新: graph.add_node(...).add_edge(...).compile().invoke(initial_state)

设计参考：
  - LangGraph: https://github.com/langchain-ai/langgraph
  - 状态图模式: State Pattern + Directed Acyclic Graph
  - 节点函数: 纯函数，无副作用（MCP 调用通过 state 中的 client 传递）

版本历史：
  v0.3.0 — 初版，支持 add_node/add_edge/add_conditional_edges/compile/invoke/resume
"""

from __future__ import annotations

import json
import logging
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple, Union

logger = logging.getLogger(__name__)


# =============================================================================
# 类型定义
# =============================================================================

# 状态节点函数: 接收 State dict，返回 State dict
NodeFunc = Callable[[Dict[str, Any]], Dict[str, Any]]

# 条件路由函数: 接收 State dict，返回路由 key（字符串）
RouterFunc = Callable[[Dict[str, Any]], str]

# 路由映射: {路由key: 下一节点名称}
RouteMap = Dict[str, str]


# =============================================================================
# StateGraph — 状态图核心
# =============================================================================

class StateGraph:
    """
    LangGraph 风格的状态图引擎。

    使用方式:
        graph = StateGraph()
        graph.add_node("start", start_handler)
        graph.add_node("process", process_handler)
        graph.add_node("done", done_handler)
        graph.add_edge("start", "process")
        graph.add_conditional_edges("process", router, {"ok": "done", "retry": "process"})
        graph.set_entry_point("start")
        graph.set_finish_point("done")
        
        compiled = graph.compile()
        final_state = compiled.invoke({"input": "hello"})
    
    特性:
        - 支持断点续跑 (checkpoint): 每个节点执行后自动保存状态快照
        - resume(state): 从中断处继续执行
        - 执行历史: 记录每个节点的时间戳和耗时
        - 循环检测: 编译时检测死循环风险
    """

    def __init__(self, name: str = "StateGraph"):
        """
        Args:
            name: 图名称，用于日志和序列化标识
        """
        self.name = name
        # 图结构
        self._nodes: Dict[str, NodeFunc] = {}  # {节点名: 处理函数}
        self._edges: Set[Tuple[str, str]] = set()  # {(src, dst), ...}
        self._conditional_edges: Dict[str, Tuple[RouterFunc, RouteMap]] = {}  # {src: (router, route_map)}
        # 入口/出口
        self._entry_point: Optional[str] = None
        self._finish_point: Optional[str] = None
        # 元数据
        self._node_metadata: Dict[str, Dict[str, Any]] = {}  # 节点描述等

    # ---- 构建 API ----

    def add_node(
        self,
        name: str,
        func: NodeFunc,
        description: str = "",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> "StateGraph":
        """
        注册一个状态节点。

        Args:
            name: 节点唯一名称
            func: 节点处理函数，签名为 (state: dict) -> dict
            description: 节点描述（用于日志/调试）
            metadata: 额外元数据

        Returns:
            self，支持链式调用

        Raises:
            ValueError: 节点名已存在
        """
        if name in self._nodes:
            raise ValueError(f"节点 '{name}' 已注册，请使用不同名称")
        
        self._nodes[name] = func
        self._node_metadata[name] = {
            "description": description,
            **(metadata or {}),
        }
        logger.debug(f"[{self.name}] 注册节点: {name} — {description}")
        return self

    def add_edge(self, src: str, dst: str) -> "StateGraph":
        """
        添加无条件跳转边。

        Args:
            src: 源节点名
            dst: 目标节点名

        Returns:
            self，支持链式调用
        """
        if src not in self._nodes:
            raise ValueError(f"源节点 '{src}' 未注册，请先 add_node")
        if dst not in self._nodes:
            raise ValueError(f"目标节点 '{dst}' 未注册，请先 add_node")
        
        self._edges.add((src, dst))
        logger.debug(f"[{self.name}] 添加边: {src} → {dst}")
        return self

    def add_conditional_edges(
        self,
        src: str,
        router: RouterFunc,
        route_map: RouteMap,
    ) -> "StateGraph":
        """
        添加条件分支边。执行 src 节点后，调用 router(state) 获取路由 key，
        然后根据 route_map 决定下一节点。

        Args:
            src: 源节点名
            router: 路由函数，接收 state dict，返回路由 key 字符串
            route_map: 路由映射表，{路由key: 下一节点名}

        Returns:
            self，支持链式调用

        示例:
            def router(state):
                if state.get("success"):
                    return "ok"
                return "retry"
            
            graph.add_conditional_edges("run", router, {"ok": "done", "retry": "run"})
        """
        if src not in self._nodes:
            raise ValueError(f"源节点 '{src}' 未注册，请先 add_node")
        
        # 验证 route_map 中的目标节点存在
        for route_key, dst in route_map.items():
            if dst not in self._nodes:
                raise ValueError(
                    f"条件边 '{src}' → ({route_key}) → '{dst}' 中目标节点 '{dst}' 未注册"
                )
        
        self._conditional_edges[src] = (router, route_map)
        logger.debug(
            f"[{self.name}] 添加条件边: {src} → [{', '.join(route_map.keys())}]"
        )
        return self

    def set_entry_point(self, name: str) -> "StateGraph":
        """
        设置入口节点（起始状态）。

        Args:
            name: 入口节点名

        Raises:
            ValueError: 节点未注册
        """
        if name not in self._nodes:
            raise ValueError(f"入口节点 '{name}' 未注册")
        self._entry_point = name
        return self

    def set_finish_point(self, name: str) -> "StateGraph":
        """
        设置结束节点（到达此节点时停止执行）。

        Args:
            name: 结束节点名

        Raises:
            ValueError: 节点未注册
        """
        if name not in self._nodes:
            raise ValueError(f"结束节点 '{name}' 未注册")
        self._finish_point = name
        return self

    # ---- 编译与执行 ----

    def compile(self) -> "CompiledGraph":
        """
        编译状态图为可执行对象。

        执行校验:
            1. 必须有入口节点
            2. 所有非结束节点必须有出边
            3. 检测孤立节点（既非入口也无入边）

        Returns:
            CompiledGraph 可执行实例
        """
        if self._entry_point is None:
            raise ValueError("必须设置入口节点 (set_entry_point)")
        
        # 收集所有有出边的节点
        nodes_with_outgoing = set()
        nodes_with_outgoing.update(src for src, _ in self._edges)
        nodes_with_outgoing.update(self._conditional_edges.keys())
        
        # 检测孤立节点（非入口、非结束、无入边、无出边）
        nodes_with_incoming = set(dst for _, dst in self._edges)
        for route_map in self._conditional_edges.values():
            nodes_with_incoming.update(route_map[1].values())
        
        for node_name in self._nodes:
            if (
                node_name != self._entry_point
                and node_name != self._finish_point
                and node_name not in nodes_with_incoming
                and node_name not in nodes_with_outgoing
            ):
                logger.warning(f"[{self.name}] 孤立节点: {node_name} — 无入边也无出边")
        
        # 检测循环 — 简单 DFS 检测
        max_iterations = self._detect_potential_loop()
        
        return CompiledGraph(
            graph=self,
            max_iterations=max_iterations,
        )

    def _detect_potential_loop(self) -> int:
        """
        检测图中可能的循环路径，返回建议的最大迭代次数。

        策略: 计算从入口到最远节点的最长简单路径 × 安全系数
        如果没有显式结束节点，使用节点数 × 10 作为上限。

        Returns:
            建议的最大迭代次数
        """
        if self._finish_point and self._finish_point in self._nodes:
            # BFS 找最长路径
            visited: Set[str] = set()
            max_depth = 0
            
            def dfs(node: str, depth: int, path: Set[str]):
                nonlocal max_depth
                if node in path:
                    return  # 检测到环
                if node == self._finish_point:
                    max_depth = max(max_depth, depth)
                    return
                
                path.add(node)
                # 无条件边
                for src, dst in self._edges:
                    if src == node and dst not in path:
                        dfs(dst, depth + 1, path.copy())
                # 条件边
                if node in self._conditional_edges:
                    _, route_map = self._conditional_edges[node]
                    for dst in route_map.values():
                        if dst not in path:
                            dfs(dst, depth + 1, path.copy())
            
            if self._entry_point:
                dfs(self._entry_point, 0, set())
            
            return max(max_depth * 3, 50)  # 安全系数 3x，最少 50 次
        
        # 无结束点：每个节点平均 10 次迭代
        return len(self._nodes) * 10

    def to_dict(self) -> Dict[str, Any]:
        """序列化图结构（不含函数实现）。"""
        return {
            "name": self.name,
            "nodes": list(self._nodes.keys()),
            "edges": [[s, d] for s, d in self._edges],
            "conditional_edges": {
                src: list(route_map.keys())
                for src, (_, route_map) in self._conditional_edges.items()
            },
            "entry_point": self._entry_point,
            "finish_point": self._finish_point,
            "node_metadata": self._node_metadata,
        }


# =============================================================================
# CompiledGraph — 编译后的可执行状态图
# =============================================================================

class CompiledGraph:
    """
    编译后的状态图，可执行 invoke/resume。

    执行流程:
        1. 从 entry_point 开始
        2. 执行当前节点函数 node(state)
        3. 保存 checkpoint（状态快照）
        4. 查找下一条边（无条件边 或 条件边）
        5. 如果到达 finish_point 或超过最大迭代次数 → 停止
        6. 否则跳转到下一节点，重复步骤 2

    Checkpoint 机制:
        每次节点执行后自动保存:
        {
            "current_node": "IMPLEMENT",
            "state_snapshot": {...},
            "history": [...],
            "timestamp": "2026-05-12T10:00:00"
        }
    """

    def __init__(self, graph: StateGraph, max_iterations: int = 100):
        self._graph = graph
        self._max_iterations = max_iterations

    @property
    def name(self) -> str:
        return self._graph.name

    @property
    def nodes(self) -> List[str]:
        return list(self._graph._nodes.keys())

    # ---- 执行 ----

    def invoke(
        self,
        initial_state: Dict[str, Any],
        checkpoint_dir: Optional[Path] = None,
    ) -> Dict[str, Any]:
        """
        执行状态图。

        Args:
            initial_state: 初始状态 dict，必须包含所有节点需要的字段
            checkpoint_dir: checkpoint 保存目录（None 则不保存）

        Returns:
            最终状态 dict

        Raises:
            RuntimeError: 达到最大迭代次数仍未结束
        """
        state = deepcopy(initial_state)
        
        # 初始化执行上下文
        state.setdefault("__graph_name__", self._graph.name)
        state.setdefault("__history__", [])
        state.setdefault("__current_node__", self._graph._entry_point)
        state.setdefault("__iteration__", 0)
        state.setdefault("__finished__", False)
        state.setdefault("__error__", None)

        current = state["__current_node__"]
        if current is None:
            raise RuntimeError("入口节点未设置")

        logger.info(f"[{self._graph.name}] 开始执行，入口: {current}")

        while not state.get("__finished__") and state["__iteration__"] < self._max_iterations:
            state["__iteration__"] += 1
            state["__current_node__"] = current
            iteration = state["__iteration__"]

            # 1) 执行节点
            node_func = self._graph._nodes.get(current)
            if node_func is None:
                raise RuntimeError(f"节点 '{current}' 未注册处理函数")

            desc = self._graph._node_metadata.get(current, {}).get("description", "")
            logger.info(f"[{self._graph.name}] [{iteration}] 执行节点: {current} — {desc}")

            t_start = datetime.now()
            try:
                state = node_func(state)
            except Exception as e:
                logger.exception(f"[{self._graph.name}] 节点 '{current}' 执行失败: {e}")
                state["__error__"] = str(e)
                state["__finished__"] = True
                break
            elapsed = (datetime.now() - t_start).total_seconds()

            # 2) 记录历史
            state["__history__"].append({
                "node": current,
                "iteration": iteration,
                "timestamp": t_start.isoformat(),
                "elapsed_s": round(elapsed, 3),
                "description": desc,
            })

            # 3) 保存 checkpoint
            if checkpoint_dir:
                self._save_checkpoint(checkpoint_dir, state, current)

            # 4) 检查是否到达结束点
            if current == self._graph._finish_point:
                logger.info(f"[{self._graph.name}] 到达结束节点: {current}")
                state["__finished__"] = True
                break

            # 5) 查找下一节点
            next_node = self._resolve_next(current, state)
            if next_node is None:
                logger.info(f"[{self._graph.name}] 节点 '{current}' 无出边，执行结束")
                state["__finished__"] = True
                break

            current = next_node

        # 检查是否超迭代
        if state["__iteration__"] >= self._max_iterations and not state.get("__finished__"):
            msg = (
                f"[{self._graph.name}] 达到最大迭代次数 {self._max_iterations}，"
                f"可能是死循环。最后节点: {current}"
            )
            logger.error(msg)
            state["__error__"] = msg
            state["__finished__"] = True

        logger.info(
            f"[{self._graph.name}] 执行完成: "
            f"{state['__iteration__']} 次迭代, "
            f"最终节点: {current}, "
            f"错误: {state.get('__error__')}"
        )
        return state

    def resume(
        self,
        state: Dict[str, Any],
        checkpoint_dir: Optional[Path] = None,
    ) -> Dict[str, Any]:
        """
        从断点恢复执行。

        与 invoke 的区别: 不清除 __history__ 和 __iteration__，
        从 state["__current_node__"] 继续执行。

        Args:
            state: 包含 checkpoint 信息的状态 dict
            checkpoint_dir: checkpoint 目录

        Returns:
            最终状态 dict
        """
        # 确保状态有执行上下文
        state.setdefault("__history__", [])
        state.setdefault("__iteration__", 0)
        state["__finished__"] = False
        state["__error__"] = None

        current = state.get("__current_node__", self._graph._entry_point)
        if current is None:
            current = self._graph._entry_point

        logger.info(f"[{self._graph.name}] 从节点 '{current}' 恢复执行 (第 {state['__iteration__']} 次迭代)")

        # 继续执行（invoke 会从 current 开始）
        return self.invoke(state, checkpoint_dir=checkpoint_dir)

    # ---- 内部方法 ----

    def _resolve_next(self, current: str, state: Dict[str, Any]) -> Optional[str]:
        """
        解析当前节点的下一节点。

        优先级: 条件边 > 无条件边 > 结束

        Args:
            current: 当前节点名
            state: 当前状态

        Returns:
            下一节点名，None 表示结束
        """
        # 1) 检查条件边
        if current in self._graph._conditional_edges:
            router, route_map = self._graph._conditional_edges[current]
            try:
                route_key = router(state)
            except Exception as e:
                logger.error(f"[{self._graph.name}] 路由函数执行失败: {e}")
                raise

            if route_key in route_map:
                next_node = route_map[route_key]
                logger.debug(
                    f"[{self._graph.name}] 条件路由: {current} → ({route_key}) → {next_node}"
                )
                return next_node
            else:
                logger.error(
                    f"[{self._graph.name}] 路由 key '{route_key}' 不在 route_map 中: "
                    f"{list(route_map.keys())}"
                )
                raise ValueError(f"未知路由 key: {route_key}")

        # 2) 检查无条件边
        for src, dst in self._graph._edges:
            if src == current:
                logger.debug(f"[{self._graph.name}] 无条件跳转: {current} → {dst}")
                return dst

        # 3) 无出边
        return None

    def _save_checkpoint(
        self,
        checkpoint_dir: Path,
        state: Dict[str, Any],
        current_node: str,
    ):
        """
        保存 checkpoint 到磁盘。

        文件格式: {checkpoint_dir}/{timestamp}_{current_node}.json

        Args:
            checkpoint_dir: checkpoint 目录
            state: 当前状态
            current_node: 当前节点名
        """
        try:
            checkpoint_dir.mkdir(parents=True, exist_ok=True)
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"{timestamp}_{current_node}.json"
            filepath = checkpoint_dir / filename

            # 序列化状态（过滤不可序列化的对象）
            serializable = _make_serializable(state)

            with open(filepath, "w", encoding="utf-8") as f:
                json.dump(serializable, f, ensure_ascii=False, indent=2)
            
            logger.debug(f"[{self._graph.name}] Checkpoint 已保存: {filepath}")
        except Exception as e:
            logger.warning(f"[{self._graph.name}] Checkpoint 保存失败: {e}")

    def to_dict(self) -> Dict[str, Any]:
        return self._graph.to_dict()


# =============================================================================
# 辅助函数
# =============================================================================

def _make_serializable(obj: Any) -> Any:
    """
    将对象转换为 JSON 可序列化格式。

    处理规则:
        - dict → 递归处理 values
        - list/tuple → 递归处理 items
        - datetime → ISO 字符串
        - Path → 字符串
        - set → list
        - 其他不可序列化对象 → str()
    """
    if isinstance(obj, dict):
        return {k: _make_serializable(v) for k, v in obj.items()}
    elif isinstance(obj, (list, tuple, set)):
        return [_make_serializable(item) for item in obj]
    elif isinstance(obj, datetime):
        return obj.isoformat()
    elif isinstance(obj, Path):
        return str(obj)
    elif isinstance(obj, (str, int, float, bool, type(None))):
        return obj
    else:
        # 尝试转换为字符串
        try:
            return str(obj)
        except Exception:
            return f"<{type(obj).__name__}>"
