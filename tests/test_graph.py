"""
test_graph.py — 状态图引擎测试 v0.3.0

测试覆盖:
    1. 基本构建: add_node/add_edge/set_entry_point/set_finish_point
    2. 链式构建
    3. 条件分支 (add_conditional_edges)
    4. invoke 执行
    5. resume 断点续跑
    6. checkpoint 保存
    7. 循环检测
    8. 错误处理
    9. 序列化 (to_dict)
    10. 边界情况
"""

import json
import tempfile
from pathlib import Path

import pytest

from agent_mcp.graph import StateGraph, CompiledGraph, _make_serializable


# =============================================================================
# Fixtures
# =============================================================================

@pytest.fixture
def simple_graph():
    """简单的线性图: start → process → done"""
    graph = StateGraph("test_simple")

    def start_handler(state):
        state["started"] = True
        return state

    def process_handler(state):
        state["processed"] = True
        state["result"] = state.get("input", "") + "_processed"
        return state

    def done_handler(state):
        state["done"] = True
        return state

    graph.add_node("start", start_handler, "开始处理")
    graph.add_node("process", process_handler, "主处理逻辑")
    graph.add_node("done", done_handler, "结束")
    graph.add_edge("start", "process")
    graph.add_edge("process", "done")
    graph.set_entry_point("start")
    graph.set_finish_point("done")

    return graph


@pytest.fixture
def conditional_graph():
    """条件分支图: check → (pass→done | fail→process→check)"""
    graph = StateGraph("test_conditional")

    def check_handler(state):
        state["checks"] = state.get("checks", 0) + 1
        return state

    def process_handler(state):
        state["processed"] = True
        return state

    def done_handler(state):
        state["done"] = True
        return state

    def router(state):
        if state.get("ready") or state.get("checks", 0) > 3:
            return "pass"
        return "fail"

    graph.add_node("check", check_handler, "检查状态")
    graph.add_node("process", process_handler, "处理")
    graph.add_node("done", done_handler, "完成")
    graph.add_edge("check", "process")  # 默认边（条件边优先）
    graph.add_conditional_edges("process", router, {"pass": "done", "fail": "check"})
    graph.set_entry_point("check")
    graph.set_finish_point("done")

    return graph


# =============================================================================
# 基本构建测试
# =============================================================================

class TestGraphBuilding:
    """测试图的基本构建 API"""

    def test_add_node(self):
        """测试添加节点"""
        graph = StateGraph("test")
        graph.add_node("n1", lambda s: s, "节点1")
        assert "n1" in graph._nodes
        assert graph._node_metadata["n1"]["description"] == "节点1"

    def test_add_duplicate_node_raises(self):
        """重复添加节点应抛出异常"""
        graph = StateGraph("test")
        graph.add_node("n1", lambda s: s)
        with pytest.raises(ValueError, match="已注册"):
            graph.add_node("n1", lambda s: s)

    def test_add_edge(self):
        """测试添加边"""
        graph = StateGraph("test")
        graph.add_node("a", lambda s: s)
        graph.add_node("b", lambda s: s)
        graph.add_edge("a", "b")
        assert ("a", "b") in graph._edges

    def test_add_edge_nonexistent_raises(self):
        """边指向不存在的节点应抛出异常"""
        graph = StateGraph("test")
        graph.add_node("a", lambda s: s)
        with pytest.raises(ValueError, match="未注册"):
            graph.add_edge("a", "b")

    def test_add_conditional_edges(self):
        """测试添加条件边"""
        graph = StateGraph("test")
        graph.add_node("a", lambda s: s)
        graph.add_node("b", lambda s: s)
        graph.add_node("c", lambda s: s)

        router = lambda s: "go_b" if s.get("flag") else "go_c"
        graph.add_conditional_edges("a", router, {"go_b": "b", "go_c": "c"})

        assert "a" in graph._conditional_edges
        router_fn, route_map = graph._conditional_edges["a"]
        assert route_map == {"go_b": "b", "go_c": "c"}

    def test_add_conditional_edges_invalid_dst_raises(self):
        """条件边指向不存在的节点应抛出异常"""
        graph = StateGraph("test")
        graph.add_node("a", lambda s: s)
        router = lambda s: "x"
        with pytest.raises(ValueError, match="未注册"):
            graph.add_conditional_edges("a", router, {"x": "nonexistent"})

    def test_chain_building(self):
        """测试链式构建"""
        graph = (
            StateGraph("chain")
            .add_node("a", lambda s: s)
            .add_node("b", lambda s: s)
            .add_edge("a", "b")
            .set_entry_point("a")
            .set_finish_point("b")
        )
        assert graph._entry_point == "a"
        assert graph._finish_point == "b"
        assert ("a", "b") in graph._edges

    def test_set_entry_point_nonexistent_raises(self):
        """入口节点不存在应抛出异常"""
        graph = StateGraph("test")
        with pytest.raises(ValueError, match="未注册"):
            graph.set_entry_point("nonexistent")

    def test_set_finish_point_nonexistent_raises(self):
        """结束节点不存在应抛出异常"""
        graph = StateGraph("test")
        with pytest.raises(ValueError, match="未注册"):
            graph.set_finish_point("nonexistent")


