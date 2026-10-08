# -*- coding: utf-8 -*-
"""AI 分析师 - Agent 编排器

流程：用户问题 -> LLM 判断调用哪些工具 -> 执行工具 -> 结果回填 -> LLM 生成最终回答。
支持会话持久化（SQLite：analyst_sessions / analyst_messages）。
"""

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List

from core.analyst import llm
from core.analyst.tools import TOOLS, execute_tool
from core.db import get_connection
from core.logging import get_logger

logger = get_logger("analyst.agent")

SYSTEM_PROMPT = """你是这家连锁门店经营数据的「数据分析师」助手，服务于公司数据专员。

数据口径说明：
- 金额单位：元（工具返回的收入汇总即元）。
- 「收入汇总」= 各平台收入指标之和（美团收款、芸苔非团购、抖音收款、乐摇摇、多金宝、鲸舰、StarThing、汇联、八达通、兑币机等）。
- 场地为门店/店名，通常含 香港/深圳/广州/江门/福州/东莞 等区域关键字。
- 查询数据必须使用工具，工具结果是最权威的数据来源。

回答规则：
1. 涉及具体数字、排名、对比、趋势、汇总的问题，必须先用 query_summary 或 dashboard_snapshot 获取数据后再回答；
   涉及预测、目标完成率推演、异常波动的问题，使用 forecast 或 detect_anomaly 工具。
2. 禁止编造或推算工具没有返回的数字；工具返回 error 时如实告知用户，不猜测。
3. 用中文回答，先给一句结论，再列最多 3 条关键发现；默认不超过 300 字，除非用户要求详细展开。金额保留两位小数。
4. 数字结论必须标注日期范围。不要寒暄、重复总结、大段分章节或罗列无关指标；多门店比较优先使用最多 5 行的小表格。
5. “最近两个采集日”必须使用本轮系统提供的数据库实际采集日期，不能沿用历史回答或示例日期。采集日期存在不代表所有平台采集完整；缺失数据不能当零。
6. 比较两个采集日时分别查询两日数据。月累计差是期间新增收入；日收入之差才是日环比增量，不得混用分子分母计算贡献率。跨月不能直接减月累计。
7. 累计不变只说明未观察到增长，不能据此断言断采、闭店或经营异常；工具没有证据的原因保持未知。"""


def _system_prompt() -> str:
    conn = get_connection()
    try:
        dates = [row[0] for row in conn.execute(
            "SELECT DISTINCT date FROM daily_summary ORDER BY date DESC LIMIT 2"
        ).fetchall()]
    finally:
        conn.close()
    today = datetime.now(timezone(timedelta(hours=8))).date().isoformat()
    return (SYSTEM_PROMPT + f"\n\n本轮北京时间日期：{today}。"
            + "数据库最近两个实际采集日（新到旧）："
            + ("、".join(dates) if dates else "暂无采集数据")
            + "。不足两个日期时如实说明，禁止补猜。")

_MAX_HISTORY = 24  # 保留最近 N 条历史消息，避免上下文过长


def create_session(title: str = "新对话") -> str:
    session_id = str(uuid.uuid4())
    conn = get_connection()
    try:
        conn.execute(
            "INSERT INTO analyst_sessions (id, title) VALUES (?, ?)",
            (session_id, (title or "新对话")[:40]),
        )
        conn.commit()
    finally:
        conn.close()
    return session_id


def list_sessions(limit: int = 30) -> List[Dict[str, Any]]:
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT id, title, created_at, updated_at FROM analyst_sessions "
            "ORDER BY updated_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_session_messages(session_id: str, limit: int = 200) -> List[Dict[str, Any]]:
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT role, content, created_at FROM analyst_messages "
            "WHERE session_id=? ORDER BY id DESC LIMIT ?",
            (session_id, limit),
        ).fetchall()
        return [dict(r) for r in reversed(rows)]
    finally:
        conn.close()


def _save_message(session_id: str, role: str, content: str) -> None:
    conn = get_connection()
    try:
        conn.execute(
            "INSERT INTO analyst_messages (session_id, role, content) VALUES (?, ?, ?)",
            (session_id, role, content),
        )
        conn.execute(
            "UPDATE analyst_sessions SET updated_at=? WHERE id=?",
            (datetime.now(), session_id),
        )
        conn.commit()
    finally:
        conn.close()


