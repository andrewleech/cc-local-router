"""Unit tests for ResolverSwitchPatch.discover().

Covers the two load-bearing behaviours: it finds the resolver-switch
anchor and emits a correctly-shaped edit on first run, and re-running
discover() against its own output finds nothing (idempotence) instead
of inserting a second alias arm.
"""

import os
import unittest

os.environ.setdefault("CLAUDE_PATCHER_MODEL_ALIAS", "local")

from cc_local_router.model_alias import ResolverSwitchPatch


class FakeBun:
    def __init__(self, payload_start: int, offsets_struct_offset: int):
        self.payload_start = payload_start
        self.offsets_struct_offset = offsets_struct_offset


class FakeContext:
    """Minimal stand-in for DiscoveryContext -- just the surface
    ResolverSwitchPatch.discover() touches."""

    def __init__(self, buf: bytes):
        self.buf = buf
        self.bun = FakeBun(payload_start=0, offsets_struct_offset=len(buf))

    def containing_string_pointer(self, abs_offset: int):
        return None


SWITCH_BODY = (
    b'switch(e){case"opus":return rXe(t);case"sonnet":return vPn(t);'
    b'case"haiku":return phi(t);case"fable":return dhi(t);'
    b'case"opusplan":return vPn(t);'
    b'case"best":return SPn(t);'
    b'default:return null}'
)


class ResolverSwitchPatchTests(unittest.TestCase):
    def test_first_run_finds_one_match_and_inserts_alias_arm(self):
        patch = ResolverSwitchPatch()
        ctx = FakeContext(SWITCH_BODY)
        edits = patch.discover(ctx)
        self.assertEqual(len(edits), 1)
        edit = edits[0]
        self.assertEqual(edit.old, b"default:return null")
        self.assertIn(b'case"local":return vPn(t);', edit.new)
        self.assertTrue(edit.new.endswith(b"default:return null"))

    def test_rerun_against_already_patched_output_is_a_no_op(self):
        patch = ResolverSwitchPatch()
        ctx = FakeContext(SWITCH_BODY)
        edits = patch.discover(ctx)
        edit = edits[0]

        patched = bytearray(SWITCH_BODY)
        patched[edit.offset:edit.offset + len(edit.old)] = edit.new

        ctx2 = FakeContext(bytes(patched))
        edits2 = patch.discover(ctx2)
        self.assertEqual(
            edits2, [],
            "re-running discover() on an already-patched switch must "
            "find no further edits (idempotence)",
        )

    def test_brace_block_intervening_arm_is_traversed(self):
        # The live Claude Code binary renders the "best" arm as a brace
        # block sitting between opusplan and default. The anchor must
        # traverse brace-block arms as well as return arms to reach the
        # default arm; otherwise the alias is added to the picker and
        # allowlists but never gets a resolver arm and resolves to null.
        body_with_block_arm = (
            b'switch(e){case"opus":return rXe(t);case"sonnet":return vPn(t);'
            b'case"haiku":return phi(t);case"fable":return dhi(t);'
            b'case"opusplan":return vPn(t);'
            b'case"best":{let r=SPn[KGl()];return r!==void 0?r.builtinDefault(t):rXe(t)}'
            b'default:return null}'
        )
        patch = ResolverSwitchPatch()
        ctx = FakeContext(body_with_block_arm)
        edits = patch.discover(ctx)
        self.assertEqual(len(edits), 1)
        edit = edits[0]
        self.assertEqual(edit.old, b"default:return null")
        self.assertIn(b'case"local":return vPn(t);', edit.new)


if __name__ == "__main__":
    unittest.main()
