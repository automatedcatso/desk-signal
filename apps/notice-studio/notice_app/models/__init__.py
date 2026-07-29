"""Database models for the Notice Studio."""
from datetime import datetime

from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()


class ImportSession(db.Model):
    """A single Excel import. One active session at a time per the workflow."""

    __tablename__ = "import_session"

    id = db.Column(db.Integer, primary_key=True)
    filename = db.Column(db.String(255), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(
        db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )
    total_records = db.Column(db.Integer, default=0)

    # Global signatory settings chosen on the main page.
    sender_name = db.Column(db.String(255), default="")
    sender_role = db.Column(db.String(255), default="No signature")

    records = db.relationship(
        "Record", backref="session", cascade="all, delete-orphan", lazy="dynamic"
    )


class Record(db.Model):
    """One notice = one Excel row."""

    __tablename__ = "record"

    id = db.Column(db.Integer, primary_key=True)
    session_id = db.Column(
        db.Integer, db.ForeignKey("import_session.id"), nullable=False, index=True
    )
    row_index = db.Column(db.Integer, nullable=False)

    acknowledgement_no = db.Column(db.String(255), default="")
    bank = db.Column(db.String(255), default="", index=True)
    layer = db.Column(db.String(255), default="", index=True)
    account_no = db.Column(db.String(255), default="")
    ifsc = db.Column(db.String(255), default="")
    transaction_date = db.Column(db.String(255), default="")
    transaction_id = db.Column(db.String(255), default="")
    transaction_amount = db.Column(db.String(255), default="")
    reference_no = db.Column(db.String(255), default="")
    company_email = db.Column(db.String(320), default="", index=True)
    remarks = db.Column(db.Text, default="")
    action_taken = db.Column(db.String(255), default="")
    date_of_action = db.Column(db.String(255), default="")

    # Per-record signatory overrides (optional). Fall back to session globals.
    sender_name = db.Column(db.String(255), default="")
    sender_role = db.Column(db.String(255), default="")

    # Workflow state.
    reference_name = db.Column(db.String(255), default="")
    status = db.Column(db.String(32), default="missing", index=True)
    validation_errors = db.Column(db.Text, default="")  # JSON-encoded list.

    def to_dict(self) -> dict:
        import json

        return {
            "id": self.id,
            "row_index": self.row_index,
            "acknowledgement_no": self.acknowledgement_no,
            "bank": self.bank,
            "layer": self.layer,
            "account_no": self.account_no,
            "ifsc": self.ifsc,
            "transaction_date": self.transaction_date,
            "transaction_id": self.transaction_id,
            "transaction_amount": self.transaction_amount,
            "reference_no": self.reference_no,
            "company_email": self.company_email,
            "remarks": self.remarks,
            "action_taken": self.action_taken,
            "date_of_action": self.date_of_action,
            "sender_name": self.sender_name,
            "sender_role": self.sender_role,
            "reference_name": self.reference_name,
            "status": self.status,
            "validation_errors": json.loads(self.validation_errors or "[]"),
        }
