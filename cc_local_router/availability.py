"""Force the `xa()` model-availability gate to always return true.

`xa(model, opts?)` (renamed `Ka()` in 2.1.199+) is the central check
Claude Code calls before accepting any model name (CLI flag,
settings.json, Agent tool field, picker selection). Behaviour on the
unmodified binary:

  - If no `availableModels` policy is configured in settings, the
    function returns true unconditionally — so the patch is a no-op
    for that case.
  - If a managed-policy `availableModels` allowlist IS configured
    (an enterprise account), it returns false for names not in the
    list, and gates 2/3/5 fail for the alias.

We rewrite the body to `return!0` + padding so the function passes
every model name regardless of policy.

The function's minified NAME changes between releases (xa in 2.1.195,
Ka in 2.1.199, likely different again in future). We anchor on the
first line of the body — `function X(e,t){if(t?.allowlist===void 0)`
— which is unique in the bundle and structurally stable. The body
end is found by ctx.find_balanced_close.
"""

import re

from cc_patcher.context import DiscoveryContext
from cc_patcher.edits import Edit


class AvailabilityGatePatch:
    """Same-length body rewrite of the availability gate to
    `return!0`. Required for managed-policy users (those with
    `availableModels` set in settings.json); no-op for accounts
    without a policy."""

    name = "Model availability gate (xa)"
    description = (
        "Force the model availability gate consulted by every "
        "model-validation entry point to return true unconditionally. "
        "Necessary for the alias to pass managed-policy availableModels "
        "checks; no-op for accounts without a policy."
    )
    may_grow = False
    expect_count = 1
    diag_anchor = b"t?.allowlist===void 0"
    # Anchor on the body signature, not the function name — the name
    # is minified and drifts (xa in 2.1.195, Ka in 2.1.199).
    ANCHOR_RX = re.compile(
        rb"function [a-zA-Z0-9_$]{1,4}\(e,t\)\{(?=if\(t\?\.allowlist===void 0\))"
    )
    NEW_BODY = b"return!0"

    def discover(self, ctx: DiscoveryContext) -> list[Edit]:
        matches = ctx.find_regex_in_payload(self.ANCHOR_RX.pattern)
        if len(matches) != 1:
            return []
        m = matches[0]
        body_start = m.end()
        body_end = ctx.find_balanced_close(
            body_start, ctx.bun.offsets_struct_offset,
        )
        if body_end is None:
            return []
        body_len = body_end - body_start
        if body_len < len(self.NEW_BODY):
            return []
        old = bytes(ctx.buf[body_start:body_end])
        new = self.NEW_BODY + b" " * (body_len - len(self.NEW_BODY))
        return [Edit(
            offset=body_start, old=old, new=new,
            patch_name=self.name,
        )]

    def cache_key(self) -> str:
        return (
            f"AvailabilityGatePatch:{self.ANCHOR_RX.pattern.decode('latin1')}:"
            f"{self.NEW_BODY.decode()}"
        )
