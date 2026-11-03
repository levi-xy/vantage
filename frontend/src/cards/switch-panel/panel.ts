// Where each port sits on a switch's front panel. Pure, so it's easy to test.
import type { Port } from "../snmp-ports/ports";

export const BLOCK_SIZE = 8;

export interface Placed<T> {
  port: T;
  /** 1-based column within its block. */
  column: number;
  /** 1 = top row, 2 = bottom row. */
  row: 1 | 2;
  /** An SFP cage's own number (sfp-sfpplus2 -> 2); copper ports show their index. */
  cage?: number;
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

/** The number in an SFP-named port id (sfp-sfpplus3 -> 3), if there is one. */
function sfpNumber(id: string): number | undefined {
  const m = /sfp.*?(\d+)\D*$/i.exec(id);
  return m ? Number(m[1]) : undefined;
}

/**
 * The faceplate: copper ports in blocks of 8, then the SFP cages in one separate block.
 *
 * A cage is known by its name or a 10G speed, but an empty cage (no module, no link) may
 * report neither. The cages sit at the end of the panel, so when the first known cage is
 * among the last 8 ports, every port after it with the same kind of id is a cage too (SwOS: port25-28 with only
 * some of them linked).
 *
 * Cages are numbered on their own (SFP+ 1-4, not the ifIndex 25-28): by the number in an
 * SFP-named id when every cage has a distinct one, else in order. Odd on the bottom, even
 * on top, like the copper ports.
 */
export function faceplate<T extends Pick<Port, "id" | "speed">>(
  ports: T[],
): { copper: Placed<T>[][]; sfp: Placed<T>[] } {
  const first = ports.findIndex(isSfp);
  const trailing = first > 0 && ports.length - first <= BLOCK_SIZE;
  // Only ports named like that cage (port25 -> port26) follow it into the SFP block, so a
  // bridge, VLAN or copper port listed after the cages stays out.
  const shape = (id: string) => id.replace(/\d+/g, "#");
  const cageShape = first >= 0 ? shape(ports[first]?.id ?? "") : "";
  const cage = (p: T, i: number) =>
    isSfp(p) || (trailing && i > first && shape(p.id) === cageShape);
  const cages = ports.filter(cage);
  const numbers = cages.map((p) => sfpNumber(p.id));
  const byId = numbers.every((n) => n !== undefined) && new Set(numbers).size === numbers.length;
  const numbered = cages
    .map((port, j) => ({ port, cage: byId ? (numbers[j] as number) : j + 1 }))
    .sort((a, b) => a.cage - b.cage);
  return {
    copper: panelBlocks(ports.filter((p, i) => !cage(p, i))),
    sfp: numbered.map(({ port, cage }, j) => ({
      port,
      cage,
      column: Math.floor(j / 2) + 1,
      row: j % 2 === 0 ? 2 : 1,
    })),
  };
}

/** An SFP/SFP+ cage rather than a copper jack: by its stable name or a 10G+ speed. */
export function isSfp(port: Pick<Port, "id" | "speed">): boolean {
  return /sfp/i.test(port.id) || (port.speed ?? 0) >= 10e9;
}
