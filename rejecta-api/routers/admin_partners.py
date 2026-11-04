"""Admin: partner API clients (ChemLabFlow and other server-to-server integrations).

* A client has a plan (its limits) and scopes (which features it may call).
* Secrets are shown once. Up to two can be active, so a partner can rotate
  without downtime: create a new one, deploy it, then revoke the old one.
* Revoking a secret, disabling the client, or changing its plan or scopes
  invalidates every token it holds (token_version), within a minute at most.
"""

import uuid
from typing import Literal

import asyncpg
from fastapi import APIRouter, Depends, Query, Request, Response, status

from auth import partners
from auth.dependencies import CurrentUser, require_admin
from auth.errors import ApiError
from auth.request_meta import request_meta
from db.pool import get_pool
from db.repos import partners as repo
from routers.admin_common import admin_audit, db_errors, escape_like, not_found
from routers.admin_schemas import (
    AdminPartner,
    AdminPartnerDetail,
    AdminPartnerList,
    NewPartnerSecret,
    PartnerCreate,
    PartnerCreated,
    PartnerSecret,
    PartnerUpdate,
    PlanRef,
)
from routers.me import feature_usage

router = APIRouter(prefix="/admin/partners", tags=["admin"])

_SELECT = """
    SELECT c.*, p.key AS plan_key, p.name AS plan_name
    FROM api_clients c JOIN plans p ON p.id = c.plan_id
"""


async def _partner(conn: asyncpg.Connection, client_uuid: uuid.UUID) -> AdminPartner:
    row = await conn.fetchrow(_SELECT + " WHERE c.id = $1", client_uuid)
    if row is None:
        raise not_found("Partner")
    secrets_ = await repo.list_secrets(conn, client_uuid)
    return AdminPartner(
        id=row["id"], client_id=row["client_id"], name=row["name"], description=row["description"],
        plan=PlanRef(id=row["plan_id"], key=row["plan_key"], name=row["plan_name"]),
        scopes=list(row["scopes"]), status=row["status"], created_at=row["created_at"],
        last_used_at=row["last_used_at"], secrets=[PartnerSecret(**s.__dict__) for s in secrets_],
    )


async def _validate(conn: asyncpg.Connection, plan_id: uuid.UUID | None, scopes: list[str] | None) -> None:
    if plan_id is not None:
        plan_status = await conn.fetchval("SELECT status FROM plans WHERE id = $1", plan_id)
        if plan_status is None:
            raise not_found("Plan")
        if plan_status != "active":
            raise ApiError(400, "plan_archived", "Partners cannot be put on an archived plan.")
    if scopes is not None:
        known = {r["key"] for r in await conn.fetch("SELECT key FROM features")}
        unknown = sorted(set(scopes) - known)
        if unknown:
            raise ApiError(400, "unknown_feature", f"Unknown scope(s): {', '.join(unknown)}.")


async def _issue_secret(conn: asyncpg.Connection, client_uuid: uuid.UUID, client_id: str,
                        created_by: uuid.UUID) -> NewPartnerSecret:
    raw, digest, prefix, last4 = partners.new_secret()
    secret_id = await repo.add_secret(conn, client_uuid, digest, prefix, last4, created_by)
    info = next(s for s in await repo.list_secrets(conn, client_uuid) if s.id == secret_id)
    return NewPartnerSecret(client_id=client_id, client_secret=raw, secret=PartnerSecret(**info.__dict__))


_PARTNER_FILTER = """
    WHERE ($1::text IS NULL OR name ILIKE $1 OR client_id ILIKE $1)
      AND ($2::text IS NULL OR status = $2)
"""


@router.get("", response_model=AdminPartnerList)
async def list_partners(
    search: str | None = Query(default=None, max_length=120),
    partner_status: Literal["active", "disabled"] | None = Query(default=None, alias="status"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=25, ge=1, le=100),
    _: CurrentUser = Depends(require_admin),
) -> AdminPartnerList:
    pattern = f"%{escape_like(search.strip())}%" if search and search.strip() else None
    async with get_pool().acquire() as conn:
        ids = await conn.fetch(
            f"SELECT id FROM api_clients {_PARTNER_FILTER} ORDER BY status, name, id LIMIT $3 OFFSET $4",
            pattern, partner_status, page_size, (page - 1) * page_size,
        )
        total = await conn.fetchval(f"SELECT count(*) FROM api_clients {_PARTNER_FILTER}", pattern, partner_status)
        items = [await _partner(conn, r["id"]) for r in ids]
    return AdminPartnerList(items=items, total=total, page=page, page_size=page_size)


