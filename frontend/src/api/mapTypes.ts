// Network maps: mirrors backend/app/api/map_document.py (Contract 2 "Network maps").
// Every string in a document is user text: render it as React text only.

/** A device colour name (`app/deviceColors.ts`). */
export type MapColor = string;

export interface MapVlan {
  id: string;
  name: string;
  color: MapColor;
  subnet: string;
  note: string;
}

export interface MapVlanSettings {
  untagged: string | null;
  tagged: string[];
}

export type MapPortKind = "rj45" | "sfp" | "sfp_plus" | "sfp28" | "qsfp" | "console" | "usb";

export interface MapPortGroup {
  id: string;
  kind: MapPortKind;
  count: number;
  rows: 1 | 2;
  /** Two rows: fill the top row first, or number columns in pairs with odd on top / bottom. */
  order: "rows" | "odd_top" | "odd_bottom";
  first: number;
  prefix: string;
}

export interface MapPort {
  id: string;
  group: string;
  n: number;
  caption: string;
  interface: string;
  vlans: MapVlanSettings;
  hidden: boolean;
}

/** A box's front panel (was `ports` before M11). */
export interface MapPanel {
  look: "rack" | "desktop" | "flat";
  place: "bottom" | "top" | "side";
  size: "s" | "m" | "l";
  numbers: boolean;
  captions: "cables" | "typed" | "off";
  source: "manual" | "device";
  groups: MapPortGroup[];
  ports: MapPort[];
}

export type MapIcon =
  | "internet"
  | "cloud"
  | "router"
  | "firewall"
  | "switch"
  | "server"
  | "nas"
  | "ap"
  | "pc"
  | "laptop"
  | "phone"
  | "camera"
  | "printer"
  | "iot"
  | "generic";

/** A generic glyph, or a dashboard-icons slug (`di:<slug>`, files under /icons/di/). */
export type MapIconRef = MapIcon | `di:${string}`;

export interface MapItem {
  id: string;
  label: string;
  value: string;
  sub: string;
  icon: MapIconRef | null;
  color: MapColor | null;
  figure: string | null;
}

export interface MapSection {
  id: string;
  title: string;
  kind: "tiles" | "services" | "list";
  columns: 1 | 2 | 3 | 4;
  items: MapItem[];
}

export interface MapNas {
  bays: number;
  bay_disks: { bay: number; label: string; disk: string }[];
}

interface MapGeometry {
  id: string;
  x: number;
  y: number;
  w: number;
  h: number;
}

export interface MapBox extends MapGeometry {
  kind: "box";
  label: string;
  subtitle: string;
  shape: "card" | "cloud" | "device" | "nas" | "ap";
  role: string;
  icon: MapIconRef;
  model: string;
  address: string;
  version: string;
  instance_id: string | null;
  show: { details: boolean; status: boolean; figures: boolean };
  figures: string[];
  sections: MapSection[];
  color: MapColor | null;
  panel: MapPanel | null;
  nas: MapNas | null;
}

export interface MapGuests {
  auto: boolean;
  hidden: string[];
  order: string[];
  manual: { id: string; label: string; subtitle: string }[];
}

export interface MapGuestGroup {
  id: string;
  title: string;
  members: string[];
}

export interface MapFrame extends MapGeometry {
  kind: "frame";
  label: string;
  instance_id: string | null;
  color: MapColor | null;
  guests: MapGuests;
  groups: MapGuestGroup[];
  bars: boolean;
  side: { model: string; address: string; version: string; sections: MapSection[] } | null;
  model: string;
  address: string;
  version: string;
}

export type MapSpeed =
  "100M" | "1G" | "2.5G" | "5G" | "10G" | "25G" | "40G" | "100G" | "wifi" | "virtual";

export interface MapCableEnd {
  element: string;
  port: string | null;
}

export interface MapCable {
  id: string;
  kind: "cable";
  /** The cable's start is stored under the JSON key `from`. */
  from: MapCableEnd;
  to: MapCableEnd;
  speed: MapSpeed | null;
  style: "solid" | "dotted" | "dashed";
  role: "access" | "trunk" | null;
  vlan: string | null;
  color: MapColor | null;
  label: string;
  show_rate: boolean;
  bends: [number, number][];
}

export interface MapText extends MapGeometry {
  kind: "text";
  text: string;
  size: "s" | "m" | "l";
  panel: boolean;
}

export interface MapTableRow {
  cells: string[];
  color: MapColor | null;
}

export interface MapTable extends MapGeometry {
  kind: "table";
  title: string;
  columns: string[];
  rows: MapTableRow[];
  badges: number[];
}

export interface MapLegend extends MapGeometry {
  kind: "legend";
}

export interface MapNetworks extends MapGeometry {
  kind: "networks";
}

export type MapElement =
  MapBox | MapFrame | MapCable | MapText | MapTable | MapLegend | MapNetworks;

export interface MapDocument {
  version: 1;
  vlans: MapVlan[];
  elements: MapElement[];
}

/** `GET /maps`: every map in tab order. */
export interface NetworkMapSummary {
  id: number;
  name: string;
  position: number;
}

/** `GET /maps/{id}`, and the answer to `POST /maps`. */
export interface NetworkMap extends NetworkMapSummary {
  document: MapDocument;
  revision: number;
  updated_at: string;
}
