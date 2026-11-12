"""Refund idempotency key; case-insensitive ticket type names

Revision ID: 0002
Revises: 0001
"""

import sqlalchemy as sa

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("refunds", sa.Column("idempotency_key", sa.String(length=100), nullable=True))
    op.create_unique_constraint("uq_refunds_purchase_idempotency_key", "refunds", ["purchase_id", "idempotency_key"])

    # "VIP" and "vip" are now the same ticket type within an event.
    op.drop_constraint("uq_ticket_types_event_name", "ticket_types", type_="unique")
    op.create_index("uq_ticket_types_event_name", "ticket_types", ["event_id", sa.text("lower(name)")], unique=True)


def downgrade() -> None:
    op.drop_index("uq_ticket_types_event_name", table_name="ticket_types")
    op.create_unique_constraint("uq_ticket_types_event_name", "ticket_types", ["event_id", "name"])
    op.drop_constraint("uq_refunds_purchase_idempotency_key", "refunds", type_="unique")
    op.drop_column("refunds", "idempotency_key")
