rpc.exports = {
  assemblies() {
    return engine.assemblies();
  },
  classes(assembly) {
    return engine.classes(assembly);
  }
};
