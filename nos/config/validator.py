from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import pydantic

from nos.config.schema import BgpTypeEnum, NOSConfig


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------

@dataclass
class ValidationIssue:
    path: str
    message: str

    def __str__(self) -> str:
        return f"{self.path}: {self.message}"


class ValidationResult:
    """Accumulates validation errors from a single validate() call."""

    def __init__(self) -> None:
        self.errors: list[ValidationIssue] = []

    @property
    def is_valid(self) -> bool:
        return len(self.errors) == 0

    def add_error(self, path: str, message: str) -> None:
        self.errors.append(ValidationIssue(path=path, message=message))

    def __bool__(self) -> bool:
        return self.is_valid

    def __repr__(self) -> str:
        if self.is_valid:
            return "ValidationResult(valid)"
        return f"ValidationResult({len(self.errors)} error(s))"


# ---------------------------------------------------------------------------
# Validator
# ---------------------------------------------------------------------------

class ConfigValidator:
    """Phase 1 validator: syntactic (schema) + semantic (cross-reference) checks.

    Usage::

        result = ConfigValidator().validate(config_dict)
        if not result.is_valid:
            for issue in result.errors:
                print(issue)
    """

    def validate(self, config: dict) -> ValidationResult:
        result = ValidationResult()
        nos = self._parse_schema(config, result)
        if nos is None:
            # Schema errors prevent cross-reference checks
            return result
        self._check_required_fields(nos, result)
        self._check_references(nos, result)
        return result

    # ------------------------------------------------------------------
    # Schema (Pydantic) validation
    # ------------------------------------------------------------------

    def _parse_schema(self, config: dict, result: ValidationResult) -> Optional[NOSConfig]:
        try:
            return NOSConfig.model_validate(config)
        except pydantic.ValidationError as exc:
            for err in exc.errors():
                loc = ".".join(str(p) for p in err["loc"])
                result.add_error(loc, err["msg"])
            return None

    # ------------------------------------------------------------------
    # Required-field checks (fields not enforced at schema level so that
    # partial candidate configs can still be loaded from disk)
    # ------------------------------------------------------------------

    def _check_required_fields(self, config: NOSConfig, result: ValidationResult) -> None:
        for name, vlan in config.vlans.items():
            if vlan.vlan_id is None:
                result.add_error(
                    f"vlans.{name}.vlan_id",
                    "vlan_id is required",
                )

        for name, ri in config.routing_instances.items():
            if ri.instance_type is None:
                result.add_error(
                    f"routing_instances.{name}.instance_type",
                    "instance_type is required",
                )

    # ------------------------------------------------------------------
    # Cross-reference checks
    # ------------------------------------------------------------------

    def _check_references(self, config: NOSConfig, result: ValidationResult) -> None:
        self._check_vlan_member_references(config, result)
        self._check_l3_interface_references(config, result)
        self._check_protocol_interface_references(config, result)
        self._check_bgp_policy_references(config, result)
        self._check_routing_instance_interface_references(config, result)
        self._check_policy_prefix_list_references(config, result)

    def _check_vlan_member_references(self, config: NOSConfig, result: ValidationResult) -> None:
        """Switchport vlan member names must resolve to a defined VLAN (or be 'all' / numeric)."""
        vlan_names = set(config.vlans.keys())
        for iface_name, iface in config.interfaces.items():
            if not iface.unit:
                continue
            for unit_id, unit in iface.unit.items():
                if not unit.family_ethernet_switching:
                    continue
                sw = unit.family_ethernet_switching
                if not sw.vlan or not sw.vlan.members:
                    continue
                path = (
                    f"interfaces.{iface_name}.unit.{unit_id}"
                    ".family_ethernet_switching.vlan.members"
                )
                for member in sw.vlan.members:
                    if member == "all":
                        continue
                    if isinstance(member, int):
                        if not (1 <= member <= 4094):
                            result.add_error(path, f"VLAN ID {member!r} out of range (1–4094)")
                    elif member not in vlan_names:
                        result.add_error(
                            path, f"VLAN {member!r} is not defined in vlans"
                        )

    def _check_l3_interface_references(self, config: NOSConfig, result: ValidationResult) -> None:
        """A VLAN l3_interface 'irb.N' must have a matching interfaces.irb.unit.N entry."""
        for vlan_name, vlan in config.vlans.items():
            if not vlan.l3_interface:
                continue
            # format already validated by schema: irb.<digits>
            unit_id = vlan.l3_interface.split(".")[1]
            irb = config.interfaces.get("irb")
            if irb is None or irb.unit is None or unit_id not in irb.unit:
                result.add_error(
                    f"vlans.{vlan_name}.l3_interface",
                    f"{vlan.l3_interface!r} does not match any interfaces.irb.unit.{unit_id} entry",
                )

    def _check_protocol_interface_references(
        self, config: NOSConfig, result: ValidationResult
    ) -> None:
        """Interfaces referenced inside protocols must exist in the interfaces stanza."""
        if not config.protocols:
            return
        iface_names = set(config.interfaces.keys())

        if config.protocols.isis:
            for iface_name in config.protocols.isis.interface:
                if iface_name not in iface_names:
                    result.add_error(
                        f"protocols.isis.interface.{iface_name}",
                        f"Interface {iface_name!r} is not defined in interfaces",
                    )

        if config.protocols.bgp:
            for group_name, group in config.protocols.bgp.group.items():
                if group.local_address:
                    # local_address is an IP, not an interface ref — already IP-validated
                    pass

    def _check_bgp_policy_references(self, config: NOSConfig, result: ValidationResult) -> None:
        """BGP export / import_policy names must exist in policy_options.policy_statement."""
        if not config.protocols or not config.protocols.bgp:
            return
        defined = set()
        if config.policy_options:
            defined = set(config.policy_options.policy_statement.keys())

        for group_name, group in config.protocols.bgp.group.items():
            for attr, label in ((group.export, "export"), (group.import_policy, "import_policy")):
                if attr is not None and attr not in defined:
                    result.add_error(
                        f"protocols.bgp.group.{group_name}.{label}",
                        f"Policy {attr!r} is not defined in policy_options.policy_statement",
                    )

    def _check_routing_instance_interface_references(
        self, config: NOSConfig, result: ValidationResult
    ) -> None:
        """Interfaces assigned to a routing-instance must exist in the interfaces stanza."""
        iface_names = set(config.interfaces.keys())
        for ri_name, ri in config.routing_instances.items():
            for iface in ri.interface:
                if iface not in iface_names:
                    result.add_error(
                        f"routing_instances.{ri_name}.interface",
                        f"Interface {iface!r} is not defined in interfaces",
                    )

    def _check_policy_prefix_list_references(
        self, config: NOSConfig, result: ValidationResult
    ) -> None:
        """from_config.prefix_list references must exist in policy_options.prefix_list."""
        if not config.policy_options:
            return
        defined = set(config.policy_options.prefix_list.keys())
        for ps_name, ps in config.policy_options.policy_statement.items():
            for term_name, term in ps.term.items():
                if term.from_config and term.from_config.prefix_list:
                    pl = term.from_config.prefix_list
                    if pl not in defined:
                        result.add_error(
                            f"policy_options.policy_statement.{ps_name}.term.{term_name}.from_config.prefix_list",
                            f"prefix_list {pl!r} is not defined in policy_options.prefix_list",
                        )
