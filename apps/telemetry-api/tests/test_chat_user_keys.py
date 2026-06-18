"""Chat-user key resolver tests (PR 31)."""
from __future__ import annotations

import asyncio
import uuid

from services.chat_user_keys import KEY_PLAINTEXT_PREFIX


def test_user_key_prefix_distinct_from_admin_keys():
    """Operators eyeball the prefix to know which key they're looking
    at. fm_ for admin-issued keys; fmu_ for per-user gateway keys.
    Don't accidentally collapse them."""
    assert KEY_PLAINTEXT_PREFIX == "fmu_"


def test_user_key_prefix_never_matches_admin_fm_prefix():
    assert not KEY_PLAINTEXT_PREFIX.startswith("fm_")


# Round-trip integration with a real DB happens in the migration smoke
# test + a future e2e suite. Pure-function coverage on this module is
# limited because every meaningful path touches the DB; we don't fake
# the SQLAlchemy AsyncSession here because that would test the mock,
# not the logic.
