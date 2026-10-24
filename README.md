# GX Works2 MCP: instructions for AI agents

This server edits the offline `MAIN` ladder program in the currently open GX Works2 FX3U/FX3UC project. It does not transfer a program to a PLC.

## Safe call sequence

1. Call `gx_get_status`. Proceed only when GX Works2 is connected and `main_editor_active` is true. Confirm that the open project is the intended one.
2. Call `gx_validate_program` with the complete candidate. Resolve every error and review all warnings and the ladder logic.
3. Call `gx_write_program` with the same complete candidate. It replaces all of MAIN after exporting a backup.
4. Report success only when the returned `status` is exactly `SUCCESS`. That status requires backup, CSV import, explicit successful build, explicit zero-error Program Check, and readback equality for instructions and statements. Any other status is failure or uncertainty; include the backup and rollback results in the response.

Do not retry a write automatically after a timeout or uncertain result. Check the returned backup and rollback state first. A successful GX Works2 check is not proof of machine safety or a substitute for engineering review.

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

The validator is limited to the supported FX3U/FX3UC device subset and built-in address ranges. X/Y addresses are octal (for example, `X10` follows `X7`; `X8` is invalid). Use `gx_validate_program` as the authority for the accepted syntax. The target parameter must be `FX3U/FX3UC`; other PLC families are rejected.

## Tools

- `gx_get_status`: report GX Works2 connection and whether MAIN is active.
- `gx_read_program`: export and parse the active MAIN program.
- `gx_validate_program`: validate the candidate without modifying GX Works2.
- `gx_write_program`: validate, back up, replace, build, check, and read back MAIN. Keep the returned backup path on failure.
- `gx_compile_and_check`: build and check the active MAIN program without replacing it.
- `gx_write_device_comments`: save a comments CSV artifact. Automatic GUI import is unsupported. Comment text is limited to 32 characters and must not include quotes or control characters.

The tool serializes GUI operations. Generated artifacts are written to the server's `Documents/Codex-Gx` directory with unique names; existing files are never overwritten. Missing or unrecognized GUI results are failures.

