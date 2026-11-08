"""The network-map document (M9.1; Contract 2 "Network maps", approved 2026-11-07).

Strict: unknown fields are refused, every list and string is bounded. Strings are plain
text; the frontend never renders them as HTML. ``instance_id`` is not checked against
the instances (a removed device is kept and shown as removed).
"""

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.models.instance import DeviceColor

MAX_ELEMENTS = 500
MAX_VLANS = 64
MAX_DOCUMENT_BYTES = 256 * 1024

Color = DeviceColor
Coord = Annotated[int, Field(ge=0, le=20_000)]
Size = Annotated[int, Field(ge=8, le=4_000)]
ElementId = Annotated[str, Field(min_length=1, max_length=32, pattern=r"^[A-Za-z0-9_-]+$")]
VlanId = Annotated[str, Field(min_length=1, max_length=16)]
Str64 = Annotated[str, Field(max_length=64)]
InstanceRef = Annotated[str, Field(max_length=64)] | None
# A generic glyph, or a dashboard-icons slug (M11.1). Only the pattern is checked: the
# frontend falls back to the generic glyph when the file is missing.
IconRef = Annotated[
    str,
    Field(
        pattern=(
            r"^(internet|cloud|router|firewall|switch|server|nas|ap|pc|laptop|phone|camera"
            r"|printer|iot|generic|di:[a-z0-9-]{1,64})$"
        )
    ),
]
FigureKey = Annotated[str, Field(min_length=1, max_length=64)]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Vlan(_Strict):
    id: VlanId
    name: Annotated[str, Field(min_length=1, max_length=32)]
    color: Color
    subnet: Annotated[str, Field(max_length=43)] = ""
    note: Annotated[str, Field(max_length=64)] = ""


class PortVlans(_Strict):
    untagged: VlanId | None = None
    tagged: Annotated[list[VlanId], Field(max_length=16)] = []


class PortGroup(_Strict):
    id: ElementId
    kind: Literal["rj45", "sfp", "sfp_plus", "sfp28", "qsfp", "console", "usb"] = "rj45"
    count: Annotated[int, Field(ge=1, le=64)] = 1
    rows: Literal[1, 2] = 1
    odd_top: bool = False
    first: Annotated[int, Field(ge=0, le=999)] = 1
    prefix: Annotated[str, Field(max_length=16)] = ""


class Port(_Strict):
    id: Annotated[str, Field(min_length=1, max_length=64)]
    group: Annotated[str, Field(max_length=32)] = ""
    n: Annotated[int, Field(ge=0, le=999)] = 0
    caption: Annotated[str, Field(max_length=16)] = ""
    interface: Str64 = ""
    vlans: PortVlans = PortVlans()
    hidden: bool = False


class Panel(_Strict):
    look: Literal["rack", "desktop", "flat"] = "flat"
    place: Literal["bottom", "top", "side"] = "bottom"
    size: Literal["s", "m", "l"] = "m"
    numbers: bool = True
    captions: Literal["cables", "typed", "off"] = "typed"
    source: Literal["manual", "device"] = "manual"
    groups: Annotated[list[PortGroup], Field(max_length=8)] = []
    ports: Annotated[list[Port], Field(max_length=128)] = []


def legacy_ports_to_panel(old: dict[str, Any]) -> dict[str, Any]:
    """Turn a pre-M11 ``ports`` object into a ``panel`` (ids, VLANs and cables kept)."""
    rows = 2 if old.get("layout") == "panel" else 1
    groups: list[dict[str, Any]] = []
    ports: list[dict[str, Any]] = []
    device = old.get("source") == "device"
    for port in old.get("ports") or []:
        kind = "sfp" if port.get("kind") == "sfp" else "rj45"
        if not groups or groups[-1]["kind"] != kind:
            groups.append(
                {
                    "id": f"g{len(groups) + 1}",
                    "kind": kind,
                    "count": 0,
                    "rows": rows,
                    "odd_top": False,
                    "first": 1,
                    "prefix": "",
                }
            )
        group = groups[-1]
        group["count"] += 1
        ports.append(
            {
                "id": port["id"],
                "group": group["id"],
                "n": group["count"],
                "caption": port.get("label", ""),
                "interface": port["id"] if device else "",
                "vlans": port.get("vlans") or {"untagged": None, "tagged": []},
                "hidden": False,
            }
        )
    return {
        "look": "flat",
        "place": "bottom",
        "size": "m",
        "numbers": True,
        "captions": "typed",
        "source": "device" if device else "manual",
        "groups": groups,
        "ports": ports,
    }