def _assistant_message_dict(msg: Any) -> Dict[str, Any]:
    tool_calls = []
    for tc in msg.tool_calls:
        tool_calls.append(
            {
                "id": tc.id,
                "type": "function",
                "function": {
                    "name": tc.function.name,
                    "arguments": tc.function.arguments,
                },
            }
        )
    return {
        "role": "assistant",
        "content": msg.content or "",
        "tool_calls": tool_calls,
    }


def run_question(question: str, session_id: str = "") -> Dict[str, Any]:
    """同步执行一轮问答（内部走流式逻辑，兼容旧接口）"""
    for evt_type, payload in run_question_stream(question, session_id):
        if evt_type == "done":
            return payload
        if evt_type == "error":
            raise llm.AnalystError(payload["error"])
    raise llm.AnalystError("分析失败：未收到模型响应")


def run_question_stream(question: str, session_id: str = ""):
    """流式执行问答，yield (event_type, payload) 事件：

    - ("meta",  {"session_id": ...})
    - ("tool",  {"name": ...})            工具开始执行
    - ("delta", {"text": ...})            最终回答文本增量
    - ("done",  {"session_id", "answer", "tool_rounds", ...})
    - ("error", {"error": ...})
    """
    question = (question or "").strip()
    if not question:
        yield "error", {"error": "问题不能为空"}
        return

    if not session_id:
        session_id = create_session(title=question)
    else:
        conn = get_connection()
        try:
            row = conn.execute(
                "SELECT id FROM analyst_sessions WHERE id=?",
                (session_id,),
            ).fetchone()
        finally:
            conn.close()
        if not row:
            yield "error", {"error": "会话不存在，请刷新页面后重试"}
            return

    yield "meta", {"session_id": session_id}

    history = get_session_messages(session_id, limit=_MAX_HISTORY)
    messages: List[Dict[str, Any]] = [
        {"role": "system", "content": _system_prompt()}
    ]
    for m in history:
        if m["role"] in ("user", "assistant"):
            messages.append({"role": m["role"], "content": m["content"]})
    messages.append({"role": "user", "content": question})
    _save_message(session_id, "user", question)

    max_rounds = llm.get_max_tool_rounds()
    tool_rounds = 0
    for _ in range(max_rounds):
        content_parts: List[str] = []
        tool_calls_acc: Dict[int, Dict[str, str]] = {}
        try:
            for chunk in llm.chat_stream(messages, tools=TOOLS):
                if not getattr(chunk, "choices", None):
                    continue
                delta = chunk.choices[0].delta
                text = getattr(delta, "content", None)
                if text:
                    content_parts.append(text)
                    yield "delta", {"text": text}
                for tc in (getattr(delta, "tool_calls", None) or []):
                    entry = tool_calls_acc.setdefault(
                        tc.index, {"id": "", "name": "", "arguments": ""}
                    )
                    if tc.id:
                        entry["id"] = tc.id
                    fn = tc.function
                    if fn:
                        if fn.name:
                            entry["name"] += fn.name
                        if fn.arguments:
                            entry["arguments"] += fn.arguments
        except llm.AnalystError as e:
            yield "error", {"error": str(e)}
            return
        except Exception as e:  # noqa: BLE001 - 流式链路异常需回传给前端
            logger.error("Agent 流式执行异常: %s", e)
            yield "error", {"error": f"分析失败：{e}"}
            return

        if not tool_calls_acc:
            answer = "".join(content_parts).strip()
            _save_message(session_id, "assistant", answer)
            yield "done", {
                "session_id": session_id,
                "answer": answer,
                "tool_rounds": tool_rounds,
            }
            return

        assistant_tool_calls = [
            {
                "id": tool_calls_acc[i]["id"],
                "type": "function",
                "function": {
                    "name": tool_calls_acc[i]["name"],
                    "arguments": tool_calls_acc[i]["arguments"],
                },
            }
            for i in sorted(tool_calls_acc)
        ]
        messages.append(
            {
                "role": "assistant",
                "content": "".join(content_parts),
                "tool_calls": assistant_tool_calls,
            }
        )
        for tc in assistant_tool_calls:
            name = tc["function"]["name"]
            yield "tool", {"name": name}
            tool_rounds += 1
            result = execute_tool(name, tc["function"]["arguments"])
            messages.append(
                {"role": "tool", "tool_call_id": tc["id"], "content": result}
            )

    answer = "分析步骤较多未能完成，请缩小问题范围后重试。"
    _save_message(session_id, "assistant", answer)
    yield "done", {
        "session_id": session_id,
        "answer": answer,
        "tool_rounds": tool_rounds,
        "truncated": True,
    }
