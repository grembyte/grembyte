rpc.exports = {
  assemblies() {
    return engine.assemblies();
  },
  classes(assembly) {
    return engine.classes(assembly);
  },
  methodBodies(assembly, tokens) {
    return engine.methodBodies(assembly, tokens);
  },
  nativeSymbols() {
    // [[address, name], ...] of compiled engine code (IL2CPP, Unreal)
    return engine.nativeSymbols();
  },
  resolveTokens(assembly, tokens) {
    return engine.resolveTokens(assembly, tokens);
  }
};
