"""`[planning]`, its per-repository overrides, the planner's model and the
level labels (#2343)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from sbxloop.config import LEVEL_LABELS, AgentModels, Config, PlanningConfig
from sbxloop.vcs.github.labels import lifecycle_specs


def _config(**repo: object) -> Config:
    return Config.model_validate({"vcs": {"repos": [{"repo": "o/r", **repo}, {"repo": "o/s"}]}})


class TestDefaults:
    def test_on_with_the_spikes_caps(self) -> None:
        planning = Config().planning
        assert planning == PlanningConfig(
            enabled=True,
            max_epics_per_initiative=8,
            max_tasks_per_epic=12,
            max_questions=5,
            close_completed=True,
        )

    @pytest.mark.parametrize(
        ("key", "value"),
        [
            ("max_epics_per_initiative", 0),
            ("max_tasks_per_epic", 51),
            ("max_questions", -1),
            ("max_questions", 11),
        ],
    )
    def test_bounds_are_refused_at_load(self, key: str, value: int) -> None:
        with pytest.raises(ValidationError):
            Config.model_validate({"planning": {key: value}})


class TestPerRepository:
    def test_a_repository_narrows_what_it_sets_and_inherits_the_rest(self) -> None:
        config = _config(planning={"max_tasks_per_epic": 4, "close_completed": False})
        here = config.planning_for("o/r")
        assert here.max_tasks_per_epic == 4 and here.close_completed is False
        assert here.max_epics_per_initiative == 8 and here.enabled is True
        assert config.planning_for("o/s") == config.planning

    def test_an_unset_global_value_is_what_a_repository_inherits(self) -> None:
        config = Config.model_validate(
            {"planning": {"max_questions": 2}, "vcs": {"repos": [{"repo": "o/r"}]}}
        )
        assert config.planning_for("o/r").max_questions == 2


class TestThePlannersModel:
    def test_plan_is_a_phase_key_globally_and_per_repository(self) -> None:
        assert AgentModels(plan="opus").plan == "opus"
        config = _config(agent_models={"plan": "haiku"})
        entry = config.find_repo("o/r")
        assert entry is not None and entry.agent_models.plan == "haiku"

    def test_a_blank_plan_model_is_refused(self) -> None:
        with pytest.raises(ValidationError):
            AgentModels(plan="  ")


class TestLevelLabels:
    def test_the_label_set_carries_the_levels_where_planning_is_on(self) -> None:
        config = _config(planning={"enabled": False})
        assert config.labels_for("o/s").levels == LEVEL_LABELS
        assert config.labels_for("o/r").levels == {}

    def test_label_sync_creates_them_with_the_lifecycle_labels(self) -> None:
        config = _config(planning={"enabled": False})
        on = {spec.name: spec.kind for spec in lifecycle_specs(config.labels_for("o/s"))}
        assert {on["sbx:initiative"], on["sbx:epic"], on["sbx:task"]} == {
            "initiative",
            "epic",
            "task",
        }
        off = {spec.name for spec in lifecycle_specs(config.labels_for("o/r"))}
        assert not off & set(LEVEL_LABELS.values())
