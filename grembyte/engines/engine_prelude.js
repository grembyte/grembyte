const engineAdapters = [];

function registerEngine(adapter) {
  engineAdapters.push(adapter);
}

function readUtf16Text(address, length) {
  // The UTF-16 text of ``length`` code units at ``address`` (to a NUL, as
  // readUtf16String). Frida throws 'invalid string' for a surrogate without
  // its partner: one cut off by a length limit (an emoji at the 80th unit), or
  // one the game's string really holds. Those become U+FFFD here.
  if (length <= 0)
    return '';
  try {
    return address.readUtf16String(length);
  } catch (_error) {
    // read unit by unit below
  }
  const units = new Uint16Array(address.readByteArray(2 * length));
  const isHigh = unit => unit >= 0xd800 && unit <= 0xdbff;
  const isLow = unit => unit >= 0xdc00 && unit <= 0xdfff;
  const parts = [];
  for (let i = 0; i < units.length && units[i] !== 0; i++) {
    const unit = units[i];
    if (isHigh(unit) && i + 1 < units.length && isLow(units[i + 1]))
      parts.push(String.fromCharCode(unit, units[++i]));
    else
      parts.push(isHigh(unit) || isLow(unit) ? '�' : String.fromCharCode(unit));
  }
  return parts.join('');
}
