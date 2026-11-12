# Assembly (x86-64)

The [code explorer](../guide/code-explorer.md), the [write watch](../guide/write-watch.md) and the [native tools](../guide/native-tools.md) show machine code as assembly: one line per instruction. You do not need to write assembly to use Grembyte, but reading a few lines tells you what a function does, which arguments it takes and which value it changes. This page covers what that takes, for the x86-64 code of today's games; the [code explorer example](../examples/dummy-game-code-explorer.md) applies it step by step.

## Reading a line

```text
0x4006d7  48 8b 40 28   mov rax, qword ptr [rax + 0x28]
address   bytes         mnemonic  operands
```

- **Address:** where the instruction is. The code explorer shows addresses of the file; in the running game the module may sit elsewhere (`module+0x6d7` stays the same).
- **Bytes:** the machine code itself. Byte patterns for scripts are made of these, with `??` for the parts that change between builds.
- **Mnemonic:** what the instruction does, here `mov` (copy).
- **Operands:** what it works on. Grembyte shows Intel syntax, the **destination comes first**: `mov rax, [rax + 0x28]` means *rax = the 8 bytes at address rax + 0x28*.

Numbers are hexadecimal when they start with `0x`. A negative displacement reads `[rbp - 0x10]`.

## Registers

The processor's own variables. Each 64-bit register has smaller parts with names of their own; writing a 32-bit part clears the upper half, writing an 8- or 16-bit part leaves the rest alone.

| 64 bits | 32 bits | 16 bits | 8 bits | Usual role |
| --- | --- | --- | --- | --- |
| `rax` | `eax` | `ax` | `al` | result of a function |
| `rbx` | `ebx` | `bx` | `bl` | kept across calls |
| `rcx` | `ecx` | `cx` | `cl` | 4th argument (Linux), 1st (Windows); shift count |
| `rdx` | `edx` | `dx` | `dl` | 3rd argument (Linux), 2nd (Windows) |
| `rsi` | `esi` | `si` | `sil` | 2nd argument (Linux) |
| `rdi` | `edi` | `di` | `dil` | 1st argument (Linux) |
| `rbp` | `ebp` | `bp` | `bpl` | frame pointer, or kept across calls |
| `rsp` | `esp` | `sp` | `spl` | stack pointer: the top of the stack |
| `r8`–`r15` | `r8d`–`r15d` | `r8w`–`r15w` | `r8b`–`r15b` | more arguments (`r8`, `r9`), scratch, kept across calls (`r12`–`r15`) |

Others you meet:

- **`rip`**, the instruction pointer: the address of the next instruction. `[rip + 0x2a31]` is an address relative to the code, the usual way to reach a **global variable** or a string; the code explorer shows the result (`g_game`).
- **`xmm0`–`xmm15`**: 128-bit registers for floating point (and SIMD). A `float` uses their lowest 4 bytes, a `double` 8. `ymm`/`zmm` are their 256- and 512-bit forms.
- **Flags** (`rflags`): bits set by comparisons and arithmetic (zero, sign, carry, overflow), read by conditional jumps.

The size of a value follows from the register name: `eax` is an `int`, `al` a `bool` or byte, `rax` a pointer or 64-bit number. In memory operands the size is spelled out: `byte ptr` (1), `word ptr` (2), `dword ptr` (4), `qword ptr` (8).

## Operands

| Form | Example | Means |
| --- | --- | --- |
| Register | `rax` | the register's value |
| Immediate | `0x14` | a constant in the instruction |
| Memory | `[rax]` | the value at the address in `rax` |
| Base + displacement | `[rax + 0x28]` | a field of a struct or object `rax` points to |
| Base + index × scale | `[rax + rcx*4]` | element `rcx` of an array of 4-byte values |
| RIP-relative | `[rip + 0x2a31]` | a global variable or constant near the code |

`lea` computes the address of a memory operand **without reading memory**: `lea rax, [rdi + 8]` is *rax = rdi + 8*. Compilers also use it for plain arithmetic: `lea edx, [rdx + rdx*4]` is *edx = edx × 5*.

## Instructions

### Moving data

| Instruction | Means |
| --- | --- |
| `mov a, b` | a = b |
| `movzx a, b` | a = b, the smaller b zero-extended (unsigned) |
| `movsx` / `movsxd a, b` | a = b, sign-extended (signed) |
| `lea a, [address]` | a = the address itself (no memory access) |
| `push a` / `pop a` | put a on the stack / take the top of the stack into a |
| `xchg a, b` | swap |
| `cmovcc a, b` | a = b if the condition holds (`cmovl`, `cmove`, …: see the jumps) |
| `setcc a` | a (a byte) = 1 if the condition holds, else 0: a `bool` |

