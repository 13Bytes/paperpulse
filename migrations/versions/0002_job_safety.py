"""Add durable job claim and retry metadata.

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
    # Revision 0001 historically creates the then-current ORM metadata. The guards keep
    # fresh installs safe while still upgrading databases created by the original 0001.
    inspector = sa.inspect(op.get_bind())
    columns = {column["name"] for column in inspector.get_columns("job_runs")}
    with op.batch_alter_table("job_runs") as batch_op:
        if "attempt_count" not in columns:
            batch_op.add_column(
                sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="1")
            )
        if "claim_token" not in columns:
            batch_op.add_column(sa.Column("claim_token", sa.String(length=36), nullable=True))
        if "last_attempt_at" not in columns:
            batch_op.add_column(
                sa.Column("last_attempt_at", sa.DateTime(timezone=True), nullable=True)
            )

    if "last_attempt_at" not in columns:
        op.execute("UPDATE job_runs SET last_attempt_at = started_at WHERE last_attempt_at IS NULL")
        with op.batch_alter_table("job_runs") as batch_op:
            batch_op.alter_column("last_attempt_at", nullable=False)

    indexes = {index["name"] for index in sa.inspect(op.get_bind()).get_indexes("job_runs")}
    if "ix_job_runs_claim_token" not in indexes:
        op.create_index("ix_job_runs_claim_token", "job_runs", ["claim_token"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("job_runs") as batch_op:
        batch_op.drop_index("ix_job_runs_claim_token")
        batch_op.drop_column("last_attempt_at")
        batch_op.drop_column("claim_token")
        batch_op.drop_column("attempt_count")
