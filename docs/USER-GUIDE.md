# Using Vantage

A task-by-task guide to the web UI. Installing and running Vantage (network access, certificates, upgrades, backups) is in [OPERATIONS.md](OPERATIONS.md); setting up each device type is in [docs/integrations](integrations).

The main tabs are **Dashboard**, **Devices**, **Alerts** and **Map**. Admins also get **Settings**. **My account** is in the account menu (under **More** on a phone).

Contents:

1. [First steps](#first-steps)
2. [Devices](#devices)
3. [Dashboards](#dashboards)
4. [Maps](#maps)
5. [Alerts](#alerts)
6. [Users and settings](#users-and-settings)
7. [Troubleshooting](#troubleshooting)

---

## First steps

### How do I start without setting anything up on my devices?

Start with ping checks. A **Ping check** needs only an IP address: no SNMP, no API tokens, no exporters on the device. Add each device by its IP as a Ping check (see [How do I add a device?](#how-do-i-add-a-device)) and you already get:

- **Up/down state** on the Devices page, on the device's card and on the map.
- **Latency and loss**: the Ping check card shows the round trip and the packet loss, and on a large enough card the last hour as a small chart with min, avg and max. It says **100% LOSS** when nothing comes back.
- **The map**: a box linked to a ping check shows UP or DOWN, its latency and loss, and its cables turn red when it is down.
- **History**: the device's page has **Round-trip time** and **Packet loss** charts for 1 hour up to 30 days.
- **Alerts**: the templates **ping_check: instance down** and **ping: packet loss above 20% for 5 min** send you a Telegram message.

What needs the device's full integration instead of a ping check:

- switch ports and their link state (front panels on cards and maps)
- interface traffic, and live rates on map cables
- Proxmox guests (VMs and containers)
- disk health (SMART) and drive bays
- anything else the device itself reports: CPU, memory, gateways, tunnels, and so on

You can switch a device to its full integration later; see [How do I move a device from a ping check to its full integration?](#how-do-i-move-a-device-from-a-ping-check-to-its-full-integration)

Ping only works if the device answers ping and your firewalls let ICMP echo through from the Vantage server. A host that drops ping looks down even when its services work.

### Where do I get the setup code?

`setup.sh` prints a one-time **setup code** (four groups of four characters) after it starts Vantage, next to the address to open. You need it once, to create the first admin account.

If you lost it, it didn't print, or it was used without creating an admin, get a new one from the folder you ran `setup.sh` in. A new code replaces the old one:

```bash
docker compose exec backend python -m app.cli setup-code
```

Once an admin exists, the command only says setup is done. More in [OPERATIONS.md](OPERATIONS.md#the-setup-code).

Installed with the compose file in Portainer or Dockge? Nothing prints a code there. Run `python -m app.cli setup-code` in the backend container's console; see [the standalone setup code](OPERATIONS.md#the-setup-code-standalone).

### How do I create the admin account?

Open the address `setup.sh` printed. The **Welcome to Vantage** page asks for:

- **Setup code**: the code from `setup.sh`.
- **Admin username**: 1 to 64 characters, letters, digits, dot, dash or underscore, starting with a letter or digit.
- **Password** and **Confirm password**: at least 12 characters.
- **Back up your master key**: a short note on the master key (it encrypts every stored device credential and channel token; without it you enter them all again, and a database backup can't help) and where it is (`secrets/master.key` in the folder where you ran `setup.sh`, or the `master-key` volume on the standalone install; see [OPERATIONS.md](OPERATIONS.md#back-up-the-master-key)). Copy it somewhere safe outside the server, then tick **I have backed up my master key**. **Create admin account** stays disabled until you do. The tick is only a browser-side reminder: nothing is sent or stored, and nothing reminds you again later.

Press **Create admin account**. You are logged in and land on the dashboard. This page only exists while there is no user; more accounts are added under **Settings → Users** (see [Users and settings](#users-and-settings)).

If the page says the code is wrong or was already used, get a new one with the command above. After too many wrong codes it asks you to wait a few minutes.

### How do I add my first device with its full integration?

1. Read the device's setup guide in [docs/integrations](integrations) first. Most integrations need something on the device: a read-only API token (Proxmox, OPNsense, Cloudflare), SNMP turned on (switches), or an exporter (node_exporter for Linux hosts, Telegraf for pfSense).
2. Go to **Devices → Add device**, pick the type and fill in the form.
3. Press **Test connection**. If it fails with a firewall hint, open that port and test again.
4. Press **Add device**. Its cards go straight onto your main dashboard.

The full walkthrough is in [Devices](#devices).

---

## Devices

Every role can see the Devices page and each device's own page. Only admins can add, test, edit, disable and delete devices.

### How do I add a device?

1. Go to **Devices** and press **Add device**.
2. Pick the type. The search box (**Search: pfSense, switch, ping…**) narrows the list; Enter picks the only match. Each type has a one-line hint, for example **Ping check**: "Is a host answering ping? Round trip and loss."
3. Fill in the form:
   - **Name**: required, shown on cards and the map.
   - **Site** (optional): the Devices page groups devices by site. Devices without one are listed under **No site**.
   - **Tags** (optional): separate them with commas.
   - **Enabled**: on by default.
   - **_Type_ settings**: the fields of that integration. For a Ping check these are **Host** (IP address or DNS name, on a private network, for example `10.0.0.30`), **Packets per check** (default 3) and **Reply timeout (seconds)** (default 1).
4. Press **Test connection** (see below), then **Add device**. **Change type** goes back to the list of types.

A new device gets a colour of its own, and its cards are added to the main dashboard (the first dashboard tab).

Device addresses must be on a private network (10.x, 172.16–31.x, 192.168.x, 100.64.x CGNAT, or IPv6 ULA). Public internet addresses are refused, except the fixed API hosts of cloud integrations such as Cloudflare. Other ranges can be allowed by the server admin; see [SECURITY.md](SECURITY.md).

Password and token fields are never shown again after saving. When you edit a device they say **set** or **not set**; leave them blank to keep the stored value.

### How do I test the connection?

- In the add or edit form: **Test connection** tests what is in the form, before you save.
- On the Devices page: **Test** next to a saved device.

You get either "Connection OK: …" or "Connection failed: …". Editing a saved device with a password or token: to test changed settings before saving, type the secret again, or save first and use **Test** on the Devices page (the form tells you which fields).

### What does the firewall hint mean?

When a test fails because the device didn't answer in time, the failure comes with a **Firewall check** block, for example:

> Allow TCP port 8006 from the Vantage server to 10.0.0.20.

or, for a ping check, "Allow ICMP echo (ping) from the Vantage server to 10.0.0.30." Add that rule on the firewall between the Vantage server and the device (and on the device itself, if it filters), then test again.

### How do I edit a device?

On **Devices**, press **Edit** next to it. The edit page shows the device's type, site and current state above the same form. Change what you need and press **Save changes**. The type of a device can't be changed; see the next question.

### How do I move a device from a ping check to its full integration?

Add the device again with its full type (**Add device**), then disable or delete the ping check. Afterwards:

- On a map, select the device's box and change its **Linked device** to the new one.
- Alert rules that were set to the ping check's device need the new device picked under **Device**.
- The new device's cards are added to the main dashboard; remove the old Ping check card in edit mode if you deleted the ping check.

You can also keep both: the ping check as a simple "is it answering" signal next to the full integration.

### How do I disable a device?

Press **Disable** next to it on the Devices page, or untick **Enabled** in its edit form. A disabled device isn't checked any more and shows **DISABLED**. **Enable** turns it back on. **Delete** removes it after you press **Confirm delete**.

### What do the states mean?

| State | Meaning |
|---|---|
| **UP** | The latest check worked. |
| **PENDING** | Not checked yet (just added, just enabled, or Vantage just restarted). |
| **DOWN** | Several checks in a row failed (3 by default, about 2–4 minutes at the usual 60 s interval; see [OPERATIONS.md](OPERATIONS.md#device-down-detection)). |
| **UNKNOWN** | The state isn't known, for example a scraped device Prometheus hasn't reported on yet. |
| **DISABLED** | The device is turned off in Vantage. |

A device that failed a check but isn't down yet stays UP with its error in amber in the **Last error** column. Sites with a down or failing device are listed first, and the header counts them ("3 devices · 1 down · 1 failing").

Devices that Prometheus scrapes directly (Linux hosts, pfSense, SMART, cloudflared) take their state from Prometheus' scrape; their **Last success** says "scraped by Prometheus".

### Where do I see everything about one device?

Click the device's name on the Devices page or in a card's header. The device page shows its state in full (last checked, last success, error, tags), its **Open alerts**, a **History** section with charts and a range picker (1 hour to 30 days), and all of its **Cards**, larger than on the dashboard. It is read-only for everyone.

### How do I change a device's colour?

Each device has a colour, so its cards are easy to spot: every card of the device has a strip in that colour. A new device gets a colour no other device uses yet. To change it, open **Edit** and pick one under **Colour** (admins only). The palette has no red, amber or green, which are kept for problems and healthy states.

---

## Dashboards

Dashboards are shared: everyone sees the same dashboards, and only admins can edit them (viewers see no **Edit dashboard** button or **+** tab). Editing works on desktop widths only; a phone shows the cards in one column.

### How do I rearrange the dashboard?

Only admins can do this; viewers see the dashboard as it is.

1. Press **Edit dashboard**.
2. Drag a card by its body to move it. Drag its bottom-right corner to resize it. Cards float up into free space.
3. Press **Done** to save, or **Cancel** to throw the changes away.

A card made smaller than its full size shows less (for example just the key figure); bigger, it shows everything. Nothing is saved until **Done**.

### How do I add and remove cards?

In edit mode:

- **Add card** opens the list of cards you can still add: dashboard-wide cards first (**Devices healthy**, **Active alerts**, **WAN status**, **Throughput**), then each device's cards under its name. Click one (**+ Ping check**, for example) to add it. **Hide card list** closes the list.
- **✕** on a card removes it from this dashboard. The device itself isn't touched.
- **⚙** appears on cards that have settings, such as **Throughput** (tick up to 8 ports whose traffic it sums) and pfSense **Gateways**.

An empty dashboard offers **Add your first card**.

### How do I use several dashboards?

The dashboards are tabs above the cards.

- **+** (New dashboard) asks for a name and creates it. Tick **Copy of _name_** to start from a copy of the dashboard you are on.
- In edit mode: **Rename**, **Move left**, **Move right** and **Delete** act on the current dashboard. The last dashboard can't be deleted.

The first tab is the main dashboard, shown at `/`. New devices add their cards to it, and its **Throughput** card shows live rates about every 2 seconds; on other dashboards Throughput shows the rate from Prometheus.

### What does the dashboard look like on a phone?

One column in reading order, each card as tall as its content. The small summary cards (Devices healthy, Active alerts, WAN status) sit two per row. A **Dashboard** menu switches between dashboards. There is no edit mode on a phone.

### What do the card headers say?

The header shows the device's name and colour, its type, and a state: **UP**, **DOWN 12m** (how long), **NO DATA** (the values shown are old, or none arrived) or **PENDING**. A subtitle says "no data since 14:02" when the values are old. A card can also name its own problem, such as **100% LOSS** on a Ping check.

---

## Maps

A map is a drawing you make yourself: Vantage doesn't lay it out for you. Everyone can view maps. Only admins can create, edit, rename, move and delete them, and only on a desktop-width screen.

### How do I create a map?

On the **Map** tab press **+ New map**, type a **Name** and choose how to start:

- **Blank canvas**: start empty and place everything yourself.
- **Start with my devices**: one box per device, in rows by site. No cables yet: you arrange and connect them.
- **Copy a map**: pick the map to copy.

The new map opens ready to edit. Several maps show as tabs; **Rename**, **Move left**, **Move right** and **Delete** are next to them when you aren't editing.

### How does editing work?

Press **Edit map**. The editor has a tool rail on the left, the drawing in the middle and a settings panel on the right.

- **Undo** and **Redo** (Ctrl+Z, Ctrl+Shift+Z or Ctrl+Y).
- Click an element to select it; Shift-click adds or removes one; drag to move. Arrow keys nudge (Shift for a grid step). Delete or Backspace removes the selection.
- Drag the background to pan.
- **Snap to grid** keeps things aligned.
- Nothing is saved until **Done**: "Everyone sees the changes when you press Done." **Cancel** asks before throwing changes away.

If another admin saved the map while you were editing, **Done** fails with "Someone else saved this map. Reload to see their version." Press **Reload**; your unsaved changes are lost.

### How do I place devices and boxes?

- **Device**: pick a device from the list, then click the map. The **Not on the map yet** list lists every device not on this map; drag one in or click it to place it.
- **Box**: a box that isn't linked to a device, for things Vantage doesn't watch (an ISP modem, a printer). Its **Linked device** is **None (not monitored)** and it is drawn with a dashed border.

A box's settings have two tabs:

- **Box**: **Linked device**, **Label**, **Subtitle**, **Icon**, **Colour** (default: the device's colour), the **Content** (role, model, address, version, and up to 4 sections of tiles, services or lists, whose items can show a live figure) and **More: what shows on the box** (**Type and address**, **Status (UP / DOWN)**, **Figures**, up to 3).
- **Look and ports**: the **Shape** (**Card**, **Cloud**, **Device**, **NAS bays**, **Access point**), the **Panel look** (**Rack 1U**, **Desktop**, **Flat**) and the front panel.

A box linked to a device shows its live state; a box linked to a ping check shows UP or DOWN, latency and loss. **Remove from map** only changes the map; the device keeps being monitored.

### How do I give a box a front panel with ports?

On the **Look and ports** tab, under **Front panel**, use **Start from…**:

- **Read from the device**: the device's own interfaces (MikroTik, SNMP, pfSense, Linux).
- a model from the preset list: a starting point, copied into the map and editable afterwards.
- **Blank**: add port groups yourself.

Linking a box without ports to an SNMP or MikroTik switch gives it the switch's front panel automatically.

Each port group has a kind (RJ45, SFP and so on), a count, **1 row** or **2 rows**, a first number, an interface prefix and a numbering (**Top row first**, **Odd on top**, **Odd on bottom**). The panel's **Jack size**, **Place** (**Bottom**, **Top**, **Side**), **Captions** (**From cables**, **Typed**, **Off**) and **Numbers** are set below. The port table lets you set each port's interface name, caption, VLANs and whether it is hidden. Starting from something else replaces the current ports; cables keep their box but lose their port. **Remove the front panel** removes it.

A **NAS bays** box also gets **Drive bays**: set the number of bays and pick each bay's disk from the device's SMART exporter.

### How do I draw cables?

Pick **Cable**, then drag from a port or a box to another one. Escape cancels. A cable dragged between ports takes its speed from the port when the port reports one.

Select a cable to set:

- **From** and **To**: the element and port at each end (**Edge of the box** for no port).
- **Speed**: 100M up to 100G, **Wi-Fi** or **Virtual**. It sets the line's thickness.
- **Line**: **Cable**, **Over the internet** (dotted) or **Console** (dashed).
- **Role**: **Access (one VLAN)** with its **VLAN**, or **Trunk (several VLANs)**.
- **Colour** (red is kept for problems; an access cable defaults to its VLAN's colour) and **Label**.
- **Show the rate next to the label**: the live traffic. It needs a port with traffic counters at one end, so a full integration, not a ping check.
- **Route**: cables are always drawn at right angles. Drag the cable to add a bend, drag a bend to move it, double-click a bend to remove it.

On the live map a cable turns red when the device or port at either end is down. Hover over a cable to see both ends, its speed, its state, the current rate and the last hour.

### How do I show a Proxmox host with its guests?

Pick **Frame** and place it, then in its settings:

- **Proxmox instance**: the Proxmox device.
- **Fill in guests automatically**: on, the frame shows the host's VMs and containers as tiles. Under **Guests from Proxmox** you can hide guests and move them up or down.
- **Tiles of your own**: tiles typed by hand, for things that aren't Proxmox guests.
- Guest groups: titled columns; drag guests into a group. Guests in no group go into **Other**.
- **CPU and memory bars on the guests** and **Side column (identity and sections)**.

### How do I list the VLANs?

Press **VLANs** in the editor's toolbar to open **VLANs of this map**: each VLAN has an id, name, subnet, note and colour. **Add VLAN** adds one. Removing a VLAN that ports or cables use asks first, then clears it from them.

Ports get their untagged and tagged VLANs in the port table; access cables get theirs in the cable settings. The **Networks** tool places a list of the map's VLANs on the drawing.

### How do I add a legend, text and tables?

- **Legend**: lists the cable types (speeds, line styles, roles, VLANs) this map uses. Nothing to set.
- **Text**: a note, in three sizes, optionally on a panel.
- **Table**: a title, columns and rows; a column can be shown as badges.

All text is plain text.

### What do I see when I view a map?

Live state on the drawing: boxes show UP or DOWN, a down device's name and cables turn red, and figures and cable rates update. Click a linked box or frame to open its details: state, figures, a chart of the last hour, its cables, its guests and a link to the device's page. A box whose device was deleted shows **Device removed**.

### What does a map look like on a phone?

No drawing. The map page lists the map's **Problems** (devices that are down) first, then all **Devices** in drawing order with their address, model and which device they connect to, then **Notes and tables**. Tap a monitored device to open its page. Maps can't be edited on a phone.

---

## Alerts

Everyone sees open alerts and the history on the **Alerts** page. Only admins manage rules, and only admins add Telegram channels.

### How do I create an alert rule?

On **Alerts**, either:

- open **Create from template** and press **Use** on one, which fills in the form, or
- press **Add a rule** and write it yourself.

The form has:

- **Rule name** and **Severity** (**Info**, **Warning**, **Critical**).
- **Expression (PromQL)**: the rule fires while this returns any series. Each device (series) it returns is its own alert.
- **For (seconds)**: how long it must hold before the alert fires.
- **Device**: **All devices**, or one device.
- **Send to**: the Telegram channels to notify.
- **Enabled**.

There are templates for most integrations, for example **ping_check: instance down**, **ping: packet loss above 20% for 5 min**, **linux_host: disk above 85%**, **pfsense: gateway down** and **smart_exporter: SMART health failed**. A template is only a starting point: the rule it creates is an ordinary rule you can change.

Rules can be edited, disabled and deleted from the list. Changing a rule's expression, or disabling or deleting it, closes its open alerts.

### What do firing, no data and resolved mean?

- **FIRING**: the condition is true.
- **NO DATA**: the alert was firing, but Vantage can't see the value any more, so it can't tell whether the problem is gone. The reason is shown: "device not reporting", "device not answering", "metric missing", "query failing", "Prometheus unreachable" or "metrics not being collected". A missing value never counts as recovered. A NO DATA message is sent after 5 minutes, with one reminder after 24 hours.
- **RESOLVED**: the value came back and the condition was false on two different checks.

**Open now** lists the firing and no-data alerts; **History** lists closed ones with how they ended. Admins can **Close** an open alert by hand (then **Confirm close**). Vantage's own alerts (Prometheus unreachable, metrics not collected, alert checks stalled) say "system alert, closes by itself" and can't be closed by hand. Timing details are in [OPERATIONS.md](OPERATIONS.md#how-alerts-resolve).

### How do I get alerts on Telegram?

1. Create a bot with @BotFather and note its token. Find the chat ID of the chat or group the bot should post to.
2. Go to **Settings → Notification channels** and fill in **Add a Telegram channel**: **Name**, **Chat ID**, **Bot token**. Leave **Send Vantage's own alerts here** ticked to also get Vantage's own alerts.
3. Press **Add channel**, then **Send test message**.
4. Tick the channel under **Send to** in your alert rules.

The bot token is stored encrypted and never shown again; the edit form only says **Set** or **Not set**.

---

## Users and settings

### What can admins and viewers do?

| | Viewer | Admin |
|---|---|---|
| See dashboards, devices, device pages, maps, alerts | yes | yes |
| Arrange dashboards (cards, tabs) | | yes |
| Change own password and display settings (**My account**) | yes | yes |
| Add, test, edit, disable, delete devices | | yes |
| Create and edit maps | | yes |
| Manage alert rules, close alerts | | yes |
| **Settings**: time, users, audit log, notification channels | | yes |

### How do I add a user?

**Settings → Users**: enter a **Username**, a **New password** (typed twice) and a **Role**, **Viewer (read only)** or **Admin**, and press **Add user**. For each user you can **Make admin** / **Make viewer**, **Reset password** (ends that user's sessions) and **Delete**. The last admin can't be demoted or deleted, and you can't delete your own account.

### How do I change my password?

**My account** (in the account menu): **Current password**, **New password**, **Repeat the new password**, then **Change password**. Your other sessions are logged out. The same page has **Display**: show traffic rates in bits per second or bytes per second (this browser only).

### How do I set the timezone?

**Settings → Time**: search for your zone (**Search timezones**), pick it under **Timezone** and press **Save**. Everyone sees times in this zone: web pages, Telegram messages and log lines. The clock itself is the host's; keep NTP running on the host. See [OPERATIONS.md](OPERATIONS.md#time).

### Where do I see who changed what?

**Settings → Audit log** lists every change, newest first: who (a user, **Admin CLI** or **System**), what, and the changed values. Passwords and tokens only ever appear as `<changed>`. **Load older entries** pages back.

---

## Troubleshooting

### A device shows DOWN

1. Look at the **Last error** on the Devices page (or hover the state on its card).
2. Press **Test** next to the device. If the result has a firewall hint, open that port or allow ping from the Vantage server, then test again.
3. For a ping check: does the host answer ping from another machine on the same network? Many hosts and firewalls drop ping by default.
4. Check the device's own setup guide in [docs/integrations](integrations): the token, SNMP community or exporter it needs.

A device only turns DOWN after several failed checks in a row; see [OPERATIONS.md](OPERATIONS.md#device-down-detection).

### The health status says "Live connection lost"

Live updates are paused; the page still works and reconnects by itself. Behind a reverse proxy this usually means WebSocket upgrades or the original `Host` header aren't passed through. See [Reverse proxy](OPERATIONS.md#reverse-proxy) for working settings.

### A card or alert says NO DATA

The values shown are old, or none arrived. Common causes:

- The device isn't answering (it will turn DOWN soon).
- For Linux hosts, pfSense, SMART and cloudflared: Prometheus can't scrape the exporter. A new device is picked up within about a minute; an empty card says "No data yet. Prometheus may not have scraped this device."
- Prometheus itself is down. The health status in the top bar (Healthy / Warning / Critical) then shows it with the cause; click it for details.

### Nothing on the map is live

- A box shows live state only if its **Linked device** is set. Boxes with **None (not monitored)** never change.
- A box whose device was deleted shows **Device removed**: link it to the new device.
- Cable rates need **Show the rate next to the label** and a port with traffic counters at one end. Ping checks have no ports: link the box to the device's full integration (SNMP, MikroTik SwOS, pfSense, Linux host) and pick the port at the cable's end.
- Proxmox frames need **Proxmox instance** set and **Fill in guests automatically** on.

### Where are the integration guides?

- Setting up each device type: [docs/integrations](integrations) (for example [ping_check.md](integrations/ping_check.md), [proxmox.md](integrations/proxmox.md), [snmp_device.md](integrations/snmp_device.md)).
- How integrations work and how to write one: [INTEGRATIONS.md](INTEGRATIONS.md).
- Running Vantage: [OPERATIONS.md](OPERATIONS.md).
