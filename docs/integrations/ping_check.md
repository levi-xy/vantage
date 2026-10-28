# Ping check (`ping_check`)

**Style:** polled over ICMP. **Category:** `generic`. **Version:** `0.1.0`. Code: `backend/app/integrations/builtin/ping_check/integration.py`. Guide: [INTEGRATIONS.md](../INTEGRATIONS.md).

**What it reads:** `count` ICMP echo requests per poll through `ctx.ping_client()`. Echo changes nothing on the target. The client resolves the host once, checks every address with the SSRF guard, and pings only the checked address over an unprivileged ICMP datagram socket (no raw sockets).

**Config:** see `PingCheckConfig`. `host` is found by name by the save-time check, is ASCII, and must be on a private network. It is stored in plaintext. `count` times the reply timeout must fit in the ping client's 15 s limit minus the share reserved for DNS; the config model enforces this, so a config that was valid before an upgrade tightened it goes `down` with `stored config is not valid for this integration`.

**Secrets:** none.

**Metrics** (gauges, constants `METRIC_*` in the source): `vantage_ping_check_packet_loss_ratio`, `..._packets_sent`, `..._packets_received`, `..._rtt_min_seconds`, `..._rtt_avg_seconds`, `..._rtt_max_seconds`.

**Status and failures:** no reply at all raises `NoReply` (`poll failed (NoReply)`). Missing ICMP permission raises `PingError` (`poll failed (PingError)`); a guard refusal shows `refused by the network guard: ...`. Partial loss is a result and the instance stays `up`.

**Firewall:** `icmp`, port `None`: allow echo requests from the backend to the host and the replies back. The backend container needs the `net.ipv4.ping_group_range` sysctl, which `deploy/docker-compose.yml` sets.

**Cards:** `ping-check`, "Ping check", 2 by 2.

**Limits and caveats:** a host that drops ICMP looks down even if its services work.

**Fixtures:** none; replies are scripted with `fake_icmp`.
