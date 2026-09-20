# Bruce LoRa link — protocol v1

A reliable, ordered byte stream in each direction between a **controller** (this Mac) and a **Core2 running
Bruce**, over a Reyax RYLR998 on each end. The Core2 side bridges the stream to Bruce's normal CLI, so anything
you can type on USB you can type here, and whatever it prints comes back.

Implemented twice, kept identical by tests:

| side | code |
|---|---|
| controller (Python) | `bruce_lora/protocol.py` wire format · `engine.py` state machine · `auto.py` policies |
| Core2 (C++) | `src/modules/lora/lora_link_engine.{h,cpp}` in the Bruce repo (no Arduino dependencies) |
| Core2 platform | `src/modules/lora/LoRaLink.{h,cpp}` UART driver, CLI bridge, slider screen |

## Radio facts this design is built on (measured on real modules, fw `RYLR998_REYAX_V1.2.3`)

* `AT+SEND` answers `+OK` only **after the packet has finished transmitting** — so `+OK` = end of TX. Time to
  `+OK` = LoRa time-on-air + 13…60 ms (UART/processing). `bruce_lora.protocol.airtime_ms` matches within 60 ms.
* The module accepts `SF ≤ 9 @ 125 kHz`, `SF ≤ 10 @ 250 kHz`, `SF ≤ 11 @ 500 kHz` (`+ERR=4` otherwise). Those three
  corners have sensitivities within 1 dB of each other (≈ −129 dBm), so **the proven `9,7,4,4` is already the
  maximum range the module firmware allows**. The slider trades range for speed, not the other way round.
* Preamble 2…13 and CR 1…4 are accepted. TX power `AT+CRFOP` 0…22 dBm.
* Two radios next to each other at 22 dBm **overload each other's receivers** (RSSI reads 0 dBm; about a quarter
  of exchanges fail). Hence TX power control below.
* Half duplex, no collision detection.

## Profiles

Index 0 is the **home / rendezvous** profile: both ends fall back to it.

| idx | name | SF | BW | CR | max frame | 128 B on air | sensitivity | ≈ output rate¹ |
|---|---|---|---|---|---|---|---|---|
| 0 | Range | 9 | 125 kHz | 4/8 | 128 | 1.02 s | −129.5 dBm | 79 B/s |
| 1 | Long | 8 | 125 kHz | 4/8 | 128 | 0.57 s | −127.0 | 133 B/s |
| 2 | Standard | 7 | 125 kHz | 4/7 | 128 | 0.29 s | −124.5 | 227 B/s |
| 3 | Fast | 7 | 250 kHz | 4/6 | 160 | 0.15 s | −121.5 | 432 B/s |
| 4 | Quick | 7 | 500 kHz | 4/5 | 192 | 0.08 s | −118.5 | 650 B/s |
| 5 | Turbo | 5 | 500 kHz | 4/5 | 192 | 0.03 s | −113.5 | 900 B/s |

¹ measured end to end on real radios (2.3 KB `help` output).

Band 915 MHz, network id 18, address 0 on both ends. Time on air uses the standard LoRa formula
(explicit header, CRC on, low-data-rate optimisation when Tsym > 16 ms; SF5/6 use 6.25 preamble symbols).

## Frames

Printable ASCII only (0x20–0x7E), so nothing depends on how the module treats control bytes.

```
frame  = body CRC4
CRC4   = CRC-16/CCITT-FALSE (poly 0x1021, init 0xFFFF) of body, 4 uppercase hex chars   (check: "123456789" -> 29B1)
hex    = one uppercase hex digit;  SID 1..F (0 = no session);  a frame that fails any check is dropped silently
```

| type | dir | body |
|---|---|---|
| `H` HELLO | C→D | `H sid ver fresh` — `fresh=1`: start a new session even if `sid` matches |
| `W` WELCOME | D→C | `W sid ver cur want res pp QQQQ` — `cur` device profile, `want` its slider (`F` = auto), `res` 1 = session resumed, `pp` 2-hex TX power |
| `D` DATA | C→D | `D sid seq ack flags payload` |
| `D` DATA | D→C | `D sid seq ack flags want QQQQ payload` |
| `P` PROFILE | C→D | `P sid target` |
| `Q` PROFILE-OK | D→C | `Q sid target QQQQ` |
| `T` POWER | C→D | `T sid pp` (pp = 2 hex, 0…22 dBm) |
| `U` POWER-OK | D→C | `U sid pp QQQQ` |
| `N` NO-SESSION | D→C | `N sid QQQQ` |

