#!/usr/bin/env python3
"""
Lark streaming cards — progressive CardKit 2.0 reply cards for Noto.

Two layers:
  * Pure schema layer — `RunState` + `render()`: build a CardKit
    schema-2.0 card JSON from immutable state. No I/O; unit-testable.
  * `CardStream` — orchestrates a live streaming reply: create the card,
    send the message, then push throttled updates as the research
    pipeline reports progress and the answer streams in token-by-token.

CREATE + UPDATE only — never deletes anything in Lark. (This file is
covered by lark_client.assert_no_lark_delete()'s `lark_*.py` scan.)

CLI: python tools/lark_cards.py selftest
"""

import sys
import threading
import time
from typing import Any, Dict, Optional

# Stable element ids — the streaming-content API targets elements by id.
STATUS_EL = "status"
ANSWER_EL = "answer"

# Lark rejects card elements over ~30 KB — keep the answer well under.
# Long deliverables become a Lark doc anyway, so this only ever clips
# the in-card preview, never the answer the user receives.
MAX_ANSWER_BYTES = 25_000

_TRUNCATED = "\n\n_… (truncated here — see the full document)_"


def _clip_bytes(text: str, limit: int) -> str:
    """Clip `text` to `limit` UTF-8 bytes without splitting a codepoint."""
    b = text.encode("utf-8")
    if len(b) <= limit:
        return text
    return b[:limit].decode("utf-8", "ignore").rstrip() + _TRUNCATED


# ---------------------------------------------------------------------------
# Pure schema layer
# ---------------------------------------------------------------------------

class RunState:
    """Immutable-ish render state for one streaming reply card."""

    def __init__(self, title: str = "🤖 Noto", summary: str = ""):
        self.title = title
        self.summary = summary or "Noto is working on your question…"
        self.status = "🔎 Getting started…"
        self.answer = ""
        self.note = ""               # footer note, e.g. a doc link
        self.phase = "live"          # live | done | error

    @property
    def live(self) -> bool:
        return self.phase == "live"

    def render(self) -> Dict[str, Any]:
        """Build the CardKit schema-2.0 card JSON for the current state."""
        elements = [
            {"tag": "markdown", "element_id": STATUS_EL,
             "content": self.status or " "},
            {"tag": "hr"},
            {"tag": "markdown", "element_id": ANSWER_EL,
             "content": _clip_bytes(self.answer, MAX_ANSWER_BYTES) or " "},
        ]
        if self.note:
            elements.append({"tag": "hr"})
            elements.append({"tag": "markdown", "content": self.note})
        template = ("red" if self.phase == "error"
                    else "green" if self.phase == "done" else "blue")
        return {
            "schema": "2.0",
            "config": {
                "streaming_mode": self.live,
                "summary": {"content": self.summary[:120]},
            },
            "header": {
                "title": {"tag": "plain_text", "content": self.title},
                "template": template,
            },
            "body": {"elements": elements},
        }


# ---------------------------------------------------------------------------
# Live streaming orchestrator
# ---------------------------------------------------------------------------

class CardStream:
    """Drives one streaming reply card end to end.

    `client` is duck-typed — any object exposing create_card / send_card /
    update_card / stream_card_element (LarkClient does). Updates carry a
    per-card monotonic `sequence` so the server drops out-of-order pushes;
    answer deltas are throttled so card edits stay well under Lark limits.
    Thread-safe — the research pipeline may call from a worker thread.
    """

    def __init__(self, client: Any, receive_id: str, *,
                 receive_id_type: str = "chat_id", title: str = "🤖 Noto",
                 summary: str = "", throttle_ms: int = 450):
        self.client = client
        self.receive_id = receive_id
        self.receive_id_type = receive_id_type
        self.state = RunState(title=title, summary=summary)
        self.card_id: Optional[str] = None
        self.message_id: Optional[str] = None
        self._seq = 0
        self._throttle = max(throttle_ms, 250) / 1000.0
        self._last = 0.0
        self._answer_started = False
        self._lock = threading.RLock()

    # -- lifecycle -------------------------------------------------------
    def start(self) -> str:
        """Create the card entity and send the message. Returns message_id.
        Raises on failure so the caller can fall back to a plain reply."""
        with self._lock:
            self.card_id = self.client.create_card(self.state.render())
            self.message_id = self.client.send_card(
                self.receive_id, self.card_id, self.receive_id_type)
            self._last = time.monotonic()
            return self.message_id

    def progress(self, status: str) -> None:
        """Update the status line (pipeline step). Always pushed —
        progress steps are seconds apart, well within rate limits."""
        with self._lock:
            self.state.status = status
            self._push_card(force=True)

    def stream_answer(self, delta: str) -> None:
        """Append a synthesis token/delta; pushes are throttled."""
        with self._lock:
            self.state.answer += delta
            if not self._answer_started:
                self._answer_started = True
                self.state.status = "✍️ Writing the answer…"
                self._push_card(force=True)
                return
            now = time.monotonic()
            if now - self._last >= self._throttle:
                self._last = now
                self._push_element()

    def finalize(self, *, status: Optional[str] = None,
                 answer: Optional[str] = None, note: str = "",
                 error: bool = False) -> None:
        """Land the terminal state — flips streaming_mode off, full push."""
        with self._lock:
            if answer is not None:
                self.state.answer = answer
            if note:
                self.state.note = note
            self.state.phase = "error" if error else "done"
            self.state.status = status or (
                "⚠️ Something went wrong." if error else "✅ Done.")
            self._push_card(force=True)

    # -- internals -------------------------------------------------------
    def _seq_next(self) -> int:
        self._seq += 1
        return self._seq

    def _push_card(self, force: bool = False) -> None:
        """Full-card update (status/structure changes)."""
        if not self.card_id:
            return
        now = time.monotonic()
        if not force and now - self._last < self._throttle:
            return
        self._last = now
        try:
            self.client.update_card(
                self.card_id, self.state.render(), self._seq_next())
        except Exception as e:                       # pragma: no cover
            print(f"[lark_cards] update_card failed: {e}", file=sys.stderr)

    def _push_element(self) -> None:
        """Stream just the answer element (lighter — native typewriter)."""
        if not self.card_id:
            return
        try:
            self.client.stream_card_element(
                self.card_id, ANSWER_EL,
                _clip_bytes(self.state.answer, MAX_ANSWER_BYTES) or " ",
                self._seq_next())
        except Exception as e:                       # pragma: no cover
            print(f"[lark_cards] stream element failed: {e}", file=sys.stderr)


