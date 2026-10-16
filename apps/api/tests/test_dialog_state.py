"""DialogState (P2-09, P6-05, P6-06): слияние патчей, слоты по сценариям, замена фактов,
JSON для хранения и промпта."""

import pytest

from app.modules.memory.domain.dialog_state import MAX_FACTS
from app.modules.memory.public import DialogState, PendingConfirmation


def test_apply_merges_slots_of_active_scenario_and_appends_unique_lists() -> None:
    state = DialogState(
        slots={"gift": {"budget": 3000, "relation": "сестра"}},
        facts=("аллергия",),
        active_scenario="gift",
    )

    updated = state.apply(
        {
            "slots": {"budget": 5000, "relation": None, "recipient": "мама"},
            "facts": ["аллергия", "любит цитрусы"],
            "shown_entities": ["e_1", "e_1", "e_2"],
            "unknown": "игнорируется",
        }
    )

    assert updated == DialogState(
        slots={"gift": {"budget": 5000, "recipient": "мама"}},
        facts=("аллергия", "любит цитрусы"),
        shown_entities=("e_1", "e_2"),
        active_scenario="gift",
    )
    assert state.slots == {"gift": {"budget": 3000, "relation": "сестра"}}  # исходное то же


def test_forget_facts_replaces_refined_fact() -> None:
    # Уточнённый факт — новой строкой в facts, прежняя — в forget_facts (ADR-0039).
    state = DialogState(facts=("Ищет унисекс-аромат; не любит сладкие.", "аллергия"))

    updated = state.apply(
        {
            "forget_facts": ["Ищет унисекс-аромат; не любит сладкие.", "такого нет"],
            "facts": ["Ищет унисекс-аромат на осень; не любит сладкие ароматы."],
        }
    )

    assert updated.facts == ("аллергия", "Ищет унисекс-аромат на осень; не любит сладкие ароматы.")


def test_facts_over_limit_drop_oldest() -> None:
    state = DialogState(facts=tuple(f"факт {i}" for i in range(MAX_FACTS)))

    updated = state.apply({"facts": ["новый"]})

    assert len(updated.facts) == MAX_FACTS
    assert updated.facts[0] == "факт 1" and updated.facts[-1] == "новый"
    restored = DialogState.from_dict({"facts": [f"f{i}" for i in range(MAX_FACTS + 5)]})
    assert restored.facts == tuple(f"f{i}" for i in range(5, MAX_FACTS + 5))


def test_slots_go_to_scenario_of_patch_and_previous_scenario_keeps_its_own() -> None:
    state = DialogState(slots={"skincare": {"skin_type": "dry"}}, active_scenario="skincare")

    gift = state.apply({"active_scenario": "gift", "slots": {"budget": 6000}})

    assert gift.slots == {"skincare": {"skin_type": "dry"}, "gift": {"budget": 6000}}
    assert gift.active_slots == {"budget": 6000}
    # Возврат к сценарию — его слоты снова активны.
    back = gift.apply({"active_scenario": "skincare"})
    assert back.active_slots == {"skin_type": "dry"}


def test_slots_without_scenario_are_not_written() -> None:
    state = DialogState().apply({"slots": {"budget": 3000}, "facts": ["спешит"]})

    assert state == DialogState(facts=("спешит",))
    assert state.active_slots == {}


def test_removing_last_slot_drops_scenario_entry() -> None:
    state = DialogState(slots={"gift": {"budget": 3000}}, active_scenario="gift")

    assert state.apply({"slots": {"budget": None}}).slots == {}


def test_partial_patch_keeps_other_parts() -> None:
    state = DialogState(slots={"skincare": {"budget": 3000}}, active_scenario="skincare")

    assert state.apply({"facts": ["спешит"]}) == DialogState(
        slots={"skincare": {"budget": 3000}}, facts=("спешит",), active_scenario="skincare"
    )


def test_dict_roundtrip_omits_empty_parts() -> None:
    state = DialogState(slots={"gift": {"budget": 3000}}, facts=("спешит",))

    assert state.to_dict() == {"v": 2, "slots": {"gift": {"budget": 3000}}, "facts": ["спешит"]}
    assert DialogState.from_dict(state.to_dict()) == state
    assert DialogState.from_dict({}).is_empty
    assert DialogState.from_dict(None).to_dict() == {}


def test_flat_slots_saved_before_p6_05_belong_to_active_scenario() -> None:
    saved = {"slots": {"budget": 3000, "concerns": ["acne"]}, "active_scenario": "skincare"}

    state = DialogState.from_dict(saved)

    assert state.slots == {"skincare": {"budget": 3000, "concerns": ["acne"]}}
    assert DialogState.from_dict({"slots": {"budget": 3000}}).is_empty


