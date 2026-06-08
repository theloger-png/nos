from __future__ import annotations

import ipaddress
import re
from enum import Enum
from typing import Any, Dict, List, Optional, Union

_NET_RE = re.compile(
    r"^49(\.[0-9a-fA-F]{4})+\.[0-9a-fA-F]{4}\.[0-9a-fA-F]{4}\.[0-9a-fA-F]{4}\.00$"
)

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------

class SpeedEnum(str, Enum):
    AUTO = "auto"
    TEN_M = "10m"
    HUNDRED_M = "100m"
    ONE_G = "1g"
    TEN_G = "10g"
    TWENTY_FIVE_G = "25g"
    FORTY_G = "40g"
    HUNDRED_G = "100g"


class DuplexEnum(str, Enum):
    AUTO = "auto"
    HALF = "half"
    FULL = "full"


class UserClassEnum(str, Enum):
    SUPER_USER = "super-user"
    OPERATOR = "operator"
    READ_ONLY = "read-only"


class InterfaceModeEnum(str, Enum):
    ACCESS = "access"
    TRUNK = "trunk"


class InstanceTypeEnum(str, Enum):
    VRF = "vrf"
    VIRTUAL_ROUTER = "virtual-router"


class BgpTypeEnum(str, Enum):
    INTERNAL = "internal"
    EXTERNAL = "external"


class ProtocolEnum(str, Enum):
    BGP = "bgp"
    ISIS = "isis"
    OSPF = "ospf"
    STATIC = "static"
    DIRECT = "direct"


# ---------------------------------------------------------------------------
# IP validation helpers
# ---------------------------------------------------------------------------

def _assert_ip_address(value: str, label: str) -> None:
    try:
        ipaddress.ip_address(value)
    except ValueError:
        raise ValueError(f"Invalid IP address for {label}: {value!r}")


def _assert_ip_prefix(value: str, label: str) -> None:
    try:
        ipaddress.ip_network(value, strict=False)
    except ValueError:
        raise ValueError(f"Invalid IP prefix for {label}: {value!r}")


def _assert_ip_interface(value: str, label: str) -> None:
    """Accept both bare IPs and IP/prefix notation (e.g. 10.0.0.1/30)."""
    try:
        ipaddress.ip_interface(value)
    except ValueError:
        raise ValueError(f"Invalid IP interface for {label}: {value!r}")


# ---------------------------------------------------------------------------
# System models
# ---------------------------------------------------------------------------

class UserAuthentication(BaseModel):
    password: Optional[str] = None  # stored as sha512 crypt hash, never plaintext
    ssh_rsa: Optional[str] = None


class UserConfig(BaseModel):
    user_class: Optional[UserClassEnum] = None
    authentication: UserAuthentication = UserAuthentication()


class SyslogFile(BaseModel):
    any: Optional[str] = None  # log severity level


class SyslogConfig(BaseModel):
    file: Dict[str, SyslogFile] = {}


class NtpConfig(BaseModel):
    server: List[str] = []

    @field_validator("server", mode="before")
    @classmethod
    def coerce_to_list(cls, v: Any) -> List[str]:
        return [v] if isinstance(v, str) else v


class LoginConfig(BaseModel):
    user: Dict[str, UserConfig] = {}


# ---------------------------------------------------------------------------
# DHCP models
# ---------------------------------------------------------------------------

class DhcpPoolRange(BaseModel):
    low: Optional[str] = None
    high: Optional[str] = None

    @field_validator("low", "high")
    @classmethod
    def validate_ip(cls, v: Optional[str]) -> Optional[str]:
        if v is not None:
            _assert_ip_address(v, "DHCP pool range")
        return v


class DhcpPoolConfig(BaseModel):
    range: Optional[DhcpPoolRange] = None
    gateway: Optional[str] = None
    dns_server: Optional[str] = None

    @field_validator("gateway", "dns_server")
    @classmethod
    def validate_ip(cls, v: Optional[str]) -> Optional[str]:
        if v is not None:
            _assert_ip_address(v, "DHCP pool address")
        return v


