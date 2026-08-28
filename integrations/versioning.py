"""Jobber GraphQL API versioning helpers.

Jobber requires X-JOBBER-GRAPHQL-VERSION on every request. Versions are
YYYY-MM-DD. Breaking changes ship in new dates. Deprecated fields (example:
Quote.cost) stay on the old path until a later breaking version removes them.

We always pin a version from settings, record the version Jobber actually
served, and keep deprecation notices from introspection so upgrades can be
planned from changelog + live schema, not guesswork.
"""


def normalize_versioning(extensions: dict | None, requested_version: str) -> dict:
    payload = (extensions or {}).get("versioning") or {}
    served = payload.get("version") or ""
    warning = (payload.get("warning") or "").strip()
    if served and requested_version and served != requested_version and not warning:
        warning = (
            f"Requested API version {requested_version} but Jobber served {served}. "
            "The requested version may have been removed and auto-upgraded."
        )
    return {
        "requested": requested_version,
        "served": served,
        "warning": warning,
        "mismatch": bool(served and requested_version and served != requested_version),
    }


def extract_deprecated_fields(types: dict) -> list[dict]:
    deprecated = []
    for type_name, type_def in (types or {}).items():
        if not isinstance(type_def, dict):
            continue
        for field in type_def.get("fields") or []:
            if not field.get("isDeprecated"):
                continue
            deprecated.append(
                {
                    "type": type_name,
                    "field": field.get("name"),
                    "reason": field.get("deprecationReason") or "",
                }
            )
    deprecated.sort(key=lambda row: (row["type"], row["field"]))
    return deprecated
