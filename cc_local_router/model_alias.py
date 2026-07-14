"""Add a custom model alias (default: "local") to every site Claude
Code uses to validate model names.

Sites touched (architecture doc §7):

  S1   `Nff`  — Agent tool zod enum
  S3   `spd`  — alias allowlist for `tU()`
  S4   `tMe`  — CLI picker array
  S5   `oMe`  — TUI picker array
       ↑ all four are literal `["sonnet","opus","haiku","fable"]` —
         one `ArrayAppendPatch` matches all four with `expect_count=4`

  S2   `hye`  — alias allowlist for `v0()`, ends `..."opusplan"]`
       handled by a second `ArrayAppendPatch` instance keyed on a
       different anchor

  S6   `zo()` resolver switch — adds a `case "<alias>":` arm that
       resolves via the same function as `case "opusplan":` (so the
       wire-level API model id is well-defined and a downstream
       proxy can route on it)

The alias is read from `CLAUDE_PATCHER_MODEL_ALIAS` (default `local`)
on first patch invocation, NOT at import time — so `--list-patches`
and `--emit-cache-key` don't trigger env validation. The value is
constrained to `[A-Za-z0-9_-]{1,16}` to ensure inserted bytes are
ASCII (Latin1 module encoding).
"""

import os
import re

from cc_patcher.context import DiscoveryContext
from cc_patcher.edits import Edit


_ALIAS_PATTERN = re.compile(rb"^[A-Za-z0-9_-]{1,16}$")
_DEFAULT_ALIAS = b"local"
_alias_cache: bytes | None = None


def _get_alias() -> bytes:
    global _alias_cache
    if _alias_cache is not None:
        return _alias_cache
    env = os.environ.get("CLAUDE_PATCHER_MODEL_ALIAS", "")
    alias = env.encode("ascii") if env else _DEFAULT_ALIAS
    if not _ALIAS_PATTERN.fullmatch(alias):
        raise ValueError(
            f"invalid CLAUDE_PATCHER_MODEL_ALIAS {alias!r}; "
            f"must match {_ALIAS_PATTERN.pattern.decode()}"
        )
    _alias_cache = alias
    return alias


class ArrayAppendPatch:
    """Append `,"<alias>"` immediately before `]` at every match of the
    declared anchor regex. One growable Edit per match, each growing
    the StringPointer region it lives inside (module[0].contents for
    every match on 2.1.195)."""

    may_grow = True

    def __init__(self, name: str, description: str, anchor: bytes,
                 expect_count: int):
        self.name = name
        self.description = description
        self.diag_anchor = anchor
        self.anchor = anchor
        self.expect_count = expect_count

    def discover(self, ctx: DiscoveryContext) -> list[Edit]:
        alias = _get_alias()
        insert = b',"' + alias + b'"]'
        edits: list[Edit] = []
        for m in ctx.find_regex_in_payload(self.anchor):
            close_off = m.end() - 1
            ref = ctx.containing_string_pointer(close_off)
            edits.append(Edit(
                offset=close_off, old=b"]", new=insert,
                patch_name=self.name, grows_region=ref,
            ))
        return edits

    def cache_key(self) -> str:
        return (
            f"ArrayAppendPatch:{self.name}:"
            f"{self.anchor.decode('latin1')}:{_get_alias().decode()}"
        )


def short_array_patch() -> ArrayAppendPatch:
    return ArrayAppendPatch(
        name="Model alias / short arrays (Nff, spd, tMe, oMe)",
        description=(
            "Append the custom alias to the four "
            '["sonnet","opus","haiku","fable"] literals: '
            "the Agent tool zod enum (Nff), the alias allowlist (spd), "
            "and the CLI + TUI picker arrays (tMe, oMe)."
        ),
        anchor=rb'\["sonnet","opus","haiku","fable"\]',
        expect_count=4,
    )


def hye_array_patch() -> ArrayAppendPatch:
    return ArrayAppendPatch(
        name="Model alias / hye allowlist",
        description=(
            "Append the custom alias to the hye allowlist (the long "
            'array ending in "opusplan"]).'
        ),
        anchor=rb'"opusplan"\]',
        expect_count=1,
    )


class ResolverSwitchPatch:
    """Add `case "<alias>":` to the API-id resolver switch (yAn).

    Anchor pattern matches from `case"opusplan":return XX(t);` through
    `default:return null`, allowing any number of intervening arms. An
    intervening arm is either a return arm (`case"NAME":return BODY;`)
    or a brace-block arm (`case"NAME":{...}`); recent builds render the
    `"best"` arm as a brace block, so both forms must be traversed to
    reach the `default` arm. The opusplan arm's resolver function
    identifier is
    captured and reused for the new arm — the alias resolves to the
    same API model id as `opusplan` does, which a downstream proxy
    can route on.

    A match whose intervening arms already contain `case"<alias>":` is
    skipped — the binary already carries this patch, so re-running
    against an already-patched binary is a no-op.
    """

    name = "Model alias / resolver switch (yAn)"
    description = (
        "Insert a new case arm into the yAn() resolver switch so the "
        "alias resolves to the same API model id as opusplan does."
    )
    may_grow = True
    expect_count = 1
    diag_anchor = b'case"opusplan":'
    PATTERN = re.compile(
        rb'case"opusplan":return ([a-zA-Z0-9_$]+)\(t\);'
        rb'(?:case"[a-zA-Z0-9_$]+":(?:\{[^}]*\}|return [^;]+;))*'
        rb'default:return null'
    )

    def discover(self, ctx: DiscoveryContext) -> list[Edit]:
        alias = _get_alias()
        start = ctx.bun.payload_start
        end = ctx.bun.offsets_struct_offset
        matches = list(self.PATTERN.finditer(ctx.buf, start, end))
        edits: list[Edit] = []
        for m in matches:
            block = m.group(0)
            if b'case"' + alias + b'":' in block:
                continue
            ident = m.group(1)
            default_rel = block.rfind(b"default:return null")
            if default_rel < 0:
                continue
            default_off = m.start() + default_rel
            old = b"default:return null"
            new = (
                b'case"' + alias + b'":return ' + ident + b'(t);'
                + old
            )
            ref = ctx.containing_string_pointer(default_off)
            edits.append(Edit(
                offset=default_off, old=old, new=new,
                patch_name=self.name, grows_region=ref,
            ))
        return edits

    def cache_key(self) -> str:
        return (
            f"ResolverSwitchPatch:{self.PATTERN.pattern.decode('latin1')}:"
            f"{_get_alias().decode()}"
        )
