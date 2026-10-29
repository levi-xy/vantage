# Proxmox VE (`proxmox`)

**Style:** polled (HTTPS API). **Category:** `hypervisor`. **Version:** `0.1.0`. Code: `backend/app/integrations/builtin/proxmox/integration.py`. Guide: [INTEGRATIONS.md](../INTEGRATIONS.md).

**What it reads:** the Proxmox VE REST API, `GET` only, through `ctx.http_client()` (the SSRF-guarded client: the host is resolved once, checked, and only the checked address is connected to). Every request goes through `_get_json`, which refuses any path not on the allowlist (`ALLOWED_PATHS`) before anything is sent:

| Path | When | Permission it needs | Why it's read-only |
|---|---|---|---|
| `GET /api2/json/version` | connection test | any valid token | returns the release |
| `GET /api2/json/nodes` | connection test and poll | `Sys.Audit` (nodes the token can't see are left out) | lists nodes and their online state |
| `GET /api2/json/cluster/resources?type=vm` | poll | `VM.Audit` (guests the token can't see are left out) | a status listing of all QEMU VMs and LXC containers |
| `GET /api2/json/nodes/{node}/status` | poll, per online node | `Sys.Audit` on the node | CPU, memory, root disk, uptime, load |
| `GET /api2/json/nodes/{node}/storage` | poll, per online node | `Datastore.Audit` (storages it can't see are left out) | storage pools and their usage |

`{node}` is only ever a node name taken from the `/nodes` answer that matches a host-name pattern; any other name is skipped, never requested. Nothing else is called: no `POST`/`PUT`/`DELETE`, no console, `vncproxy`, `termproxy`, `spiceproxy`, guest-agent or `execute` endpoint, no task, backup or configuration endpoint, no ticket login. Redirects are not followed. In a cluster, the node you configure answers for all nodes (it proxies the per-node requests over the cluster network), so one instance covers the whole cluster.

**Config:** see `ProxmoxConfig`.

- `host` is any node of the cluster (IP literal preferred), found by name by the save-time SSRF check and resolved with `port` (8006 by default). It must be on a private network. Stored in plaintext.
- `token_id` is the full token ID, `user@realm!tokenname`. Stored in plaintext: it identifies the token but is not a credential.
- `tls_ca_certificate` (**Trusted certificate (PEM)**) pins the certificate the API must present. See TLS below.
- `tls_insecure_skip_verify` (**INSECURE: don't verify the TLS certificate**) is off by default. Can't be combined with `tls_ca_certificate`.
- `timeout_seconds` applies to each request (at most 15 s, the guarded client's own limit).
- `max_guests` and `max_storages` cap what one poll reports; see Limits.

**Secrets:** `token_secret`, a write-only `SecretStr`. It is sent only in the `Authorization: PVEAPIToken=<token_id>=<secret>` header, to the configured host and port. It never appears in a message, log line, status, metric or API response. The validator refuses a secret with spaces or control characters without echoing it.

### Creating a read-only token

Give Vantage a dedicated user and a token with the **`PVEAuditor`** role only. `PVEAuditor` holds only `*.Audit` privileges (read-only); it can't start, stop, change or open a console on anything. On any node, as root:

```sh
pveum user add vantage@pve --comment "Vantage dashboard (read-only)"
pveum acl modify / --users vantage@pve --roles PVEAuditor
pveum user token add vantage@pve monitoring --privsep 1
pveum acl modify / --tokens 'vantage@pve!monitoring' --roles PVEAuditor
```

The third command prints the token secret once: paste it into **API token secret**, and `vantage@pve!monitoring` into **API token ID**. In the web UI the same is Datacenter, Permissions: Users (add `vantage@pve`), API Tokens (add `monitoring`, keep **Privilege Separation** ticked), then Permissions, Add, **User Permission** and **API Token Permission**, path `/`, role `PVEAuditor`, Propagate ticked.

A privilege-separated token gets only what is granted to the token itself, and never more than its user has; that's why both get `PVEAuditor`. A token without permissions isn't refused by Proxmox: it gets empty lists. The connection test reports that as "can't see any node". Don't use `root@pam` or a token without privilege separation.

### TLS

Verification is **on** by default, against the system's CA store. Proxmox nodes ship with a certificate signed by the cluster's own CA, which the system doesn't trust, so the connection test then fails with "certificate not trusted". Fix it, in order of preference:

1. **Paste the cluster CA** into **Trusted certificate (PEM)**: the contents of `/etc/pve/pve-root-ca.pem` on any node (in the web UI: node, System, Certificates, `pve-root-ca.pem`, View Certificate). Only the pasted certificate(s) are then trusted, not the system store, TLS 1.2 is the minimum version (PVE 8 offers 1.2 and 1.3), and the hostname is still checked: `host` must be a name or address in the node's certificate. Proxmox puts the node's host name, FQDN and its IP addresses into the certificate it generates; if you connect by another name, regenerate it (`pvecm updatecerts --force`) or use a name it covers.
2. Or paste **the node's own certificate** (`/etc/pve/nodes/<node>/pve-ssl.pem`, or `pveproxy-ssl.pem` if you uploaded one). It works as the trust anchor on its own (`VERIFY_X509_PARTIAL_CHAIN`). Pin the CA instead if you can: a node certificate is regenerated on some upgrades and the pin then breaks.
3. If the node has a certificate from a public CA (for example ACME), leave both TLS fields alone.
4. **Last resort:** **INSECURE: don't verify the TLS certificate**. The token is then sent to whatever answers on that address, so anyone who can intercept the traffic gets a read-only token. Only on a network you control.

The field is at most 16 KiB (a PVE CA is about 2 KB) and is parsed line by line, so hostile input can't make the check slow. It accepts only `-----BEGIN CERTIFICATE-----` blocks (at most 8) and refuses anything else, so a pasted private key is never stored. Certificates are public; the field is stored in plaintext and shown to every user. SHA-256 fingerprint pinning isn't supported: paste the CA or the node certificate instead.

**Metrics:** prefix `vantage_proxmox_`. Constants at the top of `integration.py`. Counters are exposed with `_total`.

| Metric | Type | Labels | Meaning |
|---|---|---|---|
| `nodes`, `nodes_omitted` | gauge | none | nodes in the `/nodes` answer; how many were beyond `MAX_NODES` (32) and not polled |
| `guests`, `guests_omitted` | gauge | none | QEMU VMs and LXC containers (templates excluded); how many were beyond `max_guests` |
| `storages`, `storages_omitted` | gauge | none | storage entries over all polled nodes; how many were beyond `max_storages` |
| `node_up` | gauge | `node` | 1 if the node is `online` in `/nodes`, else 0 |
| `node_cpu_ratio` | gauge | `node` | CPU use, 0 to 1 over all cores |
| `node_cpus` | gauge | `node` | logical CPUs |
| `node_memory_used_bytes`, `node_memory_total_bytes` | gauge | `node` | RAM |
| `node_root_disk_used_bytes`, `node_root_disk_total_bytes` | gauge | `node` | the root file system |
| `node_uptime_seconds` | gauge | `node` | |
| `node_load1`, `node_load5`, `node_load15` | gauge | `node` | load average |
| `guest_running` | gauge | `node`, `vmid`, `guest_type` | 1 if `running`, else 0 (stopped, paused shows as running) |
| `guest_cpu_ratio`, `guest_cpus` | gauge | same | CPU use, 0 to 1 of its vCPUs; vCPUs |
| `guest_memory_used_bytes`, `guest_memory_max_bytes` | gauge | same | |
| `guest_disk_used_bytes`, `guest_disk_max_bytes` | gauge | same | LXC: real usage. QEMU: Proxmox reports 0 used (it can't see inside the VM disk) |
| `guest_network_receive_bytes`, `guest_network_transmit_bytes` | counter | same | since the guest started |
| `guest_disk_read_bytes`, `guest_disk_written_bytes` | counter | same | since the guest started |
| `guest_uptime_seconds` | gauge | same | 0 when stopped |
| `guest_info` | gauge, 1 | same, plus `guest_name` | one per reported guest; `guest_name` is the guest's name, cleaned and capped (`ifmib.clean_text`, 128 characters), empty if it has none |
| `storage_active` | gauge | `node`, `storage` | 1 if the storage is active on that node |
| `storage_used_bytes`, `storage_total_bytes` | gauge | `node`, `storage` | absent for an inactive storage |

`guest_type` is `qemu` or `lxc`. A guest that migrates gets new series with the new `node`. A shared storage appears once per node. Labels on value series are stable identifiers only (node name, VM id, guest type, storage id), each checked against Proxmox's own format first. The guest's name is the one free-form label: it appears only on `guest_info` (value 1), under the maintainer's 2026-10-28 exception (INTEGRATIONS.md section 9), so renaming a guest doesn't split its value series. Join on `vmid` to show names. Tags, descriptions and storage types are not exported. A field Proxmox leaves out (for example on a container that has never started) gives no sample rather than a 0.

**Status and failures:** the two cluster-wide requests must succeed; otherwise the instance goes `down` with:

- `Proxmox VE API at <host>:<port> refused the API token (HTTP 401): ...`: wrong token ID or secret, expired or deleted token.
- `the API token lacks permission on <host>:<port> (HTTP 403): ...`.
- `certificate not trusted: the TLS certificate of <host>:<port> could not be verified. ...`: see TLS.
- `could not connect to <host>:<port> (refused or unreachable)`, `timed out waiting for <host>:<port>`.
- `Proxmox VE API at <host>:<port> answered HTTP <status>`, `<host>:<port> answered, but not like the Proxmox VE API (...)`: wrong port, or something in between. Bodies over 4 MiB are refused the same way.
- `refused by the network guard: ...`: the host resolves to a denied address.
- `Proxmox VE poll of <host>:<port> took longer than 50 s (...)`: the poll's own budget (`POLL_BUDGET_SECONDS`), inside the collector's 60 s cut-off. Two cluster-wide requests plus two per online node, each up to 15 s, could otherwise run far past it.

A per-node request answered with an error (a node that went offline mid-poll answers 595 through the proxy, or the token can't read one node) leaves only that node's details and storages out; `node_up` stays, and a warning with the instance id and a count is logged. A 401 on any request fails the whole poll.

**Firewall:** `tcp` to the configured port (8006 by default) on the configured node, from the Vantage backend, for polls and connection tests. Other cluster nodes need no rule for Vantage: the configured node reaches them itself.

**Cards:** `proxmox-node`, "Proxmox node", 4 by 3: `node_up`, `node_cpu_ratio`, memory and root disk used/total, `node_load1`, `node_uptime_seconds`, and the node's `storage_*`. `proxmox-guests`, "Guests", 4 by 3: per guest `guest_running`, `guest_cpu_ratio`, memory used/max, `rate()` of the network and disk counters, `guest_uptime_seconds`, `guest_info` for names (joined on `vmid`), plus `guests_omitted`. The card components are frontend work (M5.5).

**Limits and caveats:**

- At most `MAX_NODES` (32) nodes are polled per instance, `max_guests` guests (lowest VM ids first, at most 500) and `max_storages` storage entries (sorted by node, then storage id, at most 200), so one poll stays far below the collector's 10 000 samples. The `*_omitted` gauges say when a cap bit.
- Templates are skipped. QEMU disk usage is 0 (see above). Paused VMs report `running`.
- Bodies are capped at 4 MiB; those over 64 KiB are parsed in a worker thread, off the event loop.
- Not yet verified against a live Proxmox VE: field names and shapes come from the PVE 8 API documentation. Maintainer lab checkpoint.

**Fixtures:** `backend/tests/integrations/builtin/fixtures/proxmox_*.json`, hand-written in the shape of Proxmox VE 8.2 answers: node `pve-example` (and `-2`, `-3`), example guest names, zeroed fingerprints and repo ids. Tests generate a throwaway CA certificate at run time; no key or certificate is stored.
