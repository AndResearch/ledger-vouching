"""Answer content hashes — the evidence layer's zero-integration join key.

# Design rationale:
#   Two SHA-256 hex digests of the observed terminal answer ride every terminal
#   observation and its audit verdict event:
#     sha_raw   — over the raw UTF-8 bytes of the answer exactly as observed
#                 (byte identity; tamper evidence at full strength).
#     sha_canon — over the canonicalized text: the paste-match join key that
#                 survives copy/paste mangling (editors normalize newlines and
#                 strip trailing whitespace) without weakening tamper detection
#                 on content.
#   The hashes let an evidence service match a pasted report body to a wire
#   observation with ZERO harness integration (identity tier 3).
#   provenance ≠ correctness still holds: a hash match proves the pasted text
#   IS the observed text — never that the text is right.
#
# canonicalize spec (FROZEN — these four steps and nothing more): NFC, newline
#   unification, trailing-whitespace strip per line, outer blank-line strip.
#   Adding steps would silently widen what counts as "the same text" (a token
#   normalization it is not — the value-token matching in grounding.py is a
#   separate, unrelated normalization). This module is the REFERENCE
#   implementation of the spec; the evidence portal and its viewer JS carry
#   equality-verified mirrors — never change any copy independently, or the
#   published join key breaks.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata


# The reference implementation of the frozen 4-step spec (mirrored in the
# evidence portal and its viewer JS — do not modify; see module rationale).
def canonicalize(t: str) -> str:
    t = unicodedata.normalize('NFC', t)
    t = t.replace('\r\n', '\n').replace('\r', '\n')
    lines = [re.sub(r'\s+$', '', ln) for ln in t.split('\n')]
    while lines and not lines[0]:
        lines.pop(0)
    while lines and not lines[-1]:
        lines.pop()
    return '\n'.join(lines)


def answer_hashes(answer: str) -> dict[str, str]:
    """The two digests of one observed terminal answer (hex, lowercase)."""
    return {
        "sha_raw": hashlib.sha256(answer.encode("utf-8")).hexdigest(),
        "sha_canon": hashlib.sha256(canonicalize(answer).encode("utf-8")).hexdigest(),
    }
