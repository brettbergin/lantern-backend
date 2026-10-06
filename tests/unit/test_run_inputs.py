"""A workload a chat message asked for can read that message's attachments:
they are copied into its data directory and its ask names them there."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from lantern.daemon.inputs import inputs_note, stage_chat_inputs
from lantern.daemon.model import WorkItem
from lantern.db.collaboration_models import ChannelInputFileRow
from lantern.engine.model import RunResult
from lantern.events import EventBus
from tests.unit.test_daemon_loop import Harness

CHANNEL = "chn_1"
MESSAGE = "msg_asking"


def _attach(
    h: Harness,
    file_id: str,
    name: str,
    data: bytes | None,
    *,
    message: str = MESSAGE,
    position: int = 0,
    status: str = "attached",
) -> None:
    root = h.config.paths.channel_files
    root.mkdir(parents=True, exist_ok=True)
    if data is not None:
        (root / file_id).write_bytes(data)
    with h.dstore.transaction() as session:
        session.add(
            ChannelInputFileRow(
                id=file_id,
                workspace_id="local",
                channel_id=CHANNEL,
                uploader_id="usr_1",
                client_upload_id=file_id,
                display_name=name,
                declared_size=len(data or b"x"),
                size=len(data or b"x"),
                sha256=None,
                status=status,
                message_id=message,
                position=position,
                created_at=1.0,
                uploaded_at=1.0,
            )
        )


def _workload(key: str = MESSAGE, channel: str | None = CHANNEL) -> WorkItem:
    return WorkItem(
        item_id=f"chat:{key}",
        source_key=key,
        title="summarise the notes",
        body="read the attached notes and summarise them",
        url="",
        kind="workload",
        channel_id=channel,
    )


def _file_id(n: int) -> str:
    return "fin_" + str(n) * 24


class TestStaging:
    def test_the_asking_messages_files_are_copied_under_inputs(self, tmp_path: Path) -> None:
        h = Harness(tmp_path)
        _attach(h, _file_id(1), "notes.txt", b"the secret word is lantern\n")
        _attach(h, _file_id(2), "notes.txt", b"second", position=1)
        _attach(h, _file_id(3), "elsewhere.txt", b"other message", message="msg_other")
        _attach(h, _file_id(4), "pending.txt", b"not attached", status="uploaded")
        staged = stage_chat_inputs(h.dstore, h.config, _workload(), "r1")
        assert [(s.path, s.size) for s in staged] == [
            ("inputs/notes.txt", 27),
            ("inputs/notes (2).txt", 6),
        ]
        inputs = h.config.paths.run_workspace("r1") / "inputs"
        assert (inputs / "notes.txt").read_bytes() == b"the secret word is lantern\n"
        assert sorted(p.name for p in inputs.iterdir()) == ["notes (2).txt", "notes.txt"]
        note = inputs_note(staged)
        assert "`inputs/notes.txt` (27 bytes)" in note and "untrusted data" in note

    def test_a_later_participants_key_still_finds_its_message(self, tmp_path: Path) -> None:
        h = Harness(tmp_path)
        _attach(h, _file_id(1), "a.csv", b"1,2\n")
        staged = stage_chat_inputs(h.dstore, h.config, _workload(f"{MESSAGE}:1"), "r1")
        assert [s.path for s in staged] == ["inputs/a.csv"]

    def test_a_name_never_escapes_the_inputs_directory(self, tmp_path: Path) -> None:
        h = Harness(tmp_path)
        _attach(h, _file_id(1), "..", b"x")
        _attach(h, _file_id(2), "../../etc/passwd", b"y", position=1)
        staged = stage_chat_inputs(h.dstore, h.config, _workload(), "r1")
        assert [s.path for s in staged] == ["inputs/attachment", "inputs/passwd"]

    def test_a_missing_original_is_left_out(self, tmp_path: Path) -> None:
        h = Harness(tmp_path)
        _attach(h, _file_id(1), "gone.txt", None)
        _attach(h, _file_id(2), "here.txt", b"ok", position=1)
        staged = stage_chat_inputs(h.dstore, h.config, _workload(), "r1")
        assert [s.path for s in staged] == ["inputs/here.txt"]

    def test_nothing_for_work_no_chat_message_asked_for(self, tmp_path: Path) -> None:
        h = Harness(tmp_path)
        _attach(h, _file_id(1), "notes.txt", b"x")
        assert stage_chat_inputs(h.dstore, h.config, _workload(channel=None), "r1") == ()
        api = _workload().model_copy(update={"item_id": f"api:{MESSAGE}"})
        assert stage_chat_inputs(h.dstore, h.config, api, "r1") == ()
        assert not (h.config.paths.run_workspace("r1") / "inputs").exists()
        assert inputs_note(()) == ""


class TestTheRunSeesThem:
    def _start(self, h: Harness, item: WorkItem) -> dict[str, Any]:
        started: list[dict[str, Any]] = []

        class Engine:
            def start(self, outcome: str, **kwargs: Any) -> RunResult:
                started.append({"outcome": outcome, **kwargs})
                return RunResult(run_id=kwargs["run_id"], state="completed")

        class Handle:
            engine = Engine()

        h.loop._live_run = lambda run_id: Handle()  # type: ignore[method-assign,assignment,return-value]
        h.loop._default_runner(item, h.config, "r1", EventBus(), False)
        (kwargs,) = started
        return kwargs

    def test_the_ask_names_the_copies_and_the_mount_is_required(self, tmp_path: Path) -> None:
        h = Harness(tmp_path)
        _attach(h, _file_id(1), "qa-notes.txt", b"secret\n")
        started = self._start(h, _workload())
        assert "`inputs/qa-notes.txt` (7 bytes)" in started["outcome"]
        assert started["expects_mount"] is True
        staged = h.config.paths.run_workspace("r1") / "inputs" / "qa-notes.txt"
        assert staged.read_bytes() == b"secret\n"

    def test_a_workload_without_attachments_starts_as_before(self, tmp_path: Path) -> None:
        h = Harness(tmp_path)
        started = self._start(h, _workload())
        assert "inputs/" not in started["outcome"]
        assert started["expects_mount"] is None
