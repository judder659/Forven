"""Prebuilt strategy catalog — returns ParamSpec-shaped entries for the frontend."""

import logging
import re

log = logging.getLogger("forven.strategies.catalog")

# rule_engine is the no-code visual-builder runtime: it is useless as a
# selectable prebuilt template (it needs a `spec` param the catalog can't
# supply), so it must not appear in the strategy dropdown.
_SKIP_TYPES = {"stress_test", "rule_engine"}


def get_prebuilt_catalog() -> list[dict]:
    """Build the prebuilt strategy template catalog from the type registry."""
    from forven.strategies.registry import _TYPE_MAP, discover

    # This endpoint backs the Strategy Creator's built-in template selector.
    # Loading every custom strategy made it instantiate thousands of generated
    # classes on each request and return multi-megabyte payloads.
    discover(include_custom=False)

    catalog: list[dict] = []
    for type_name, cls in sorted(_TYPE_MAP.items()):
        if type_name in _SKIP_TYPES or not cls.__module__.startswith("forven.strategies.builtin."):
            continue
        try:
            instance = cls(f"_catalog_{type_name}", {})
            param_space = instance.parameter_space()
            parameters: dict[str, dict] = {}
            for key, value in instance.default_params.items():
                if key.startswith("_"):
                    continue
                vtype = type(value).__name__
                if vtype == "int":
                    vtype = "number"
                elif vtype == "float":
                    vtype = "number"
                spec: dict = {"type": vtype, "default": value}
                if key in param_space:
                    lo, hi, step = param_space[key]
                    spec.update({"min": lo, "max": hi, "step": step})
                parameters[key] = spec

            regimes = set()
            try:
                regimes = instance.compatible_regimes
            except Exception:
                pass

            # Strip trailing ticker like " (BTC)" or " (ETH)" from prebuilt names
            clean_name = re.sub(r"\s*\([A-Z]{2,6}(/[A-Z]{2,6})?\)\s*$", "", instance.name)

            catalog.append({
                "name": clean_name,
                "api_name": type_name,
                "type": type_name,
                "version": "1.0.0",
                "description": instance.describe(),
                "compatible_regimes": sorted(regimes),
                "parameters": parameters,
                "source": "prebuilt",
            })
        # Strategy classes are extension code.  A broken constructor must not be
        # able to terminate the API process while this informational catalog is
        # being built (``SystemExit`` is not an ``Exception`` in Python).
        except (Exception, SystemExit) as exc:
            log.debug("Skipping catalog entry for %s: %s", type_name, exc)
            continue

    return catalog
