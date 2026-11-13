import { screen, within } from "@testing-library/react";
import { ApiClient } from "../../api";
import { renderCard, vector } from "../testRender";
import { ProxmoxGuestsCard } from "../proxmox-guests/ProxmoxGuestsCard";
import { ProxmoxNodeCard } from "./ProxmoxNodeCard";

const client = new ApiClient({ fetch: vi.fn() });
vi.mock("../../app/session", () => ({
  useApiClient: () => client,
  useCanEditDashboards: () => true,
}));

const node = { node: "pve1" };
const guest = (vmid: string) => ({ node: "pve1", vmid, guest_type: "qemu" });
function answer(query: string) {
  if (query.includes("node_up{")) return vector([[node, "1"]]);
  if (query.includes("node_cpu_ratio")) return vector([[node, "25"]]);
  if (query.includes("node_memory_used")) return vector([[node, "50"]]);
  if (query.includes("node_root_disk")) return vector([[node, "10"]]);
  if (query.includes("node_uptime")) return vector([[node, "3600"]]);
  if (query.includes("storage_used")) return vector([[{ ...node, storage: "local-lvm" }, "80"]]);
  if (query.includes("guest_running"))
    return vector([
      [guest("101"), "0"],
      [guest("100"), "1"],
    ]);
  if (query.includes("guest_cpu_ratio")) return vector([[guest("100"), "12"]]);
  if (query.includes("guest_memory_used")) return vector([[guest("100"), "40"]]);
  if (query.includes("guest_info"))
    return vector([
      [{ ...guest("100"), guest_name: "web-1", guest_tags: "a;b;c;prod;web" }, "1"],
      [{ ...guest("101"), guest_name: "", guest_tags: "" }, "1"],
    ]);
  return vector([]);
}

describe("Proxmox cards", () => {
  it("node shows usage per node and its storage", async () => {
    renderCard(client, ProxmoxNodeCard, answer);
    expect(await screen.findByText("pve1")).toBeInTheDocument();
    expect(screen.getByText(/online · up 1h/)).toBeInTheDocument();
    expect(screen.getByRole("progressbar", { name: "local-lvm" })).toHaveAttribute(
      "aria-valuenow",
      "80",
    );
  });

  it("guests are named from the info series, running first", async () => {
    renderCard(client, ProxmoxGuestsCard, answer);
    expect(await screen.findByText("web-1")).toBeInTheDocument();
    expect(screen.getByText("Guest 101")).toBeInTheDocument();
    expect(screen.getByText("CPU 12%")).toBeInTheDocument();
    expect(screen.getAllByRole("listitem")[0]).toHaveTextContent("web-1");
  });

  it("guests show their tags as chips, the rest as +N", async () => {
    renderCard(client, ProxmoxGuestsCard, answer);
    const [web, other] = (await screen.findAllByRole("listitem")) as [HTMLElement, HTMLElement];
    const chips = within(web).getByLabelText("Tags");
    expect([...chips.children].map((c) => c.textContent)).toEqual(["a", "b", "c", "+2"]);
    expect(within(chips).getByText("+2")).toHaveAttribute("title", "a, b, c, prod, web");
    expect(within(other).queryByLabelText("Tags")).not.toBeInTheDocument();
  });

  it("small: the node's CPU and memory; guests running", async () => {
    renderCard(client, ProxmoxNodeCard, answer, undefined, { tier: "small" });
    expect(await screen.findByRole("group", { name: "CPU and memory" })).toHaveTextContent(
      "CPU 25% · Mem 50%",
    );
    expect(screen.queryByRole("progressbar")).not.toBeInTheDocument();
  });

  it("small guests: how many run", async () => {
    renderCard(client, ProxmoxGuestsCard, answer, undefined, { tier: "small" });
    expect(await screen.findByRole("group", { name: "Guests running" })).toHaveTextContent(
      "1/2 running",
    );
    expect(screen.queryByLabelText("Tags")).not.toBeInTheDocument();
  });

  it("say so when there is no data", async () => {
    renderCard(client, ProxmoxNodeCard, () => vector([]));
    expect(await screen.findByText(/No data yet/)).toBeInTheDocument();
  });
});
