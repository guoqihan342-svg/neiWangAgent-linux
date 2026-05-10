"""
日志追踪系统 — 为 neiWangAgent 提供结构化日志、步骤追踪和性能分析。

功能层级：
  1. 结构化日志（JSON Lines 格式，方便 grep / jq 分析）
  2. 步骤追踪（每个状态转换自动记录开始/结束/耗时）
  3. 文件轮转（RotatingFileHandler，单文件 10MB，保留 5 个备份）
  4. 双通道输出（控制台人类可读 + 文件 JSON 结构化）
  5. 性能装饰器（@trace_step 自动记录函数耗时）

使用示例：

    from agent_mcp.tracing import get_tracer

    tracer = get_tracer()
    tracer.info("agent.start", requirement="修bug")

    with tracer.span("state.WORKTREE_GUARD"):
        # 自动记录进入/退出/耗时
        git_status()

输出格式（控制台）：
    12:34:56 [INFO ] state.WORKTREE_GUARD | duration=0.23s | ✅ 工作区干净

输出格式（文件 .agent/logs/agent.jsonl）：
    {"ts":"2026-05-10T12:34:56","level":"INFO","event":"state.WORKTREE_GUARD","duration":0.23,"ok":true}
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from functools import wraps
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Callable, Optional

# =============================================================================
# 日志格式常量
# =============================================================================

# 控制台格式：时间 + 级别 + 事件 + 关键字段
CONSOLE_FORMAT = (
    "%(asctime)s.%(msecs)03d [%(levelname)-5s] %(message)s"
)
# 控制台日期格式（不含年份，节省空间）
CONSOLE_DATE_FORMAT = "%H:%M:%S"

# 文件格式：纯 JSON，每行一条记录
# 由 JsonFormatter 手动构造，不走 logging.Formatter


# =============================================================================
# JSON 格式化器
# =============================================================================

class JsonFormatter(logging.Formatter):
    """
    将日志记录格式化为单行 JSON，方便后续 grep / jq 分析。

    每条日志的 JSON 结构：
        {
            "ts":      "2026-05-10T12:34:56.789+08:00",  // ISO8601 时间戳
            "level":   "INFO",                            // 日志级别
            "event":   "state.COMMIT",                    // 事件名（由 msg 提供）
            "run_id":  "20260510-123456",                 // 运行 ID（可选）
            "step":    "160",                             // 步骤码（可选）
            "duration": 0.23,                             // 耗时秒（可选）
            "ok":      true,                              // 是否成功（可选）
            "detail":  "..."                              // 额外详情（可选）
        }
    """

    def format(self, record: logging.LogRecord) -> str:
        # ── 基础字段 ──
        log_entry: dict[str, Any] = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "event": record.getMessage(),  # 事件名 = msg
        }

        # ── 额外字段（通过 extra 字典注入） ──
        for key in ("run_id", "step", "duration", "ok", "detail"):
            val = getattr(record, key, None)
            if val is not None:
                log_entry[key] = val

        # ── 异常信息 ──
        if record.exc_info and record.exc_info[1]:
            log_entry["error"] = str(record.exc_info[1])
        elif record.exc_text:
            log_entry["error"] = record.exc_text

        return json.dumps(log_entry, ensure_ascii=False, default=str)


# =============================================================================
# 控制台格式化器（人类可读）
# =============================================================================

class ConsoleFormatter(logging.Formatter):
    """
    控制台输出格式：简洁、彩色友好、关键字段内联。

    示例输出：
        12:34:56.789 [INFO ] state.WORKTREE_GUARD | duration=0.23s | ✅ 通过
        12:34:57.012 [ERROR] state.COMMIT           | ❌ git commit 失败
    """

    def format(self, record: logging.LogRecord) -> str:
        # ── 时间 ──
        ts = datetime.now().strftime("%H:%M:%S") + f".{int(time.time()*1000)%1000:03d}"

        # ── 级别 ──
        level = record.levelname[:5].ljust(5)

        # ── 正文 ──
        msg = record.getMessage()
        parts = [f"{ts} [{level}] {msg}"]

        # ── 附加字段 ──
        duration = getattr(record, "duration", None)
        if duration is not None:
            parts.append(f"duration={duration:.2f}s")

        ok = getattr(record, "ok", None)
        if ok is True:
            parts.append("✅")
        elif ok is False:
            parts.append("❌")

        detail = getattr(record, "detail", None)
        if detail:
            parts.append(str(detail))

        return " | ".join(parts)


# =============================================================================
# 日志配置函数（创建 logger 实例）
# =============================================================================

def _build_logger(name: str = "neiWangAgent") -> logging.Logger:
    """
    构建并配置一个 logger 实例。

    特性：
      - 日志文件路径：.agent/logs/agent.log（自动创建目录）
      - 文件轮转：单文件 10MB，保留 5 个历史文件
      - 控制台输出：仅 WARNING 及以上（避免刷屏）
      - 文件输出：DEBUG 及以上（完整记录）
      - 文件格式：JSON Lines（每行一个 JSON 对象）

    返回：
        logging.Logger：配置好的 logger 实例。
    """
    logger = logging.getLogger(name)
    logger.setLevel(logging.DEBUG)  # logger 本身接受所有级别，由 handler 过滤
    logger.propagate = False        # 不向根 logger 传递，避免重复输出

    # ========== 控制台 Handler（WARNING+） ==========
    if not any(isinstance(h, logging.StreamHandler) for h in logger.handlers):
        console = logging.StreamHandler(sys.stderr)
        console.setLevel(logging.WARNING)  # 控制台只显示警告和错误
        console.setFormatter(ConsoleFormatter())
        logger.addHandler(console)

    # ========== 文件 Handler（DEBUG+，JSON 格式） ==========
    if not any(isinstance(h, RotatingFileHandler) for h in logger.handlers):
        log_dir = Path(".agent/logs")
        log_dir.mkdir(parents=True, exist_ok=True)

        file_handler = RotatingFileHandler(
            str(log_dir / "agent.log"),
            maxBytes=10 * 1024 * 1024,  # 10MB
            backupCount=5,              # 保留 5 个备份
            encoding="utf-8",
        )
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(JsonFormatter())
        logger.addHandler(file_handler)

    return logger


# =============================================================================
# Tracer — 核心追踪器
# =============================================================================

class Tracer:
    """
    核心追踪器，封装 logger 并提供结构化日志方法。

    所有日志通过此对象发出，确保：
      - 控制台：人类可读的简洁输出
      - 文件：  结构化 JSON 供后续分析
      - 上下文：run_id 自动注入到 extra 字段

    使用方式：
        tracer = get_tracer()
        tracer.info("state.INIT", step="000")

        with tracer.span("state.WARMUP"):
            do_warmup()  # 自动记录进入/退出/耗时
    """

    def __init__(self, logger: logging.Logger):
        self._logger = logger
        self._run_id: str = ""  # 当前运行的 ID
        self._lock = threading.Lock()  # 线程安全（预留）

    # ── 设置运行上下文 ──

    def set_run_id(self, run_id: str) -> None:
        """
        设置当前运行 ID，后续所有日志自动携带。

        参数：
            run_id: 运行 ID，格式如 "20260510-123456"
        """
        self._run_id = run_id

    # ── 结构化日志方法 ──

    def _log(
        self,
        level: int,
        event: str,
        *,
        step: str = "",
        duration: Optional[float] = None,
        ok: Optional[bool] = None,
        detail: Any = None,
    ) -> None:
        """
        内部日志方法，构造 extra 字典。

        参数：
            level:    logging 级别常量（DEBUG/INFO/WARNING/ERROR）
            event:    事件名，如 "state.COMMIT"、"llm.request"
            step:     步骤码，如 "160"（对应状态机状态）
            duration: 耗时（秒），用于性能分析
            ok:       操作是否成功（True/False/None=未判定）
            detail:   额外详情（字符串或可序列化对象）
        """
        extra: dict[str, Any] = {}
        if self._run_id:
            extra["run_id"] = self._run_id
        if step:
            extra["step"] = step
        if duration is not None:
            extra["duration"] = round(duration, 3)
        if ok is not None:
            extra["ok"] = ok
        if detail is not None:
            extra["detail"] = detail

        self._logger.log(level, event, extra=extra)

    def debug(self, event: str, **kwargs: Any) -> None:
        """DEBUG 级别日志（仅写入文件，不在控制台显示）"""
        self._log(logging.DEBUG, event, **kwargs)

    def info(self, event: str, **kwargs: Any) -> None:
        """INFO 级别日志"""
        self._log(logging.INFO, event, **kwargs)

    def warning(self, event: str, **kwargs: Any) -> None:
        """WARNING 级别日志"""
        self._log(logging.WARNING, event, **kwargs)

    def error(self, event: str, **kwargs: Any) -> None:
        """ERROR 级别日志"""
        self._log(logging.ERROR, event, **kwargs)

    # ── 上下文管理器：自动计时 ──

    @contextmanager
    def span(self, event: str, *, step: str = ""):
        """
        上下文管理器：进入时记录开始，退出时记录结束+耗时。

        用法：
            with tracer.span("state.COMMIT", step="160"):
                git_commit()

        日志输出：
            [DEBUG] state.COMMIT.start     （文件）
            [INFO ] state.COMMIT           duration=0.23s | ✅ （控制台+文件）

        异常处理：
            如果 span 内抛出异常，自动记录 ERROR 级别日志并标记 ok=False。
        """
        # ── 进入 span ──
        start = time.perf_counter()
        self.debug(f"{event}.start", step=step)

        ok_flag = True
        error_detail = None

        try:
            yield  # span 体执行
        except Exception as exc:
            ok_flag = False
            error_detail = str(exc)
            self.error(f"{event}.error", step=step, detail=error_detail)
            raise
        finally:
            # ── 退出 span ──
            elapsed = time.perf_counter() - start
            self.info(event, step=step, duration=elapsed, ok=ok_flag)


# =============================================================================
# 装饰器：@trace_step — 自动追踪函数调用
# =============================================================================

def trace_step(event: Optional[str] = None):
    """
    装饰器：自动记录函数调用的开始/结束/耗时。

    用法：
        @trace_step("state.COMMIT")
        def _handle_commit(self):
            ...

    如果不传 event 参数，自动使用 `module.func_name` 作为事件名。

    注意：
        被装饰函数的第一个参数如果是 self，且 self 有 tracer 属性，则使用该 tracer。
        否则回退到全局 tracer。
    """

    def decorator(func: Callable) -> Callable:
        @wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            # ── 确定事件名 ──
            evt = event or f"{func.__module__}.{func.__qualname__}"

            # ── 尝试从 self 获取 tracer ──
            tracer = None
            if args and hasattr(args[0], "tracer"):
                tracer = args[0].tracer
            if tracer is None:
                tracer = _global_tracer

            # ── 执行并计时 ──
            start = time.perf_counter()
            tracer.debug(f"{evt}.start")
            try:
                result = func(*args, **kwargs)
                elapsed = time.perf_counter() - start
                tracer.info(evt, duration=elapsed, ok=True)
                return result
            except Exception:
                elapsed = time.perf_counter() - start
                tracer.error(evt, duration=elapsed, ok=False)
                raise

        return wrapper

    return decorator


# =============================================================================
# 全局单例 & 工厂函数
# =============================================================================

# 全局 tracer 实例（懒加载）
_global_tracer: Optional[Tracer] = None
_tracer_lock = threading.Lock()


def get_tracer() -> Tracer:
    """
    获取全局 Tracer 单例。

    线程安全，首次调用时自动创建。

    返回：
        Tracer：全局追踪器实例。
    """
    global _global_tracer
    if _global_tracer is None:
        with _tracer_lock:
            if _global_tracer is None:
                logger = _build_logger("neiWangAgent")
                _global_tracer = Tracer(logger)
    return _global_tracer


# =============================================================================
# 便捷方法：重置 tracer（测试用）
# =============================================================================

def reset_tracer() -> None:
    """
    重置全局 tracer（仅用于测试）。
    """
    global _global_tracer
    _global_tracer = None
