---
name: theseus-reference-runs
description: Use when a question needs the original actually running - observing runtime values, ordering, UI behavior, files it writes or the traffic it exchanges with a device - or when setting up or debugging an isolated run of the original (compatibility layer with a virtual display, or a virtual machine with device passthrough and traffic capture).
---

# Reference runs: run the original in isolation and record ground truth

A reference run gathers evidence; it never replaces implementing the behavior. Whatever the backend, the original stays read-only, the run is isolated from the user's session, and the result is a record another agent can check.

**REQUIRED BACKGROUND:** the `runtime-observation.md` reference of `reverse-engineer-anything` covers REA's debugger and process-capture tools.

## Rules for every backend

- The original (`paths.original`) is read-only. Enforce it (read-only bind mount, read-only disk image, snapshot), do not rely on intent. Hash the executables and every input you rely on before and after the run; compare.
- One run directory per run under the scratch root, holding a private home, prefix or VM overlay, logs and captures; durable results move to the data root `evidence/<task-id>/<run>/`.
- Nothing opens, focuses or captures a window on the user's display.
- Record: exact commands, environment, backend version, input hashes before and after, logs, captures, exit status, and a leftover-process check. Stop everything you started.
- Disclose any patching, injected state or debugger modification; a modified run does not establish unmodified behavior.
- A reference observation establishes that build, configuration, device and scenario only.
- Scripts that set up or drive a backend live in the repository (`tools/reference/`), never in scratch.

## Backend: compatibility layer with a private virtual display

For Windows applications whose authority is their UI, files or computed values.

- Wine with a private `WINEPREFIX` exported before every Wine command, including `wineboot`, `wine reg`, `wineserver -k` and `winedbg`; a bare Wine command creates `~/.wine`.
- Run every Wine process inside `bwrap` with `--ro-bind` of the original: the wineserver creates files on the application's behalf, so a wineserver started outside the bind can write into the installation.
- Private display: `source tools/lib/xvfb.sh; xvfb_start 1280x1024x24 "$RUN/xvfb.log"` sets `xvfb_display`; drive input with `xdotool` against that display only and screenshot with `import -display "$xvfb_display" -window root`. Applications that need a mode list often need a Wine virtual desktop.
- Debugger observation: gdb as the application's parent with logged breakpoints (address, registers, memory, wall time) gives values comparable with screenshot times.

## Backend: virtual machine with device passthrough and traffic capture

For applications whose authority is the protocol they speak with hardware (USB cameras, instruments, controllers).

- A disposable VM (QEMU/libvirt) from a read-only base image with a per-run overlay; the original is installed in the image, never on the host.
- Pass the device through (USB host device by vendor and product ID, or a whole controller) and record the device's identity: vendor and product ID, serial, firmware version.
- Capture on the host while the guest drives the device: `modprobe usbmon`, find the bus with `lsusb`, `tshark -i usbmonN -w "$RUN/capture.pcapng"` (or `dumpcap`); timestamp each UI action in a run log so captures line up with actions.
- Name captures by content: `cap_<first 12+ hex of the pcapng SHA-256>`, and cite frames as `cap_<hash>@<frame>`. These citations are indexed in `docs/re/citation-index.md` and must survive folding like native addresses.
- Decode with Wireshark dissectors or a project script in the repository; record which fields are Recovered from traffic and which are Reconstruction.
- Never send unrecorded commands to real hardware from the new application before the matching original traffic is captured and understood; a device can be damaged by out-of-range values.

## Known failure points

| Symptom | Cause and fix |
| --- | --- |
| A file appears inside the original's directory | Something ran outside the read-only bind or overlay; rerun with every process inside it |
| `~` paths fail after overriding `HOME` | Resolve paths before exporting the private `HOME` |
| Input ignored or cursor jumping | Fullscreen applications often recenter the cursor; run windowed |
| Empty usbmon capture | Wrong bus number, or the device is bound to a host driver; check `lsusb -t` and detach it before passthrough |
| Captures do not match actions | Missing timestamps in the run log; log every action with wall time |

## Recording

Report commands, environment, backend setup, input hashes before and after, logs, capture names and hashes, exit status and the leftover check. Generated files (saves, exports, captures) are fixtures with recorded provenance and never enter Git.
