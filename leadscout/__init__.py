"""leadscout: find small-business leads and audit their websites.

Two jobs:
  1. find businesses from OpenStreetMap (Overpass API) or a local JSON/CSV list.
  2. audit a business website quickly and politely, and report concrete,
     plain-English problems.

See README.md for usage. Everything here is stdlib-only, no API keys needed.
"""

__version__ = "0.1.0"

DEFAULT_USER_AGENT = (
    "leadscout/{version} (+https://github.com/taktekhq/leadscout; "
    "contact via GitHub issues)"
).format(version=__version__)