### Arithmetic and logic

| Instruction | Means |
| --- | --- |
| `add a, b` / `sub a, b` | a += b / a -= b |
| `inc a` / `dec a` | a += 1 / a -= 1 |
| `neg a` | a = -a |
| `imul a, b` / `imul a, b, c` | a *= b / a = b × c (signed) |
| `mul` / `div` / `idiv` | multiply / divide using `rdx:rax` (rare: compilers prefer the tricks below) |
| `and` / `or` / `xor a, b` | bitwise; `xor eax, eax` sets eax to 0 |
| `not a` | flip all bits |
| `shl` / `shr` / `sar a, n` | shift left (× 2ⁿ) / right unsigned / right signed (÷ 2ⁿ) |

### Comparing and branching

`cmp a, b` computes a − b and `test a, b` computes a AND b, both only for the flags; the jump after them decides.

| Jump | Taken if (after `cmp a, b`) | Signed? |
| --- | --- | --- |
| `je` / `jz` | a == b (after `test a, a`: a == 0) | |
| `jne` / `jnz` | a != b | |
| `jl` / `jge` | a < b / a >= b | signed |
| `jle` / `jg` | a <= b / a > b | signed |
| `jb` / `jae` | a < b / a >= b | unsigned |
| `jbe` / `ja` | a <= b / a > b | unsigned |
| `js` / `jns` | the result is negative / not | |
| `jmp` | always | |

The same conditions name `cmovcc` and `setcc`: `setg al`, `cmovl eax, edx`. Whether a comparison uses signed or unsigned conditions tells you whether the value is signed.

### Calling

| Instruction | Means |
| --- | --- |
| `call f` | push the return address, jump to f |
| `call qword ptr [rax + 0x18]` | call through a pointer: a C++ virtual method, a callback, an imported function |
| `ret` | return to the address on the stack |
| `jmp f` at the end of a function | a *tail call*: f returns straight to our caller |

### Floating point (SSE)

The suffix tells the type: `ss` = one `float`, `sd` = one `double`, `ps`/`pd` = several at once.

| Instruction | Means |
| --- | --- |
| `movss` / `movsd` | copy a float / double |
| `addss`, `subss`, `mulss`, `divss` (and `…sd`) | arithmetic |
| `ucomiss` / `comiss` (and `…sd`) | compare, followed by a jump (`ja`, `jb`, … as for unsigned numbers) |
| `cvtsi2ss` / `cvttss2si` | int → float / float → int (truncated) |
| `cvtss2sd` / `cvtsd2ss` | float → double / double → float |
| `xorps xmm0, xmm0` | xmm0 = 0.0 |
| `sqrtss`, `minss`, `maxss` | square root, minimum, maximum |

### Seen often, doing little

`nop` (nothing, padding), `int3` (breakpoint, padding between functions), `endbr64` (marks a valid jump target), `leave` (undo the stack frame before `ret`).

## Patterns that come up again and again

| Code | Reads as |
| --- | --- |
| `xor eax, eax` | `eax = 0` |
| `test eax, eax` / `je L` | `if (eax == 0) goto L` |
| `cmp ecx, 0x63` / `jg L` | `if (ecx > 99) goto L` (signed) |
| `mov rax, [rip + x]` / `mov rax, [rax + 0x28]` | `rax = global->field` (a pointer chain) |
| `add dword ptr [rax], edi` | `*rax += edi` (a 4-byte value changed: what a write watch reports) |
| `mov eax, [rcx + rdx*4]` | `eax = array[rdx]` (4-byte elements) |
| `mov rax, [rcx]` / `call qword ptr [rax + 0x30]` | `rcx->virtualMethod()`: the object's vtable, its 7th entry (0x30 / 8 = 6, counted from 0) |
| `movzx eax, byte ptr [rdi + 0x174]` / `test al, al` | read a `bool` field |
| `setne al` / `movzx eax, al` / `ret` | `return x != y;` |
| `push rbx` … `sub rsp, 0x20` … `add rsp, 0x20` / `pop rbx` / `ret` | prologue and epilogue: saving registers and room on the stack, not game logic |
| `mov eax, 0x2f80` / `call __chkstk` / `sub rsp, rax` | a large stack frame (Windows) |
| `mov rax, [rip + __security_cookie]` / `xor rax, rsp` | a stack protector, not game logic |
| `imul rdx, rdx, 0x2e8ba2e9` / `sar rdx, 0x21` | division by a constant (here 11): compilers multiply by a "magic" number instead of dividing |
| `lea eax, [rax + rax*2]` / `shl eax, 2` | `eax × 3`, then `× 4`: multiplication without `imul` |

