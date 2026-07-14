"""cc-local-router: model-alias and availability-gate patches for the
cc-patcher engine, backing claude-net's local-inference model
splitter.

`PATCHES` is the entry point resolved by `cc_patcher.patches` under
the `cc_patcher.patches` group (see pyproject.toml).
"""

from .availability import AvailabilityGatePatch
from .model_alias import (
    ResolverSwitchPatch,
    hye_array_patch,
    short_array_patch,
)

PATCHES = [
    short_array_patch(),
    hye_array_patch(),
    ResolverSwitchPatch(),
    AvailabilityGatePatch(),
]
