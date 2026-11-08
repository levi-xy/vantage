// Network maps: mirrors backend/app/api/map_document.py (Contract 2 "Network maps").
// Every string in a document is user text: render it as React text only.

/** A device colour name (`app/deviceColors.ts`). */
export type MapColor = string;

export interface MapVlan {
  id: string;
  name: string;
  color: MapColor;
}

export interface MapPort {
  id: string;
  label: string;
  kind: "rj45" | "sfp";
  vlans: { untagged: string | null; tagged: string[] };
}

export interface MapPorts {
  source: "device" | "manual";
  layout: "row" | "panel";
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
  icon: MapIcon;
  instance_id: string | null;
  show: { details: boolean; status: boolean; figures: boolean };
  figures: string[];
  color: MapColor | null;
  ports: MapPorts | null;
}

export interface MapGuests {
  auto: boolean;
  hidden: string[];
  order: string[];
  manual: { id: string; label: string; subtitle: string }[];
}

export interface MapFrame extends MapGeometry {
  kind: "frame";
  label: string;
  instance_id: string | null;
  color: MapColor | null;
  guests: MapGuests;
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
}

export interface MapLegend extends MapGeometry {
  kind: "legend";
}

export type MapElement = MapBox | MapFrame | MapCable | MapText | MapTable | MapLegend;

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
