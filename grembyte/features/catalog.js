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
  staticAreas(requests) {
    // Where classes keep their static fields (Mono), for the pointer scan
    return engine.staticAreas(requests);
  },
  resolveTokens(assembly, tokens) {
    return engine.resolveTokens(assembly, tokens);
  },
  functionCalls(assembly, tokens) {
    // The calls in Blueprint bytecode (Unreal), see the adapter's functionCalls
    return engine.functionCalls(assembly, tokens);
  }
};