def test_prompt_dict_has_only_slots_of_active_scenario() -> None:
    pending = {"confirm_id": "cf_1", "tool": "create_lead", "arguments": {}}
    state = DialogState.from_dict(
        {
            "slots": {"skincare": {"skin_type": "dry"}, "gift": {"budget": 6000}},
            "facts": ["сестра любит цветочные"],
            "active_scenario": "gift",
            "pending_confirmation": pending,
        }
    )

    assert state.prompt_dict() == {
        "slots": {"budget": 6000},
        "facts": ["сестра любит цветочные"],
        "active_scenario": "gift",
    }
    assert DialogState(slots={"gift": {"budget": 1}}).prompt_dict() == {}


def test_pending_confirmation_is_replaced_and_cleared_by_patch() -> None:
    pending = {"confirm_id": "cf_1", "tool": "create_lead", "arguments": {"form_key": "c"}}

    state = DialogState(facts=("спешит",)).apply({"pending_confirmation": pending})

    expected = PendingConfirmation("cf_1", "create_lead", {"form_key": "c"})
    assert state.pending_confirmation == expected
    assert state.apply({"facts": ["спешит"]}).pending_confirmation == state.pending_confirmation
    assert state.apply({"pending_confirmation": None}) == DialogState(facts=("спешит",))
    assert DialogState.from_dict(state.to_dict()) == state
    assert state.to_dict()["pending_confirmation"] == pending


# --- версия схемы и повреждённое состояние (REVIEW.md: M-3) ---


def test_stored_state_has_schema_version_but_prompt_does_not() -> None:
    state = DialogState(facts=("спешит",), active_scenario="gift")

    assert state.to_dict()["v"] == 2
    assert "v" not in state.prompt_dict()
    assert DialogState().to_dict() == {}
    assert DialogState.parse(state.to_dict()) == (state, [])


def test_version_2_keeps_object_slot_inside_scenario() -> None:
    """В версии 2 слоты всегда по сценариям: слот-объект не путается с сценарием."""
    saved = {"v": 2, "slots": {"gift": {"address": {"city": "Москва"}}}, "active_scenario": "gift"}

    state, problems = DialogState.parse(saved)

    assert problems == []
    assert state.slots == {"gift": {"address": {"city": "Москва"}}}


def test_version_2_flat_slot_is_a_problem_not_a_migration() -> None:
    state, problems = DialogState.parse(
        {"v": 2, "slots": {"budget": 3000}, "active_scenario": "gift"}
    )

    assert state.slots == {}
    assert problems == ["slots.budget"]


def test_unknown_version_is_read_as_current_with_problem() -> None:
    state, problems = DialogState.parse({"v": 99, "facts": ["спешит"]})

    assert state.facts == ("спешит",)
    assert problems == ["v"]


@pytest.mark.parametrize(
    ("saved", "expected_problems", "kept"),
    [
        ("not a dict", ["state"], DialogState()),
        ({"slots": ["budget"]}, ["slots"], DialogState()),
        ({"facts": "спешит"}, ["facts"], DialogState()),
        ({"facts": ["спешит", 42]}, ["facts"], DialogState(facts=("спешит",))),
        ({"shown_entities": {"e1": 1}}, ["shown_entities"], DialogState()),
        ({"active_scenario": 5}, ["active_scenario"], DialogState()),
        (
            {"pending_confirmation": {"tool": "create_lead"}},
            ["pending_confirmation"],
            DialogState(),
        ),
        (
            {"pending_confirmation": {"confirm_id": "cf", "tool": "t", "arguments": [1]}},
            ["pending_confirmation"],
            DialogState(),
        ),
        ({"pending_confirmation": "cf_1"}, ["pending_confirmation"], DialogState()),
        (
            {"pending_confirmation": {"confirm_id": "cf", "tool": "t", "arguments": []}},
            ["pending_confirmation"],
            DialogState(),
        ),
        ({"v": True, "slots": {"budget": 1}}, ["v", "slots.budget"], DialogState()),
        ({"slots": {"budget": 1}}, ["slots.budget"], DialogState()),
    ],
    ids=[
        "not-mapping",
        "slots-list",
        "facts-string",
        "facts-mixed",
        "shown-dict",
        "scenario-int",
        "pending-no-id",
        "pending-bad-arguments",
        "pending-string",
        "pending-falsy-arguments",
        "version-bool",
        "v1-flat-without-scenario",
    ],
)
def test_damaged_part_is_dropped_others_kept(
    saved: object, expected_problems: list[str], kept: DialogState
) -> None:
    state, problems = DialogState.parse(saved)

    assert problems == expected_problems
    assert state == kept


def test_damage_in_one_part_keeps_the_rest() -> None:
    saved = {
        "v": 2,
        "slots": {"gift": {"budget": 3000}},
        "facts": ["спешит"],
        "active_scenario": "gift",
        "pending_confirmation": {"confirm_id": 7},
    }

    state, problems = DialogState.parse(saved)

    assert problems == ["pending_confirmation"]
    assert state == DialogState(
        slots={"gift": {"budget": 3000}}, facts=("спешит",), active_scenario="gift"
    )
