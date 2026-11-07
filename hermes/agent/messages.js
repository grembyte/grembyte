// Errors whose text hermes renders in the user's language (see messages.py):
// the message carries a catalog key and its values, marked so hermes finds
// them in an RPC error, a stack or a sent payload.
function hermesMessage(key, params) {
  const values = JSON.stringify(params || {}, (name, value) =>
    (typeof value === 'bigint' || value instanceof NativePointer || value instanceof Int64
      || value instanceof UInt64) ? value.toString() : value)
    .replace(/⟦/g, '\\u27e6').replace(/⟧/g, '\\u27e7');
  return `⟦hermes:${key}:${values}⟧`;
}

function hermesError(key, params) {
  return new Error(hermesMessage(key, params));
}
