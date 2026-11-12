// Errors whose text grembyte renders in the user's language (see messages.py):
// the message carries a catalog key and its values, marked so grembyte finds
// them in an RPC error, a stack or a sent payload.
function grembyteMessage(key, params) {
  const values = JSON.stringify(params || {}, (name, value) =>
    (typeof value === 'bigint' || value instanceof NativePointer || value instanceof Int64
      || value instanceof UInt64) ? value.toString() : value)
    .replace(/⟦/g, '\\u27e6').replace(/⟧/g, '\\u27e7');
  return `⟦grembyte:${key}:${values}⟧`;
}

function grembyteError(key, params) {
  return new Error(grembyteMessage(key, params));
}
