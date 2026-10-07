"""DialogState (P2-09, P6-05): слияние патчей, слоты по сценариям, JSON для хранения и промпта."""

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

    assert state.to_dict() == {"slots": {"gift": {"budget": 3000}}, "facts": ["спешит"]}
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
