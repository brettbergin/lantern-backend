"""When a mark on work stops standing.

A dismissal acknowledges the state the work was in when a person looked at
it. Work that moves again — retried and failed a second time, resumed and
parked on a new question — is a new thing to look at, so the mark goes the
moment the state changes, whichever code changed it: this release's, a
rolled-back one's, or the CLI in another process. That is why the rule is
a trigger and not a line in the store.

* An item whose ``state`` changes loses its ``dismissed`` mark, and its
  ``deleted`` mark too unless the new state is one a finished item rests in
  (re-admitted work is visible again).
* An item row that is deleted takes its marks with it: a later item may
  reuse the id.
* A run whose ``state`` changes loses the ``dismissed`` mark kept on the run
  itself. Marks kept on an item are the item trigger's alone — an operator's
  abandon cancels the run *after* the item settled, and that must not bring
  the alert back.

The DDL is defined once here for a database built from the metadata;
Alembic revision 0049 carries its own frozen copy for a deployed one, and a
test holds the two to the same text.
"""

from __future__ import annotations

from sqlalchemy import DDL, event

from lantern.db.base import Base

#: The states a finished item rests in (``TERMINAL_ITEM_STATES`` in the
#: daemon store, which a test holds this to): a ``deleted`` mark survives a
#: move between them and nothing else.
RESTING_ITEM_STATES: tuple[str, ...] = ("blocked", "cancelled", "done", "failed")

_RESTING = ", ".join(f"'{state}'" for state in RESTING_ITEM_STATES)

#: Trigger name → its DDL. Fixed identifiers only; nothing here is input.
TRIGGERS: dict[str, str] = {
    "work_marks_item_state": (
        "CREATE TRIGGER IF NOT EXISTS work_marks_item_state "
        "AFTER UPDATE OF state ON daemon_work_items "
        "WHEN NEW.state IS NOT OLD.state "
        "BEGIN DELETE FROM daemon_work_marks "
        "WHERE subject_kind = 'item' AND subject_key = NEW.item_id "
        f"AND (mark = 'dismissed' OR NEW.state NOT IN ({_RESTING})); END"  # nosec B608
    ),
    "work_marks_item_delete": (
        "CREATE TRIGGER IF NOT EXISTS work_marks_item_delete "
        "AFTER DELETE ON daemon_work_items "
        "BEGIN DELETE FROM daemon_work_marks "
        "WHERE subject_kind = 'item' AND subject_key = OLD.item_id; END"
    ),
    "work_marks_run_state": (
        "CREATE TRIGGER IF NOT EXISTS work_marks_run_state "
        "AFTER UPDATE OF state ON runs "
        "WHEN NEW.state IS NOT OLD.state "
        "BEGIN DELETE FROM daemon_work_marks "
        "WHERE subject_kind = 'run' AND subject_key = NEW.run_id "
        "AND mark = 'dismissed'; END"
    ),
}

# On the metadata rather than one table: each trigger names two tables, and
# both must exist before it does.
for _ddl in TRIGGERS.values():
    event.listen(
        Base.metadata,
        "after_create",
        DDL(_ddl).execute_if(dialect="sqlite"),  # type: ignore[no-untyped-call]
    )
