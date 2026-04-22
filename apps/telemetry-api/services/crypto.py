"""Envelope-style encryption for secrets stored in Postgres (PR 27).

The actual crypto runs inside the database via pgcrypto's
``pgp_sym_encrypt``/``pgp_sym_decrypt`` so we never ferry plaintext
across the wire as bound parameters the way a pure-Python ``cryptography``
path would. The symmetric key is supplied by the app from environment
(or a KMS sidecar) on every call — it is never persisted.

Rotation is version-aware: every row carries ``key_version``. When you
turn over the master key, bump ``FLOWMIND_DATA_KEY_VERSION``, call
:func:`rewrap_all`, and the table transitions in place.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


def _data_key() -> str:
    key = os.getenv("FLOWMIND_DATA_KEY")
    if not key:
        raise RuntimeError(
            "FLOWMIND_DATA_KEY is unset. Refuse to touch secrets without a key."
        )
    return key


def _key_version() -> int:
    return int(os.getenv("FLOWMIND_DATA_KEY_VERSION", "1"))


@dataclass(frozen=True)
class Secret:
    id: str
    kind: str
    ref: str
    plaintext: str
    key_version: int


async def store_secret(
    db: AsyncSession,
    *,
    tenant_id: str,
    kind: str,
    ref: str,
    plaintext: str,
) -> str:
    """Encrypt ``plaintext`` with the current key and upsert on (tenant, kind, ref).

    The key is bound as a parameter so pgcrypto sees it alongside the
    plaintext — neither is ever string-interpolated into the statement.
    """
    raw = text(
        """
        INSERT INTO encrypted_secrets (tenant_id, secret_kind, secret_ref,
                                       ciphertext, key_version, created_at)
        VALUES (:tenant_id, :kind, :ref,
                pgp_sym_encrypt(:pt, :key), :kv, now())
        ON CONFLICT (tenant_id, secret_kind, secret_ref)
        DO UPDATE SET ciphertext = EXCLUDED.ciphertext,
                      key_version = EXCLUDED.key_version,
                      rotated_at = now()
        RETURNING id::text
        """
    )
    row = (
        await db.execute(
            raw,
            {
                "tenant_id": tenant_id,
                "kind": kind,
                "ref": ref,
                "pt": plaintext,
                "key": _data_key(),
                "kv": _key_version(),
            },
        )
    ).one()
    await db.commit()
    return row[0]


async def read_secret(
    db: AsyncSession,
    *,
    tenant_id: str,
    kind: str,
    ref: str,
) -> Secret | None:
    row = (
        await db.execute(
            text(
                """
                SELECT id::text, secret_kind, secret_ref, key_version,
                       pgp_sym_decrypt(ciphertext, :key) AS plaintext
                FROM encrypted_secrets
                WHERE tenant_id = :tenant_id
                  AND secret_kind = :kind
                  AND secret_ref = :ref
                """
            ),
            {
                "tenant_id": tenant_id,
                "kind": kind,
                "ref": ref,
                "key": _data_key(),
            },
        )
    ).first()
    if row is None:
        return None
    return Secret(
        id=row.id,
        kind=row.secret_kind,
        ref=row.secret_ref,
        plaintext=row.plaintext,
        key_version=row.key_version,
    )


async def rewrap_all(
    db: AsyncSession,
    *,
    old_key: str,
    new_key: str,
    new_version: int,
) -> int:
    """Decrypt every row with ``old_key`` and re-encrypt with ``new_key``.

    Called once during a key rotation. Runs under a single transaction so
    a partial failure leaves the table on the old key.
    """
    result = await db.execute(
        text(
            """
            UPDATE encrypted_secrets
            SET ciphertext = pgp_sym_encrypt(
                    pgp_sym_decrypt(ciphertext, :old_key),
                    :new_key
                ),
                key_version = :new_version,
                rotated_at = now()
            WHERE key_version != :new_version
            """
        ),
        {"old_key": old_key, "new_key": new_key, "new_version": new_version},
    )
    await db.commit()
    return result.rowcount
