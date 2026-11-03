<?php
// Interface descriptions for Vantage's pfSense cards, as Telegraf input (influx line format).
//
// Read-only: reads pfSense's config.xml and prints one line per assigned interface, e.g.
//   pfsense_interface,interface=igc0,descr=WAN info=1i
// which Telegraf's prometheus_client output serves as
//   pfsense_interface_info{interface="igc0",descr="WAN"} 1
// It writes nothing and prints nothing else from the config. Setup: docs/integrations/pfsense.md.

$config = @simplexml_load_file($argv[1] ?? '/cf/conf/config.xml');
if ($config === false || !isset($config->interfaces)) {
    exit(0);
}
foreach ($config->interfaces->children() as $key => $iface) {
    $os = (string) $iface->if;
    if (!preg_match('/^[A-Za-z0-9_.]{1,32}$/', $os)) {
        continue;
    }
    // pfSense shows the key in capitals (WAN, LAN, OPT1) when no description is set.
    $descr = trim((string) $iface->descr);
    if ($descr === '') {
        $descr = strtoupper((string) $key);
    }
    $descr = substr(preg_replace('/[^A-Za-z0-9 _.-]/', '', $descr), 0, 32);
    $descr = trim($descr);
    if ($descr === '') {
        continue;
    }
    // Spaces in a tag value are escaped in influx line format.
    echo 'pfsense_interface,interface=' . $os . ',descr=' . str_replace(' ', '\\ ', $descr) . " info=1i\n";
}
