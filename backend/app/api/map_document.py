"""The network-map document (M9.1; Contract 2 "Network maps", approved 2026-11-07).

Strict: unknown fields are refused, every list and string is bounded. Strings are plain
text; the frontend never renders them as HTML. ``instance_id`` is not checked against
the instances (a removed device is kept and shown as removed).
"""

from typing import Annotated, Literal

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


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Vlan(_Strict):
    id: VlanId
    name: Annotated[str, Field(min_length=1, max_length=32)]
    color: Color


class PortVlans(_Strict):
    untagged: VlanId | None = None
    tagged: Annotated[list[VlanId], Field(max_length=16)] = []


class Port(_Strict):
    id: Annotated[str, Field(min_length=1, max_length=64)]
    label: Annotated[str, Field(max_length=16)] = ""
    kind: Literal["rj45", "sfp"] = "rj45"
    vlans: PortVlans = PortVlans()


class Ports(_Strict):
    source: Literal["device", "manual"]
    layout: Literal["row", "panel"] = "row"
    ports: Annotated[list[Port], Field(max_length=64)] = []


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


class Box(_Geometry):
    kind: Literal["box"]
    label: Str64 = ""
    subtitle: Str64 = ""
    icon: Literal[
        "internet",
        "cloud",
        "router",
        "firewall",
        "switch",
        "server",
        "nas",
        "ap",
        "pc",
        "laptop",
        "phone",
        "camera",
        "printer",
        "iot",
        "generic",
    ] = "generic"
    instance_id: InstanceRef = None
    show: Show = Show()
    figures: Annotated[
        list[Annotated[str, Field(min_length=1, max_length=64)]], Field(max_length=3)
    ] = []
    color: Color | None = None
    ports: Ports | None = None


class ManualGuest(_Strict):
    id: Annotated[str, Field(min_length=1, max_length=32)]
    label: Str64 = ""
    subtitle: Str64 = ""


class Guests(_Strict):
    auto: bool = True
    hidden: Annotated[list[Str64], Field(max_length=200)] = []
    order: Annotated[list[Str64], Field(max_length=200)] = []
    manual: Annotated[list[ManualGuest], Field(max_length=50)] = []


class Frame(_Geometry):
    kind: Literal["frame"]
    label: Str64 = ""
    instance_id: InstanceRef = None
    color: Color | None = None
    guests: Guests = Guests()


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


class Legend(_Geometry):
    kind: Literal["legend"]


Element = Annotated[Box | Frame | Cable | Text | Table | Legend, Field(discriminator="kind")]


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
            if isinstance(el, Box) and el.ports is not None:
                for port in el.ports.ports:
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
                        and target.ports is not None
                        and target.ports.source == "manual"
                        and end.port not in {p.id for p in target.ports.ports}
                    ):
                        raise ValueError(f"cable {el.id!r} names an unknown port")
        return self
