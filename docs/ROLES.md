# Roles and permissions

Access is checked in two independent steps on every protected request.

1. **Permission**: does the user's *role* allow this kind of action at all?
   Defined in one place: [`app/core/permissions.py`](../app/core/permissions.py).
2. **Branch scope**: may this *user* act on *this branch*?
   Admins: any branch. Branch managers and staff: only the branch they are assigned to.

Failing either check returns `403` and writes an `access.denied` entry to the audit log.
Roles are re-read from the database on every request (not trusted from the JWT),
so a role change or deactivation takes effect immediately.

## The three roles

| Role | Scope | In one line |
|---|---|---|
| **Admin** | All branches | Runs the system: users, branches, products, and anything a manager can do anywhere. |
| **Branch Manager** | Own branch | Runs one branch: corrects stock, approves and dispatches transfers, sees its audit trail. |
| **Staff** | Own branch | Serves customers: creates and progresses orders, receives incoming transfers. |

## Permission matrix

| Permission | Admin | Branch Manager | Staff | Notes |
|---|:-:|:-:|:-:|---|
| `branch:read` | ✅ | ✅ own | ✅ own | |
| `branch:manage` | ✅ | ❌ | ❌ | create, update, deactivate |
| `product:read` | ✅ | ✅ | ✅ | the catalogue is shared by all branches |
| `product:manage` | ✅ | ❌ | ❌ | create, update, deactivate |
| `user:read` | ✅ | ✅ own | ❌ | |
| `user:manage` | ✅ | ❌ | ❌ | create, update, deactivate |
| `inventory:read` | ✅ | ✅ own | ✅ own | stock levels and ledger |
| `inventory:adjust` | ✅ | ✅ own | ❌ | manual corrections / goods received; reason is mandatory |
| `order:read` | ✅ | ✅ own | ✅ own | |
| `order:create` | ✅ | ✅ own | ✅ own | |
| `order:confirm` | ✅ | ✅ own | ✅ own | |
| `order:fulfill` | ✅ | ✅ own | ✅ own | |
| `order:cancel` | ✅ | ✅ own | ✅ own | releases the reservation |
| `transfer:read` | ✅ | ✅ source or destination | ✅ source or destination | |
| `transfer:request` | ✅ | ✅ source or destination | ❌ | |
| `transfer:approve` | ✅ | ✅ source | ❌ | approve or reject |
| `transfer:dispatch` | ✅ | ✅ source | ❌ | stock leaves the source |
| `transfer:receive` | ✅ | ✅ destination | ✅ destination | stock arrives at the destination |
| `transfer:cancel` | ✅ | ✅ source or destination | ❌ | only before dispatch |
| `audit:read` | ✅ | ✅ own | ❌ | |

## Assumptions behind these choices

- **Staff cannot adjust stock.** A manual adjustment changes inventory without a
  customer order or transfer behind it, which is the easiest way to hide loss or theft.
  Keeping it to managers creates separation of duties.
- **Approving a transfer belongs to the source branch**, because it is the source's stock that leaves.
- **Receiving belongs to the destination branch**, and staff may do it: they are the
  people physically unpacking the delivery.
- **Only admins manage users.** A manager creating accounts could grant access
  to their own branch without oversight. (Possible future extension: managers may
  create *staff* in their own branch.)
- **Nothing is ever hard-deleted.** Users, branches and products are deactivated, because the
  stock ledger and audit trail must keep pointing at them.
- **A deactivated branch** blocks its managers and staff from signing in.
