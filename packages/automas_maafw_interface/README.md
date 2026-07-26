# automas-maafw-interface

MaaFW ProjectInterface parser and AUTO-MAS plugin service.

It provides `maafw.interface.v1` and can also be imported directly by other
Python packages.

Version 0.2.0 implements the ProjectInterface v2.8.1 declarations used by
modern MaaFW projects:

- root and imported `setting` sections are preserved in protocol order;
- `hotkey` options retain human-readable strings in task snapshots and presets;
- preview data exposes `settings` and each option's `hotkeys`;
- setting, preset, task, resource and controller option references are validated.

Controller-specific hotkey-to-keycode conversion is intentionally performed by
`automas-maafw-runner`, after the active controller is known.
