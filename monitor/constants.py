"""Pinned identifiers verified against live docs on 2026-11-12.

Anthropic: Claude Haiku 5.5, model id ``claude-haiku-5-5``.
JSON output is ``output_config.format`` with ``type: json_schema``.
Effort is set to ``low`` so a 300-passage run stays under the cost cap.
https://platform.claude.com/docs/en/models/haiku-5-5/overview
https://platform.claude.com/docs/en/build-with-claude/structured-outputs

Notion: latest API version ``2026-03-11``. Database properties are set on
``initial_data_source``. Pages are parented to the data source id.
https://developers.notion.com/reference/versioning
https://developers.notion.com/reference/create-database
"""

MODEL_ID = "claude-haiku-5-5"
NOTION_VERSION = "2026-03-11"

USER_AGENT = (
    "ALPRSignalMonitor/1.0 (public-records research; "
    "+https://github.com/joshferguson3-alt/alpr-monitor)"
)

# The salesperson in this spec sells against Flock Safety.
INCUMBENT_VENDOR = "flock"

# States the registry is designed to cover.
COVERED_STATES = {"AL", "FL", "GA", "KY", "LA", "MS", "NC", "SC", "TN", "VA"}

# Shared bill and docket search. LegiScan's public search accepts OR.
BILL_QUERY = (
    '"license plate reader" OR "license plate recognition" OR "automated license plate" '
    'OR "plate reader" OR ALPR OR "Flock Safety" OR "Flock Group" '
    'OR "vehicle location data" OR "location surveillance"'
)

DIGEST_RECIPIENT = "josh.ferguson3@gmail.com"

PASSAGE_CAP = 300
CLASSIFY_DOC_CAP = 150
CLASSIFY_WORKERS = 4
# Leave room inside a 45-minute job for the publish step after collection stops.
RUN_BUDGET_SECONDS = 32 * 60
HTTP_TIMEOUT = 20.0
HTTP_RETRIES = 2
MIN_HOST_DELAY = 2.0

SIGNAL_TYPES = (
    "contract_event",
    "budget_event",
    "policy_event",
    "legislation_event",
    "stance_statement",
    "public_sentiment",
    "incident",
    "vendor_move",
    "leadership_change",
    "funding_event",
)

STANCES = ("supports", "opposes", "conditional", "neutral", "unclear")
STANCE_TARGETS = (
    "alpr_generally",
    "incumbent_vendor",
    "specific_vendor",
    "specific_policy",
)