# =============================================================================
# 编译测试
# =============================================================================

class TestCompilation:
    """测试图的编译"""

    def test_compile_no_entry_raises(self):
        """无入口节点编译应抛出异常"""
        graph = StateGraph("test")
        graph.add_node("a", lambda s: s)
        with pytest.raises(ValueError, match="入口节点"):
            graph.compile()

    def test_compile_success(self, simple_graph):
        """正常编译"""
        compiled = simple_graph.compile()
        assert isinstance(compiled, CompiledGraph)
        assert compiled.name == "test_simple"

    def test_compile_with_loop(self):
        """带循环的图应正常编译（含循环检测）"""
        graph = StateGraph("loop")
        graph.add_node("a", lambda s: s)
        graph.add_node("b", lambda s: s)
        graph.add_edge("a", "b")
        graph.add_edge("b", "a")  # 环
        graph.set_entry_point("a")
        # 无结束点 + 环 → max_iterations = 2 * 10 = 20
        compiled = graph.compile()
        assert compiled._max_iterations == 20


# =============================================================================
# 执行测试
# =============================================================================

class TestInvoke:
    """测试 invoke 执行"""

    def test_simple_invoke(self, simple_graph):
        """简单线性图执行"""
        compiled = simple_graph.compile()
        result = compiled.invoke({"input": "hello"})

        assert result["started"] is True
        assert result["processed"] is True
        assert result["done"] is True
        assert result["result"] == "hello_processed"
        assert result["__finished__"] is True
        assert result["__error__"] is None
        assert result["__iteration__"] == 3  # start→process→done

    def test_conditional_pass(self, conditional_graph):
        """条件分支: 直接通过"""
        compiled = conditional_graph.compile()
        result = compiled.invoke({"ready": True})

        assert result["done"] is True
        # check → process → done (ready=True 所以一次通过)
        assert len(result["__history__"]) >= 2

    def test_conditional_retry(self, conditional_graph):
        """条件分支: 多次重试后通过"""
        compiled = conditional_graph.compile()
        result = compiled.invoke({"ready": False})

        # checks > 3 时通过，所以 check 执行了至少 4 次
        assert result["checks"] > 3
        assert result["done"] is True

    def test_max_iterations(self):
        """达到最大迭代次数应停止"""
        graph = StateGraph("infinite")
        graph.add_node("loop", lambda s: s)
        graph.add_edge("loop", "loop")
        graph.set_entry_point("loop")
        # 无结束点，每个节点 10 次 → 1 节点 = 10 次
        compiled = graph.compile()

        result = compiled.invoke({})
        assert result["__iteration__"] >= compiled._max_iterations
        assert result["__error__"] is not None

    def test_invoke_preserves_extra_keys(self, simple_graph):
        """invoke 应保留状态中的额外字段"""
        compiled = simple_graph.compile()
        result = compiled.invoke({"input": "x", "custom_key": "custom_value"})
        assert result["custom_key"] == "custom_value"

    def test_node_error_stops_execution(self):
        """节点抛出异常应停止执行并记录错误"""
        graph = StateGraph("error")
        
        def faulty_handler(state):
            raise RuntimeError("模拟错误")

        def never_reached(state):
            state["reached"] = True
            return state

        graph.add_node("start", faulty_handler)
        graph.add_node("end", never_reached)
        graph.add_edge("start", "end")
        graph.set_entry_point("start")
        graph.set_finish_point("end")

        compiled = graph.compile()
        result = compiled.invoke({})

        assert result["__error__"] == "模拟错误"
        assert result["__finished__"] is True
        assert "reached" not in result  # 不应到达 end 节点


# =============================================================================
# Resume 断点续跑测试
# =============================================================================

