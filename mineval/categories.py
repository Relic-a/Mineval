"""Stable primary engineering areas; historical uncategorized records stay readable."""
CATEGORIES = {
    'behavior': 'Core behavior and algorithms: domain logic, correctness, parsing and computation.',
    'integration': 'Interfaces and integration: APIs, protocols and interoperability between components.',
    'data': 'Data and persistence: storage, queries, migrations, serialization and data integrity.',
    'reliability': 'Reliability and performance: concurrency, resource use, recovery and execution efficiency.',
    'security': 'Security and access: authentication, authorization and trust boundaries.',
    'developer-tools': 'Developer tooling: builds, packaging, configuration and development workflows.',
    'user-interface': 'User interfaces: interaction, rendering, accessibility and presentation.',
}
LEGACY = 'uncategorized'

# Older selector handoffs described change type instead of engineering area.
LEGACY_LABELS = {'feature', 'bug', 'bugfix', 'fix', 'refactor'}


def category_for(metadata):
    value = metadata.get('category')
    return value if isinstance(value, str) and value in CATEGORIES else LEGACY
