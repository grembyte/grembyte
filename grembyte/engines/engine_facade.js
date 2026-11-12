const engine = (() => {
  // ECMA-335 element types; the same for every .NET runtime.
  const TYPE = {
    VOID: 0x01, BOOLEAN: 0x02, CHAR: 0x03, I1: 0x04, U1: 0x05, I2: 0x06, U2: 0x07,
    I4: 0x08, U4: 0x09, I8: 0x0a, U8: 0x0b, R4: 0x0c, R8: 0x0d, STRING: 0x0e,
    VALUETYPE: 0x11, GENERICINST: 0x15, I: 0x18, U: 0x19
  };
  // Answers of the naming calls while no runtime is known (see base.py).
  const UNKNOWN = { locate: undefined, name: null, describe: null, identify: null };
  let active = null;

  function refresh() {
    if (active !== null)
      return true;
    for (const adapter of engineAdapters) {
      if (adapter.refresh()) {
        active = adapter;
        return true;
      }
    }
    return false;
  }

  function current() {
    if (!refresh())
      throw grembyteError('error.engine.no_runtime');
    return active;
  }

  // Look once while the agent loads: exception handlers may name code
  // before any feature calls refresh().
  refresh();
  return new Proxy({}, {
    get(_target, key) {
      if (key === 'refresh')
        return refresh;
      if (key === 'TYPE')
        return TYPE;
      if (key === 'id')
        return active === null ? null : active.id;
      // Naming runs in exception handlers: it never searches for a runtime.
      if (Object.prototype.hasOwnProperty.call(UNKNOWN, key))
        return active === null ? () => UNKNOWN[key] : active[key];
      const adapter = current();
      const value = adapter[key];
      if (value === undefined)
        throw grembyteError('error.engine.no_member', { engine: adapter.id, name: String(key) });
      return value;
    }
  });
})();
