"""Category configuration and filtering.

Categories group packages that should be tracked together and describe what
matters for that group (for example ``perimeter`` focuses on remotely
exploitable issues while ``user`` focuses on privilege escalation). The
category tables live under ``[tracking.<name>]`` in the osv-feed config and
override the built-in defaults for the same name.
"""

from __future__ import annotations

SEVERITY_ORDER = {"low": 0, "medium": 1, "high": 2, "critical": 3}

# Fallback applied by ``match`` to packages that belong to no named category.
# It carries a conservative "medium" minimum-severity policy so unidentified
# packages are still triaged rather than passed through unfiltered.
DEFAULT_FALLBACK_NAME = "default"
DEFAULT_FALLBACK_CATEGORY: dict = {
    "severity": "medium",
    "states": ["no_fix_yet", "fix_available"],
}

DEFAULT_CATEGORIES: dict[str, dict] = {
    "perimeter": {
        "packages": ["openssh", "nginx"],
        "severity": "high",
        "states": ["no_fix_yet", "fix_available"],
    },
    "user": {
        "packages": ["sudo"],
        "severity": "high",
        "interests": ["privilege escalation", "local privilege"],
    },
    "network": {
        "packages": ["dnsmasq", "squid", "keepalived", "wireguard-tools"],
        "severity": "medium",
        "states": ["no_fix_yet", "fix_available"],
    },
    "runtimes": {
        "packages": ["docker.io", "containerd", "runc", "podman"],
        "severity": "high",
        "states": ["no_fix_yet", "fix_available"],
    },
    # Container image findings are driven by ``container/*.manifest`` files
    # rather than the ``packages`` list (which stays empty); the table only
    # supplies severity/state policy for the ``match --container-dir`` path.
    "container": {
        "packages": [],
        "severity": "high",
        "states": ["no_fix_yet", "fix_available"],
    },
}


def _string_list(value, field: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(
        isinstance(item, str) and item.strip() for item in value
    ):
        raise ValueError(f"{field} must be a list of non-empty strings")
    return [item.strip() for item in value]


def category_packages(config: dict) -> list[str]:
    """Return the ``packages`` list from a ``[tracking.<name>]`` table."""
    return _string_list(config.get("packages"), "packages")


def package_belongs(package: str, category_config: dict) -> bool:
    """Return True when ``package`` is listed in a category's package set.

    Matches on the source package name; a ``name=version`` entry counts as
    belonging for its ``name`` part.
    """
    wanted = package.lower()
    for entry in category_packages(category_config):
        name = entry.split("=", 1)[0].lower()
        if name == wanted:
            return True
    return False


def category_ecosystem(config: dict, default: str | None = None) -> str | None:
    """Return the category ecosystem, falling back to ``default`` (host OS).

    Returns ``None`` when the category sets no ecosystem and no default was
    provided (i.e. the host OS is not Debian/Ubuntu).
    """
    return config.get("ecosystem") or default


def passes_category(finding: dict, config: dict) -> bool:
    """Return True when a finding is relevant to a category policy.

    A category may set ``severity`` (minimum severity), ``states``
    (allowed CVE states), and ``interests`` (substrings matched against the
    OSV summary/details). When a field is absent, that dimension is not
    filtered.
    """
    severity = config.get("severity")
    if severity is not None:
        if severity not in SEVERITY_ORDER:
            raise ValueError(f"severity must be one of {sorted(SEVERITY_ORDER)}")
        finding_severity = finding.get("severity") or ""
        if SEVERITY_ORDER.get(finding_severity, -1) < SEVERITY_ORDER[severity]:
            return False

    states = _string_list(config.get("states"), "states")
    if states and finding.get("state") not in states:
        return False

    interests = _string_list(config.get("interests"), "interests")
    if not interests:
        return True
    return bool(matched_interests(finding, config))


def matched_interests(finding: dict, config: dict) -> list[str]:
    """Return the category interests found in a finding's summary/details.

    Interests are case-insensitive substrings matched against the OSV
    summary and details, e.g. ``rce``, ``remote code execution`` or
    ``privilege escalation``. Used to explain why a finding is relevant.
    """
    interests = _string_list(config.get("interests"), "interests")
    if not interests:
        return []
    blob = " ".join(
        (
            finding.get("summary") or "",
            finding.get("details") or "",
        )
    ).lower()
    return [interest for interest in interests if interest.lower() in blob]