class TestResume:
    """测试 resume 断点续跑"""

    def test_resume_from_middle(self, simple_graph):
        """从中间节点恢复执行"""
        compiled = simple_graph.compile()
        
        # 手动构造中间状态（模拟从 process 节点恢复）
        state = {
            "input": "hello",
            "started": True,
            "__current_node__": "process",
            "__iteration__": 1,
            "__history__": [
                {"node": "start", "iteration": 1}
            ],
        }
        result = compiled.resume(state)

        assert result["processed"] is True
        assert result["done"] is True
        assert len(result["__history__"]) >= 3  # 原有1条 + process + done

    def test_resume_preserves_history(self, simple_graph):
        """resume 应保留原有历史"""
        compiled = simple_graph.compile()
        
        existing_history = [
            {"node": "start", "iteration": 1},
            {"node": "process", "iteration": 2},
        ]
        state = {
            "input": "hello",
            "started": True,
            "processed": True,
            "result": "hello_processed",
            "__current_node__": "done",
            "__iteration__": 2,
            "__history__": existing_history,
        }
        result = compiled.resume(state)

        assert result["done"] is True
        assert len(result["__history__"]) == 3
        assert result["__history__"][0]["node"] == "start"
        assert result["__history__"][1]["node"] == "process"
        assert result["__history__"][2]["node"] == "done"


# =============================================================================
# Checkpoint 测试
# =============================================================================

class TestCheckpoint:
    """测试 checkpoint 保存"""

    def test_checkpoint_saved(self, simple_graph):
        """checkpoint 文件应保存到指定目录"""
        with tempfile.TemporaryDirectory() as tmpdir:
            checkpoint_dir = Path(tmpdir) / "checkpoints"
            compiled = simple_graph.compile()
            result = compiled.invoke({"input": "test"}, checkpoint_dir=checkpoint_dir)

            # 应该有3个 checkpoint 文件（start, process, done）
            files = list(checkpoint_dir.glob("*.json"))
            assert len(files) == 3
            # 每个都是有效 JSON
            for f in files:
                data = json.loads(f.read_text())
                assert "__current_node__" in data or "__graph_name__" in data


# =============================================================================
# 序列化测试
# =============================================================================

class TestSerialization:
    """测试序列化"""

    def test_to_dict(self, simple_graph):
        """to_dict 应返回图结构信息"""
        d = simple_graph.to_dict()
        assert d["name"] == "test_simple"
        assert set(d["nodes"]) == {"start", "process", "done"}
        assert len(d["edges"]) == 2
        assert d["entry_point"] == "start"
        assert d["finish_point"] == "done"

    def test_make_serializable_datetime(self):
        """_make_serializable 应正确处理 datetime"""
        from datetime import datetime
        obj = {"time": datetime(2026, 5, 12, 10, 0, 0)}
        result = _make_serializable(obj)
        assert isinstance(result["time"], str)
        assert "2026-05-12" in result["time"]

    def test_make_serializable_path(self):
        """_make_serializable 应正确处理 Path"""
        obj = {"path": Path("/tmp/test")}
        result = _make_serializable(obj)
        assert result["path"] == "/tmp/test"

    def test_make_serializable_set(self):
        """_make_serializable 应正确处理 set"""
        obj = {"items": {1, 2, 3}}
        result = _make_serializable(obj)
        assert isinstance(result["items"], list)
        assert set(result["items"]) == {1, 2, 3}


# =============================================================================
# 边界情况测试
# =============================================================================

class TestEdgeCases:
    """测试边界情况"""

    def test_empty_initial_state(self, simple_graph):
        """空初始状态应正常工作"""
        compiled = simple_graph.compile()
        result = compiled.invoke({})
        assert result["__finished__"] is True

    def test_single_node_graph(self):
        """单节点图"""
        graph = StateGraph("single")
        graph.add_node("only", lambda s: {**s, "done": True})
        graph.set_entry_point("only")
        graph.set_finish_point("only")
        compiled = graph.compile()
        result = compiled.invoke({})
        assert result["done"] is True
        assert result["__iteration__"] == 1

    def test_multiple_conditional_edges(self):
        """多个条件边应在同一节点共存"""
        graph = StateGraph("multi_cond")
        graph.add_node("router", lambda s: s)
        graph.add_node("path_a", lambda s: {**s, "path": "a"})
        graph.add_node("path_b", lambda s: {**s, "path": "b"})
        graph.add_node("done", lambda s: {**s, "done": True})

        def router(s):
            return s.get("choice", "a")

        graph.add_conditional_edges("router", router, {"a": "path_a", "b": "path_b"})
        graph.add_edge("path_a", "done")
        graph.add_edge("path_b", "done")
        graph.set_entry_point("router")
        graph.set_finish_point("done")

        compiled = graph.compile()
        result = compiled.invoke({"choice": "b"})
        assert result["path"] == "b"
        assert result["done"] is True

    def test_router_exception_bubbles(self):
        """路由函数异常应向上传播"""
        graph = StateGraph("bad_router")
        graph.add_node("a", lambda s: s)
        graph.add_node("b", lambda s: s)
        
        def bad_router(s):
            raise ValueError("路由失败")

        graph.add_conditional_edges("a", bad_router, {"x": "b"})
        graph.set_entry_point("a")

        compiled = graph.compile()
        with pytest.raises(ValueError, match="路由失败"):
            compiled.invoke({})
