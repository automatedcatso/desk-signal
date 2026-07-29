"""Central placeholder definition.

Edit this ONE file if the tokens inside notice_template.docx differ from these
defaults. The format is {{token}} (double curly braces, lowercase snake_case).
The docx engine logs any placeholder left unreplaced after generation.
"""

# Per-record fields -> Record attribute name.
RECORD_PLACEHOLDERS = {
    "reference_name": "reference_name",
    "acknowledgement_no": "acknowledgement_no",
    "bank": "bank",
    "layer": "layer",
    "account_no": "account_no",
    "ifsc": "ifsc",
    "transaction_date": "transaction_date",
    "transaction_id": "transaction_id",
    "transaction_amount": "transaction_amount",
    "reference_no": "reference_no",
    "company_email": "company_email",
    "remarks": "remarks",
    "action_taken": "action_taken",
    "date_of_action": "date_of_action",
}

# Alias tokens that some templates use, mapped to the same Record attribute as
# their canonical token above. Lets existing notice_template.docx files work
# without editing the template.
ALIAS_PLACEHOLDERS = {
    "appno": "acknowledgement_no",
    "bank_name": "bank",
}

# Tokens whose value is computed per generation (not a Record attribute).
# Handled in docx_engine.build_mapping: 'date' (generation date) and 'subject'.
COMPUTED_TOKENS = ("date", "subject")

# Token whose presence triggers account-table insertion in the engine.
ACCOUNT_TABLE_TOKEN = "account_table"

# Sender tokens handled with special logic in the engine.
SENDER_NAME_TOKEN = "sender_name"
SENDER_ROLE_TOKEN = "sender_role"

OPEN = "{{"
CLOSE = "}}"


def token(name: str) -> str:
    return f"{OPEN}{name}{CLOSE}"