class DhcpInterfaceConfig(BaseModel):
    pool: List[str] = []

    @field_validator("pool", mode="before")
    @classmethod
    def coerce_pool(cls, v: Any) -> List[str]:
        if isinstance(v, dict):
            return [k for k, val in v.items() if val]
        if isinstance(v, str):
            return [v]
        return list(v) if v else []


class DhcpLocalServerConfig(BaseModel):
    interface: Dict[str, DhcpInterfaceConfig] = {}
    pool: Dict[str, DhcpPoolConfig] = {}


class ServicesConfig(BaseModel):
    dhcp_local_server: Optional[DhcpLocalServerConfig] = None


# ---------------------------------------------------------------------------
# System models (continued)
# ---------------------------------------------------------------------------

class SystemConfig(BaseModel):
    host_name: Optional[str] = None
    domain_name: Optional[str] = None
    name_server: List[str] = []
    ntp: Optional[NtpConfig] = None
    login: Optional[LoginConfig] = None
    syslog: Optional[SyslogConfig] = None
    interface_rename: bool = False
    services: Optional[ServicesConfig] = None

    @field_validator("name_server", mode="before")
    @classmethod
    def coerce_to_list(cls, v: Any) -> List[str]:
        return [v] if isinstance(v, str) else v


# ---------------------------------------------------------------------------
# Interface models
# ---------------------------------------------------------------------------

class InetAddress(BaseModel):
    primary: bool = False


def _validate_address_dict_keys(v: Any, family: str) -> Any:
    if not isinstance(v, dict):
        raise ValueError("address must be a mapping")
    for key in v:
        _assert_ip_interface(key, f"family {family} address")
    return v


class FamilyInet(BaseModel):
    address: Dict[str, InetAddress] = {}
    dhcp: bool = False

    @field_validator("address", mode="before")
    @classmethod
    def validate_address_keys(cls, v: Any) -> Any:
        return _validate_address_dict_keys(v, "inet")

    @model_validator(mode="after")
    def check_dhcp_xor_static(self) -> "FamilyInet":
        if self.dhcp and self.address:
            raise ValueError("family inet dhcp and static address are mutually exclusive")
        return self


class FamilyInet6(BaseModel):
    address: Dict[str, InetAddress] = {}

    @field_validator("address", mode="before")
    @classmethod
    def validate_address_keys(cls, v: Any) -> Any:
        return _validate_address_dict_keys(v, "inet6")


