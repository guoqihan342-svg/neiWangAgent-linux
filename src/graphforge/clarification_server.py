"""
Clarification MCP Server v0.1.5 — 澄清沟通 + 文件存档

★ P2-13: 澄清内容落文件（不只是终端打印）

存档结构：
  .graphforge/runs/{run_id}/clarification/
    questions.md      — 人类可读问题列表
    questions.json    — 结构化问题
    copy_message.md   — 可复制消息模板（用于粘贴回复）
    answers.md        — 人类回复（人工填写后 resume 读取）
    answers.json      — 结构化回复

resume 时读取 answers.md/answers.json，不再直接重新跑。
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from graphforge.base_mcp import BaseMCPServer


class ClarificationMCPServer(BaseMCPServer):
    """Clarification MCP Server — 继承 BaseMCPServer。"""

    name = "clarification-mcp"
    version = "0.1.5"

    def __init__(self):
        super().__init__()
        self.tools = {
            "clarification_ask": {
                "description": "向用户提问以澄清需求，问题存档到文件",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "questions": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "需要澄清的问题列表",
                        },
                        "run_id": {"type": "string", "description": "运行 ID"},
                    },
                    "required": ["questions"],
                },
            },
            "clarification_get_answers": {
                "description": "获取用户的回复（从 answers.md）",
                "inputSchema": {
                    "type": "object",
                    "properties": {"run_id": {"type": "string"}},
                    "required": ["run_id"],
                },
            },
            "clarification_save_answers": {
                "description": "保存用户回复到文件",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "run_id": {"type": "string"},
                        "answers": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["run_id", "answers"],
                },
            },
        }

    def _call_tool(self, name: str, args: dict):
        handler = {
            "clarification_ask": self._ask,
            "clarification_get_answers": self._get_answers,
            "clarification_save_answers": self._save_answers,
        }.get(name)
        if handler:
            try:
                result = handler(**args)
                return {"content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False)}]}
            except Exception as e:
                return {"content": [{"type": "text", "text": f"Error: {e}"}], "isError": True}
        return {"content": [{"type": "text", "text": f"Unknown: {name}"}], "isError": True}

    # ------------------------------------------------------------------
    # ★ P2-13: 文件存档
    # ------------------------------------------------------------------

    @staticmethod
    def _clarification_dir(run_id: str) -> Path:
        """获取澄清存档目录。"""
        d = Path(f".graphforge/runs/{run_id}/clarification")
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _ask(self, questions: list[str], run_id: str = "") -> dict:
        """
        ★ P2-13: 生成问题并存档到文件。

        产出：
          - questions.md    — Markdown 格式问题列表
          - questions.json  — 结构化 JSON（含时间戳）
          - copy_message.md — 可复制消息模板
        """
        rid = run_id or "unknown"
        cdir = self._clarification_dir(rid)
        ts = datetime.now().isoformat()

        # ── questions.json ──
        q_data = {
            "run_id": rid,
            "asked_at": ts,
            "questions": [{"index": i + 1, "question": q} for i, q in enumerate(questions)],
            "status": "waiting",
        }
        (cdir / "questions.json").write_text(
            json.dumps(q_data, indent=2, ensure_ascii=False), encoding="utf-8"
        )

        # ── questions.md ──
        md_lines = [
            f"# 需求澄清 — {rid}",
            f"",
            f"> 提问时间: {ts}",
            f"> 状态: 等待回复",
            f"",
            f"## 问题列表",
            f"",
        ]
        for i, q in enumerate(questions, 1):
            md_lines.append(f"{i}. {q}")
            md_lines.append("")
        md_lines.extend([
            f"---",
            f"",
            f"## 如何回复",
            f"",
            f"请在下方每个问题后填写答案，然后运行 `graphforge resume {rid}` 继续。",
            f"",
        ])
        for i in range(len(questions)):
            md_lines.extend([
                f"### 问题 {i + 1}",
                f"",
                f"<!-- 在此填写答案 -->",
                f"",
            ])
        (cdir / "questions.md").write_text("\n".join(md_lines), encoding="utf-8")

        # ── copy_message.md ──
        copy_lines = [
            f"# 需求澄清（可复制消息）",
            f"",
            f"请回答以下 {len(questions)} 个问题：",
            f"",
        ]
        for i, q in enumerate(questions, 1):
            copy_lines.append(f"{i}. {q}")
            copy_lines.append(f"   答：")
            copy_lines.append("")
        copy_lines.append(f"回复后运行：`graphforge resume {rid}`")
        (cdir / "copy_message.md").write_text("\n".join(copy_lines), encoding="utf-8")

        return {"questions": questions, "run_id": rid, "status": "waiting", "files_saved": True}

    def _get_answers(self, run_id: str) -> dict:
        """
        ★ P2-13: 从存档文件读取回复。

        读取优先级：answers.json > answers.md > 旧格式 clarification.json
        """
        cdir = self._clarification_dir(run_id)

        # 优先读 answers.json
        json_path = cdir / "answers.json"
        if json_path.exists():
            data = json.loads(json_path.read_text(encoding="utf-8"))
            if data.get("answers"):
                return {"status": "answered", "answers": data["answers"], "run_id": run_id}
            return {"status": "no_answers", "questions": data.get("questions", [])}

        # fallback: 读旧格式
        old_path = Path(f".graphforge/runs/{run_id}/clarification.json")
        if old_path.exists():
            data = json.loads(old_path.read_text(encoding="utf-8"))
            if data.get("answers"):
                return {"status": "answered", "answers": data["answers"], "run_id": run_id}
            return {"status": "waiting", "questions": data.get("questions", [])}

        return {"status": "not_found", "run_id": run_id}

    def _save_answers(self, run_id: str, answers: list[str]) -> dict:
        """
        ★ P2-13: 保存用户回复到文件。

        产出：
          - answers.json  — 结构化回复
          - answers.md    — Markdown 格式回复
        """
        cdir = self._clarification_dir(run_id)
        ts = datetime.now().isoformat()

        # ── answers.json ──
        a_data = {
            "run_id": run_id,
            "answered_at": ts,
            "answers": [{"index": i + 1, "answer": a} for i, a in enumerate(answers)],
            "status": "answered",
        }
        (cdir / "answers.json").write_text(
            json.dumps(a_data, indent=2, ensure_ascii=False), encoding="utf-8"
        )

        # ── answers.md ──
        md_lines = [
            f"# 澄清回复 — {run_id}",
            f"",
            f"> 回复时间: {ts}",
            f"",
        ]
        for i, a in enumerate(answers, 1):
            md_lines.append(f"## 问题 {i}")
            md_lines.append(f"")
            md_lines.append(a)
            md_lines.append("")
        (cdir / "answers.md").write_text("\n".join(md_lines), encoding="utf-8")

        # ── 同时更新 questions.json 状态 ──
        q_path = cdir / "questions.json"
        if q_path.exists():
            q_data = json.loads(q_path.read_text(encoding="utf-8"))
            q_data["status"] = "answered"
            q_data["answered_at"] = ts
            q_path.write_text(json.dumps(q_data, indent=2, ensure_ascii=False), encoding="utf-8")

        return {"saved": True, "run_id": run_id, "count": len(answers), "files_saved": True}


if __name__ == "__main__":
    ClarificationMCPServer().run_stdio()