class Show(_Strict):
    details: bool = True
    status: bool = True
    figures: bool = True


class _Geometry(_Strict):
    id: ElementId
    x: Coord
    y: Coord
    w: Size
    h: Size


class Item(_Strict):
    id: Annotated[str, Field(min_length=1, max_length=32)]
    label: Annotated[str, Field(max_length=32)] = ""
    value: Annotated[str, Field(max_length=32)] = ""
    sub: Annotated[str, Field(max_length=48)] = ""
    icon: IconRef | None = None
    color: Color | None = None
    figure: FigureKey | None = None


class Section(_Strict):
    id: Annotated[str, Field(min_length=1, max_length=32)]
    title: Annotated[str, Field(max_length=32)] = ""
    kind: Literal["tiles", "services", "list"] = "tiles"
    columns: Annotated[int, Field(ge=1, le=4)] = 2
    items: Annotated[list[Item], Field(max_length=12)] = []


Sections = Annotated[list[Section], Field(max_length=4)]


class BayDisk(_Strict):
    bay: Annotated[int, Field(ge=1, le=16)]
    label: Annotated[str, Field(max_length=16)] = ""
    disk: Str64 = ""


class Nas(_Strict):
    bays: Annotated[int, Field(ge=1, le=16)] = 4
    bay_disks: Annotated[list[BayDisk], Field(max_length=16)] = []


class Box(_Geometry):
    kind: Literal["box"]
    label: Str64 = ""
    subtitle: Str64 = ""
    shape: Literal["card", "cloud", "device", "nas", "ap"] = "card"
    role: Annotated[str, Field(max_length=32)] = ""
    icon: IconRef = "generic"
    model: Str64 = ""
    address: Str64 = ""
    version: Str64 = ""
    instance_id: InstanceRef = None
    show: Show = Show()
    figures: Annotated[list[FigureKey], Field(max_length=3)] = []
    sections: Sections = []
    color: Color | None = None
    panel: Panel | None = None
    nas: Nas | None = None

    @model_validator(mode="before")
    @classmethod
    def _legacy_ports(cls, data: Any) -> Any:
        """Documents saved before M11 carry ``ports``; read them as a ``panel``."""
        if isinstance(data, dict) and "ports" in data:
            data = dict(data)
            old = data.pop("ports")
            if old is not None and data.get("panel") is None and isinstance(old, dict):
                data["panel"] = legacy_ports_to_panel(old)
        return data


class ManualGuest(_Strict):
    id: Annotated[str, Field(min_length=1, max_length=32)]
    label: Str64 = ""
    subtitle: Str64 = ""


class Guests(_Strict):
    auto: bool = True
    hidden: Annotated[list[Str64], Field(max_length=200)] = []
    order: Annotated[list[Str64], Field(max_length=200)] = []
    manual: Annotated[list[ManualGuest], Field(max_length=50)] = []


class GuestGroup(_Strict):
    id: Annotated[str, Field(min_length=1, max_length=32)]
    title: Annotated[str, Field(max_length=32)] = ""
    members: Annotated[list[Str64], Field(max_length=200)] = []


class Side(_Strict):
    model: Str64 = ""
    address: Str64 = ""
    version: Str64 = ""
    sections: Sections = []


