"""DialogState (P2-09): слияние патчей и JSON для хранения и промпта."""

from app.modules.memory.public import DialogState, PendingConfirmation


def test_apply_merges_slots_and_appends_unique_lists() -> None:
    state = DialogState(slots={"budget": 3000, "skin": "сухая"}, facts=("аллергия",))

    updated = state.apply(
        {
            "slots": {"budget": 5000, "skin": None, "recipient": "мама"},
            "facts": ["аллергия", "любит цитрусы"],
            "shown_entities": ["e_1", "e_1", "e_2"],
            "active_scenario": "gift_selection",
            "unknown": "игнорируется",
        }
    )

    assert updated == DialogState(
        slots={"budget": 5000, "recipient": "мама"},
        facts=("аллергия", "любит цитрусы"),
        shown_entities=("e_1", "e_2"),
        active_scenario="gift_selection",
    )
    assert state.slots == {"budget": 3000, "skin": "сухая"}  # исходное не меняется


def test_partial_patch_keeps_other_parts() -> None:
    state = DialogState(slots={"budget": 3000}, active_scenario="skincare")

    assert state.apply({"facts": ["спешит"]}) == DialogState(
        slots={"budget": 3000}, facts=("спешит",), active_scenario="skincare"
    )


def test_dict_roundtrip_omits_empty_parts() -> None:
    state = DialogState(slots={"budget": 3000}, facts=("спешит",))

    assert state.to_dict() == {"slots": {"budget": 3000}, "facts": ["спешит"]}
    assert DialogState.from_dict(state.to_dict()) == state
    assert DialogState.from_dict({}).is_empty
    assert DialogState.from_dict(None).to_dict() == {}


def test_pending_confirmation_is_replaced_and_cleared_by_patch() -> None:
    pending = {"confirm_id": "cf_1", "tool": "create_lead", "arguments": {"form_key": "c"}}

    state = DialogState(slots={"budget": 3000}).apply({"pending_confirmation": pending})

    expected = PendingConfirmation("cf_1", "create_lead", {"form_key": "c"})
    assert state.pending_confirmation == expected
    assert state.apply({"facts": ["спешит"]}).pending_confirmation == state.pending_confirmation
    assert state.apply({"pending_confirmation": None}) == DialogState(slots={"budget": 3000})
    assert DialogState.from_dict(state.to_dict()) == state
    assert state.to_dict()["pending_confirmation"] == pending