## Reading a function: add_gold

From the test game of the [tutorial](../getting-started/first-hook.md):

```text
mov rax, qword ptr [rip + 0x2a31]   ; rax = g_game           (a global)
mov rax, qword ptr [rax + 0x28]     ; rax = g_game->player   (field at +0x28)
add dword ptr [rax], edi            ; player->gold += amount (first argument, an int)
ret                                 ; return, nothing in eax that callers use
```

In C: `void add_gold(int amount) { g_game->player->gold += amount; }`.

A longer one, `on_treasure`, with a division in disguise:

```text
call rand                         ; eax = rand()
movsxd rdx, eax                   ; rdx = eax (signed, 64-bit)
mov ecx, eax
imul rdx, rdx, 0x2e8ba2e9         ; ┐
sar ecx, 0x1f                     ; │ edx = eax / 11
sar rdx, 0x21                     ; │ (multiplied by a magic number,
sub edx, ecx                      ; ┘  corrected for negative numbers)
lea ecx, [rdx + rdx*4]            ; ecx = 5 × quotient
lea edx, [rdx + rcx*2]            ; edx = 11 × quotient
sub eax, edx                      ; eax = rand() - 11 × quotient = rand() % 11
lea edi, [rax + 0x14]             ; edi = that + 20          → the argument
call add_gold                     ; add_gold(20 + rand() % 11)
add rsp, 8
jmp refresh_ui                    ; tail call: refresh_ui(), then back to our caller
```

How to approach an unknown function:

1. **Read the comments first.** The code explorer names strings, functions, imports and globals; often they tell the story (`"Not enough gold"`, `SDL_PlaySound`).
2. **Look at the calls.** What a function calls says more than its arithmetic.
3. **Find the memory writes.** `mov [reg + offset], …`, `add [reg], …`: which fields of which object change.
4. **Follow the arguments** from their registers (see the calling conventions below) to where they are used.
5. **Skip prologue and epilogue**, stack protectors and `__chkstk`.

## Calling conventions

Where a function finds its arguments and leaves its result:

| | Linux (System V) | Windows and Wine/Proton |
| --- | --- | --- |
| Integer and pointer arguments | `rdi`, `rsi`, `rdx`, `rcx`, `r8`, `r9`, then the stack | `rcx`, `rdx`, `r8`, `r9`, then the stack |
| `float` and `double` arguments | `xmm0`–`xmm7` | `xmm0`–`xmm3`, at the argument's position |
| Result | `rax`, or `xmm0` | `rax`, or `xmm0` |
| A call may change | `rax`, `rcx`, `rdx`, `rsi`, `rdi`, `r8`–`r11`, `xmm0`–`xmm15` | `rax`, `rcx`, `rdx`, `r8`–`r11`, `xmm0`–`xmm5` |
| A call keeps | `rbx`, `rbp`, `r12`–`r15` | `rbx`, `rbp`, `rdi`, `rsi`, `r12`–`r15`, `xmm6`–`xmm15` |
| C++ `this` | the first argument | the first argument |

That is why a value a function needs after a call sits in `rbx` or `r12`–`r15`, and why the first instructions often copy argument registers there. How to tell a function's arguments and result from its code is on the example page: [What add_gold takes](../examples/dummy-game-code-explorer.md#4-what-add_gold-takes).

## 32-bit code

32-bit programs use `eax`, `ebx`, `ecx`, `edx`, `esi`, `edi`, `ebp`, `esp` (no `r8`–`r15`, no `rip`-relative addressing). Arguments are usually pushed on the stack (`push`, then `call`) and read as `[esp + 4]`, `[ebp + 8]`; globals appear as absolute addresses (`mov eax, [0x5a4f10]`). C++ methods compiled with MSVC pass `this` in `ecx`.

## Further reading

- [Intel 64 and IA-32 Architectures Software Developer's Manual](https://intel.github.io/SDM/sdm.html): Intel's own reference, with every instruction and its exact effect.
- [Compiler Explorer](https://godbolt.org/): type C or C++ and see the assembly a compiler makes of it; the best way to learn what code looks like.
