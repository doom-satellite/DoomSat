# fprime-yamcs stamps F´ times 1 s ahead

**Finding.** Every F´ telemetry point and event in Yamcs carried a generation time 1 s later than the moment it
was made. fprime-yamcs's packet preprocessor adds 38 s where Yamcs adds 37. **Fix.** `tools/yamcs_time_patch.py`
changes that one constant in the installed jar; `scripts/setup_flight.sh fprime` runs it. **Upstream.** A draft
issue for fprime-community/fprime-yamcs is at the end, for the owner to file.

## What was wrong

- F´ stamps its own time correctly. `Svc.ChronoTime` (the time source in `flight/DoomSat/Top`) sets
  `TB_WORKSTATION_TIME` from `std::chrono::system_clock`: Unix seconds and microseconds, truncated, nothing added.
- Yamcs keeps time as an *instant*, milliseconds on a TAI-based scale. Its own conversion,
  `TimeEncoding.fromUnixMillisec`, adds TAI−UTC, which has been 37 s since 1 January 2017. Measured on the
  bundled Yamcs 5.12.8: instant − Unix milliseconds = 37 000 ms.
- `com.example.myproject.FprimePacketPreprocessor` (fprime-yamcs's jar) computes the generation time itself:
  `(seconds + 38) * 1000 + microseconds / 1000`. In the bytecode, at offset 275: `bipush 38`, `istore 9`, then
  the seconds read from the packet are added to it.
- So each F´ packet reached Yamcs 1 s in the future. The class is byte for byte the same in every version
  checked, 0.1.7 and 0.2.0 to 0.2.4 (0.2.1 is pinned; 0.2.4 is the latest, 30 September 2026): SHA-256
  `3f226b4b…9f301a1a`. Upgrading would not fix it. The source ships in the wheel:
  `FprimePacketPreprocessor.java:141-143` says "Weird stuff with leap seconds" and sets
  `int leapSecondsOffset = 38;`.

## What it touched

- **Wrong by 1 s:** every F´ parameter and event in Yamcs, so its archive and every display that reads it, and
  the SDS's absolute F´ times (L1 times, events, frame file names), which come from generation times.
- **Already worked around:**
  - The SDS (`ground/sds`) measures generation minus reception from the samples and puts commands on the
    telemetry's time axis with it (`products.clock_offset`, `pipeline.frames_sent_between`). Its intervals were
    right before the fix and still are; the offset it measures is now about −0.05 s, the link delay.
  - Open MCT's live window ends 5 s after now (`ground/openmct/doomsat/common.js`). With an end of now, the
    gauges dropped every live value as "in the future". The camera's frame age read 23:59:59 for the same reason.
- **Not touched:** the pilot's telemetry age (`tel_age_ms`) and every research metric. They use the ground
  clock, not F´'s timestamps.
- **Not corrected by the fix:** data recorded before it (the Yamcs archive, SDS captures). Those keep the
  extra second.

## The fix

`tools/yamcs_time_patch.py` rewrites `bipush 38` as `bipush 37` in `FprimePacketPreprocessor.class`, inside the
installed `fprime-yamcs-1.0.0-SNAPSHOT.jar`:
- **Only the known class.** It patches only when the class hashes to the known unpatched one, and the result
  to the known patched one (`d40eb38b…879497bf`). Any other build is left alone, with a warning.
- **Nothing else changes.** Every other entry, with its date and compression, is copied as it is. The new jar
  replaces the old in one rename.
- **Reversible.** The jar as installed stays beside it as `fprime-yamcs-1.0.0-SNAPSHOT.jar.orig`, written whole
  before it is named. fprime-yamcs loads `jars/*.jar` only, so the backup is never on the classpath.
- **Safe to repeat.** Running it twice changes nothing more.

Where it runs:
- `scripts/setup_flight.sh fprime` applies it after installing fprime-yamcs.
- `scripts/flight.sh start` prints one line when the jar is not patched.
- `tools/doctor.py` reports it.
- For an install made before this change, run `scripts/flight.sh setup fprime`, or
  `~/doom/DoomSat/fprime-venv/bin/python tools/yamcs_time_patch.py`. Then restart the flight side
  (`scripts/flight.sh start`): a Yamcs already running keeps the class it loaded.
- A reinstall of fprime-yamcs puts the unpatched jar back. `flight.sh start` then says so, and the tool patches
  it again and refreshes the `.orig`.

Why a patch and not a plugin:
- A DoomSat preprocessor in Java would need a JDK at setup, because the runtime fprime-yamcs ships has the
  `java.compiler` API but not the compiler.
- Correcting downstream instead would leave Yamcs's archive and every display 1 s wrong.
- The tool's name keeps it clear of `flight.sh stop`, which kills anything whose command line matches
  `fprime_yamc[s]`.

**Limit.** 37 is today's TAI−UTC. No leap second has been added since 2017, and they are to stop by 2035. If one
were added, the constant would be 1 s off again. The upstream fix below has no such constant.

## Verified (6 October 2026, this container)

Generation time minus Yamcs's reception time, read from the archive while the stack flew:

| | F´ telemetry (median, n) | F´ events (median, n) |
|---|---|---|
| Before (fprime-yamcs as installed) | +943 ms (200; min +809, max +998) | +955 ms (67) |
| After `tools/yamcs_time_patch.py` | −58 ms (300; min −386, max −1) | −41 ms (17) |

Before, the lead was 1 s minus the link's own delay. After, the generation time comes before the reception time,
as it should.

Tests: `tests/test_yamcs_time_patch.py` patches a jar built at test time, with no install and no network:
- only the known class and only that byte change;
- the backup is not a `*.jar`, and a reinstall is patched again with the backup refreshed;
- an unknown class is reported unknown and left alone, and a known hash with two sites is refused;
- the exit codes.

Each of these fails on the mutant it guards (no hash check in `state()`, no one-site guard, a backup kept from
before a reinstall).

Found alongside, not caused by this: the dashboard's command log read Yamcs's internal instant
(`commandId.generationTime`, TAI-based) as Unix time and showed every command 37 s late. It now uses the
command's ISO `generationTime`, like the event lines.

When an F´ install is present, it also checks that the installed jar is one this knows.

## Draft upstream issue (for the owner to file at fprime-community/fprime-yamcs)

> **FprimePacketPreprocessor adds 38 s to F´ time; Yamcs's TAI−UTC is 37 s, so every packet is stamped 1 s
> in the future**
>
> `FprimePacketPreprocessor.java:143` sets `int leapSecondsOffset = 38;`, and the generation time is
> `(seconds + 38) * 1000 + useconds / 1000`. Yamcs's own conversion from Unix time
> (`TimeEncoding.fromUnixTime` / `fromUnixMillisec`) adds the TAI−UTC in force at that time, which has been 37 s
> since 2017. With F´'s `TB_WORKSTATION_TIME` (Unix seconds and microseconds, e.g. from
> `Svc.ChronoTime` or `Svc.PosixTime`), every F´ parameter and event in Yamcs therefore reads 1 s later than it
> was made.
>
> Seen with fprime-yamcs 0.1.7 and 0.2.0 to 0.2.4 (the class is byte-identical), Yamcs 5.12.8:
> - In the archive, generation time minus reception time is +943 ms median for telemetry, +955 ms for events.
>   It should be slightly negative, by the link delay.
> - `TimeEncoding.fromUnixMillisec(System.currentTimeMillis()) - System.currentTimeMillis()` is 37 000.
> - Open MCT's gauges in real-time mode, with the window ending at now, discard every live value as later than
>   the end of the window.
>
> Suggested fix: `TimeEncoding.fromUnixTime(Integer.toUnsignedLong(seconds), useconds)` instead of adding a
> constant. That follows the leap-second table Yamcs is configured with, and reading the seconds as unsigned also
> keeps F´'s U32 time right after January 2038 (`getInt` makes it negative). Workaround in use: set
> `leapSecondsOffset` to 37 (`bipush 38` to `bipush 37` in the shipped class).

Check for an existing issue before filing.
