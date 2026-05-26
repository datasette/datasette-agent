"""Agent identity CRUD + credential (token) mint / verify.

An *agent identity* is a standing actor (``agent:<slug>``) that a document can
be shared with. It authenticates with its own credential so its tool calls are
authorized only by what has been granted to ``agent:<id>`` (least privilege),
and edits are attributed to the agent rather than the human who launched it.

Credential mechanism (DECISIONS.md):
    Datasette **signed tokens** are the PRIMARY path — they give us expiry and
    tamper resistance for free. We mint a signed ``dsatok_`` token (payload =
    agent actor id + creation time + duration + nonce, signed via
    ``datasette.sign`` under the ``token`` namespace) and hand the plaintext to
    the owner once. To support revocation/rotation (signed tokens are otherwise
    stateless) we also store ``token_hash`` = sha256(token); ``verify_token``
    requires BOTH a valid signature AND a matching stored hash, so rotating the
    token invalidates every previously minted one. The hashed-token machinery
    is deliberately confined to this module so the credential scheme can change
    without touching callers.

    A distinct ``dsatok_`` prefix (vs. Datasette's own ``dstok_``) keeps agent
    credentials clearly separable; ``actor_from_request`` (task 03) routes only
    these through the agent auth path, leaving ``dstok_`` to Datasette core.
"""

import hashlib
import secrets
import time
from datetime import datetime, timezone

from ulid import ULID

from .schema import ensure_tables

# Default token lifetime (seconds). None => no expiry.
DEFAULT_TOKEN_TTL = 365 * 24 * 60 * 60  # one year

AGENT_PREFIX = "agent:"

# Prefix for the agent credential. The payload is signed with Datasette's own
# signing machinery (datasette.sign / unsign, namespace "token") so it IS a
# Datasette signed token; we add a per-mint nonce claim so rotation always
# yields a distinct credential and embed expiry we enforce on verify.
TOKEN_PREFIX = "dsatok_"
TOKEN_NAMESPACE = "token"


def _now():
    return datetime.now(timezone.utc).isoformat()


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _slug_to_id(slug: str) -> str:
    return f"{AGENT_PREFIX}{slug}"


def _mint_token(datasette, identity_id, *, ttl=DEFAULT_TOKEN_TTL):
    """Mint a signed token for an agent identity (PRIMARY credential path).

    The credential is a Datasette **signed token**: the payload (actor id +
    creation time + duration + a random nonce) is signed with
    ``datasette.sign`` under the ``token`` namespace. Signing buys us tamper
    resistance for free; the embedded ``d`` (duration) gives expiry, which we
    enforce in :func:`_verify_signed_token`. The nonce guarantees every mint —
    including rapid rotations — produces a distinct credential, so storing
    ``token_hash`` lets us revoke/rotate.

    Returns the plaintext ``dsatok_…`` token. The caller persists
    ``token_hash`` so a rotated/revoked credential stops verifying.
    """
    payload = {
        "a": identity_id,
        "t": int(time.time()),
        "n": secrets.token_hex(8),
    }
    if ttl is not None:
        payload["d"] = int(ttl)
    return TOKEN_PREFIX + datasette.sign(payload, namespace=TOKEN_NAMESPACE)


def _verify_signed_token(datasette, token):
    """Verify the signature + expiry of an agent token. Returns the decoded
    payload dict (with the ``a`` actor id) or None. No DB access."""
    import itsdangerous

    if not token or not token.startswith(TOKEN_PREFIX):
        return None
    raw = token[len(TOKEN_PREFIX) :]
    try:
        decoded = datasette.unsign(raw, namespace=TOKEN_NAMESPACE)
    except itsdangerous.BadSignature:
        return None
    if not isinstance(decoded, dict) or "a" not in decoded:
        return None
    created = decoded.get("t")
    duration = decoded.get("d")
    if duration is not None and isinstance(created, int):
        if time.time() - created > duration:
            return None
    return decoded


async def create_identity(
    datasette,
    *,
    display_name,
    owner_actor_id,
    slug=None,
    description=None,
    avatar_icon=None,
    avatar_color=None,
    model_id=None,
    system_prompt=None,
    shareable=False,
    ttl=DEFAULT_TOKEN_TTL,
):
    """Create an agent identity and mint its credential.

    Returns a dict with the stored row plus a one-time ``token`` (plaintext);
    the token is NOT stored in the clear — only its sha256 hash is persisted.
    """
    db = datasette.get_internal_database()
    await ensure_tables(db)

    if slug:
        identity_id = _slug_to_id(slug)
    else:
        slug = str(ULID()).lower()
        identity_id = _slug_to_id(slug)

    # Enforce the agent: prefix on the id so it never collides with human
    # actor ids and is detectable by the resolver / auth path.
    if not identity_id.startswith(AGENT_PREFIX):
        identity_id = AGENT_PREFIX + identity_id

    now = _now()
    token = _mint_token(datasette, identity_id, ttl=ttl)
    token_hash = _hash_token(token)

    await db.execute_write(
        """
        INSERT INTO agent_identities
            (id, slug, display_name, description, avatar_icon, avatar_color,
             owner_actor_id, model_id, system_prompt, token_hash, shareable,
             created_at, updated_at, disabled)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
        """,
        [
            identity_id,
            slug,
            display_name,
            description,
            avatar_icon,
            avatar_color,
            owner_actor_id,
            model_id,
            system_prompt,
            token_hash,
            1 if shareable else 0,
            now,
            now,
        ],
    )
    row = await get_identity(datasette, identity_id)
    result = dict(row)
    result["token"] = token  # surfaced once to the owner
    return result


