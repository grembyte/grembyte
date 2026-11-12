// --- Memory accesses under a watchpoint ----------------------------------------
// (see grembyte/features/scripts.py)
//
// A write or access watch (grembyte/features/write_watch.js) arms a hardware
// watchpoint on every thread, the game thread scripts run on included. Frida
// guards its memory accesses (ptr.readS32(), ptr.writeS32(), …) and reports a
// trap inside them as an error, "breakpoint triggered", although the access
// has happened: the processor traps right after it. The watch agent never
// sees that trap.
//
// So the readers and writers of NativePointer try Frida's access first, and
// only on such a trap repeat it through a small compiled copy whose
// exceptions go to the process's handlers, where the watch agent takes the
// trap and lets the copy finish. A write is checked first: Frida's write has
// stored the value already, and is only repeated if it has not.

(function guardMemoryAccessesFromWatchTraps() {
  const POINTER = Process.pointerSize;
  // C long: 4 bytes on Windows, else a pointer's size
  const LONG = Process.platform === 'windows' ? 4 : POINTER;
  const SIZES = {
    S8: 1, U8: 1, S16: 2, U16: 2, Short: 2, UShort: 2,
    S32: 4, U32: 4, Int: 4, UInt: 4, Float: 4,
    S64: 8, U64: 8, Double: 8, Long: LONG, ULong: LONG, Pointer: POINTER
  };

  function isWatchTrap(error) {
    return /breakpoint|single.?step/i.test(String(error));
  }

  // Compiled at the first trap: a script that never meets one needs no compiler
  let copy = null;
  function nativeCopy() {
    if (copy === null) {
      // One instruction per access of 1 to 8 bytes, as the game's own code does
      const module = new CModule(`
void copy (void * to, const void * from, unsigned long size)
{
  if (size == 1)
    *(volatile unsigned char *) to = *(volatile const unsigned char *) from;
  else if (size == 2)
    *(volatile unsigned short *) to = *(volatile const unsigned short *) from;
  else if (size == 4)
    *(volatile unsigned int *) to = *(volatile const unsigned int *) from;
  else if (size == 8)
    *(volatile unsigned long long *) to = *(volatile const unsigned long long *) from;
  else
  {
    volatile unsigned char * target = to;
    volatile const unsigned char * source = from;
    unsigned long i;
    for (i = 0; i != size; i++)
      target[i] = source[i];
  }
}
`);
      const fn = new NativeFunction(module.copy, 'void', ['pointer', 'pointer', 'ulong'],
                                    { exceptions: 'propagate' });
      copy = (to, from, size) => fn(to, from, size);
      copy.module = module;  // kept alive with the function
    }
    return copy;
  }

  // Bytes of a write as it should land in memory
  function sameBytes(target, expected, size) {
    const stored = Memory.alloc(size);
    nativeCopy()(stored, target, size);
    const a = new Uint8Array(stored.readByteArray(size));
    const b = new Uint8Array(expected.readByteArray(size));
    return a.every((byte, index) => byte === b[index]);
  }

  const prototype = NativePointer.prototype;
  for (const [type, size] of Object.entries(SIZES)) {
    const read = prototype[`read${type}`];
    const write = prototype[`write${type}`];
    if (typeof read === 'function') {
      prototype[`read${type}`] = function () {
        try {
          return read.call(this);
        } catch (error) {
          if (!isWatchTrap(error))
            throw error;
          // The trap proves the memory is there: the native copy is safe
          const buffer = Memory.alloc(size);
          nativeCopy()(buffer, this, size);
          return read.call(buffer);
        }
      };
    }
    if (typeof write === 'function') {
      prototype[`write${type}`] = function (value) {
        try {
          return write.call(this, value);
        } catch (error) {
          if (!isWatchTrap(error))
            throw error;
          const buffer = Memory.alloc(size);
          write.call(buffer, value);
          if (!sameBytes(this, buffer, size))
            nativeCopy()(this, buffer, size);
          return this;
        }
      };
    }
  }

  const readByteArray = prototype.readByteArray;
  prototype.readByteArray = function (length) {
    try {
      return readByteArray.call(this, length);
    } catch (error) {
      if (!isWatchTrap(error) || !(length > 0))
        throw error;
      const buffer = Memory.alloc(length);
      nativeCopy()(buffer, this, length);
      return readByteArray.call(buffer, length);
    }
  };

  const writeByteArray = prototype.writeByteArray;
  prototype.writeByteArray = function (bytes) {
    try {
      return writeByteArray.call(this, bytes);
    } catch (error) {
      const length = bytes.byteLength !== undefined ? bytes.byteLength : bytes.length;
      if (!isWatchTrap(error) || !(length > 0))
        throw error;
      const buffer = Memory.alloc(Math.max(length, 1));
      writeByteArray.call(buffer, bytes);
      // A copy of many bytes stops at the first one watched: write them all
      if (!sameBytes(this, buffer, length))
        nativeCopy()(this, buffer, length);
      return this;
    }
  };
})();