@router.post("", response_model=PartnerCreated, status_code=201)
async def create_partner(body: PartnerCreate, request: Request, current: CurrentUser = Depends(require_admin)
                         ) -> PartnerCreated:
    meta = request_meta(request)
    scopes = sorted(set(body.scopes))
    async with get_pool().acquire() as conn:
        await _validate(conn, body.plan_id, scopes)
        with db_errors():
            async with conn.transaction():
                client_id = partners.new_client_id()
                client_uuid = await conn.fetchval(
                    """
                    INSERT INTO api_clients (client_id, name, description, plan_id, scopes, created_by)
                    VALUES ($1, $2, $3, $4, $5, $6) RETURNING id
                    """,
                    client_id, body.name.strip(), body.description, body.plan_id, scopes, current.id,
                )
                credentials = await _issue_secret(conn, client_uuid, client_id, current.id)
                await admin_audit(conn, current, meta, "admin.partner_created", target_type="api_client",
                                  target_id=client_uuid, after={"name": body.name, "plan_id": str(body.plan_id),
                                                                "scopes": scopes})
        return PartnerCreated(partner=await _partner(conn, client_uuid), credentials=credentials)


@router.get("/{client_uuid}", response_model=AdminPartnerDetail)
async def get_partner(client_uuid: uuid.UUID, _: CurrentUser = Depends(require_admin)) -> AdminPartnerDetail:
    async with get_pool().acquire() as conn:
        partner = await _partner(conn, client_uuid)
        usage = await feature_usage(conn, subject_type="client", subject_id=client_uuid,
                                    plan_id=partner.plan.id, user_id=None)
    return AdminPartnerDetail(**partner.model_dump(), features=usage)


@router.patch("/{client_uuid}", response_model=AdminPartner)
async def update_partner(client_uuid: uuid.UUID, body: PartnerUpdate, request: Request,
                         current: CurrentUser = Depends(require_admin)) -> AdminPartner:
    meta = request_meta(request)
    changes = body.model_dump(exclude_none=True)
    if not changes:
        raise ApiError(400, "nothing_to_change", "No changes given.")
    scopes = sorted(set(body.scopes)) if body.scopes is not None else None
    async with get_pool().acquire() as conn:
        before = await _partner(conn, client_uuid)
        await _validate(conn, body.plan_id, scopes)
        with db_errors():
            async with conn.transaction():
                await conn.execute(
                    """
                    UPDATE api_clients SET name = COALESCE($2, name), description = COALESCE($3, description),
                        plan_id = COALESCE($4, plan_id), scopes = COALESCE($5, scopes), status = COALESCE($6, status)
                    WHERE id = $1
                    """,
                    client_uuid, body.name, body.description, body.plan_id, scopes, body.status,
                )
                await admin_audit(conn, current, meta, "admin.partner_updated", target_type="api_client",
                                  target_id=client_uuid, before=before.model_dump(include=set(changes) | {"plan"},
                                                                                 mode="json"),
                                  after={**changes, "scopes": scopes} if scopes else changes)
        partners.invalidate(client_uuid)
        return await _partner(conn, client_uuid)


@router.post("/{client_uuid}/secrets", response_model=NewPartnerSecret, status_code=201)
async def add_secret(client_uuid: uuid.UUID, request: Request, response: Response,
                     current: CurrentUser = Depends(require_admin)) -> NewPartnerSecret:
    """Issue an extra secret for rotation (max 2 active). Shown once."""
    meta = request_meta(request)
    async with get_pool().acquire() as conn:
        partner = await _partner(conn, client_uuid)
        async with conn.transaction():
            await conn.execute("SELECT 1 FROM api_clients WHERE id = $1 FOR UPDATE", client_uuid)
            if await repo.active_secret_count(conn, client_uuid) >= partners.MAX_ACTIVE_SECRETS:
                raise ApiError(409, "too_many_secrets", "Revoke an old secret before creating another.")
            created = await _issue_secret(conn, client_uuid, partner.client_id, current.id)
            await admin_audit(conn, current, meta, "admin.partner_secret_created", target_type="api_client",
                              target_id=client_uuid, after={"secret_id": str(created.secret.id)})
    response.headers["Cache-Control"] = "no-store"
    return created


@router.delete("/{client_uuid}/secrets/{secret_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_secret(client_uuid: uuid.UUID, secret_id: uuid.UUID, request: Request,
                        current: CurrentUser = Depends(require_admin)) -> Response:
    meta = request_meta(request)
    async with get_pool().acquire() as conn:
        async with conn.transaction():
            revoked = await conn.fetchval(
                """
                UPDATE api_client_secrets SET revoked_at = now()
                WHERE id = $1 AND api_client_id = $2 AND revoked_at IS NULL RETURNING 1
                """,
                secret_id, client_uuid,
            )
            if not revoked:
                raise not_found("Active secret")
            # A revoked secret may have leaked: end every token the client holds.
            await conn.execute("UPDATE api_clients SET token_version = token_version + 1 WHERE id = $1", client_uuid)
            await admin_audit(conn, current, meta, "admin.partner_secret_revoked", target_type="api_client",
                              target_id=client_uuid, after={"secret_id": str(secret_id)})
    partners.invalidate(client_uuid)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
