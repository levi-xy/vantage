// `snmp_device` and `mikrotik_swos` share these cards and publish the same series under
// their own prefix, so queries match the name with a regex. The selector still pins
// `vantage_instance_id`, so only this instance's series are read.
export function snmpMetric(sel: (extra?: string) => string, name: string, extra?: string): string {
  const matcher = `__name__=~"vantage_(snmp_device|mikrotik_swos)_${name}"`;
  return sel(extra ? `${matcher},${extra}` : matcher);
}

/** IF-MIB ifOperStatus / ifAdminStatus values: 1 = up, 2 = down. */
export const IF_UP = 1;
export const IF_DOWN = 2;
