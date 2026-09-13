"""Shared required delivery fields and identities for decomposition."""

import hashlib
import json


def decomposition_provenance(source, item):
    """Bind an Orka slice label to its exact source and reviewed content."""
    fields = {
        "source": str(source).upper(),
        "id": item.get("id"),
        "summary": item.get("summary"),
        "behavior": item.get("behavior"),
        "migration_owner": item.get("migration_owner"),
        "test_plan": item.get("test_plan"),
        "acceptance_criteria": item.get("acceptance_criteria"),
        "depends_on": item.get("depends_on", []),
    }
    digest = hashlib.sha256(
        json.dumps(fields, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return "orka-slice-v1-" + digest
def validate_delivery(item, error):
    owner = item.get("migration_owner")
    tests = item.get("test_plan")
    if not isinstance(owner, str) or not owner.strip() or len(owner) > 32:
        raise error("slice requires migration_owner (owning slice id or 'none')")
    if (not isinstance(tests, list) or not 1 <= len(tests) <= 30
            or any(not isinstance(test, str) or not test.strip() or len(test) > 2000 for test in tests)):
        raise error("slice requires a bounded explicit test_plan")


def validate_owner(item, identifiers, error):
    if item["migration_owner"] != "none" and item["migration_owner"] not in identifiers:
        raise error("migration_owner must name a slice in this decomposition or 'none'")
