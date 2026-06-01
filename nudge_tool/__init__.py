"""shift-nudge-tool: standalone owned tool for SHIFT FTV nudges.

Luke runs this from his machine. It reads Beta exports, computes who hit which
nudge trigger, and tags those climbers in SHIFT's Mailchimp via the API; a
tag-triggered Customer Journey does the actual sending. See FTV_PILOT_SPEC.md
section 13 for the full architecture.
"""

__version__ = "0.1.0"
