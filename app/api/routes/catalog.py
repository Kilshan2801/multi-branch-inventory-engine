import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status

from app.api.deps import Ctx, DbSession, Paging, require_permission
from app.core.permissions import Permission
from app.models import Branch, Product, User
from app.schemas.catalog import (
    BranchCreate,
    BranchRead,
    BranchUpdate,
    ProductCreate,
    ProductRead,
    ProductUpdate,
)
from app.schemas.common import Page, error_responses
from app.services import catalog_service

branches = APIRouter(prefix="/branches", tags=["Branches"])
products = APIRouter(prefix="/products", tags=["Products"])

CanReadBranches = Annotated[User, Depends(require_permission(Permission.BRANCH_READ))]
CanManageBranches = Annotated[User, Depends(require_permission(Permission.BRANCH_MANAGE))]
CanReadProducts = Annotated[User, Depends(require_permission(Permission.PRODUCT_READ))]
CanManageProducts = Annotated[User, Depends(require_permission(Permission.PRODUCT_MANAGE))]


# --- Branches ---------------------------------------------------------------


@branches.post(
    "", response_model=BranchRead, status_code=status.HTTP_201_CREATED, responses=error_responses(401, 403, 409, 422)
)
def create_branch(body: BranchCreate, db: DbSession, ctx: Ctx, actor: CanManageBranches) -> Branch:
    """Create a branch (admin only)."""
    return catalog_service.create_branch(db, body, actor, ctx)


@branches.get("", response_model=Page[BranchRead], responses=error_responses(401))
def list_branches(db: DbSession, _: CanReadBranches, paging: Paging, is_active: bool | None = None) -> Page[BranchRead]:
    """The branch directory. Visible to every signed-in user;
    stock, orders and users of a branch remain restricted to that branch."""
    items, total = catalog_service.list_branches(db, is_active=is_active, limit=paging.limit, offset=paging.offset)
    return Page[BranchRead](items=items, total=total, limit=paging.limit, offset=paging.offset)


@branches.get("/{branch_id}", response_model=BranchRead, responses=error_responses(401, 404))
def get_branch(branch_id: uuid.UUID, db: DbSession, _: CanReadBranches) -> Branch:
    return catalog_service.get_branch(db, branch_id)


@branches.patch("/{branch_id}", response_model=BranchRead, responses=error_responses(401, 403, 404, 409, 422))
def update_branch(
    branch_id: uuid.UUID, body: BranchUpdate, db: DbSession, ctx: Ctx, actor: CanManageBranches
) -> Branch:
    """Update or deactivate (`is_active: false`) a branch. Admin only.
    Refused with 409 while the branch has reserved stock, open orders or open transfers."""
    return catalog_service.update_branch(db, branch_id, body, actor, ctx)


# --- Products ---------------------------------------------------------------


@products.post(
    "", response_model=ProductRead, status_code=status.HTTP_201_CREATED, responses=error_responses(401, 403, 409, 422)
)
def create_product(body: ProductCreate, db: DbSession, ctx: Ctx, actor: CanManageProducts) -> Product:
    """Add a product to the shared catalogue (admin only). Stock is added per branch via adjustments."""
    return catalog_service.create_product(db, body, actor, ctx)


@products.get("", response_model=Page[ProductRead], responses=error_responses(401))
def list_products(
    db: DbSession,
    _: CanReadProducts,
    paging: Paging,
    search: Annotated[str | None, Query(max_length=100, description="Matches SKU or name")] = None,
    is_active: bool | None = None,
) -> Page[ProductRead]:
    items, total = catalog_service.list_products(
        db, search=search, is_active=is_active, limit=paging.limit, offset=paging.offset
    )
    return Page[ProductRead](items=items, total=total, limit=paging.limit, offset=paging.offset)


@products.get("/{product_id}", response_model=ProductRead, responses=error_responses(401, 404))
def get_product(product_id: uuid.UUID, db: DbSession, _: CanReadProducts) -> Product:
    return catalog_service.get_product(db, product_id)


@products.patch("/{product_id}", response_model=ProductRead, responses=error_responses(401, 403, 404, 422))
def update_product(
    product_id: uuid.UUID, body: ProductUpdate, db: DbSession, ctx: Ctx, actor: CanManageProducts
) -> Product:
    """Update or deactivate (`is_active: false`) a product. Admin only."""
    return catalog_service.update_product(db, product_id, body, actor, ctx)
