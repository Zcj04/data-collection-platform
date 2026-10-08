"""最新 N 条消息按对话顺序返回，隔离真实数据库和模型。"""

import copy
import sqlite3
from unittest import mock

import pytest

from core import db
from core.analyst import agent


@pytest.fixture
def conversation(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "history.db"))
    db.init_db()
    session = agent.create_session("history-test")
    conn = db.get_connection()
    try:
        conn.executemany(
            "INSERT INTO analyst_messages (session_id, role, content) VALUES (?, ?, ?)",
            [(session, "user" if index % 2 else "assistant", str(index)) for index in range(1, 206)],
        )
        conn.commit()
    finally:
        conn.close()
    other = agent.create_session("other-test")
    agent._save_message(other, "user", "not-this-session")
    return session


@pytest.mark.parametrize("limit", [1, 24, 200, 300])
def test_latest_messages_remain_chronological(conversation, limit):
    messages = agent.get_session_messages(conversation, limit)
    assert [item["content"] for item in messages] == [str(i) for i in range(max(1, 206 - limit), 206)]
    assert agent.get_session_messages("missing") == []


def test_stream_uses_latest_history(conversation):
    captured = []

    def chat(messages, **kwargs):
        captured.extend(copy.deepcopy(messages))
        return iter(())

    with mock.patch.object(agent.llm, "get_max_tool_rounds", return_value=1), \
            mock.patch.object(agent.llm, "chat_stream", side_effect=chat):
        list(agent.run_question_stream("new-question", conversation))
    assert captured[0]["role"] == "system"
    assert [item["content"] for item in captured[1:]] == [str(i) for i in range(182, 206)] + ["new-question"]


@pytest.mark.parametrize("dates,expected", [
    ([], "暂无采集数据"),
    (["2026-09-10"], "2026-09-10"),
    (["2026-08-11", "2026-09-09", "2026-09-10", "2026-09-10"],
     "2026-09-10、2026-09-09"),
])
def test_prompt_uses_actual_latest_distinct_collection_dates(tmp_path, monkeypatch, dates, expected):
    path = str(tmp_path / "dates.db")
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE daily_summary (date TEXT)")
        conn.executemany("INSERT INTO daily_summary VALUES (?)", [(d,) for d in dates])
    monkeypatch.setattr(agent, "get_connection", lambda: sqlite3.connect(path))
    prompt = agent._system_prompt()
    assert "数据库最近两个实际采集日（新到旧）：" + expected + "。" in prompt
    assert "2026-08-11" not in prompt