# ---------------------------------------------------------------------------
# selftest
# ---------------------------------------------------------------------------

class _FakeClient:
    """Records calls — lets CardStream be tested with no network."""

    def __init__(self):
        self.calls = []

    def create_card(self, card):
        self.calls.append(("create", card))
        return "card_test"

    def send_card(self, rid, card_id, rid_type="chat_id"):
        self.calls.append(("send", card_id, rid))
        return "msg_test"

    def update_card(self, card_id, card, sequence):
        self.calls.append(("update", sequence, card))

    def stream_card_element(self, card_id, element_id, text, sequence):
        self.calls.append(("stream", sequence, element_id, text))


def _selftest() -> int:
    ok = True

    # -- pure render ----------------------------------------------------
    st = RunState(summary="target list for Bee Chun")
    card = st.render()
    if (card.get("schema") == "2.0"
            and card["config"]["streaming_mode"] is True
            and {e.get("element_id") for e in card["body"]["elements"]
                 if "element_id" in e} == {STATUS_EL, ANSWER_EL}):
        print("PASS: render -> schema 2.0 card with status+answer elements")
    else:
        print(f"FAIL: render -> {card}"); ok = False

    # -- byte clip ------------------------------------------------------
    st.answer = "x" * 40_000
    ans_el = next(e for e in st.render()["body"]["elements"]
                  if e.get("element_id") == ANSWER_EL)
    blen = len(ans_el["content"].encode("utf-8"))
    if blen <= MAX_ANSWER_BYTES + len(_TRUNCATED.encode()) + 8:
        print(f"PASS: answer element clipped to ~{blen} bytes")
    else:
        print(f"FAIL: clip -> {blen} bytes"); ok = False

    # -- terminal templates --------------------------------------------
    st.phase = "done"
    done_tpl = st.render()["header"]["template"]
    st.phase = "error"
    err_tpl = st.render()["header"]["template"]
    if (done_tpl, err_tpl) == ("green", "red"):
        print("PASS: header template tracks phase (done=green, error=red)")
    else:
        print(f"FAIL: templates {done_tpl}/{err_tpl}"); ok = False

    # -- CardStream lifecycle ------------------------------------------
    fc = _FakeClient()
    cs = CardStream(fc, "oc_chat", summary="q", throttle_ms=10_000)
    mid = cs.start()
    if mid == "msg_test" and [c[0] for c in fc.calls] == ["create", "send"]:
        print("PASS: CardStream.start -> create_card + send_card")
    else:
        print(f"FAIL: start calls {fc.calls}"); ok = False

    cs.progress("📚 Gathering documents…")
    if (fc.calls[-1][0] == "update"
            and "Gathering" in fc.calls[-1][2]["body"]["elements"][0]["content"]):
        print("PASS: progress() -> full card update with new status")
    else:
        print(f"FAIL: progress {fc.calls[-1]}"); ok = False

    # first answer delta forces a full update; rest throttle out (10s)
    before = len(fc.calls)
    for tok in ["Hello", " there", ", here", " is", " the answer."]:
        cs.stream_answer(tok)
    after = fc.calls[before:]
    streamed = [c for c in after if c[0] == "stream"]
    if len(after) == 1 and after[0][0] == "update" and not streamed:
        print("PASS: answer streaming throttles (1 forced push, rest coalesced)")
    else:
        print(f"FAIL: streaming pushes {after}"); ok = False
    if cs.state.answer == "Hello there, here is the answer.":
        print("PASS: coalesced deltas still accumulate in state")
    else:
        print(f"FAIL: answer state {cs.state.answer!r}"); ok = False

    cs.finalize(note="📄 [Open the document](https://example.com/doc)")
    last = fc.calls[-1]
    final_card = last[2]
    if (last[0] == "update"
            and final_card["config"]["streaming_mode"] is False
            and cs.state.phase == "done"
            and any("Open the document" in e.get("content", "")
                    for e in final_card["body"]["elements"])):
        print("PASS: finalize -> streaming_mode off, doc note attached")
    else:
        print(f"FAIL: finalize {last}"); ok = False

    # -- monotonic sequence --------------------------------------------
    seqs = [c[1] for c in fc.calls if c[0] in ("update", "stream")]
    if seqs == sorted(seqs) and len(seqs) == len(set(seqs)):
        print(f"PASS: sequence strictly increasing {seqs}")
    else:
        print(f"FAIL: sequence {seqs}"); ok = False

    print("\nselftest:", "ALL PASS" if ok else "FAILURES")
    return 0 if ok else 1


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == "selftest":
        return _selftest()
    print(__doc__)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
