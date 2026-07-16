"""Initial multi-user Paperpulse schema.

Revision ID: 0001
"""

import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

metadata = sa.MetaData()

users = sa.Table(
    "users",
    metadata,
    sa.Column("id", sa.Integer(), primary_key=True),
    sa.Column("email", sa.String(320), nullable=False, unique=True, index=True),
    sa.Column("is_admin", sa.Boolean(), nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
)
magic_links = sa.Table(
    "magic_links",
    metadata,
    sa.Column("id", sa.Integer(), primary_key=True),
    sa.Column("email", sa.String(320), nullable=False, index=True),
    sa.Column("token_hash", sa.String(64), nullable=False, unique=True, index=True),
    sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("used_at", sa.DateTime(timezone=True)),
    sa.Column("requested_ip", sa.String(64)),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
)
auth_sessions = sa.Table(
    "auth_sessions",
    metadata,
    sa.Column("id", sa.Integer(), primary_key=True),
    sa.Column("user_id", sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True),
    sa.Column("token_hash", sa.String(64), nullable=False, unique=True, index=True),
    sa.Column("csrf_token", sa.String(64), nullable=False),
    sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
)
topics = sa.Table(
    "topics",
    metadata,
    sa.Column("id", sa.Integer(), primary_key=True),
    sa.Column("slug", sa.String(100), nullable=False, unique=True, index=True),
    sa.Column("name", sa.String(100), nullable=False),
    sa.Column("name_normalized", sa.String(100), nullable=False, unique=True),
    sa.Column("description", sa.Text(), nullable=False),
    sa.Column("status", sa.String(16), nullable=False, index=True),
    sa.Column("created_by_id", sa.ForeignKey("users.id", ondelete="SET NULL")),
    sa.Column("reviewed_by_id", sa.ForeignKey("users.id", ondelete="SET NULL")),
    sa.Column("review_reason", sa.Text()),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("reviewed_at", sa.DateTime(timezone=True)),
)
topic_terms = sa.Table(
    "topic_terms",
    metadata,
    sa.Column("id", sa.Integer(), primary_key=True),
    sa.Column(
        "topic_id", sa.ForeignKey("topics.id", ondelete="CASCADE"), nullable=False, index=True
    ),
    sa.Column("kind", sa.String(16), nullable=False),
    sa.Column("value", sa.String(120), nullable=False),
    sa.Column("value_normalized", sa.String(120), nullable=False),
    sa.Column("status", sa.String(16), nullable=False, index=True),
    sa.Column("created_by_id", sa.ForeignKey("users.id", ondelete="SET NULL")),
    sa.Column("reviewed_by_id", sa.ForeignKey("users.id", ondelete="SET NULL")),
    sa.Column("review_reason", sa.Text()),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("reviewed_at", sa.DateTime(timezone=True)),
    sa.CheckConstraint("kind IN ('category', 'keyword')"),
    sa.UniqueConstraint("topic_id", "kind", "value_normalized"),
)
subscriptions = sa.Table(
    "subscriptions",
    metadata,
    sa.Column("user_id", sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
    sa.Column("topic_id", sa.ForeignKey("topics.id", ondelete="CASCADE"), primary_key=True),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
)
reports = sa.Table(
    "reports",
    metadata,
    sa.Column("id", sa.Integer(), primary_key=True),
    sa.Column(
        "topic_id", sa.ForeignKey("topics.id", ondelete="CASCADE"), nullable=False, index=True
    ),
    sa.Column("kind", sa.String(16), nullable=False, index=True),
    sa.Column("title", sa.String(240), nullable=False),
    sa.Column("period_start", sa.Date(), nullable=False),
    sa.Column("period_end", sa.Date(), nullable=False),
    sa.Column("content_markdown", sa.Text(), nullable=False),
    sa.Column("num_papers", sa.Integer(), nullable=False),
    sa.Column("published_at", sa.DateTime(timezone=True), nullable=False, index=True),
    sa.CheckConstraint("kind IN ('daily', 'weekly')"),
    sa.UniqueConstraint("topic_id", "kind", "period_start", "period_end"),
)
report_sources = sa.Table(
    "report_sources",
    metadata,
    sa.Column(
        "weekly_report_id", sa.ForeignKey("reports.id", ondelete="CASCADE"), primary_key=True
    ),
    sa.Column("daily_report_id", sa.ForeignKey("reports.id", ondelete="CASCADE"), primary_key=True),
)
job_runs = sa.Table(
    "job_runs",
    metadata,
    sa.Column("id", sa.Integer(), primary_key=True),
    sa.Column(
        "topic_id", sa.ForeignKey("topics.id", ondelete="CASCADE"), nullable=False, index=True
    ),
    sa.Column("kind", sa.String(16), nullable=False),
    sa.Column("period_start", sa.Date(), nullable=False),
    sa.Column("period_end", sa.Date(), nullable=False),
    sa.Column("status", sa.String(16), nullable=False, index=True),
    sa.Column("message", sa.Text()),
    sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("finished_at", sa.DateTime(timezone=True)),
    sa.UniqueConstraint("topic_id", "kind", "period_start", "period_end"),
)
legacy_redirects = sa.Table(
    "legacy_redirects",
    metadata,
    sa.Column("id", sa.Integer(), primary_key=True),
    sa.Column("old_path", sa.String(300), nullable=False, unique=True, index=True),
    sa.Column("report_id", sa.ForeignKey("reports.id", ondelete="CASCADE"), nullable=False),
)


def upgrade() -> None:
    metadata.create_all(bind=op.get_bind())


def downgrade() -> None:
    metadata.drop_all(bind=op.get_bind())