async def get_identity(datasette, identity_id):
    """Return the identity row as a dict, or None."""
    db = datasette.get_internal_database()
    await ensure_tables(db)
    row = (
        await db.execute(
            "SELECT * FROM agent_identities WHERE id = ?", [identity_id]
        )
    ).first()
    return dict(row) if row is not None else None


async def get_identity_by_slug(datasette, slug):
    db = datasette.get_internal_database()
    await ensure_tables(db)
    row = (
        await db.execute(
            "SELECT * FROM agent_identities WHERE slug = ?", [slug]
        )
    ).first()
    return dict(row) if row is not None else None


async def list_identities(datasette, *, owner=None, shareable=None, include_disabled=True):
    """List agent identities, optionally filtered by owner / shareable flag."""
    db = datasette.get_internal_database()
    await ensure_tables(db)
    sql = "SELECT * FROM agent_identities"
    where = []
    params = []
    if owner is not None:
        where.append("owner_actor_id = ?")
        params.append(owner)
    if shareable is not None:
        where.append("shareable = ?")
        params.append(1 if shareable else 0)
    if not include_disabled:
        where.append("disabled = 0")
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY display_name"
    rows = (await db.execute(sql, params)).rows
    return [dict(r) for r in rows]


async def verify_token(datasette, token):
    """Verify an agent credential.

    Two-factor check:
      1. The token must be a valid Datasette signed token (signature + expiry)
         resolving to an ``agent:`` actor id (PRIMARY: signature verification).
      2. Its sha256 hash must match the identity's stored ``token_hash`` (so a
         rotated/revoked token fails even though its signature is still valid).
    The identity must not be disabled.

    Returns the identity row (dict) on success, else None.
    """
    if not token:
        return None

    decoded = _verify_signed_token(datasette, token)
    if not decoded:
        return None
    actor_id = decoded.get("a")
    if not actor_id or not str(actor_id).startswith(AGENT_PREFIX):
        return None

    row = await get_identity(datasette, actor_id)
    if row is None:
        return None
    if row.get("disabled"):
        return None
    if row.get("token_hash") != _hash_token(token):
        return None
    return row


async def rotate_token(datasette, identity_id, *, ttl=DEFAULT_TOKEN_TTL):
    """Mint a fresh credential for an identity, invalidating the previous one.

    Returns the new plaintext token, or None if the identity does not exist.
    """
    row = await get_identity(datasette, identity_id)
    if row is None:
        return None
    db = datasette.get_internal_database()
    token = _mint_token(datasette, identity_id, ttl=ttl)
    await db.execute_write(
        "UPDATE agent_identities SET token_hash = ?, updated_at = ? WHERE id = ?",
        [_hash_token(token), _now(), identity_id],
    )
    return token


async def disable_identity(datasette, identity_id, disabled=True):
    """Soft-disable (or re-enable) an identity. A disabled identity's token no
    longer authenticates (see ``verify_token``)."""
    db = datasette.get_internal_database()
    await ensure_tables(db)
    await db.execute_write(
        "UPDATE agent_identities SET disabled = ?, updated_at = ? WHERE id = ?",
        [1 if disabled else 0, _now(), identity_id],
    )


async def update_identity(datasette, identity_id, **fields):
    """Update mutable identity fields (display_name, description, avatar_*,
    model_id, system_prompt, shareable). Ignores unknown / immutable keys."""
    allowed = {
        "display_name",
        "description",
        "avatar_icon",
        "avatar_color",
        "model_id",
        "system_prompt",
        "shareable",
    }
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        return
    if "shareable" in updates:
        updates["shareable"] = 1 if updates["shareable"] else 0
    db = datasette.get_internal_database()
    await ensure_tables(db)
    set_clause = ", ".join(f"{k} = ?" for k in updates)
    params = list(updates.values()) + [_now(), identity_id]
    await db.execute_write(
        f"UPDATE agent_identities SET {set_clause}, updated_at = ? WHERE id = ?",
        params,
    )


async def load_identity_actor(datasette, identity_id):
    """Return the agent actor dict for ``identity_id`` (used by run-as-agent).

    Shape: ``{"id": "agent:…", "display_name": …, "kind": "agent",
    "agent": True}``. Returns None if the identity is missing or disabled.
    """
    row = await get_identity(datasette, identity_id)
    if row is None or row.get("disabled"):
        return None
    return {
        "id": row["id"],
        "display_name": row["display_name"],
        "kind": "agent",
        "agent": True,
    }
