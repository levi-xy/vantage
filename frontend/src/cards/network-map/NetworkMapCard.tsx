import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import type { DeviceLink, Instance } from "../../api";
import { useApiClient } from "../../app/session";
import { useUnits } from "../../app/units";
import { buttonPrimary, buttonSecondary, input } from "../../components/styles";
import { firstValue } from "../metrics/promql";
import { shownState, useScrapeStates } from "../metrics/scrapeStatus";
import { useFleetQuery } from "../metrics/useMetricQuery";
import { rateQuery } from "../throughput/ports";
import type { PortOption, PortRef } from "../throughput/ports";
import { usePortOptions } from "../throughput/ThroughputCard";
import type { WidgetProps, WidgetSettingsProps } from "../widgets";
import { circleLayout, drawableLinks, findPort, MAP_HEIGHT, MAP_WIDTH } from "./mapLayout";
import type { Point } from "./mapLayout";

const LINKS_KEY = ["links"];

const STATE_COLOUR: Record<string, string> = {
  up: "#10b981",
  down: "#dc2626",
  pending: "#f59e0b",
  unknown: "#94a3b8",
};

function useLinks() {
  const client = useApiClient();
  return useQuery<DeviceLink[]>({
    queryKey: LINKS_KEY,
    queryFn: () => client.links(),
    refetchInterval: 60_000,
  });
}

/** The traffic over a link, when both ends name a port that has counters. */
function LinkTraffic({ a, b }: { a: PortRef | null; b: PortRef | null }) {
  const { formatRate } = useUnits();
  const both = a !== null && b !== null;
  const inExpr = a && b ? rateQuery([a], "in") : "vector(0)";
  const outExpr = a && b ? rateQuery([a], "out") : "vector(0)";
  const rx = useRate(both, inExpr);
  const tx = useRate(both, outExpr);
  if (!both || rx === undefined || tx === undefined) return null;
  return (
    <>
      {" "}
      ↓ {formatRate(rx)} ↑ {formatRate(tx)}
    </>
  );
}

// Fleet queries can't be disabled, so an unused one asks for a constant and is ignored.
function useRate(enabled: boolean, expr: string): number | undefined {
  const result = useFleetQuery(expr);
  return enabled && !result.isError ? firstValue(result.data) : undefined;
}

function Line({
  link,
  from,
  to,
  groups,
}: {
  link: DeviceLink;
  from: Point;
  to: Point;
  groups: { instance: Instance; ports: PortOption[] }[];
}) {
  const a = findPort(groups, link.a_instance_id, link.a_port);
  const b = findPort(groups, link.b_instance_id, link.b_port);
  return (
    <g>
      <line
        x1={from.x}
        y1={from.y}
        x2={to.x}
        y2={to.y}
        stroke="currentColor"
        strokeOpacity={0.45}
        strokeWidth={2}
      />
      <text
        x={(from.x + to.x) / 2}
        y={(from.y + to.y) / 2 - 4}
        textAnchor="middle"
        fontSize={10}
        fill="currentColor"
      >
        {link.label}
        <LinkTraffic a={a} b={b} />
      </text>
    </g>
  );
}

/** Instances as coloured nodes, joined by the links an admin drew. */
export function NetworkMapCard({ instances }: WidgetProps) {
  const links = useLinks();
  const scrape = useScrapeStates(instances);
  const { groups } = usePortOptions(instances);
  const points = circleLayout(instances);
  if (instances.length === 0) {
    return <p className="p-4 text-sm text-slate-600 dark:text-slate-400">No devices yet.</p>;
  }
  const lines = drawableLinks(links.data ?? [], instances);
  return (
    <div className="flex h-full min-w-0 flex-col p-2">
      <svg
        viewBox={`0 0 ${MAP_WIDTH} ${MAP_HEIGHT}`}
        role="img"
        aria-label="Network map"
        className="h-full w-full text-slate-700 dark:text-slate-300"
      >
        {lines.map((l) => (
          <Line
            key={l.id}
            link={l}
            from={points.get(l.a_instance_id) as Point}
            to={points.get(l.b_instance_id) as Point}
            groups={groups}
          />
        ))}
        {instances.map((i) => {
          const p = points.get(i.id) as Point;
          const state = shownState(i, scrape);
          return (
            <g key={i.id}>
              <circle cx={p.x} cy={p.y} r={12} fill={STATE_COLOUR[state]} stroke="white">
                <title>{`${i.name}: ${state}`}</title>
              </circle>
              <text
                x={p.x}
                y={p.y + 26}
                textAnchor="middle"
                fontSize={11}
                fill="currentColor"
                className="select-none"
              >
                {i.name.length > 22 ? `${i.name.slice(0, 21)}…` : i.name}
              </text>
            </g>
          );
        })}
      </svg>
      {links.isError && (
        <p className="text-xs text-red-700 dark:text-red-400">Could not load the links.</p>
      )}
    </div>
  );
}