class FamilyIso(BaseModel):
    """IS-IS ISO/CLNS address family.

    Carries the NSAP/NET address used by isisd (e.g. 49.0001.0000.0101.0101.00).
    """

    address: Optional[str] = None

    @field_validator("address")
    @classmethod
    def validate_net(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and not _NET_RE.match(v):
            raise ValueError(
                f"Invalid ISO NET address {v!r}. "
                "Expected format: 49.XXXX.XXXX.XXXX.XXXX.00"
            )
        return v


class VlanMembers(BaseModel):
    members: List[Union[int, str]] = []

    @field_validator("members", mode="before")
    @classmethod
    def coerce_to_list(cls, v: Any) -> List[Union[int, str]]:
        if isinstance(v, (str, int)):
            v = [v]
        result: List[Union[int, str]] = []
        for item in v:
            if isinstance(item, str) and item.isdigit():
                result.append(int(item))
            else:
                result.append(item)
        return result


class EthernetSwitching(BaseModel):
    interface_mode: Optional[InterfaceModeEnum] = None
    vlan: Optional[VlanMembers] = None


class UnitConfig(BaseModel):
    vlan_id: Optional[int] = Field(None, ge=1, le=4094)
    family_ethernet_switching: Optional[EthernetSwitching] = None
    family_inet: Optional[FamilyInet] = None
    family_inet6: Optional[FamilyInet6] = None
    family_iso: Optional[FamilyIso] = None


class InterfaceConfig(BaseModel):
    description: Optional[str] = None
    mtu: Optional[int] = Field(None, ge=256, le=9192)
    speed: Optional[SpeedEnum] = None
    duplex: Optional[DuplexEnum] = None
    disable: bool = False
    family_inet: Optional[FamilyInet] = None
    family_inet6: Optional[FamilyInet6] = None
    family_iso: Optional[FamilyIso] = None
    # Keys are unit numbers stored as strings (JSON object keys are always strings)
    unit: Optional[Dict[str, UnitConfig]] = None

    @model_validator(mode="after")
    def check_switchport_xor_routed(self) -> InterfaceConfig:
        has_routed = self.family_inet is not None or self.family_inet6 is not None
        has_switching = self.unit is not None and any(
            u.family_ethernet_switching is not None for u in self.unit.values()
        )
        if has_routed and has_switching:
            raise ValueError(
                "switchport (unit/family_ethernet_switching) and routed port "
                "(family_inet/family_inet6) are mutually exclusive on the same interface"
            )
        return self


# ---------------------------------------------------------------------------
# VLAN models
# ---------------------------------------------------------------------------

class VlanConfig(BaseModel):
    vlan_id: Optional[int] = Field(None, ge=1, le=4094)
    description: Optional[str] = None
    l3_interface: Optional[str] = None

    @field_validator("l3_interface")
    @classmethod
    def validate_l3_interface_format(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and not re.fullmatch(r"irb\.\d+", v):
            raise ValueError(
                f"l3_interface must be in the form 'irb.<unit-id>', got: {v!r}"
            )
        return v


# ---------------------------------------------------------------------------
# Routing options models
# ---------------------------------------------------------------------------

class StaticRoute(BaseModel):
    next_hop: Optional[str] = None
    discard: bool = False
    reject: bool = False

    @field_validator("next_hop")
    @classmethod
    def validate_next_hop_ip(cls, v: Optional[str]) -> Optional[str]:
        if v is not None:
            _assert_ip_address(v, "next_hop")
        return v

    @model_validator(mode="after")
    def check_single_action(self) -> StaticRoute:
        count = sum([self.next_hop is not None, self.discard, self.reject])
        if count > 1:
            raise ValueError(
                "Only one of next_hop, discard, or reject may be set on a static route"
            )
        return self


class StaticRoutingConfig(BaseModel):
    route: Dict[str, StaticRoute] = {}

    @field_validator("route", mode="before")
    @classmethod
    def validate_route_prefixes(cls, v: Any) -> Any:
        if not isinstance(v, dict):
            raise ValueError("route must be a mapping")
        for key in v:
            _assert_ip_prefix(key, "static route prefix")
        return v


class RoutingOptionsConfig(BaseModel):
    static: Optional[StaticRoutingConfig] = None
    router_id: Optional[str] = None
    autonomous_system: Optional[int] = Field(None, ge=1, le=4294967295)

    @field_validator("router_id")
    @classmethod
    def validate_router_id(cls, v: Optional[str]) -> Optional[str]:
        if v is not None:
            _assert_ip_address(v, "router_id")
        return v


# ---------------------------------------------------------------------------
# Protocol models — IS-IS
# ---------------------------------------------------------------------------

class IsisInterfaceConfig(BaseModel):
    point_to_point: bool = False
    passive: bool = False
    level_1_disable: bool = False
    level_2_disable: bool = False
    hello_interval: Optional[int] = Field(None, ge=1, le=65535)
    hold_time: Optional[int] = Field(None, ge=1, le=65535)


class IsisLevelConfig(BaseModel):
    wide_metrics_only: bool = False
    disable: bool = False


class IsisConfig(BaseModel):
    interface: Dict[str, IsisInterfaceConfig] = {}
    level_1: Optional[IsisLevelConfig] = None
    level_2: Optional[IsisLevelConfig] = None


# ---------------------------------------------------------------------------
# Protocol models — BGP
# ---------------------------------------------------------------------------

class BgpNeighbor(BaseModel):
    description: Optional[str] = None
    authentication_key: Optional[str] = None
    hold_time: Optional[int] = Field(None, ge=0, le=65535)


_REDIST_PROTOCOLS: frozenset[str] = frozenset(
    {"connected", "static", "kernel", "isis", "ospf", "rip"}
)


class BgpFamilyInet(BaseModel):
    unicast: bool = False
    redistribute: Dict[str, bool] = {}

    @field_validator("redistribute", mode="before")
    @classmethod
    def validate_redistribute(cls, v: Any) -> Any:
        if isinstance(v, dict):
            for proto in v:
                if proto not in _REDIST_PROTOCOLS:
                    raise ValueError(
                        f"Invalid redistribute protocol {proto!r}. "
                        f"Must be one of: {', '.join(sorted(_REDIST_PROTOCOLS))}"
                    )
        return v


class BgpFamilyInet6(BaseModel):
    unicast: bool = False
    redistribute: Dict[str, bool] = {}

    @field_validator("redistribute", mode="before")
    @classmethod
    def validate_redistribute(cls, v: Any) -> Any:
        if isinstance(v, dict):
            for proto in v:
                if proto not in _REDIST_PROTOCOLS:
                    raise ValueError(
                        f"Invalid redistribute protocol {proto!r}. "
                        f"Must be one of: {', '.join(sorted(_REDIST_PROTOCOLS))}"
                    )
        return v


class BgpGroup(BaseModel):
    group_type: Optional[BgpTypeEnum] = None
    local_as: Optional[int] = Field(None, ge=1, le=4294967295)
    peer_as: Optional[int] = Field(None, ge=1, le=4294967295)
    local_address: Optional[str] = None
    local_interface: Optional[str] = None
    neighbor: Dict[str, BgpNeighbor] = {}
    export: Optional[str] = None
    import_policy: Optional[str] = None
    family_inet: Optional[BgpFamilyInet] = None
    family_inet6: Optional[BgpFamilyInet6] = None

    @field_validator("local_address")
    @classmethod
    def validate_local_address(cls, v: Optional[str]) -> Optional[str]:
        if v is not None:
            _assert_ip_address(v, "local_address")
        return v

    @field_validator("neighbor", mode="before")
    @classmethod
    def validate_neighbor_ips(cls, v: Any) -> Any:
        if not isinstance(v, dict):
            raise ValueError("neighbor must be a mapping")
        for key in v:
            _assert_ip_address(key, "neighbor address")
        return v

    @model_validator(mode="after")
    def check_ebgp_requires_peer_as(self) -> BgpGroup:
        if self.group_type == BgpTypeEnum.EXTERNAL and self.peer_as is None:
            raise ValueError(
                "eBGP group (group_type: external) requires peer_as to be set"
            )
        return self


class BgpConfig(BaseModel):
    group: Dict[str, BgpGroup] = {}
    family_inet: Optional[BgpFamilyInet] = None
    family_inet6: Optional[BgpFamilyInet6] = None


class ProtocolsConfig(BaseModel):
    isis: Optional[IsisConfig] = None
    bgp: Optional[BgpConfig] = None


# ---------------------------------------------------------------------------
# Policy options models
# ---------------------------------------------------------------------------

class PolicyFromConfig(BaseModel):
    prefix_list: Optional[str] = None
    protocol: Optional[ProtocolEnum] = None
    route_filter: Optional[str] = None  # free-form for phase 1


class PolicyThenConfig(BaseModel):
    accept: bool = False
    reject: bool = False
    next_hop: Optional[str] = None
    local_preference: Optional[int] = Field(None, ge=0, le=4294967295)
    metric: Optional[int] = None
    community_add: Optional[str] = None

    @field_validator("next_hop")
    @classmethod
    def validate_next_hop(cls, v: Optional[str]) -> Optional[str]:
        if v is not None:
            _assert_ip_address(v, "policy then next_hop")
        return v


class PolicyTerm(BaseModel):
    from_config: Optional[PolicyFromConfig] = None
    then_config: Optional[PolicyThenConfig] = None


class FinalTermAction(BaseModel):
    """Action for the unnamed final term of a policy-statement.

    Exactly one of accept/reject/next_policy should be set.
    """
    accept: bool = False
    reject: bool = False
    next_policy: bool = False


class PolicyStatement(BaseModel):
    term: Dict[str, PolicyTerm] = {}
    then: Optional[FinalTermAction] = None


class PolicyOptionsConfig(BaseModel):
    prefix_list: Dict[str, List[str]] = {}
    policy_statement: Dict[str, PolicyStatement] = {}


# ---------------------------------------------------------------------------
# Routing instances
# ---------------------------------------------------------------------------

class RoutingInstanceConfig(BaseModel):
    instance_type: Optional[InstanceTypeEnum] = None
    interface: List[str] = []
    route_distinguisher: Optional[str] = None
    vrf_target: Optional[str] = None
    routing_options: Optional[RoutingOptionsConfig] = None
    protocols: Optional[ProtocolsConfig] = None


# ---------------------------------------------------------------------------
# NAT models
# ---------------------------------------------------------------------------

class NatStaticRule(BaseModel):
    source: Optional[str] = None
    translated: Optional[str] = None

    @field_validator("source")
    @classmethod
    def validate_source(cls, v: Optional[str]) -> Optional[str]:
        if v is not None:
            _assert_ip_prefix(v, "nat static rule source")
        return v

    @field_validator("translated")
    @classmethod
    def validate_translated(cls, v: Optional[str]) -> Optional[str]:
        if v is not None:
            _assert_ip_address(v, "nat static rule translated")
        return v


class NatPool(BaseModel):
    address: Optional[str] = None

    @field_validator("address")
    @classmethod
    def validate_address(cls, v: Optional[str]) -> Optional[str]:
        if v is not None:
            _assert_ip_prefix(v, "nat pool address")
        return v


class NatSourceRule(BaseModel):
    match_source: Optional[str] = None
    then_pool: Optional[str] = None
    interface: Optional[str] = None

    @field_validator("match_source")
    @classmethod
    def validate_match_source(cls, v: Optional[str]) -> Optional[str]:
        if v is not None:
            _assert_ip_prefix(v, "nat source rule match source")
        return v


class NatDestinationRule(BaseModel):
    match_destination: Optional[str] = None
    match_destination_port: Optional[int] = Field(None, ge=1, le=65535)
    then_destination: Optional[str] = None
    then_destination_port: Optional[int] = Field(None, ge=1, le=65535)

    @field_validator("match_destination", "then_destination")
    @classmethod
    def validate_ip(cls, v: Optional[str]) -> Optional[str]:
        if v is not None:
            _assert_ip_address(v, "nat destination rule IP")
        return v


class NatStaticConfig(BaseModel):
    rule: Dict[str, NatStaticRule] = {}


class NatSourceConfig(BaseModel):
    rule: Dict[str, NatSourceRule] = {}


class NatDestinationConfig(BaseModel):
    rule: Dict[str, NatDestinationRule] = {}


class NatConfig(BaseModel):
    static: NatStaticConfig = NatStaticConfig()
    pool: Dict[str, NatPool] = {}
    source: NatSourceConfig = NatSourceConfig()
    destination: NatDestinationConfig = NatDestinationConfig()


class SecurityConfig(BaseModel):
    nat: NatConfig = NatConfig()


# ---------------------------------------------------------------------------
# Top-level configuration
# ---------------------------------------------------------------------------

class NOSConfig(BaseModel):
    system: Optional[SystemConfig] = None
    interfaces: Dict[str, InterfaceConfig] = {}
    vlans: Dict[str, VlanConfig] = {}
    routing_options: Optional[RoutingOptionsConfig] = None
    protocols: Optional[ProtocolsConfig] = None
    policy_options: Optional[PolicyOptionsConfig] = None
    routing_instances: Dict[str, RoutingInstanceConfig] = {}
    security: SecurityConfig = SecurityConfig()
