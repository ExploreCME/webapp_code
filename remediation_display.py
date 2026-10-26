"""Source-heading presentation for remediation modules, never question metadata."""


def remediation_label(module):
    source = (module.get("source_organ_system") or "").strip()
    return source or module.get("organ_system") or "Unassigned"


def remediation_groups(modules):
    groups = {}
    for module in modules:
        groups.setdefault(remediation_label(module), []).append(module)
    return groups.items()
