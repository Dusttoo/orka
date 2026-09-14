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
    # Preserve the v1 identity of slices created before contract fields existed.
    # Once a repository declares contracts, their exact content becomes part of
    # the immutable Jira provenance binding.
    if item.get("contracts"):
        fields["contracts"] = item["contracts"]
    digest = hashlib.sha256(
        json.dumps(fields, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return "orka-slice-v1-" + digest


def validate_delivery(item, error):
    owner = item.get("migration_owner")
    tests = item.get("test_plan")
    if not isinstance(owner, str) or not owner.strip() or len(owner) > 32:
        raise error("slice requires migration_owner (owning slice id or 'none')")
    if (
        not isinstance(tests, list)
        or not 1 <= len(tests) <= 30
        or any(
            not isinstance(test, str) or not test.strip() or len(test) > 2000
            for test in tests
        )
    ):
        raise error("slice requires a bounded explicit test_plan")


def validate_owner(item, identifiers, error):
    if item["migration_owner"] != "none" and item["migration_owner"] not in identifiers:
        raise error("migration_owner must name a slice in this decomposition or 'none'")


def required_contract_names(feature, error):
    values = feature.get("required_slice_contracts", [])
    if not isinstance(values, list) or any(
        not isinstance(value, str) or not value.strip() or len(value.strip()) > 64
        for value in values
    ):
        raise error("required_slice_contracts must be an array of bounded names")
    names = [value.strip() for value in values]
    if len(names) > 20 or len({value.casefold() for value in names}) != len(names):
        raise error("required_slice_contracts must contain at most 20 unique names")
    return names


def validate_contracts(item, required, error):
    contracts = item.get("contracts", {})
    if not isinstance(contracts, dict):
        raise error("slice contracts must be an object")
    missing = [name for name in required if name not in contracts]
    if missing:
        raise error("slice requires contracts: " + ", ".join(missing))
    if len(contracts) > 20:
        raise error("slice contracts exceed the bounded schema")
    for name, value in contracts.items():
        if not isinstance(name, str) or not name.strip() or len(name) > 64:
            raise error("slice contract names must be bounded nonempty strings")
        valid = (
            isinstance(value, str) and bool(value.strip()) and len(value) <= 4000
        ) or (
            isinstance(value, list)
            and 1 <= len(value) <= 20
            and all(
                isinstance(item, str) and item.strip() and len(item) <= 2000
                for item in value
            )
        )
        if not valid:
            raise error(
                f"slice contract {name} must be a nonempty bounded string or string array"
            )
