"""Streamlit chat page for the Care Insights assistant.

Run: streamlit run assistant/app.py
Every question, tool call and answer is appended to logs/assistant.jsonl by assistant/agent.py.
"""

from __future__ import annotations

import hmac
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import streamlit as st  # noqa: E402

from assistant.agent import Assistant  # noqa: E402

st.set_page_config(page_title="Care Insights assistant", page_icon="📊", layout="wide")
st.title("Care Insights assistant")
st.caption(
    "Answers from public NDIS and aged care statistics (read-only SQL over the gold layer) and the NDIS "
    "Practice Standards / Strengthened Aged Care Quality Standards. Public aggregate data only."
)

# D6: access code gate. Only active when APP_ACCESS_CODE is set (in Azure, from a Container Apps secret).
ACCESS_CODE = os.environ.get("APP_ACCESS_CODE")
if ACCESS_CODE and not st.session_state.get("authorised"):
    code = st.text_input("Access code", type="password")
    if code and hmac.compare_digest(code, ACCESS_CODE):  # constant-time comparison
        st.session_state.authorised = True
        st.rerun()
    if code:
        st.error("Wrong access code.")
    st.stop()  # nothing below runs, so no Claude or SQL calls without the code

if "assistant" not in st.session_state:
    st.session_state.assistant = Assistant()
    st.session_state.history = []  # (question, result) pairs for display


def show_tool_calls(tool_calls: list[dict]) -> None:
    if not tool_calls:
        return
    with st.expander(f"Sources: {len(tool_calls)} tool call(s)"):
        for call in tool_calls:
            if call["tool"] == "query_gold":
                st.code(call["input"]["sql"], language="sql")
                if call["is_error"]:
                    st.error(call["output"])
                else:
                    out = json.loads(call["output"])
                    st.dataframe([dict(zip(out["columns"], r)) for r in out["rows"]], use_container_width=True)
                    if out["truncated"]:
                        st.caption(out["note"])
            else:
                st.markdown(f"**search_policy**: _{call['input']['query']}_")
                if call["is_error"]:
                    st.error(call["output"])
                else:
                    for hit in json.loads(call["output"]):
                        st.markdown(f"- {hit['citation']}: {hit['text'][:300]}…")


for question, result in st.session_state.history:
    st.chat_message("user").write(question)
    with st.chat_message("assistant"):
        st.markdown(result["answer"])
        show_tool_calls(result["tool_calls"])

with st.sidebar:
    st.markdown(f"Session `{st.session_state.assistant.session_id}`")
    if st.button("New conversation"):
        del st.session_state["assistant"]
        st.rerun()
    st.markdown(
        "Try:\n- How many active NDIS participants were there in QLD at 30 Sep 2026?\n"
        "- Which service district had the lowest plan utilisation?\n"
        "- How often must aged care services plans be reviewed?"
    )

if question := st.chat_input("Ask about NDIS or aged care data, or the quality standards"):
    st.chat_message("user").write(question)
    with st.chat_message("assistant"):
        with st.spinner("Working…"):
            result = st.session_state.assistant.ask(question)
        st.markdown(result["answer"])
        show_tool_calls(result["tool_calls"])
    st.session_state.history.append((question, result))
