"""The `adk kvholder` argument tree, importable with nothing heavy.

`adk` builds its whole parser on every command, including on a phone. This module holds the
kvholder verbs and imports the holder (numpy) and its transports only when one of them runs.
"""

from __future__ import annotations

DEFAULT_PORT = 50062  # PATN, the engine side (adk.kvholder.DEFAULT_PORT)
DEFAULT_WS_PORT = 50063  # page + WebSocket (adk.kvholder_net.DEFAULT_WS_PORT)
PROFILE_NAMES = ("bonsai2-27b", "qwen38-27b")  # adk.kvholder.PROFILES
KV_NAMES = ("f16", "q4_0", "q8_0")  # adk.kvholder.KV_TYPES


def register(sub) -> None:
    p = sub.add_parser(
        "kvholder",
        help="Lend this device's memory to another host's context window (PATN v3 KV holder)",
    )
    s = p.add_subparsers(dest="kvholder_action")

    sv = s.add_parser("serve", help="Hold old KV pages and answer attention over them")
    sv.add_argument(
        "--host",
        default="127.0.0.1",
        help="Listen address. PATN has no auth: bind a LAN address only on a network you trust; "
        "otherwise dial a relay with --connect",
    )
    sv.add_argument("--port", type=int, default=DEFAULT_PORT)
    sv.add_argument(
        "--connect",
        default="",
        help="Dial out to a relay instead of listening (ws://HOST:50063/holder or wss://...)",
    )
    sv.add_argument("--token", default="", help="Relay token (with --connect)")
    sv.add_argument(
        "--store",
        choices=["f32", "wire", "tq4"],
        default="f32",
        help="f32: exact and fastest; wire: exact, as received; tq4: ~2x the context of q8 "
        "per MB, approximate (TurboQuant-style 4-bit)",
    )
    sv.add_argument(
        "--max-mb", type=int, default=0, help="Memory to lend (default: free RAM minus 2 GB)"
    )

    pr = s.add_parser(
        "probe",
        help="HELLO + link latency + STATS against a holder (an adk holder or a Backburner iPhone)",
    )
    pr.add_argument("target", help="HOST[:PORT]")

    pl = s.add_parser("plan", help="How much context a holder with this much free memory can keep")
    pl.add_argument(
        "--free-gb", type=float, default=0.0, help="Default: this machine's available RAM"
    )
    pl.add_argument("--profile", choices=PROFILE_NAMES, default="qwen38-27b")
    pl.add_argument("--kv", choices=KV_NAMES, default="q8_0")

    ph = s.add_parser(
        "phone", help="Use a phone (or any browser) as the holder: USB, LAN, mesh or tunnel"
    )
    ph.add_argument(
        "--via",
        choices=["usb", "lan", "tunnel", "local"],
        default="usb",
        help="usb: adb reverse + open the page on the phone (default); lan: bind every "
        "interface (mesh too); tunnel: public https via awtunnel; local: this machine",
    )
    ph.add_argument(
        "--port", type=int, default=DEFAULT_PORT, help="engine-side PATN port (loopback)"
    )
    ph.add_argument("--web-port", type=int, default=DEFAULT_WS_PORT, help="page + WebSocket port")
    ph.add_argument("--host", default="", help="address to bind/advertise for --via lan (mesh IP)")
    ph.add_argument("--token", default="", help="reuse a token (default: a fresh one)")
    ph.add_argument("--serial", default="", help="adb device serial when several are plugged in")

    st = s.add_parser("relay-status", help="Is a holder attached to the local relay")
    st.add_argument("--web-port", type=int, default=DEFAULT_WS_PORT)

    el = s.add_parser(
        "elastic",
        help="Add holders on demand (CI runners via awrun, or any machine), one join token each",
    )
    el.add_argument("--count", type=int, default=1, help="holders to add")
    el.add_argument("--minutes", type=int, default=30, help="how long each lends its memory")
    el.add_argument("--max-mb", type=int, default=4096, help="memory each holder lends")
    el.add_argument(
        "--workflow",
        default="kvholder-runner.yml",
        help="a workflow_dispatch workflow in your repo that runs `adk kvholder serve --connect` "
        "(inputs: relay, join, minutes, max_mb)",
    )
    el.add_argument("--ref", default="develop")
    el.add_argument("--priority", type=int, default=5, help="awrun priority")
    el.add_argument(
        "--print-only",
        action="store_true",
        help="mint the tokens and print one command per holder; launch nothing",
    )
    el.add_argument("--dry-run", action="store_true", help="show what would be launched")


def run(args) -> int:
    from adk import kvholder  # numpy (optional) is imported here, never at parser build

    return kvholder.run(args)