class Frame(_Geometry):
    kind: Literal["frame"]
    label: Str64 = ""
    instance_id: InstanceRef = None
    color: Color | None = None
    guests: Guests = Guests()
    groups: Annotated[list[GuestGroup], Field(max_length=12)] = []
    bars: bool = True
    side: Side | None = None
    model: Str64 = ""
    address: Str64 = ""
    version: Str64 = ""


class End(_Strict):
    element: ElementId
    port: Str64 | None = None


class Cable(_Strict):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    id: ElementId
    kind: Literal["cable"]
    from_: End = Field(alias="from")
    to: End
    speed: (
        Literal["100M", "1G", "2.5G", "5G", "10G", "25G", "40G", "100G", "wifi", "virtual"] | None
    ) = None
    style: Literal["solid", "dotted", "dashed"] = "solid"
    role: Literal["access", "trunk"] | None = None
    vlan: VlanId | None = None
    color: Color | None = None
    label: Str64 = ""
    show_rate: bool = False
    bends: Annotated[list[tuple[Coord, Coord]], Field(max_length=16)] = []


class Text(_Geometry):
    kind: Literal["text"]
    text: Annotated[str, Field(max_length=4000)] = ""
    size: Literal["s", "m", "l"] = "m"
    panel: bool = True


class TableRow(_Strict):
    cells: Annotated[list[Annotated[str, Field(max_length=128)]], Field(max_length=8)] = []
    color: Color | None = None


class Table(_Geometry):
    kind: Literal["table"]
    title: Str64 = ""
    columns: Annotated[list[Annotated[str, Field(max_length=32)]], Field(max_length=8)] = []
    rows: Annotated[list[TableRow], Field(max_length=100)] = []
    badges: Annotated[list[Annotated[int, Field(ge=0, le=7)]], Field(max_length=8)] = []


class Legend(_Geometry):
    kind: Literal["legend"]


class Networks(_Geometry):
    kind: Literal["networks"]


Element = Annotated[
    Box | Frame | Cable | Text | Table | Legend | Networks, Field(discriminator="kind")
]


class MapDocument(_Strict):
    version: Literal[1] = 1
    vlans: Annotated[list[Vlan], Field(max_length=MAX_VLANS)] = []
    elements: Annotated[list[Element], Field(max_length=MAX_ELEMENTS)] = []

    @model_validator(mode="after")
    def _references(self) -> "MapDocument":
        vlan_ids = [v.id for v in self.vlans]
        if len(set(vlan_ids)) != len(vlan_ids):
            raise ValueError("duplicate vlan id")
        known_vlans = set(vlan_ids)
        ids = [e.id for e in self.elements]
        if len(set(ids)) != len(ids):
            raise ValueError("duplicate element id")
        ends = {e.id: e for e in self.elements if isinstance(e, Box | Frame)}

        def check_vlan(vlan: str | None) -> None:
            if vlan is not None and vlan not in known_vlans:
                raise ValueError(f"unknown vlan {vlan!r}")

        for el in self.elements:
            if isinstance(el, Box) and el.panel is not None:
                group_ids = {g.id for g in el.panel.groups}
                if len(group_ids) != len(el.panel.groups):
                    raise ValueError("duplicate port group id")
                for port in el.panel.ports:
                    if port.group and port.group not in group_ids:
                        raise ValueError(f"port {port.id!r} names an unknown group")
                    check_vlan(port.vlans.untagged)
                    for tagged in port.vlans.tagged:
                        check_vlan(tagged)
            elif isinstance(el, Cable):
                check_vlan(el.vlan)
                if el.from_.element == el.to.element:
                    raise ValueError("a cable can't join an element to itself")
                for end in (el.from_, el.to):
                    target = ends.get(end.element)
                    if target is None:
                        raise ValueError(f"cable {el.id!r} points at a missing box or frame")
                    if (
                        end.port is not None
                        and isinstance(target, Box)
                        and target.panel is not None
                        and target.panel.source == "manual"
                        and end.port not in {p.id for p in target.panel.ports}
                    ):
                        raise ValueError(f"cable {el.id!r} names an unknown port")
        return self
