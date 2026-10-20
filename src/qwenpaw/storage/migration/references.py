# -*- coding: utf-8 -*-
"""Report checkpoint references against the history actually imported."""

from ..errors import StorageIdentityError


async def check_references(db, tx, store_id, session_id, payload, has_history):
    """Check explicit sequence references without assuming contiguous history.

    Retention can legitimately remove old rows. Missing rows are reported for
    operator review, not silently called complete or automatically repaired.
    """
    agent = payload.get("agent", payload)
    scroll = agent.get("scroll") if isinstance(agent, dict) else None
    if not isinstance(scroll, dict):
        return None
    fields = (
        "persisted_ids",
        "persisted_tcids",
        "seq_by_id",
        "seq_by_tcid",
        "model_turn_seq",
        "leaf_by_id",
    )
    if not any(scroll.get(name) for name in fields):
        return None
    if not has_history:
        raise StorageIdentityError(
            "Checkpoint contains scroll history references but no history "
            "source was provided",
        )
    sequences = set()
    try:
        for span in scroll.get("seq_by_id", {}).values():
            lo, hi = span
            if type(lo) is not int or type(hi) is not int or not 0 < lo <= hi:
                raise ValueError("Invalid sequence range")
            sequences.update((lo, hi))
        sequences.update(scroll.get("seq_by_tcid", {}).values())
        sequences.update(scroll.get("model_turn_seq", {}).values())
        sequences.update(
            value[0] for value in scroll.get("leaf_by_id", {}).values()
        )
        if any(type(seq) is not int or seq < 1 for seq in sequences):
            raise ValueError("Invalid sequence")
    except (TypeError, ValueError, IndexError, AttributeError) as exc:
        raise StorageIdentityError(
            "Invalid scroll history references",
        ) from exc
    missing = []
    for seq in sorted(sequences):
        row = await tx.one(
            f"SELECT seq FROM {db.table('history')} "
            f"WHERE store_id={db.bind(1)} AND session_id={db.bind(2)} "
            f"AND seq={db.bind(3)}",
            store_id,
            session_id,
            seq,
        )
        if row is None:
            missing.append(seq)
    unmapped = sorted(
        set(scroll.get("persisted_ids", []))
        - set(scroll.get("seq_by_id", {})),
    )
    unmapped_tools = sorted(
        set(scroll.get("persisted_tcids", []))
        - set(scroll.get("seq_by_tcid", {})),
    )
    return {
        "session_key": session_id,
        "checked_sequence_endpoints": len(sequences),
        "missing_sequences": missing,
        "unmapped_message_ids": unmapped,
        "unmapped_tool_ids": unmapped_tools,
        "requires_review": bool(missing or unmapped or unmapped_tools),
    }
