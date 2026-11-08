import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { IconPicker } from "./maps/editor/IconPicker";
import { resetIconIndex, searchIcons } from "./maps/editor/iconIndex";
import { MapIcon } from "./maps/MapIcon";

const INDEX = [
  { slug: "proxmox", name: "proxmox", aliases: ["pve"] },
  { slug: "pi-hole", name: "pi hole", aliases: ["pihole", "dns blocker"] },
  { slug: "truenas-scale", name: "truenas scale", aliases: [] },
];

afterEach(() => {
  resetIconIndex();
  vi.unstubAllGlobals();
});

describe("map icons", () => {
  it("shows a library icon from Vantage's own server and falls back to the generic glyph", () => {
    const { container } = render(<MapIcon icon="di:proxmox" />);
    const img = container.querySelector("img");
    expect(img?.getAttribute("src")).toBe("/icons/di/proxmox.svg");
    fireEvent.error(img as HTMLImageElement); // the file isn't there
    expect(container.querySelector("img")).toBeNull();
    expect(container.querySelector("svg")).not.toBeNull();
  });

  it("never builds a path from a malformed library name", () => {
    const { container } = render(<MapIcon icon={"di:../x" as never} />);
    expect(container.querySelector("img")).toBeNull();
    expect(container.querySelector("svg")).not.toBeNull();
  });

  it("searches name and aliases, every word having to match", () => {
    expect(searchIcons(INDEX, "pve").map((i) => i.slug)).toEqual(["proxmox"]);
    expect(searchIcons(INDEX, "dns block").map((i) => i.slug)).toEqual(["pi-hole"]);
    expect(searchIcons(INDEX, "nas pi")).toEqual([]);
    expect(searchIcons(INDEX, "", 2)).toHaveLength(2);
  });

  it("picks from the library by search, and from the generic icons", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => new Response(JSON.stringify(INDEX))),
    );
    const onChange = vi.fn();
    render(<IconPicker value="generic" onChange={onChange} />);
    fireEvent.click(screen.getByRole("button", { name: "Generic icon router" }));
    expect(onChange).toHaveBeenLastCalledWith("router");

    fireEvent.click(screen.getByRole("button", { name: "Library" }));
    await screen.findByRole("button", { name: "Library icon truenas scale" });
    fireEvent.change(screen.getByLabelText("Search icons"), { target: { value: "pihole" } });
    await waitFor(() =>
      expect(screen.queryByRole("button", { name: "Library icon truenas scale" })).toBeNull(),
    );
    fireEvent.click(screen.getByRole("button", { name: "Library icon pi hole" }));
    expect(onChange).toHaveBeenLastCalledWith("di:pi-hole");
  });

  it("says so when the library isn't installed", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => new Response("<html></html>")), // the dev server's page fallback
    );
    render(<IconPicker value="di:proxmox" onChange={() => {}} />);
    expect(await screen.findByText(/library isn.t installed/)).toBeInTheDocument();
  });
});