/** Add and remove links, shown in edit mode (the ⚙ button on the card). */
export function NetworkMapSettings({ instances }: WidgetSettingsProps) {
  const client = useApiClient();
  const queryClient = useQueryClient();
  const links = useLinks();
  const { groups } = usePortOptions(instances);
  const [a, setA] = useState("");
  const [b, setB] = useState("");
  const [aPort, setAPort] = useState("");
  const [bPort, setBPort] = useState("");
  const [label, setLabel] = useState("");
  const [error, setError] = useState<string | null>(null);
  const names = new Map(instances.map((i) => [i.id, i.name]));
  const done = () => queryClient.invalidateQueries({ queryKey: LINKS_KEY });
  const fail = (e: unknown) => setError(e instanceof Error ? e.message : "Request failed.");

  const add = useMutation({
    mutationFn: () =>
      client.createLink({
        a_instance_id: a,
        b_instance_id: b,
        a_port: aPort.trim() || null,
        b_port: bPort.trim() || null,
        label: label.trim() || null,
      }),
    onSuccess: () => {
      setError(null);
      setAPort("");
      setBPort("");
      setLabel("");
      return done();
    },
    onError: fail,
  });
  const remove = useMutation({
    mutationFn: (id: number) => client.deleteLink(id),
    onSuccess: () => {
      setError(null);
      return done();
    },
    onError: fail,
  });

  const portList = (id: string, listId: string) => (
    <datalist id={listId}>
      {(groups.find((g) => g.instance.id === id)?.ports ?? []).map((o) => (
        <option key={o.port.iface} value={o.port.iface}>
          {o.name}
        </option>
      ))}
    </datalist>
  );
  const select = (value: string, onChange: (v: string) => void, aria: string) => (
    <select
      className={input}
      aria-label={aria}
      value={value}
      onChange={(e) => onChange(e.target.value)}
    >
      <option value="">Choose a device…</option>
      {instances.map((i) => (
        <option key={i.id} value={i.id}>
          {i.name}
        </option>
      ))}
    </select>
  );

  return (
    <div className="flex flex-col gap-3">
      <p className="text-sm text-slate-600 dark:text-slate-400">
        Draw a cable between two devices. Name the port on both ends to see the traffic on the line.
      </p>
      <ul className="flex flex-col gap-1">
        {(links.data ?? []).map((l) => (
          <li key={l.id} className="flex items-center justify-between gap-2 text-sm">
            <span className="min-w-0 truncate">
              {names.get(l.a_instance_id) ?? "?"}
              {l.a_port ? ` (${l.a_port})` : ""} — {names.get(l.b_instance_id) ?? "?"}
              {l.b_port ? ` (${l.b_port})` : ""}
              {l.label ? `: ${l.label}` : ""}
            </span>
            <button
              type="button"
              className={buttonSecondary}
              onClick={() => remove.mutate(l.id)}
              aria-label={`Remove link ${names.get(l.a_instance_id) ?? "?"} to ${names.get(l.b_instance_id) ?? "?"}`}
            >
              Remove
            </button>
          </li>
        ))}
        {links.data?.length === 0 && <li className="text-sm">No links yet.</li>}
      </ul>
      <form
        className="grid gap-2 sm:grid-cols-2"
        onSubmit={(e) => {
          e.preventDefault();
          add.mutate();
        }}
      >
        {select(a, setA, "Device A")}
        {select(b, setB, "Device B")}
        <input
          className={input}
          aria-label="Port on A"
          placeholder="Port on A (optional)"
          list="map-ports-a"
          maxLength={64}
          value={aPort}
          onChange={(e) => setAPort(e.target.value)}
        />
        <input
          className={input}
          aria-label="Port on B"
          placeholder="Port on B (optional)"
          list="map-ports-b"
          maxLength={64}
          value={bPort}
          onChange={(e) => setBPort(e.target.value)}
        />
        {portList(a, "map-ports-a")}
        {portList(b, "map-ports-b")}
        <input
          className={`${input} sm:col-span-2`}
          aria-label="Label"
          placeholder="Label (optional)"
          maxLength={128}
          value={label}
          onChange={(e) => setLabel(e.target.value)}
        />
        <div className="sm:col-span-2">
          <button
            type="submit"
            className={buttonPrimary}
            disabled={!a || !b || a === b || add.isPending}
          >
            Add link
          </button>
        </div>
      </form>
      {error && (
        <p role="alert" className="text-sm text-red-700 dark:text-red-400">
          {error}
        </p>
      )}
    </div>
  );
}
