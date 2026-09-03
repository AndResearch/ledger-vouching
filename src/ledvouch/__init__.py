"""ledger-vouching — oracleless grounding ledger voucher.

An OpenAI-compatible proxy that enforces output grounding on someone else's agent
loop by spectating the wire (base_url swap only). stage A = passthrough + terminal
grounding flag. See README.md.
"""

from .grounding import Sufficiency, grounded, load_bearing_tokens, sufficiency_peek
from .ledger import Ledger, ToolRecord, build_ledger
from .proxy import create_app, make_app

__all__ = [
    "Sufficiency",
    "grounded",
    "load_bearing_tokens",
    "sufficiency_peek",
    "Ledger",
    "ToolRecord",
    "build_ledger",
    "create_app",
    "make_app",
]
