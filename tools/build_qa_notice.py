"""Generate a neutral filled notice used only for render QA."""
from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "notice-studio"))

from notice_app.services.docx_engine import render_document


def main():
    template = ROOT / "apps" / "notice-studio" / "notice_template.docx"
    output = ROOT / "work" / "qa" / "notice-sample.docx"
    output.parent.mkdir(parents=True, exist_ok=True)
    document = render_document(
        str(template),
        {
            "reference_name": "Internal Review",
            "acknowledgement_no": "ACK-1042",
            "bank": "Atlas Payments",
            "layer": "1",
            "account_no": "1234567890",
            "ifsc": "ATLS0000123",
            "transaction_date": "29/07/2026",
            "transaction_id": "TXN-77881234",
            "transaction_amount": "25,000",
            "reference_no": "REF-2048",
            "company_email": "review@atlas.example",
            "remarks": "",
            "action_taken": "",
            "date_of_action": "",
        },
        sender_name="",
        sender_role="No signature",
        unsigned_role="No signature",
        date_value="29/07/2026",
        subject_value="Information request for referenced transaction",
    )
    document.save(output)
    print(output)


if __name__ == "__main__":
    main()
