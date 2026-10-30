// Where each port sits on a switch's front panel. Pure, so it's easy to test.
import type { Port } from "../snmp-ports/ports";

export const BLOCK_SIZE = 8;

export interface Placed<T> {
  port: T;
  /** 1-based column within its block. */
  column: number;
  /** 1 = top row, 2 = bottom row. */
  row: 1 | 2;
}

/**
 * Ports in ifIndex order, in blocks of 8 (a 28-port switch: 8, 8, 8, 4). Within a block the
 * ports go in pairs, odd-numbered on the bottom and even-numbered on top, like most
 * switches; a block of n ports is ceil(n / 2) columns wide.
 */
export function panelBlocks<T>(ports: T[]): Placed<T>[][] {
  const blocks: Placed<T>[][] = [];
  for (let start = 0; start < ports.length; start += BLOCK_SIZE) {
    blocks.push(
      ports.slice(start, start + BLOCK_SIZE).map((port, j) => ({
        port,
        column: Math.floor(j / 2) + 1,
        row: j % 2 === 0 ? 2 : 1,
      })),
    );
  }
  return blocks;
}

/** "26 of 28 up · down: 5, 12" (disabled ports don't count as down). */
export function panelSummary(ports: Pick<Port, "index" | "state">[]): string {
  const up = ports.filter((p) => p.state === "up").length;
  const down = ports.filter((p) => p.state === "down").map((p) => p.index);
  const text = `${up} of ${ports.length} up`;
  return down.length ? `${text} · down: ${down.join(", ")}` : text;
}

/** An SFP/SFP+ cage rather than a copper jack: by its stable name or a 10G+ speed. */
export function isSfp(port: Pick<Port, "id" | "speed">): boolean {
  return /sfp/i.test(port.id) || (port.speed ?? 0) >= 10e9;
}