`QQQQ` = `RR SS`: how the device heard the controller's last frame — `RR` = −RSSI (dBm), `SS` = SNR + 64.
`ver` = 1. `seq`, `ack`: mod-16 sequence numbers. `flags`: `1` MORE (sender has more queued), `2` SYNC (device→controller:
adopt `seq` as the new stream start), `4` FLUSH (controller→device: drop your queued output; carries ≥ 1 payload byte).

**Payload escaping:** bytes 0x20–0x7E pass through except `\` → `\\`; `\n` → `\n`; `\r` → `\r`; everything else `\xHH`
(uppercase). An escape is never split across frames. The device drops `\r` from its output (saves airtime).

## Exchanges, ordering, reliability

The **controller is master**: it sends one frame and the device answers it exactly once, so nothing ever collides.
Data rides on those exchanges in both directions (piggy-backed), stop-and-wait per direction:

* A frame carries the sender's current segment (`seq`, payload) and `ack` = the next `seq` it expects.
* A segment is re-sent in every frame until the peer's `ack` says it arrived; a receiver delivers only `seq == expected`
  and re-acks duplicates. Result: **exactly once, in order**, under loss, corruption and duplication.
* Controller timeout after its own TX finished: `150 + 1.2 × airtime(max frame)` ms; 5 tries, then it reconnects.
* After a reply the next frame follows in 60 ms if there is data either way, else every 400 ms for 15 s after activity,
  else every 4 s (keep-alive). So output the Core2 produces on its own (e.g. `ir rx`) shows up within the poll interval.
* **Flush** (Ctrl-C in the TUI) drops the device's queued output; its next segment carries SYNC.

## Sessions and recovery

* The controller **hunts**: HELLO on the last-used profile, then 0…5, two tries each, until a `W` arrives.
* HELLO with a new `sid` (or `fresh=1`) resets the device's session. HELLO with the *same* `sid` and `fresh=0` **resumes**
  it (`res=1`): a lossy link that made the controller give up loses nothing in flight. If the device rebooted (`res=0`),
  the controller starts its streams over and re-queues the command it had in flight.
* A device that gets a `D`/`P`/`T` for an unknown session answers `N`; the controller reconnects immediately.
* A device that hears nothing for **40 s** drops the session, returns to profile 0 and to its default TX power.

## Range / speed switching

1. Controller sends `P target` on the current profile; the device answers `Q` and switches **after that reply finished
   transmitting**; the controller switches when it receives `Q`.
2. The next poll at the new profile confirms it. Retries of `P` alternate old, new, old… so a lost `Q` in either
   direction still converges.
3. Confirmation fails (3 polls) → the controller reverts to the old profile and avoids the target for 2 min; the device
   reverts on its own `5 × T_wait + 3 s` after switching if it never hears the controller. Chain: `target → previous → home`.
4. The Core2's own slider is reported in every reply (`want`); when it changes, the controller follows and leaves auto.

## TX power control

Overload protection, not range: full power is used whenever the path needs it.

* The controller normalises every RSSI to "what this would read at 22 dBm" (`rssi + 22 − tx_power`), so its own power
  changes don't look like path changes.
* Per direction: target power = `22 − (rssi_at_full_power − (−45 dBm))`, clamped to 0…22, applied when it differs by ≥ 3 dB
  and at most every 6 s. The downlink drives the Core2's power (`T`/`U`); the uplink (reported back in `QQQQ`) drives the
  controller's own `AT+CRFOP`.
* The device applies a new power **after** confirming it, and restores its default power on **every** HELLO and after 40 s
  of silence, so power control can never strand the link.

## Auto range / speed

Link margin of profile *q* = `min(rssi_full − sensitivity(q), snr_full − 10·log10(BW_q/125k) − snr_limit(SF_q))` in the
worse direction (a clipped SNR reading ≥ 9 dB is ignored). Auto picks the fastest profile with ≥ 10 dB margin; it moves up
after 4 consecutive good evaluations, down when the margin falls below 7 dB, one step down when > 35 % of exchanges needed
retries, at most every 8 s.

## Verification

* `tests/test_protocol.py` — wire format, CRC vector, escaping, airtime vs. measured module timings.
* `tests/test_link.py`, `test_power.py` — end-to-end scenarios on a virtual-time radio (loss, corruption, duplication,
  blackout, reboot, unreachable profiles, fading signal, overloaded receivers), each run against **both** the Python model
  of the Core2 and the firmware's real C++ engine (`tests/harness`).
* `tests/test_differential.py` — 30 fuzz runs of random valid/garbled frames into both device implementations under
  AddressSanitizer: same actions, same statistics, every step.
* `tools/hw_sweep.py`, `tools/hw_stress_switch.py` — on real radios: every profile, byte-identical output, switching.
