"""DialogState (P2-09, P6-05, P6-06): слияние патчей, слоты по сценариям, замена фактов,
JSON для хранения и промпта; лимит показанных сущностей и срок жизни подтверждения
(P9-01ad)."""

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from app.modules.memory.public import (
    CONFIRMATION_TTL,
    MAX_FACTS,
    MAX_SHOWN_ENTITIES,
    DialogState,
    PendingConfirmation,
)

NOW = datetime(2026, 10, 16, 12, 0, tzinfo=UTC)


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


def test_facts_over_limit_are_trimmed_on_load() -> None:
    saved = {"v": 2, "facts": [f"факт {i}" for i in range(MAX_FACTS + 5)]}

    state, problems = DialogState.parse(saved)

    assert problems == []
    assert state.facts == tuple(f"факт {i}" for i in range(5, MAX_FACTS + 5))


# --- показанные сущности (REVIEW.md: M-2) ---


def test_shown_entities_over_limit_drop_oldest() -> None:
    state = DialogState()
    for carousel in range(5):
        state = state.apply({"shown_entities": [f"e_{carousel}_{i}" for i in range(8)]})

    assert len(state.shown_entities) == MAX_SHOWN_ENTITIES == 24
    assert state.shown_entities[0] == "e_2_0"
    assert state.shown_entities[-1] == "e_4_7"


def test_shown_again_entity_becomes_recent() -> None:
    state = DialogState(shown_entities=("e_1", "e_2", "e_3"))

    assert state.apply({"shown_entities": ["e_1", "e_4"]}).shown_entities == (
        "e_2",
        "e_3",
        "e_1",
        "e_4",
    )


def test_shown_entities_over_limit_are_trimmed_on_load() -> None:
    saved = {"v": 2, "shown_entities": [f"e_{i}" for i in range(80)]}

    state, problems = DialogState.parse(saved)

    assert problems == []
    assert state.shown_entities == tuple(f"e_{i}" for i in range(56, 80))
    assert state.prompt_dict() == {"shown_entities": list(state.shown_entities)}


# --- срок жизни подтверждения (REVIEW.md: M-4) ---


def _pending(**extra: Any) -> dict[str, Any]:
    return {"confirm_id": "cf_1", "tool": "create_lead", "arguments": {"form_key": "c"}, **extra}


def test_pending_confirmation_keeps_created_at_in_json() -> None:
    created = NOW.isoformat()

    state = DialogState().apply({"pending_confirmation": _pending(created_at=created)})

    assert state.pending_confirmation is not None
    assert state.pending_confirmation.created_at == NOW
    assert state.to_dict()["pending_confirmation"] == _pending(created_at=created)
    assert DialogState.from_dict(state.to_dict()) == state


def test_pending_confirmation_expires_after_ttl() -> None:
    pending = PendingConfirmation("cf_1", "create_lead", {}, created_at=NOW)

    assert not pending.expired(NOW)
    assert not pending.expired(NOW + CONFIRMATION_TTL - timedelta(seconds=1))
    assert pending.expired(NOW + CONFIRMATION_TTL)
    # Из будущего (часы сбиты) — срок не продлевается.
    assert not pending.expired(NOW - timedelta(seconds=30))
    assert pending.expired(NOW - timedelta(minutes=5))


def test_pending_confirmation_requires_aware_created_at() -> None:
    with pytest.raises(ValueError):
        PendingConfirmation("cf_1", "create_lead", {}, created_at=datetime(2026, 10, 16, 12))


def test_registry_timestamp_survives_roundtrip_unchanged() -> None:
    # chat сравнивает ожидание до и после хода по JSON (keep_pending_confirmation):
    # формат Registry — datetime.now(UTC).isoformat() — должен вернуться той же строкой.
    for created in (NOW, NOW.replace(microsecond=123456)):
        saved = {"v": 2, "pending_confirmation": _pending(created_at=created.isoformat())}

        assert DialogState.from_dict(saved).to_dict() == saved
    assert timedelta(hours=1) == CONFIRMATION_TTL


def test_pending_confirmation_without_created_at_is_expired() -> None:
    # Сохранено до срока жизни (P9-01ad): возраст неизвестен — может быть несколько дней.
    state, problems = DialogState.parse({"v": 2, "pending_confirmation": _pending()})

    assert problems == []
    assert state.pending_confirmation is not None
    assert state.pending_confirmation.expired(NOW)


def test_pending_confirmation_without_arguments() -> None:
    state, problems = DialogState.parse(
        {"pending_confirmation": {"confirm_id": "cf_1", "tool": "create_lead"}}
    )

    assert problems == []
    assert state.pending_confirmation == PendingConfirmation("cf_1", "create_lead", {})
    assert state.to_dict()["pending_confirmation"]["arguments"] == {}


@pytest.mark.parametrize(
    "created_at",
    ["вчера", "2026-10-16T12:00:00", 1760000000, {"at": "x"}],
    ids=["not-iso", "naive", "number", "object"],
)
def test_pending_confirmation_with_bad_created_at_is_dropped(created_at: object) -> None:
    saved = {"v": 2, "facts": ["спешит"], "pending_confirmation": _pending(created_at=created_at)}

    state, problems = DialogState.parse(saved)

    assert problems == ["pending_confirmation"]
    assert state == DialogState(facts=("спешит",))


# --- неизменяемость (REVIEW.md: M-8) ---


def test_state_does_not_share_dicts_with_caller() -> None:
    slots = {"gift": {"budget": 3000}}
    arguments = {"form_key": "c"}
    state = DialogState(
        slots=slots,
        pending_confirmation=PendingConfirmation("cf_1", "create_lead", arguments, NOW),
    )

    slots["gift"]["budget"] = 1
    arguments["form_key"] = "x"

    assert state.slots == {"gift": {"budget": 3000}}
    assert state.pending_confirmation is not None
    assert state.pending_confirmation.arguments == {"form_key": "c"}
    with pytest.raises(TypeError):
        state.slots["gift"]["budget"] = 1  # type: ignore[index]


def test_state_is_explicitly_unhashable() -> None:
    with pytest.raises(TypeError, match="unhashable"):
        hash(DialogState())
    with pytest.raises(TypeError, match="unhashable"):
        hash(PendingConfirmation("cf_1", "create_lead", {}))
