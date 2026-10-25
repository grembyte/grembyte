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
  resolveTokens(assembly, tokens) {
    return engine.resolveTokens(assembly, tokens);
  }
};
