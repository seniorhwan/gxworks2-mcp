# GX Works2 MCP: instructions for AI agents

This server edits offline `MAIN` ladder programs in GX Works2 FXCPU, QCPU, and LCPU projects. It does not transfer a program to a PLC.

## PLC families and project creation

`plc_type` accepts the family names `FXCPU`, `QCPU`, and `LCPU`, plus model
names beginning with `FX`, `Q`, or `L` (for example `FX3U/FX3UC`,
`Q03UDCPU`, or `L26CPU-BT`). The validator applies family-specific address
rules: FX X/Y addresses are octal, while Q/L X/Y and W addresses are
hexadecimal. Unknown model families are rejected rather than guessed.

Use `gx_create_project` to launch GX Works2 when needed and create a new
project through the native New Project dialog:

```json
{"project_name": "Line-1", "plc_type": "Q03UDCPU"}
```

If GX Works2 is installed outside its standard paths, set
`GXWORKS2_EXE` to the full path of `GXW2.exe`. The native dialog must expose
the requested model; the tool fails explicitly if it cannot confirm the
selection. Project creation only creates the offline project and never
downloads to a PLC.

## Safe call sequence

1. Call `gx_get_status`. Proceed only when GX Works2 is connected and `main_editor_active` is true. Confirm that the open project is the intended one.
2. Call `gx_validate_program` with the complete candidate. Resolve every error and review all warnings and the ladder logic.
3. Call `gx_write_program` with the same complete candidate. It replaces all of MAIN after exporting a backup.
4. Report success only when the returned `status` is exactly `SUCCESS`. That status requires backup, CSV import, explicit successful build, explicit zero-error Program Check, and readback equality for instructions and statements. Any other status is failure or uncertainty; include the backup and rollback results in the response.

Do not retry a write automatically after a timeout or uncertain result. Check the returned backup and rollback state first. A successful GX Works2 check is not proof of machine safety or a substitute for engineering review.

## Global device comments

AI agents can use global device comments for inputs, outputs, relays, timers,
counters, registers, and other supported devices—not only I/O:

```json
{
  "plc_type": "Q03UDCPU",
  "global_device_comments": {
    "X10": "START BUTTON",
    "M100": "CYCLE ACTIVE",
    "D100": "TARGET SPEED",
    "W1A": "STATUS WORD"
  }
}
```

`global_device_comments` is accepted by `gx_validate_program` and
`gx_write_program`; `device_comments` remains as a backwards-compatible alias.
Use only one name per call. `gx_write_device_comments` creates a validated,
PLC-specific UTF-16LE artifact, and `gx_read_global_device_comments` parses it
back into a normalized map. Comments are validated for address range,
duplicate addresses, CSV safety, and the 32-character limit.

GX Works2 stores global comments through its Device Comment editor rather than
the ladder editor. The server therefore does not pretend that saving the CSV
has imported it: `import_via_gui=true` returns an explicit unsupported status
and preserves the artifact for manual import. Ladder writes include the same
validated comment artifact alongside their backup and verification artifacts.

## Ladder input

Pass `rungs` as an ordered array. Each rung has an optional statement and an ordered list of instruction strings. Start every rung with an LD-family contact and include at least one output or data instruction. The safe linear format does not accept branches or stack instructions. Do not include `END`; GX Works2 supplies it.

```json
{
  "rungs": [
    {
      "statement": "Start/stop seal-in",
      "instructions": ["LD X0", "OR M0", "ANI X1", "OUT M0"]
    },
    {
      "statement": "Drive output",
      "instructions": ["LD M0", "OUT Y0"]
    }
  ]
}
```

Operands are whitespace separated. Use supported Mitsubishi mnemonics, not IEC aliases such as `TON`, `CTU`, `LDN`, or `ANDN`. Branch/stack instructions are deliberately rejected until their structure can be round-tripped without flattening.

The validator is limited to the supported safe instruction subset and the
selected family’s built-in address ranges. Use `gx_validate_program` as the
authority for accepted syntax. The safe ladder subset is intentionally
conservative across PLC families; model-specific instructions are rejected
until they can be round-tripped and verified.

## Tools

- `gx_get_status`: report GX Works2 connection and whether MAIN is active.
- `gx_create_project`: launch GX Works2 if necessary and create a typed offline project.
- `gx_read_program`: export and parse the active MAIN program.
- `gx_validate_program`: validate the candidate without modifying GX Works2.
- `gx_write_program`: validate, back up, replace, build, check, and read back MAIN. Keep the returned backup path on failure.
- `gx_compile_and_check`: build and check the active MAIN program without replacing it.
- `gx_write_device_comments`: save a PLC-specific global device-comments CSV artifact. Automatic GUI import is unsupported. Comment text is limited to 32 characters and must not include quotes or control characters.
- `gx_read_global_device_comments`: validate and read a previously generated global comment artifact.

The tool serializes GUI operations. Generated artifacts are written to the server's `Documents/Codex-Gx` directory with unique names; existing files are never overwritten. Missing or unrecognized GUI results are failures.
